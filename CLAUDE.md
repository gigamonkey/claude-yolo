# CLAUDE.md

## What this is

`claude-yolo.py` is a single-file Python script (no dependencies beyond the
stdlib) that runs Claude Code inside an ephemeral Docker container with
`--dangerously-skip-permissions`. Containing the blast radius of "yolo mode"
is the whole point: Claude can run unattended inside the container without
touching the host beyond the bind-mounted working directory.

There is no package, no tests, no build step — just the script. Run it directly:

```bash
./claude-yolo.py                 # default ~/.claude credentials
./claude-yolo.py ~/.claude-work  # alternate config dir
./claude-yolo.py myprofile us-west-2 some.model.id   # AWS Bedrock
./claude-yolo.py -- --network host                   # extra docker run args
```

## How it works

1. **Builds the image** (`build_docker_image`) from an inline
   `DOCKERFILE_TEMPLATE` written to a temp dir. Ubuntu 24.04 + nodejs/npm +
   Claude Code installed via the **native installer**
   (`curl https://claude.ai/install.sh | bash`) at `~/.local/bin/claude`. The
   image is rebuilt on every run (Docker layer cache makes this cheap).
   Do NOT switch to `npm install -g @anthropic-ai/claude-code` — that lands at
   `/usr/local/bin/claude`, which Claude Code's `/doctor` flags as a broken
   install and which self-update can't manage.
2. **Substitutes the host UID** into the Dockerfile's `useradd` so the
   in-container `claude` user matches `os.getuid()`. This is what makes
   bind-mounted sockets (SSH agent) accessible inside the container — keep it.
3. **Extracts credentials** (`extract_credentials`) from the macOS keychain via
   the `security` CLI, into a chmod-600 temp file that gets bind-mounted to
   `.credentials.json`. Service name is `Claude Code-credentials` by default,
   or `Claude Code-credentials-{hash8}` for a non-default config dir, where
   `hash8` is the first 8 hex chars of the SHA-256 of the resolved config path.
   This mirrors how Claude Code itself names keychain entries — if that scheme
   changes upstream, this breaks.
4. **Assembles `docker run` args** and `os.execvp`s into docker (replacing the
   process, so it's interactive `-it --rm`).

## Three credential modes (mutually exclusive, decided by positional args)

- **No args** → default `~/.claude` + keychain credentials.
- **First arg is a directory** → alternate config dir; mounted at
  `/home/claude/.claude`, credentials pulled with the hashed service name.
- **First arg is NOT a directory** → treated as an AWS profile name, with
  optional region and Bedrock model ID. Sets `CLAUDE_CODE_USE_BEDROCK=1` and
  mounts `~/.aws` read-only. No keychain extraction in this mode.

The directory-vs-profile decision hinges on `pathlib.Path(positional[0]).is_dir()`.

## Conventions / gotchas

- **macOS-only as written.** Credential extraction uses the macOS `security`
  CLI and `SSH_AUTH_SOCK` is assumed present.
- **The `/doctor` "sandbox" warning is expected — do not try to fix it.** We
  launch `claude --dangerously-skip-permissions`, which bypasses Claude Code's
  in-process OS sandbox entirely; the *container* is the sandbox. Installing
  `bubblewrap` won't help anyway — a default Docker container can't create
  unprivileged user namespaces (`bwrap: No permissions to create new
  namespace`), and granting that capability would weaken the very isolation
  this tool exists to provide.
- **Argument splitting:** `main` splits `sys.argv` on `--` *before* argparse
  sees it. Everything after `--` is appended to `docker run` last, so
  user-supplied flags win (last-one-wins).
- **`--append-system-prompt` / `-p`** is repeatable and is added *on top of* a
  built-in prompt telling Claude it's in an ephemeral Ubuntu container.
- The container name is the cwd basename, suffixed with the config dir or AWS
  profile name when those modes are active.
- The `# https://claude.ai/chat/...` URL on line 2 and the upstream gist
  reference in git history are the script's provenance — this started as
  Migurski's gist.
