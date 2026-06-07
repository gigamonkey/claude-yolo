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
./claude-yolo.py -c               # resume most recent session in this dir
./claude-yolo.py -r               # interactive session picker
./claude-yolo.py -r SESSION_ID    # resume a specific session
```

The shebang is `#!/usr/bin/env -S uv run --script` with a PEP 723 metadata block
(`requires-python = ">=3.10"`, no dependencies), so the script self-runs under
**uv**, which guarantees a Python ≥3.10 (the `str | None` annotations need it;
macOS system `python3` is often 3.9). Running it therefore requires `uv` to be
installed. It's still stdlib-only — uv just selects the interpreter. To run it
from anywhere, `chmod +x claude-yolo.py` and symlink it onto PATH (e.g.
`ln -s "$PWD/claude-yolo.py" ~/.local/bin/claude-yolo`); a symlink keeps it
tracking the repo. uv preserves the `--` separator, so docker-arg passthrough
still works.

## How it works

1. **Builds the image** (`build_docker_image`) from an inline
   `DOCKERFILE_TEMPLATE` written to a temp dir. Ubuntu 24.04 + nodejs/npm + a few
   baked-in amenities used across most projects (`ripgrep`, `fd-find` symlinked to
   `fd`, `build-essential`, `vim`, and `uv`/`uvx` copied from `ghcr.io/astral-sh/uv`) +
   Claude Code installed via the **native installer**
   (`curl https://claude.ai/install.sh | bash`) at `~/.local/bin/claude`. The
   image is rebuilt on every run (Docker layer cache makes this cheap), so baked
   amenities cost ~nothing per launch and save Claude from re-installing common
   tools in each ephemeral container. Reserve the image for *cross-cutting* tools;
   project-specific/heavy ones stay on-demand via `sudo apt` inside the container.
   Do NOT switch to `npm install -g @anthropic-ai/claude-code` — that lands at
   `/usr/local/bin/claude`, which Claude Code's `/doctor` flags as a broken
   install and which self-update can't manage.
2. **Substitutes the host UID** into the Dockerfile's `useradd` so the
   in-container `claude` user matches `os.getuid()`. This is what makes
   bind-mounted sockets (SSH agent) accessible inside the container — keep it.
3. **Checks host login** (`ensure_logged_in` / `_is_logged_in`) before launch in
   the keychain modes (skipped for Bedrock). Runs `claude auth status --json` and
   reads the `loggedIn` field; if logged out, offers to run `claude auth login`
   then re-checks. Checks login *status*, not token expiry, on purpose: an expired
   accessToken is auto-refreshed at runtime via the stored refreshToken, so expiry
   alone doesn't mean logged out. For an alternate config dir it sets
   `CLAUDE_CONFIG_DIR` so the check targets the right keychain entry. If host
   `claude` is missing/too old for `auth`, it returns True and defers to the
   empty-file check in `extract_credentials`.
4. **Extracts credentials** (`extract_credentials`) from the macOS keychain via
   the `security` CLI, into a chmod-600 temp file that gets bind-mounted to
   `.credentials.json`. Service name is `Claude Code-credentials` by default,
   or `Claude Code-credentials-{hash8}` for a non-default config dir, where
   `hash8` is the first 8 hex chars of the SHA-256 of the resolved config path.
   This mirrors how Claude Code itself names keychain entries — if that scheme
   changes upstream, this breaks.
5. **Assembles `docker run` args** and `os.execvp`s into docker (replacing the
   process, so it's interactive `-it --rm`). The args also forward the host git
   identity (`git_identity_args`) and the SSH agent (see gotchas).

## Three credential modes (mutually exclusive, decided by positional args)

- **No args** → default `~/.claude` + keychain credentials.
- **First arg is a directory** → alternate config dir; mounted at
  `/home/claude/.claude`, credentials pulled with the hashed service name.
- **First arg is NOT a directory** → treated as an AWS profile name, with
  optional region and Bedrock model ID. Sets `CLAUDE_CODE_USE_BEDROCK=1` and
  mounts `~/.aws` read-only. No keychain extraction in this mode.

The directory-vs-profile decision hinges on `pathlib.Path(positional[0]).is_dir()`.

## `--worktree NAME` (parallel sessions on one repo)

Orthogonal to the credential modes (composes with any of them). `setup_worktree`
creates/reuses a git worktree on branch `NAME` (off current `HEAD`, no upstream)
at `~/.claude-yolo/worktrees/<repo-slug>/NAME`, where `<repo-slug>` is the main
repo path slugified the way Claude names `~/.claude/projects/` buckets
(`re.sub(r"[^a-zA-Z0-9]", "-", path)`). `main` then retargets `cwd` to the
worktree (so `-w` and the `{cwd}:{cwd}` mount point there) and **additionally
mounts the shared `.git` at its identical host path** — both same-path mounts are
required because a linked worktree stores *absolute* paths to its `.git` and back.
The session is named via `claude --name NAME`. Durability is the point: commits
land in the host's shared `.git` and uncommitted edits live in the host worktree
dir, so a container exit loses nothing. Must be run from inside a git repo.

## `--continue` / `-c` and `--resume [SESSION_ID]` / `-r` (resume a session)

Mutually exclusive (argparse-enforced); both just forward the matching flag to
`claude` inside the container. They need no new mounts: session transcripts live
in `~/.claude/projects/<slug>/*.jsonl`, which is already bind-mounted, and the
slug is derived from the project path — which matches host↔container because the
cwd is mounted at its identical path. So a session started in a yolo container
(or even on the host, same dir) is resumable. `--continue` resumes the most
recent session for the cwd; `--resume` takes an optional `SESSION_ID`, and bare
`--resume` opens Claude's interactive picker (works because we run `-it`).
Composes with all credential modes and with `--worktree` (resume is keyed to the
worktree's path). In worktree mode the `--name NAME` injection is **suppressed**
when resuming, because `claude` rejects `--name` alongside `--continue`/`--resume`
(the session already has its identity).

## Conventions / gotchas

- **macOS + Docker Desktop only as written.** Credential extraction uses the
  macOS `security` CLI. SSH agent forwarding mounts Docker Desktop's
  `/run/host-services/ssh-auth.sock` (the VM-side socket the Desktop proxies to
  the host agent), NOT the raw host `$SSH_AUTH_SOCK` — that socket's listener
  lives in the macOS kernel and is unreachable from the container's Linux VM
  (the mounted inode is dead: `connect()` → ECONNREFUSED). The host must have a
  running ssh-agent for forwarding to work. The Desktop socket is mounted
  `srw-rw---- root:root`, so the in-container `claude` user (uid = host uid, a
  non-root gid) can't `connect()` to it by default — `connect()` needs write
  perm on the socket inode, and the user is neither owner nor in group 0. Fix:
  `useradd -G root` puts `claude` in group 0, granting the socket's group-rw. No
  real privilege added (the user already has NOPASSWD sudo; the container is the
  sandbox).
- **In-process sandbox is disabled deliberately — the *container* is the
  sandbox.** We append `--settings '{"sandbox":{"enabled":false}}'` to the claude
  args so that, when the mounted `~/.claude/settings.json` has
  `sandbox.enabled: true`, Claude doesn't warn at startup that `bubblewrap`/`socat`
  are missing and run unsandboxed. `--settings` is a container-only overlay (host
  settings untouched). Do NOT instead install `bubblewrap` to "fix" it — a default
  Docker container can't create unprivileged user namespaces (`bwrap: No
  permissions to create new namespace`), and granting that capability would weaken
  the very isolation this tool exists to provide. (A `/doctor` sandbox note may
  still appear; that's expected.)
- **Argument splitting:** `main` splits `sys.argv` on `--` *before* argparse
  sees it. Everything after `--` is appended to `docker run` last, so
  user-supplied flags win (last-one-wins).
- **`--append-system-prompt` / `-p`** is repeatable and is added *on top of* a
  built-in prompt telling Claude it's in an ephemeral Ubuntu container.
- **Git identity is forwarded as env vars, not a mounted gitconfig.**
  `git_identity_args` reads the host's *effective* `user.name`/`user.email` (so a
  repo-local identity wins) and exports them as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.
  Mounting `~/.gitconfig` instead would drag in macOS-only bits (osxkeychain
  credential helper, GPG signing) that break commits in the Linux container. Note
  these env vars override any repo-local identity set *inside* the container.
- The container name is the cwd basename, suffixed with the config dir or AWS
  profile name when those modes are active; in `--worktree` mode it's
  `{main_repo_name}-{NAME}`.
- The `# https://claude.ai/chat/...` URL on line 2 and the upstream gist
  reference in git history are the script's provenance — this started as
  Migurski's gist.
