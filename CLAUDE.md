# CLAUDE.md

## What this is

`yolo.py` is a single-file Python script (no dependencies beyond the
stdlib) that runs Claude Code inside an ephemeral Docker container with
`--dangerously-skip-permissions`. Containing the blast radius of "yolo mode"
is the whole point: Claude can run unattended inside the container without
touching the host beyond the bind-mounted working directory.

The script itself is **stdlib-only and standalone** — it ships as one PEP 723
file with no runtime dependencies and is run directly. The repo *also* carries a
small uv-managed dev setup (`pyproject.toml`, `tests/`) for linting and tests;
that tooling is never needed to *run* the script, only to develop it (see
**Development** below). Run it directly:

```bash
./yolo.py                          # default ~/.claude credentials
./yolo.py --config-dir ~/.claude-work          # alternate config dir
./yolo.py --bedrock --aws-profile myprofile --aws-region us-west-2 --bedrock-model some.model.id
./yolo.py --bedrock --config-dir ~/.claude-bdr # Bedrock + alternate config dir
./yolo.py --no-claude-json         # don't mount the host ~/.claude.json
./yolo.py --no-ssh-agent           # don't forward the host ssh-agent
./yolo.py -- --network host        # extra docker run args
./yolo.py -c                       # resume most recent session in this dir
./yolo.py -r                       # interactive session picker
./yolo.py -r SESSION_ID            # resume a specific session
./yolo.py start fix-auth           # new worktree+branch, launch a session (see verbs)
./yolo.py resume fix-auth          # re-enter that worktree, continue the session
./yolo.py shell fix-auth           # bash shell in that worktree's container
./yolo.py finish fix-auth          # remove the worktree, keep the branch
./yolo.py list                     # this repo's worktrees
```

All of `--config-dir`, `--bedrock`, `--worktree`, `--claude-json`, and
`--ssh-agent` are **orthogonal flags** — any reasonable combination is valid. The
only positional args are an optional `verb` (`init`/`start`/`resume`/`shell`/
`finish`/`list`) and its `TOPIC`; see [Worktree workflow verbs](#worktree-workflow-verbs).

Defaults for most flags can also live in a `.yolo.json` file (see the config
file section below):

```bash
./yolo.py init            # scaffold a .yolo.json of defaults in the cwd
echo '{"config-dir": "~/.claude-work", "ssh-agent": false}' > .yolo.json
./yolo.py                 # picks up .yolo.json; equals passing those flags
./yolo.py --ssh-agent     # explicit flag still overrides the file
```

The shebang is `#!/usr/bin/env -S uv run --script` with a PEP 723 metadata block
(`requires-python = ">=3.10"`, no dependencies), so the script self-runs under
**uv**, which guarantees a Python ≥3.10 (the `str | None` annotations need it;
macOS system `python3` is often 3.9). Running it therefore requires `uv` to be
installed. It's still stdlib-only — uv just selects the interpreter. uv preserves
the `--` separator, so docker-arg passthrough still works.

`yolo.py` is dual-purpose — the *same file* is both the standalone PEP 723 script
and an importable module with a `main()` entry point. So there are two ways to run
it from anywhere:

- **Installed** (preferred): `uv tool install <repo-or-PyPI>` (or `pipx install`)
  builds the wheel and puts a `yolo` executable on PATH in its own isolated venv,
  pulling in zero runtime deps. `uv tool upgrade claude-yolo` updates it. The
  console-script wiring is `[project.scripts] yolo = "yolo:main"` in
  `pyproject.toml`; the wheel ships only `yolo.py` (`[tool.hatch.build.targets.wheel]
  only-include`).
- **Standalone**: `chmod +x yolo.py` and symlink it onto PATH
  (`ln -s "$PWD/yolo.py" ~/.local/bin/yolo`); the PEP 723 header makes it self-run,
  and a symlink keeps it tracking the repo with no build step.

The PyPI/dist name is `claude-yolo`; the command it installs is `yolo`. `main()`
is the console-script entry point *and* the `if __name__ == "__main__"` target, so
both paths run identical code.

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
   in-container `claude` user matches `os.getuid()`. This keeps bind-mount file
   ownership correct: working-dir edits land on the host owned by the user, and
   the chmod-600 credentials file and mounted `~/.claude` stay readable inside —
   keep it. (SSH-agent socket access is *not* what needs this; that's granted
   separately by group-0 membership — see the gotchas.)
3. **Checks host login** (`ensure_logged_in` / `_is_logged_in`) before launch in
   the keychain modes (skipped for Bedrock). Runs `claude auth status --json` and
   reads the `loggedIn` field; if logged out, offers to run `claude auth login`
   then re-checks. Checks login *status*, not token expiry, on purpose: an expired
   accessToken is auto-refreshed at runtime via the stored refreshToken, so expiry
   alone doesn't mean logged out. For an alternate `--config-dir` it sets host-side
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

## Four orthogonal config/credential axes (all flags, freely combinable)

The old single overloaded positional (config dir *or* AWS profile, decided by
`is_dir()`) is gone. `main` now assembles the credential/config args from four
independent blocks, none mutually exclusive:

- **`--config-dir PATH`** (default `~/.claude`) → mounted at `/home/claude/.claude`.
  When set, credentials are pulled with the hashed service name and the container
  name gets a `-{basename}` suffix. The mount is *always* at `/home/claude/.claude`
  (= the `claude` user's `$HOME/.claude`, Claude Code's default), so **no in-container
  `CLAUDE_CONFIG_DIR` is set** — it would be redundant.
- **`--claude-json` / `--no-claude-json`** (default on) → whether to mount the host
  `~/.claude.json` (global config: MCP servers, project history/trust). It lives at
  `$HOME/.claude.json` regardless of `CLAUDE_CONFIG_DIR`, so there's only ever one.
  `--no-claude-json` gives a cleanly isolated profile — the intended pairing with an
  alternate `--config-dir`.
- **`--bedrock`** (+ optional `--aws-profile`, `--aws-region` [default `us-east-1`],
  `--bedrock-model`) → sets `CLAUDE_CODE_USE_BEDROCK=1`, mounts `~/.aws` read-only,
  and **skips keychain extraction and the login check** (AWS creds instead). Container
  name gets a `-{profile-or-bedrock}` suffix. The three AWS sub-flags require
  `--bedrock` (validated in `main`); `--aws-profile` is optional (SDK default creds
  used if omitted).
- **`--ssh-agent` / `--no-ssh-agent`** (default on) → forward the host ssh-agent
  socket (see gotchas). `--no-ssh-agent` drops the socket mount, `SSH_AUTH_SOCK`, and
  the `known_hosts` mount; in-container GitHub git auth then won't work, since the
  baked HTTPS→SSH rewrite relies on the agent.

Keychain credential extraction happens **iff not `--bedrock`**; the config-dir mount,
the `~/.claude.json` mount, and the Bedrock env are otherwise independent — so e.g.
`--bedrock --config-dir ~/.claude-bdr` (Bedrock auth, separate profile) now works,
which the old positional scheme could not express. `--bedrock` is a
`BooleanOptionalAction`, so `--no-bedrock` can turn off a `.yolo.json` that
enables it.

## `.yolo.json` config file (flag defaults)

A `.yolo.json` **JSON object** supplies defaults for most flags.
`load_yolo_config` applies them via `PARSER.set_defaults` *before* `parse_args`,
so explicit CLI flags still win. Two layers, merged low→high:

1. `~/.yolo.json` (the base), then
2. the **nearest `.yolo.json` at or above the cwd** (the overlay) — found by
   walking cwd's ancestors and taking the first hit; only that one project file
   is used, not every ancestor. Searched against the *real* cwd, before any
   `--worktree` retargeting. If the nearest file *is* `~/.yolo.json` (cwd under
   `$HOME`, nothing closer), it's loaded once.

Precedence overall: `~/.yolo.json` < nearest `.yolo.json` < CLI flags. Per key
the higher layer **overrides**, except `append-system-prompt`, which
**concatenates** across both files and then the CLI `-p` values (so prompts
accumulate; everything else replaces).

Keys mirror the flag names (dashes or underscores both accepted). Supported:
`config-dir`, `bedrock`, `aws-profile`, `aws-region`, `bedrock-model`,
`claude-json`, `ssh-agent`, `base`, `append-system-prompt` (string or list of
strings). Per-invocation **actions** — `--worktree`, `--continue`, `--resume`,
and the verbs — are deliberately **not** config keys; putting them in a
`.yolo.json` is a hard error (they're not in `YOLO_KEYS`).
`config-dir` gets `~` expanded (a JSON file can't lean on shell expansion).
Booleans must be JSON `true`/`false`. A JSON **`null`** for any key means "leave
at the built-in default" (the loader skips it). Unknown keys, wrong types, and
malformed JSON all `sys.exit` with the offending file path (`_parse_yolo_file`).

### `init` verb

`yolo.py init` (`write_default_yolo`) scaffolds a `.yolo.json` of default
values into the cwd, then exits — it does **not** run a container. `init` is one
value of the optional positional `verb` (`choices=["init", "start", "resume",
"shell", "finish", "list"]`; see [verbs](#worktree-workflow-verbs)); with no verb,
`main` proceeds to the run path. The verb is dispatched off a *first* `parse_args`
**before** any `.yolo.json` is loaded — so a broken ancestor/global config can't
block scaffolding a fresh one — and the run path then re-parses with the config
defaults layered in. `init` refuses to overwrite an existing `.yolo.json`.

The scaffold lists every key. Keys whose default is unset (`config-dir`,
`aws-profile`, `aws-region`, `bedrock-model`) are written as `null`; the rest
get their real defaults (`bedrock: false`, `claude-json`/`ssh-agent: true`,
`base: "HEAD"`, `append-system-prompt: []`). So an *unedited* scaffold is inert at the top level
(it just restates the defaults) — but note those non-null booleans, being
explicit, would **override** a `~/.yolo.json` that set them differently, since a
project `.yolo.json` is the higher-precedence layer. The `YOLO_INIT_DEFAULTS`
literal must stay in sync with `YOLO_KEYS`.

Note `--bedrock` is a `BooleanOptionalAction` partly *because* of this file: a
`.yolo.json` can set `"bedrock": true`, and `--no-bedrock` is then the only way
to override it back off. AWS sub-keys without bedrock mode now just **warn** (and
are ignored) rather than erroring, since bedrock may legitimately be toggled off
on the CLI over a `.yolo.json` that set the AWS knobs.

## Worktree workflow verbs

The opinionated front door to the worktree machinery: most work is meant to land
on a branch that can be merged or PR'd. All take a `TOPIC` (the worktree/branch
name) and run from inside a git repo.

- **`start TOPIC`** — create a new worktree + branch `TOPIC` off `--base`
  (default `HEAD`; see `base` below) and launch a container with a fresh session
  named `TOPIC`. **Errors if the worktree or branch already exists** (use
  `resume`).
- **`resume TOPIC`** — launch a container on an existing worktree. Default
  `claude --continue` (most recent session); `--new` starts a fresh named
  session; `-r [ID]` resumes a specific one / opens the picker. **Errors if the
  worktree doesn't exist** (use `start`).
- **`shell TOPIC`** — a bash shell on the worktree. If a container is **running**
  for it (label match) → `docker exec -it <id> /bin/bash`; otherwise a fresh
  ephemeral container with `--entrypoint /bin/bash`.
- **`finish TOPIC`** — `git worktree remove` the worktree, **keep the branch**.
  Refuses if a container is running, or on uncommitted changes (unless `--force`).
  Leaves transcripts (they self-expire via `cleanupPeriodDays`). Prints whether
  the kept branch is pushed.
- **`list`** — the repo's worktrees as a table (TOPIC/BRANCH/STATUS/DIRECTORY).
  STATUS is `running`/`dirty`, else `merged`/`unmerged` (idle+clean) judged by
  whether the branch is reachable from **`base`** — exactly `git branch --merged
  <base>` (default `base` is `HEAD` = the main checkout; honours
  `.yolo.json`/`--base`). So a fast-forward-merged or never-diverged branch reads
  `merged`; a *squash*-merge isn't reachable and reads `unmerged`. `do_list` runs
  the check in the main repo (not `git -C <worktree>`) so a `HEAD` base resolves
  to the main checkout, not the worktree's own branch.

Implementation shape:

- **Dispatch is two-tier** (`main`). Terminal verbs (`init`, `list`, `finish`,
  and `shell`'s exec-into-running case) run off the *first* `parse_args`, before
  `.yolo.json` is layered — they don't need credential config. Launch verbs
  (`start`, `resume`, `shell`-fresh, `--worktree`, bare) then load config,
  re-parse, and call `launch_container`.
- **`launch_container`** is the single assembly+exec path shared by every launch
  (extracted from the old inline `main`): mounts, ssh-agent block, the four
  credential/config blocks, labels, `--entrypoint` override, then `os.execvp`. It
  takes `container_base`, `command` (args after the image), and optional
  `entrypoint`. `build_claude_args` builds the `claude` command (settings,
  built-in prompt, `--continue`/`--resume`, `--name`).
- **Containers are found by docker label, not name.** Every launch is stamped
  `--label yolo.repo=<repo-slug>` (and `yolo.worktree=<topic>` for worktrees);
  `running_container_for(slug, topic)` queries `docker ps --filter label=…`. This
  is robust to the `-{config}`/`-{profile}` name suffixes. `shell`/`finish`/`list`
  rely on it.
- Verb-only flags: `--base REF` (config-backed via the `base` key; consumed only
  by `start`/`--worktree`), `--new` (resume), `--force` (finish). `--new`/`--force`
  are validated against their verb in dispatch.

## `--worktree NAME` (the underlying primitive)

`start`/`resume` are sugar over this. Orthogonal to the credential modes (composes
with any of them). `setup_worktree` creates/reuses a git worktree on branch `NAME`
(off `base`, default current `HEAD`, no upstream) at
`~/.claude-yolo/worktrees/<repo-slug>/NAME`, where `<repo-slug>` is the main repo
path slugified the way Claude names `~/.claude/projects/` buckets
(`re.sub(r"[^a-zA-Z0-9]", "-", path)`, factored into `_repo_paths`). `main` then
retargets `cwd` to the worktree (so `-w` and the `{cwd}:{cwd}` mount point there)
and **additionally mounts the shared `.git` at its identical host path** — both
same-path mounts are required because a linked worktree stores *absolute* paths to
its `.git` and back. The session is named via `claude --name NAME`. Durability is
the point: commits land in the host's shared `.git` and uncommitted edits live in
the host worktree dir, so a container exit loses nothing. Must be run from inside
a git repo.

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
  macOS `security` CLI. SSH agent forwarding (on by default, disabled with
  `--no-ssh-agent`) mounts Docker Desktop's
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
- **GitHub HTTPS git is rewritten to SSH so it reuses the agent.** The image bakes
  `git config --system url."git@github.com:".insteadOf "https://github.com/"`, so
  in-container git operations on `https://github.com/...` remotes (fetch *and* push)
  transparently route over SSH and authenticate via the forwarded ssh-agent — **no
  token ever enters the container**. This is the only HTTPS-auth approach that keeps
  the secret-never-in-container property: HTTPS auth is a bearer token (the token
  must reach whoever makes the request), whereas SSH is challenge-response (the key
  stays on the host, the agent only signs). The host's `osxkeychain` credential
  helper is a macOS binary backed by the macOS Keychain — neither exists in the
  Linux container, which is the other reason plain HTTPS push can't work here. Host
  config is untouched (we never mount `~/.gitconfig`); remotes can stay HTTPS.
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
- The container name is the cwd basename (or `{main_repo_name}-{NAME}` in
  `--worktree` mode), then suffixed with `-{config-dir-basename}` when
  `--config-dir` is set and `-{aws-profile-or-"bedrock"}` when `--bedrock` is
  set. Suffixes stack, so the axes compose in the name too.
- The `# https://claude.ai/chat/...` URL on line 2 and the upstream gist
  reference in git history are the script's provenance — this started as
  Migurski's gist.

## Development

`pyproject.toml` defines a **uv-managed project** with no runtime dependencies.
Its `dev` dependency group carries the only deps — `ruff` and `pytest`. The
project *is* packaged (hatchling build backend, `[project.scripts] yolo =
"yolo:main"`, wheel ships only `yolo.py`) so it can `uv tool install`, but the
runtime module stays stdlib-only — packaging adds no runtime dependency. `uv.lock`
is committed; `.venv/`, `dist/`, and the tool caches are gitignored.

```bash
uv sync                 # create/refresh .venv with the dev tools
uv run pytest           # run the test suite (tests/)
uv run ruff check .     # lint
uv run ruff format .    # format
uv build                # build the wheel/sdist into dist/ (for publishing)
```

Tests load `yolo.py` via `importlib` **from its file path** (not a plain
`import yolo`) so each test gets a **fresh module instance** — `main()` mutates
the module-global `PARSER` through `set_defaults`, so isolation matters; loading
from the path also pins the tests to the source file regardless of any installed
`yolo`. They
never touch the host or Docker: `tests/conftest.py`'s `run_cli` fixture stubs
`build_docker_image`, `ensure_logged_in`, `extract_credentials`,
`git_identity_args`, and `os.execvp`, then asserts on the captured `docker run`
argv. `test_config.py` covers `.yolo.json` parsing/merging and the `init`
scaffold; `test_cli.py` covers verb dispatch and arg assembly across the
credential/config axes. `test_verbs.py` covers the worktree verbs against a
**real throwaway git repo** (so the actual `git worktree` machinery runs),
stubbing only `running_container_for` (docker) plus the `run_cli` side effects.
Keep them green when changing flags or mounts.
