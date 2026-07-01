# CLAUDE.md

## What this is

`yolo.py` is a Python script that runs Claude Code inside an
ephemeral Docker container with `--dangerously-skip-permissions`. Containing the
blast radius of "yolo mode" is the whole point: Claude can run unattended inside
the container without touching the host beyond the bind-mounted working directory.

The runtime is **`yolo.py` plus a few sibling data files** — `Dockerfile.default`,
`Dockerfile.custom`, and `container-prompt.txt`, loaded by `_read_data_file`
(which resolves them relative to `__file__`, following a PATH symlink the way
`_pyproject_version` does). yolo is **runnable two ways** — standalone via its
PEP 723 header (`./yolo.py` self-runs under uv) or as an installed console script
(`uv tool install`, regular or `--editable`); a PATH symlink also works. Either
way the data files sit beside `yolo.py` (shipped in the wheel via `only-include`,
or in the source checkout), so `_read_data_file` finds them. Its one runtime
dependency is **`keyring`** (the cross-platform credential store; see the
auth/secrets sections), declared in *both* the PEP 723 `dependencies` block at the
top of `yolo.py` and `pyproject.toml` — keep the two in sync. uv carries the dep in
both run modes. (The older "stdlib-only / zero runtime deps" goal was dropped when
`keyring` was adopted for multiplatform support.) The repo *also* carries a small
uv-managed dev setup (`pyproject.toml`, `tests/`) for linting and tests; that
tooling is never needed to *run* the script, only to develop it (see
**Development** below).

**Host platforms:** macOS and Linux are fully supported; Windows is supported via
WSL2 (which presents as Linux). Native Windows (no WSL) is out of scope. The
*container* is always Linux regardless of host, so only the host-side glue —
credential store, clipboard, ssh-agent socket, temp dir — varies by OS (gated
through the `_HOST` / `_is_macos()` / `_is_linux()` helpers). Run it directly:

```bash
./yolo.py                          # default: long-lived OAuth token (consent-prompted mint on first run)
./yolo.py --config-dir ~/.claude-work          # alternate config dir
./yolo.py --auth keychain          # mount a snapshot of the rotating keychain creds instead
./yolo.py --auth bedrock --aws-profile myprofile --aws-region us-west-2 --bedrock-model some.model.id
./yolo.py --auth bedrock --config-dir ~/.claude-bdr # Bedrock + alternate config dir
./yolo.py --no-claude-json         # don't mount the host ~/.claude.json
./yolo.py --ssh-agent              # forward the host ssh-agent (off by default)
./yolo.py --submodules             # populate git submodules before launch (off by default)
./yolo.py --no-redirect-build-dirs # don't redirect .venv/target/__pycache__ off the bind mount (cwd, on by default)
./yolo.py --mount ~/refdocs        # also mount ~/refdocs (read-only) at its host path
./yolo.py --mount ~/other:rw       # extra mount, writable
./yolo.py --yolorc ./setup.sh      # source this file inside the container at startup
./yolo.py --dockerfile ./Dockerfile.yolo  # build the image from a custom Dockerfile
./yolo.py dockerfile               # print the built-in default Dockerfile (a starting point)
./yolo.py --port 8000              # forward container port 8000 (docker picks the host port)
./yolo.py --port 8000:8000         # ...or pin host port 8000 (single-session)
./yolo.py browse                   # open the browser at this session's forwarded port
./yolo.py browse fix-auth          # ...or at a worktree session's
./yolo.py setup-token              # mint+cache the long-lived OAuth token explicitly
./yolo.py tokens                   # list minted tokens (mint date, est. expiry, status)
./yolo.py forget-token             # delete this config dir's token (local only)
./yolo.py secret set GH_TOKEN      # store a secret in the credential store (stdin/prompt)
./yolo.py secret set GH_TOKEN --clipboard   # ...read the value from the clipboard
./yolo.py secret set DB_PW --project        # ...at project scope (not global)
./yolo.py secret list              # list global + this project's secrets
./yolo.py secret list --all        # ...across every project
./yolo.py secret rm GH_TOKEN       # delete a secret (keychain + registry)
./yolo.py --secret GH_TOKEN        # inject secret GH_TOKEN as env var $GH_TOKEN
./yolo.py --secret DB_PW:PGPASSWORD # ...as env var $PGPASSWORD (renamed)
./yolo.py --secret KEY:~/.ssh/id_ed25519  # ...mounted as a file at that path
./yolo.py --plugin-dir ~/.claude-yolo/skills-plugin  # load a local plugin (its skills) into every yolo session
./yolo.py --clone https://github.com/me/lib ../lib   # git clone a repo into ../lib (a sibling) at session start
./yolo.py -- --network host        # extra docker run args
./yolo.py                          # == `yolo start`: fresh session in the cwd
./yolo.py resume                   # continue most recent session in this dir
./yolo.py resume -r                # interactive session picker (cwd)
./yolo.py resume -r SESSION_ID     # resume a specific session (cwd)
./yolo.py shell                    # bash shell in the cwd's container (or fresh)
./yolo.py stop                     # stop the running session in the cwd
./yolo.py start fix-auth           # new worktree+branch, launch a session (see verbs)
./yolo.py resume fix-auth          # re-enter that worktree, continue the session
./yolo.py shell fix-auth           # bash shell in that worktree's container
./yolo.py stop fix-auth            # stop that worktree's running session
./yolo.py finish fix-auth          # remove the worktree; delete the branch if merged, else keep+warn
./yolo.py finish fix-auth --finish-action merge    # ...merge the branch into HEAD, then delete it
./yolo.py finish fix-auth --finish-action push --finish-remote origin  # ...push the branch, keep it local
./yolo.py rebase fix-auth          # rebase the worktree's branch onto --base (default HEAD)
./yolo.py list                     # this repo's worktrees
./yolo.py list --all               # every repo's worktrees under ~/.claude-yolo/worktrees
./yolo.py dir fix-auth             # print that worktree's dir (cd "$(yolo dir fix-auth)")
./yolo.py ps                       # running yolo containers, across all repos
./yolo.py ps --watch               # ...refreshing every 2s (the ps picker)
./yolo.py wip                      # the dashboard: manage every session/worktree/project
./yolo.py --tmux                   # spawn the session as a tmux window instead
./yolo.py --version                # print the version and exit
```

The **auth mechanism** is a single mutually-exclusive choice via `--auth`
(`oauth-token` [default] / `keychain` / `bedrock`). Everything else —
`--config-dir`, `--claude-json`, `--ssh-agent`, `--mount`, `--port`, `--tmux` —
is an **orthogonal flag** that composes freely with the chosen auth mode and
with each other. The only positional args are an optional `verb`
(`config`/`start`/`resume`/`shell`/`stop`/`browse`/`finish`/`rebase`/`list`/`ps`/`wip`/`dir`/
`dockerfile`/`setup-token`/`tokens`/`forget-token`/`secret`) and its `TOPIC` (for
`secret` the TOPIC is the subcommand `set`/`list`/`rm`, with the secret NAME as a
trailing positional); see [Workflow verbs](#workflow-verbs).

Defaults for most flags can also live in **host-side config** — global
`~/.yolo.json` plus a per-project entry in `~/.claude-yolo/projects.json`,
written with the `config` verb (see the config section below; an in-directory
`.yolo.json` is deliberately **no longer read**):

```bash
./yolo.py config --global --ssh-agent        # set a global default in ~/.yolo.json
./yolo.py config --config-dir ~/.claude-work --mount ~/refdocs
                          # persist those flags as THIS project's entry
./yolo.py                 # picks up both layers; equals passing those flags
./yolo.py --ssh-agent     # explicit flag still overrides the files
./yolo.py config          # show the entry that currently applies (read-only)
./yolo.py config --add-mount ~/other:rw      # edit the mounts list element-wise
./yolo.py config --remove-mount ~/refdocs    #   (vs --mount, which replaces it)
./yolo.py config --unset config-dir          # drop a key -> lower layers apply
./yolo.py config fix-auth                     # show worktree fix-auth's overlay
./yolo.py config fix-auth --add-port 8000     # edit that worktree's overlay
./yolo.py resume fix-auth --mount ~/refdocs   # resume + persist the new mount to the overlay
```

The shebang is `#!/usr/bin/env -S uv run --script` with a PEP 723 metadata block
(`requires-python = ">=3.10"`, `dependencies = ["keyring>=24"]`), so the script
self-runs under **uv**, which guarantees a Python ≥3.10 (the `str | None`
annotations need it; macOS system `python3` is often 3.9) and provisions the
`keyring` dep into an ephemeral cached environment. Running it therefore requires
`uv` to be installed. uv preserves the `--` separator, so docker-arg passthrough
still works.

`yolo.py` is dual-purpose — the *same file* is both the standalone PEP 723 script
and an importable module with a `main()` entry point. So there are two ways to run
it from anywhere:

- **Installed** (preferred): `uv tool install <repo-or-PyPI>` (or `pipx install`)
  builds the wheel and puts a `yolo` executable on PATH in its own isolated venv,
  resolving the `keyring` dep into that venv. `uv tool upgrade claude-yolo` updates
  it. The console-script wiring is `[project.scripts] yolo = "yolo:main"` in
  `pyproject.toml`; the wheel ships `yolo.py` plus its data files (`Dockerfile.default`,
  `Dockerfile.custom`, `container-prompt.txt`) via `[tool.hatch.build.targets.wheel]
  only-include`, so they land beside `yolo.py` in site-packages where
  `_read_data_file` finds them.
- **Standalone**: `chmod +x yolo.py` and symlink it onto PATH
  (`ln -s "$PWD/yolo.py" ~/.local/bin/yolo`); the PEP 723 header makes it self-run,
  and a symlink keeps it tracking the repo with no build step.

The PyPI/dist name is `claude-yolo`; the command it installs is `yolo`. `main()`
is the console-script entry point *and* the `if __name__ == "__main__"` target, so
both paths run identical code.

`--version` (the argparse `version` action) prints `_version()`, which mirrors this
dual nature: it first reads the recorded package metadata
(`importlib.metadata.version("claude-yolo")`, present in the installed wheel), and
falls back to scraping `version` out of the **adjacent pyproject.toml** (resolving
the script's symlink, so the PATH-symlink standalone install works) — so both modes
trace back to the single source of truth, pyproject.toml, with no second copy of the
number in `yolo.py`. A stray copy with neither metadata nor pyproject reports
`unknown`.

## How it works

1. **Builds the image** (`build_docker_image`) from the built-in
   `DEFAULT_DOCKERFILE` (the `Dockerfile.default` data file, a plain Dockerfile
   with no templating, loaded by `_read_data_file`) written to a
   temp dir — or, when `--dockerfile`/the `dockerfile` config key is set, from
   **that** file instead. Ubuntu 26.04 + nodejs/npm + a few
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
   install and which self-update can't manage. The image **tag is
   content-addressed** — `claude-yolo:{hash8}` where `hash8` hashes the Dockerfile
   text + host UID (`_image_tag`) — so the built-in default and each custom
   Dockerfile get *distinct* images. This matters because yolo runs sessions in
   parallel: a single fixed tag would let two concurrent builds (default vs.
   custom) race and one `docker run` pick up the other's image. `--dockerfile`
   is an *override*, not a relocation, though a custom
   file is meant to *layer on* the default via `FROM ${YOLO_BASE}` rather than
   replace it wholesale (see the `--dockerfile` flag in the orthogonal-flags
   section). The **build
   context is the temp dir and contains only the Dockerfile** — that empty
   context is what stops a custom Dockerfile's `COPY`/`ADD` from reaching host
   files (a Dockerfile also can't add host bind-mounts — those are yolo's
   host-side `docker run` args). `build_docker_image` asserts the context holds
   nothing but the Dockerfile before building, guarding the invariant against
   future regressions; yolo passes no `--secret`/`--ssh` to `docker build`, so
   `RUN --mount=type=secret/ssh` can't reach host material either.
2. **Passes the host UID as the `HOST_UID` build ARG** (`--build-arg
   HOST_UID=os.getuid()`), which the Dockerfile's `ARG HOST_UID` feeds to
   `useradd`, so the in-container `claude` user matches `os.getuid()` (no Python
   string substitution anymore — the Dockerfile is a literal). This keeps
   bind-mount file ownership correct: working-dir edits land on the host owned by
   the user, and the chmod-600 credentials file and mounted `~/.claude` stay
   readable inside — keep it. A custom `--dockerfile` should likewise `ARG
   HOST_UID` and use it for its non-root user. (SSH-agent socket access is *not*
   what needs this; that's granted separately by group-0 membership — see the
   gotchas.)
3. **Checks host login** (`ensure_logged_in` / `_is_logged_in`) before launch in
   keychain mode only (the default oauth-token mode and Bedrock skip it). Runs
   `claude auth status --json` and
   reads the `loggedIn` field; if logged out, offers to run `claude auth login`
   then re-checks. Checks login *status*, not token expiry, on purpose: an expired
   accessToken is auto-refreshed at runtime via the stored refreshToken, so expiry
   alone doesn't mean logged out. For an alternate `--config-dir` it sets host-side
   `CLAUDE_CONFIG_DIR` so the check targets the right keychain entry. If host
   `claude` is missing/too old for `auth`, it returns True and defers to the
   empty-file check in `extract_credentials`.
4. **Extracts credentials** (`extract_credentials`; keychain mode only) from
   wherever the *host's* Claude Code keeps them, into a chmod-600 file **in the
   per-session run dir** (`<run-dir>/<container>/`; see the run-dir section) that
   gets bind-mounted to `.credentials.json`. This is OS-specific because the host
   store differs: on **macOS** it reads the login Keychain via the `security` CLI,
   service `Claude Code-credentials` (or `Claude Code-credentials-{hash8}` for a
   non-default config dir, hash8 = first 8 hex of the SHA-256 of the resolved
   config path — mirrors Claude Code's own keychain naming, so an upstream scheme
   change breaks it); on **Linux/other** Claude Code has no Keychain and stores
   creds in a `.credentials.json` *file* in the config dir, which yolo simply
   reads. In the default oauth-token mode this whole step is replaced by staging
   the `CLAUDE_CODE_OAUTH_TOKEN` into `/run/secrets` (the env-secret file
   transport, not `-e` — see the oauth-token section).
5. **Assembles `docker run` args** and `os.execvp`s into docker (replacing the
   process, so it's interactive `-it --rm`). The args also forward the host git
   identity (`git_identity_args`) and the SSH agent (see gotchas).

## Auth mechanism (`--auth`) + orthogonal config axes

The old single overloaded positional (config dir *or* AWS profile, decided by
`is_dir()`) is gone. The **auth mechanism** is now a single mutually-exclusive
choice — `--auth {keychain,oauth-token,bedrock}` (default `oauth-token`,
`AUTH_CHOICES`) — so argparse's `choices` enforces the exclusivity structurally
(no hand-written "these two can't combine" guard). The config axes compose freely
on top of whichever auth is chosen:

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
- **`--ssh-agent` / `--no-ssh-agent`** (default **off**) → forward the host
  ssh-agent socket (see gotchas). Off by default to keep your SSH keys out of the
  skip-permissions container — opt in with `--ssh-agent` (or `ssh-agent: true` in
  config) when you need in-container git auth. When off, there's no socket mount,
  `SSH_AUTH_SOCK`, `known_hosts` mount, **or HTTPS→SSH rewrite** — so plain HTTPS
  clones of public repos still work (the rewrite would turn them into SSH URLs that
  can't auth without the agent), but in-container *authenticated* GitHub git auth
  won't. The rewrite is applied as run-time git config only under `--ssh-agent`.
- **`--submodules` / `--no-submodules`** (default **off**; `submodules` in
  config) → on every launch, populate the working dir's git submodules
  (`_init_submodules`: `git submodule update --init --recursive`) just before
  `docker run`. Run **host-side on purpose**: it needs the host's git credentials
  and network. git (2.53, tested) gives each worktree/checkout its **own**
  submodule git dir — a new worktree clones the submodule **fresh from the
  remote** rather than reusing the objects in a sibling worktree or the shared
  `.git/modules/<name>` — so populating generally fetches; the host has the
  creds/network for that, whereas an in-container clone of a private submodule
  would fail with the ssh-agent off by default. The files land in the
  bind-mounted working dir so Claude sees them. A **no-op** when the dir has no
  `.gitmodules` (a plain repo, or a non-repo cwd), and **best-effort** — a failure
  (network/auth) warns but doesn't block the session. Off by default since most
  repos have none. Note neither `git merge` nor `git worktree add` checks out
  submodule contents, so without this you'd populate them by hand inside the
  container.
- **`--redirect-build-dirs` / `--no-redirect-build-dirs`** (default **on**;
  `redirect-build-dirs` in config; **cwd sessions only**) → export env vars that
  point per-OS / build dirs at fixed container-local paths **off the bind mount**,
  so the container never clobbers the host's copies on the live checkout. The set
  (`_BUILD_DIR_REDIRECTS`, hardcoded): `UV_PROJECT_ENVIRONMENT=/home/claude/.yolo-env/uv`
  (uv's `./.venv`), `CARGO_TARGET_DIR=…/cargo-target` (Rust `target/`),
  `PYTHONPYCACHEPREFIX=…/pycache` (`__pycache__`). Without this, the first
  container `uv run`/`cargo`/python rebuilds a macOS-built `./.venv` for Linux,
  corrupting the host's copy and killing any host dev server that re-execs
  `./.venv/bin/python`. An **env var** is the right lever because *every*
  in-container shell inherits the container's process env — claude, the launch
  wrapper, `yolo shell`, and crucially the agent's **Bash tool** subshells, which
  source a rotating `~/.claude/shell-snapshots/*.sh` rather than `~/.bashrc` (so
  editing rc files wouldn't reach them). Paths are **fixed, not per-project**: each
  session is its own container and `/home/claude` is container-local (discarded at
  exit), so nothing collides. **On by default** unlike the other bool keys
  (`ssh-agent`/`submodules` default off) because it *removes* host risk rather than
  adding exposure; **cwd-only** because a worktree is an isolated copy (gated on
  `worktree_name is None`, like the cwd live-checkout prompt). Applied as plain
  `docker -e` args in `launch_container`, so it composes with every auth mode and
  needs no mount. (`node_modules` has no env equivalent and is **not** redirected —
  see `plans/yolo-clobber-hardening.md` for the deferred volume-shadow approach.)
- **`--mount PATH[:ro|:rw]`** (repeatable; `mounts` in config) → bind-mount extra
  host **files or directories** (reference dirs, or a single secret like a token
  file) at their **identical host paths**, like the cwd. **Read-only by default**;
  `:rw` opts in. The source must exist (docker would otherwise create a *missing*
  one as a root-owned dir on the host — wrong, and wrong for an intended file).
  Each mounted **directory** is also forwarded to claude as `--add-dir`, so the
  dirs are working directories Claude actually knows about; a mounted **file** is
  not (`--add-dir` is dir-only) — it's bind-mounted but not announced. Mount lists
  **concatenate** across the config layers and the CLI (exact
  dups deduped; on a same-path ro/rw conflict the higher layer wins). A `shell`
  exec'd into a *running* container necessarily joins it with the mounts it was
  started with — docker can't add mounts to a live container.
- **`--port [HOST:]CONTAINER`** (repeatable; `ports` in config) → forward a
  container port to the host, always **loopback-bound** (`-p 127.0.0.1:…`; a host
  *address* is deliberately not expressible, so config can't put the container's
  server on the LAN — the raw `-- -p` passthrough is the escape hatch). A bare
  container port publishes with **host port 0**: docker assigns a free ephemeral
  port per session, so parallel sessions of one project never collide, and
  `docker port` (via `yolo browse`) is the registry of what was assigned — yolo
  keeps no port state. `HOST:` pins a stable host port (single-session;
  a concurrent second session fails at `docker run` with address-in-use). Port
  lists concatenate across layers/CLI like `mounts` (same-container-port
  conflict → higher layer wins; first-configured port is `browse`'s default).
  Each launch with ports stamps a **`yolo.ports`** label (container ports,
  config order) — what `browse`/`ps` read, describing the *actual* container —
  and adds a system-prompt line telling Claude servers must bind **0.0.0.0**
  (loopback-bound servers are unreachable through docker's forward), that
  the user opens them with `yolo browse` (or `b` in the `wip` dashboard), and to
  **keep the server running in the steady state** (restart while testing is fine,
  but leave it up) so the user can browse it whenever. Like mounts, mappings are fixed at
  `docker run` time and resolved only on launch paths.
- **`--secret NAME[:TARGET]`** (repeatable; `secrets` in config) → inject a
  keychain-stored secret (set with `yolo secret set`) into the session. A
  **list/concat dest like `mounts`/`ports`** — accumulates across the global /
  project / worktree layers and the CLI. The spec's TARGET picks the mechanism by
  its first character (`/` or `~` → file, else env): bare **`NAME`** → env var
  `NAME`; **`NAME:ENVNAME`** → env var renamed to `ENVNAME`; **`NAME:/abs`** or
  **`NAME:~/path`** → bind-mounted file at that **container** path (`~` →
  `/home/claude`, *not* the host `$HOME`). A trailing **`!`** on an env target
  makes it **ephemeral** (the loader deletes it right after exporting; a file
  target can't be ephemeral — a single-file bind mount can't be unlinked from
  inside). Concat/dedup mirrors mounts/ports (exact-dup specs deduped; on a
  target collision — same env name or mount path — the higher layer wins; a secret
  needed both ways is two specs). **No secret value ever reaches the docker-run
  argv** — and so not `docker inspect`'s `Config.Env`, not host `ps`, not tmux's
  retained pane command: env secrets transit a private `/run/secrets` file mount
  consumed by a baked loader, file secrets a read-only bind mount. (An env-target
  value *does* end up in the consuming process's in-container `/proc/<pid>/environ`
  — unavoidable for an env var the tool reads — but that's inside the session's own
  trust boundary; a file target avoids even that.) The opt-in gate is the same as
  `--yolorc`/`--dockerfile` — the *key* is host-side (Claude can't grant its next
  session a new secret). See [Secrets](#secrets). The Anthropic OAuth token rides
  this same env transport in oauth-token mode (see that section).
- **`--plugin-dir PATH`** (repeatable; `plugin-dirs` in config) → load a **local
  Claude Code plugin** (a directory or `.zip`) into the session. A **list/concat
  dest** in `_CONCAT_DESTS`, like `mounts`/`ports`/`secrets` — accumulates across
  the global / project / worktree layers and the CLI (exact-path dups deduped via
  the resolved path). Each resolved dir is bind-mounted **read-only at its
  identical host path** (`_resolve_plugin_dirs` / `launch_container`, beside the
  `mounts` loop) **and** appended to the claude command as `--plugin-dir <abspath>`
  (`build_claude_args`, like the per-mount `--add-dir`) — so claude's own
  session-only plugin loader picks it up. The point is **yolo-specific skills**:
  Claude Code discovers skills only at fixed paths (`~/.claude/skills/<name>`,
  project `.claude/skills/`, plugins) with no "extra skills dir" knob, and yolo
  mounts the host `~/.claude` wholesale — so a skill dropped there shows up in
  *host* sessions too. A plugin loaded via `--plugin-dir` is **session-only**
  (host Claude never passes the flag), so its bundled skills are available in every
  yolo session yet never leak into a plain host session, while the regular
  `~/.claude/skills` stay available (the `~/.claude` mount is untouched). Kept
  **separate from `mounts`** so a plugin dir is *not* also announced to claude as
  an `--add-dir` working directory. Validation is launch-time (the path must exist,
  like a mount; else a pointed exit) — resolved only on the launch paths, so a
  stale `plugin-dirs` path can't break `list`/`finish`/`config`. The opt-in gate is
  the same host-side-key model as `--secret`/`--yolorc`. **Keep the plugin dir
  outside `~/.claude`** so the host can't discover it; a `.claude-plugin/plugin.json`
  + `skills/<name>/SKILL.md` layout is the plugin shape claude expects.
- **`--clone URL DIR`** (repeatable; `clones` in config) → **`git clone URL` into
  `DIR` inside the container at session start.** A **list/concat dest** in
  `_CONCAT_DESTS`, accumulating across the layers and the CLI. The CLI takes two
  args (`nargs=2`, the `_CloneAction`); both the CLI and the config form normalize
  to `{url, dir}` **dicts**, so the config file stores a list of objects
  (`"clones": [{"url": …, "dir": …}]`) — `yolo config --clone URL DIR` persists that
  object form. `DIR` is the **container** destination, resolved by `_resolve_clones`
  against the session's working dir: absolute as-is, `~` → the container home
  `/home/claude`, else relative to `cwd` (so `../foo` is a sibling). **Only `cwd`
  itself is bind-mounted**, so a sibling/absolute dest lives in the container's
  *ephemeral* fs (re-cloned each session — fine for a clone); a *subdir* of `cwd`
  would land on the host bind-mount (persists, clutters the repo). The clones run in
  the **claude launch wrapper** (after secrets, before the `--yolorc` and `exec
  claude` — an rc commonly starts a server that depends on the clone) via the
  baked **`/etc/yolo/clone.sh <url> <dir> [<depth>]`** (`Dockerfile.default`), which
  skips an existing dest, `sudo mkdir`s/`chown`s a root-owned parent (a sibling's
  parent is docker-created as root), and treats a clone failure as non-fatal. Public
  HTTPS URLs need no auth; under `--ssh-agent` the HTTPS→SSH rewrite routes via the
  agent. An optional per-clone **`depth`** (a positive int) becomes
  `git clone --depth <depth>` (a shallow clone), passed by `_resolve_clones` as the
  optional 3rd arg to `clone.sh`; omitted → a full clone. There's **no top-level
  `--clone … DEPTH` launch flag** (depth is config-only that way), but it *is*
  settable through the element-edit flags below and the `wip` `c` editor. Resolved
  only on launch paths. Element edits: **`--add-clone URL DIR [DEPTH]`** /
  **`--remove-clone DIR`** (config-only, like `--add-mount`/`--remove-mount`) edit a
  single `clones` element — add replaces a same-`DIR` entry (so the url/depth can be
  changed) and takes an optional positive-int `DEPTH`; remove matches the stored
  `dir`. Whole-key `--clone`/`--unset clones` still replace/drop the list. Same
  host-side-key opt-in model as `--secret`.
- **`--rebuild-image`** (default off) → pass `--no-cache` to `docker build`, forcing
  a full image rebuild from scratch (useful when a baked tool is stale or the
  Dockerfile changed).
- **`--verbose` / `-v`** (default off; CLI-only, not a config key) → print the
  assembled `docker run` line before launching. It's hidden by default — long and
  rarely legible — and carries no secrets (the OAuth token and every `--secret`
  ride the `/run/secrets` file transport, not the argv), so it's purely a
  debugging convenience. Gated where the old unconditional `print(" ".join(
  run_cmd))` was, in `launch_container` just before `_dispatch_launch`.
- **`--dockerfile PATH`** (default unset; `dockerfile` in config) → build the
  container image from this Dockerfile instead of the built-in `DEFAULT_DOCKERFILE`.
  Override semantics (a single path, not a concat key); the path must exist and
  be a readable file (validated on the launch paths, like `--config-dir`, so a
  stale config path can't break `list`/`finish`/`config`). **Path resolution
  (`_resolve_dockerfile`):** a **relative** path — the common per-project case, a
  Dockerfile committed in the repo — is resolved against the session's working
  directory (the **worktree dir** in worktree mode, else the launch cwd), so the
  same checked-in `./Dockerfile.yolo` works in the main checkout and in every
  worktree, and a topical worktree can carry its own that differs from the
  others'. An **absolute** path (including a `~`-expanded one) is used as-is, for
  a generic image kept in some central collection rather than tied to a project.
  (Caveat: in plain non-worktree mode the cwd is wherever you launched, so a
  relative path resolves against a launch *subdirectory* rather than the repo
  root — fine for the dominant repo-root and worktree launches, and the right
  behavior for a monorepo subproject.) The same
  rule is applied at *both* the launch-time read (`_build_image`, against the
  retargeted `cwd`) and the `config`-time validation (`_apply_config_edits` takes
  a `base_dir` — the worktree dir for `config TOPIC`, the cwd otherwise — so
  `yolo config TOPIC --dockerfile ./Dockerfile.yolo` validates the worktree's copy
  even when run from the main checkout). `_build_image` then reads the file, builds
  it, and derives the content-addressed image tag (see "How it works" #1). The
  **recommended** custom-Dockerfile shape *layers on* the default rather than
  replacing it: a file that mentions `YOLO_BASE` (i.e. `ARG YOLO_BASE` / `FROM
  ${YOLO_BASE}`) triggers `_build_image` to first build `DEFAULT_DOCKERFILE` as a
  base image and pass its tag in as the `YOLO_BASE` build arg (via
  `build_docker_image`'s `build_args`), so the custom image inherits the default's
  user/sudo/entrypoint/etc.; its final tag folds in the base tag so a base change
  yields a distinct image. A file that does *not* mention `YOLO_BASE` is built
  as-is (the full-replacement escape hatch) and must itself `ARG HOST_UID` and
  create the `claude` user. Either way, a custom image is checked by
  `_verify_image_user` (`docker image inspect {{.Config.User}}`), which `sys.exit`s
  unless the image runs as `claude` — yolo passes no `-u`, so the image's final
  `USER` is the runtime user, and an image left on `USER root` would write
  host files as root. `yolo dockerfile` (`do_dockerfile`) prints the default as a
  starting point. Because the feature is opt-in, a `Dockerfile.yolo` sitting
  **unconfigured** in the session dir (the worktree dir in worktree mode, else
  the launch cwd) would otherwise be a silent no-op — yolo builds the default
  image and ignores it — so a launch **warns** (launch-only, beside the
  dockerfile-exists check) when `cwd/Dockerfile.yolo` is present but no
  `dockerfile` key is set, pointing at `yolo config --dockerfile
  ./Dockerfile.yolo`. **Caveat:** a `dockerfile` pointing at a file *inside*
  the bind-mounted working tree (e.g. `./Dockerfile.yolo`) is editable by Claude
  between runs, so Claude could alter the next image build. The *key* still lives
  in host-side `projects.json` (Claude can't add it), only the referenced file is
  in-tree — an accepted trade-off for an opt-in feature, but prefer an out-of-tree
  Dockerfile when the isolation matters.
- **`--yolorc PATH`** (default unset; `yolorc` in config) → **source** this shell
  file *inside* the container before the session starts. Path resolution mirrors
  `--dockerfile` (`_resolve_yolorc`): a **relative** path resolves against the
  session working dir (the worktree dir in worktree mode, else the launch cwd), so
  a checked-in rc tracks the worktree; an **absolute** path (incl. `~`) is used
  as-is, for an out-of-tree rc the container can't edit. The resolved file is
  bind-mounted **read-only** at the fixed `/home/claude/.yolorc`
  (`_YOLORC_CONTAINER_PATH`) and `YOLO_RC` is pointed at it. **Two source paths,
  one per session kind:** a *claude* launch is command-wrapped — yolo overrides the
  entrypoint to `/bin/bash` and runs `<load-secrets>; <clones>; . "$YOLO_RC"; exec
  claude …` (so the rc is sourced *after* any clones, since an rc commonly starts a
  server depending on a clone; claude isn't a shell, so `.bashrc` never runs for
  it); the claude args are passed positionally
  to `"$@"` so the `--settings` JSON and OAuth token need no re-quoting. A `yolo
  shell` (fresh or `docker exec`'d into a running container) instead sources it via
  the baked **`.bashrc`** (guarded by a `YOLO_RC_SOURCED` sentinel so nested
  subshells don't re-run it) — the env var rides the container's runtime env.
  `source` (not run) so the rc's `export`s reach the session env; a nonzero rc
  **warns but doesn't block** the session. The point is per-session setup that
  keeps secrets out of Claude's transcript — e.g. `gh auth login --with-token <
  tokenfile`, with `tokenfile` supplied via `--mount`. **Opt-in by design** (a key,
  not presence-detection): a repo's `.yolorc` is inert unless this key points at it,
  so cloning-and-running can't auto-execute it. The blast radius is the container
  anyway (anything the rc can do, the session could), and like an in-tree
  `--dockerfile` the *key* lives in host-side config (Claude can't add it) while an
  in-tree rc file is Claude-editable between runs — prefer an out-of-tree rc when
  that matters.
- **`--tmux` / `--no-tmux`** (default **off**; `tmux` in config) → spawn the
  session as a window of a shared tmux session instead of exec'ing in the
  invoking terminal; `--tmux-session NAME` / `tmux-session` names that session
  (default `yolo`). See [tmux mode](#tmux-mode---tmux-and-the-ps-verb).
- **Guardrails** (checked just before any container launch; the terminal verbs and
  `shell`-into-running are exempt): launching with the cwd **at or above `$HOME`**
  is a hard error — it would mount the whole home dir (incl. `~/.ssh` and yolo's
  own trusted config) read-write into a skip-permissions container — overridable
  only by the deliberately CLI-only `--dangerously-allow-home`. And the opt-in
  **`require-project-entry`** (bool; set it in `~/.yolo.json`) refuses to launch
  when no `projects.json` entry matches the cwd, so a renamed project fails loudly
  instead of silently falling back to global defaults; `--no-require-project-entry`
  overrides for one run, and `yolo config --init` registers an uncustomized
  project with an empty entry to satisfy it.

The three `--auth` values (the (c) block in `launch_container`):

- **`oauth-token`** (default) → authenticate with a long-lived
  `CLAUDE_CODE_OAUTH_TOKEN` env var; **skips keychain extraction and the login
  check**. The token is delivered through the **secrets file transport**
  (`_stage_secrets`' `extra_env`), *not* `-e`: staged as a chmod-600
  `<run-dir>/secrets/CLAUDE_CODE_OAUTH_TOKEN` file, mounted at `/run/secrets`, and
  exported by the baked loader in the launch wrapper — so the token stays off the
  docker-run argv, `docker inspect`, host `ps`, and tmux's pane command. (This is
  why every oauth-token claude launch is bash-wrapped.) It also overlays a
  throwaway `{}` `.credentials.json` (`_masking_credfile`) so a stale host creds
  file can't shadow the env token under Claude Code 2.1.x (see the precedence
  caveat below). It's the default because it has no refresh boundary, so it's safe
  regardless of session timing or concurrency. See
  [Long-lived OAuth token](#long-lived-oauth-token---auth-oauth-token-the-default) below.
- **`keychain`** → `ensure_logged_in` + `extract_credentials`, mounting
  the rotating keychain creds at `.credentials.json`. The only mode that runs the
  login check. Safe only when nothing crosses the credentials' shared refresh
  boundary (see the oauth-token section for the mechanics); kept for plans
  without `setup-token` (Claude Console accounts) and as an explicit opt-in.
- **`bedrock`** (+ optional `--aws-profile`, `--aws-region` [default `us-east-1`],
  `--bedrock-model`) → sets `CLAUDE_CODE_USE_BEDROCK=1`, mounts `~/.aws` read-only,
  **skips keychain extraction and the login check** (and overlays the same
  throwaway `.credentials.json` as oauth-token, so a container can't pollute the
  host `~/.claude`). Container name gets a
  `-{profile-or-bedrock}` suffix. The three AWS sub-flags only apply under
  `--auth bedrock` (a `main` warning fires if they're set otherwise);
  `--aws-profile` is optional (SDK default creds used if omitted).

The config-dir mount, the `~/.claude.json` mount, and the auth mechanism are
independent — so e.g. `--auth bedrock --config-dir ~/.claude-bdr` (Bedrock auth,
separate profile) works, which the old positional scheme could not express.
Overriding a config file that sets `auth` is just an explicit `--auth keychain`
(etc.) on the CLI.

## Long-lived OAuth token (`--auth oauth-token`, the default)

The keychain credentials are an OAuth pair whose **refresh token rotates
single-use on every refresh** — proven on 2026-06-08 (see `token-investigation.md`).
yolo mounts a *snapshot* of that pair into each container, so every container and
the host keychain hold the *same* pair and share one **refresh boundary** — the
access token's expiry. Whoever makes the first API call past the boundary (a
container *or* the host) refreshes and wins; every other holder's refresh token
is dead, and 401s at its own next refresh moments later. The hazard is therefore
not concurrency or session length per se but **anything running when the
boundary arrives** — a session started five minutes before expiry breaks
someone in five minutes. A container win also poisons the host keychain
(nothing writes the new pair back), logging out the host CLI and every
subsequent keychain-mode launch until a host re-login; `ensure_logged_in` can't
detect it, since login *status* can't reveal a dead refresh token without
spending it. We also confirmed (2026-06-09, `precedence-probe.sh` +
`host-write-probe*.sh`) that a non-empty `~/.claude/.credentials.json` can
override the host keychain, so simply co-locating a shared file in `~/.claude`
is *not* a safe fix.

`--auth oauth-token` sidesteps the whole problem by using a **different credential
family** for containers: `claude setup-token` mints a **one-year token that is
never rotated and never written back**. Because nothing ever rewrites it, any
number of concurrent containers — and the host on its own keychain creds — can use
it simultaneously with no interference. It reaches the container as the
`CLAUDE_CODE_OAUTH_TOKEN` env var — but **delivered via the secrets file transport,
not `-e`** (`launch_container` resolves it with `ensure_oauth_token`, hands it to
`_stage_secrets` as `extra_env`, and the baked loader exports it from a chmod-600
`/run/secrets` file in the launch wrapper), so the token stays off the docker-run
argv / `docker inspect` / host `ps` / tmux's retained pane command. This is why it
became the default in 0.6.0: keychain mode was an attractive nuisance — fine in a
quick test, with breakage governed by an invisible refresh boundary rather than
anything the user can see or control.

**Precedence caveat (the env var does *not* reliably out-rank a file).** It was
once true (probed 2026-06-09) that the env token out-ranked any
`.credentials.json`, so a stale mounted file couldn't shadow it. **Claude Code
2.1.x reversed this** (confirmed 2026-06-13): a present
`~/.claude/.credentials.json` is *preferred* over the env var, so a stale file
shadows the token, fails to refresh, and forces a `/login`. And such a file does
appear: inside the Linux container Claude Code has no Keychain and falls back to
the file store, so — through the read-write `~/.claude` mount — a container
*writes one back onto the host*, which the next launch then mounts in. So
oauth-token (and bedrock) now **overlay a throwaway `{}` `.credentials.json`**
(`_masking_credfile`) at that path, exactly as keychain mode overlays the real
extracted creds: this both masks any stale host file and captures the
container's own writes in a temp file that never persists. `launch_container`
also **warns** if a `~/.claude/.credentials.json` exists on the host at all
(it never should on macOS — the Keychain is the store).

Mechanics (`ensure_oauth_token` / `generate_oauth_token`):

- **Resolution order:** an explicit `CLAUDE_CODE_OAUTH_TOKEN` in the *host* env
  wins (for CI / self-managed tokens; it's global by nature) → else the
  yolo-managed credential-store entry **for the active config dir** → else mint a
  fresh one interactively and cache it there. That last (auto-mint) step is
  **consent-prompted and gated on `sys.stdin.isatty()`**: interactively, yolo
  explains what's about to be minted (1-year token, where it's stored,
  `forget-token` / the claude.ai revoke page) and asks `Proceed? [Y/n]` before
  running the flow — minting a year-long credential the user didn't explicitly
  ask for was the original argument against making this mode the default, so it
  is never done silently (`yolo setup-token` skips the prompt: running the verb
  *is* the consent). A non-interactive launch with no cached token (script/cron/
  no TTY) exits with guidance to run `yolo setup-token` or set the env var,
  rather than hanging on a browser flow nobody can drive.
- **Per-config-dir.** The token is cached under service `claude-yolo-oauth-token`
  for the default config dir, or `claude-yolo-oauth-token-{hash8}` for an alternate
  `--config-dir`, where `hash8` is the first 8 hex chars of the SHA-256 of the
  resolved path (`_oauth_service`) — the *same* hash Claude itself uses for its
  per-dir keychain entry. So each config dir (≈ each account/profile) gets its own
  long-lived token instead of one global token silently authenticating as the
  wrong account.
- **`yolo setup-token`** (a terminal verb) forces a (re)generation —
  use it for first-time setup or when the year is up. Honours `--config-dir`
  (and a config-file `config-dir`), caching under that dir's service name, so it
  matches what a launch will read. It runs `claude setup-token`
  under a **pty** so the child sees a real terminal (the browser/paste OAuth flow
  works) while yolo tees *and* captures the output, then scrapes the `sk-ant-…`
  token out (`_scrape_token`: ANSI-stripped, last match). The pty is resized
  **wide (512 cols)** via `TIOCSWINSZ` on first read — `pty.spawn` leaves the
  window 0×0, which `claude` treats as 80 columns and hard-wraps to, splitting
  the ~108-char token across lines so the scrape silently cached a truncated
  token that 401'd at runtime. As a backstop, a scraped match that ends at a
  line break with the next line continuing in the token alphabet is treated as
  wrapped and rejected. If scraping fails (wrap detected, output shape changed),
  it falls back to prompting for a manual paste. The token is upserted into the
  credential store (`_cred_set`). The pty path is Unix-only — `pty`/`termios`/
  `fcntl` are imported lazily inside `generate_oauth_token` so the module still
  imports on Windows (where minting would use the manual-paste fallback; not built,
  as native Windows is out of scope — WSL2 gets the pty path).
- **Storage — the credential store (`keyring`, with a file fallback).** The token
  is stored via the `keyring` package's `CredentialStore` abstraction
  (`_cred_get`/`_cred_set`/`_cred_delete`/`_cred_exists`, account = the login
  name): the macOS Keychain, Secret Service (libsecret) on Linux, or the Windows
  Credential Manager, all encrypted at rest. On a **headless** box with no Secret
  Service / D-Bus session keyring selects its `fail` backend; yolo detects that
  (`_keyring_available`) and falls back to a **chmod-600 file store** under
  `~/.claude-yolo/credentials/` (one hashed-name `.cred` file per service). Force
  the file store with `YOLO_CREDENTIAL_STORE=file` (used by the test suite, via a
  conftest autouse fixture, so tests never touch a real keyring). **Upgrade
  migration (macOS, temporary):** pre-keyring yolo stored tokens/secrets in the
  login Keychain via the `security` CLI, which keyring doesn't surface; so on macOS
  `_cred_get` falls back to reading the legacy item through `security`
  (`_legacy_keychain_get`) and migrates it into the active store — otherwise an
  existing user's cached token would look absent and yolo would re-mint. A shim to
  drop a release or two after keyring lands. Either way the
  token is *extract-only* — never rotated, never written back — so none of the
  precedence/rotation hazards of the mounted `.credentials.json` apply. Because
  keyring exposes no per-item modification date (unlike the macOS keychain), the
  **token-expiry estimate now reads its mint date solely from the `tokens.json`
  registry** (`_token_minted`), and the old "re-minted outside yolo" reconciliation
  in `yolo tokens` is gone.
- **Caveat:** this *does* put a bearer token inside the container env (a shift from
  the "secret never enters the container" SSH-agent philosophy), but it's a scoped,
  inference-only token — and no worse than the mounted refresh-token snapshot,
  which it replaces. The token reaches the container's env via the `/run/secrets`
  file transport, not `-e`, so it's no longer on the host docker-run argv /
  `docker inspect` / tmux pane command (it does still appear in claude's
  in-container `/proc/<pid>/environ` — inherent to an env var, and inside the
  session's own trust boundary). Requires a Pro/Max/Team/Enterprise plan.

### Token bookkeeping: registry, expiry warning, `tokens` / `forget-token`

Because revocation is effectively out of our hands (verified 2026-06-10: no CLI
command, no documented OAuth revocation endpoint; the only path is manual at
<https://claude.ai/settings/claude-code>, whose token list shows near-zero
per-token metadata, accumulates entries from normal Claude Code usage, and has a
reported multi-day revocation lag — claude-code issues #34198/#48373/#59378/
#43801), yolo does its own bookkeeping:

- **Registry** (`~/.claude-yolo/tokens.json`; `_read_tokens_file` /
  `_write_token_entry` / `_remove_token_entry`): maps the credential-store
  **service name → `{config_dir, minted}`**. Non-secret metadata, host-side only,
  never mounted (same safety property as `projects.json`). Written by
  `_store_oauth_token` (the single funnel both mint paths go through); a re-mint
  replaces the entry and prints the *previous* mint timestamp, since the old token
  stays valid server-side. It exists for what the store can't do: enumerate yolo's
  tokens across config dirs, and map a service name back to its config dir (the
  hash8 is one-way — the mapping is recorded at mint time or lost). The **mint
  timestamp is the practical point**: it's the only handle for identifying a token
  on the claude.ai page — *and*, since the move to keyring, the **sole** source for
  the expiry estimate.
- **Expiry warning** (`_warn_token_expiry`, called from `ensure_oauth_token` on
  the cached-token path; skipped for env-supplied tokens, whose age is
  unknowable): warns at launch when the token is past or within
  `TOKEN_EXPIRY_WARN_DAYS` (7) of `minted + TOKEN_LIFETIME_DAYS` (365 — an
  *assumption*; the token is opaque and states no expiry). The date source is the
  **registry's `minted` stamp** (`_token_minted`, reading `tokens.json`): keyring,
  unlike the macOS keychain, exposes no per-item modification date, so the old
  `_keychain_mdat` path is gone and the registry is authoritative. Missing/
  unparseable entry → `None` → silently no warning (it's advisory).
- **`yolo tokens`** (`do_tokens`, terminal verb, registry-only — needs no config
  dir): table of SERVICE / CONFIG DIR / MINTED / EXPIRES~ / STATUS. STATUS
  reconciles against the credential store via `_keychain_has`: `stale (not in
  store)` for a deleted item, else `ok`. (The pre-keyring `re-minted outside yolo`
  status is gone — it relied on the keychain mdat that keyring doesn't expose.)
  Footer points at the claude.ai page and the match-by-MINTED trick.
- **`yolo forget-token`** (`do_forget_token`, terminal verb): deletes the active
  config dir's credential-store entry (`_keychain_delete`) and registry row, then is
  explicit that the token is only *forgotten*, not revoked — still valid
  server-side, revocable only at the claude.ai page, and probably impossible to
  identify there (reasons above, outside yolo's control). Named `forget-token`
  deliberately: the verb must not claim a power it doesn't have. Honours
  `--config-dir`/config-file `config-dir`, and is dispatched *before* the
  config-dir-must-exist check so a token for an already-deleted config dir can
  still be forgotten (`_oauth_service` only hashes the resolved path).

## Secrets

Arbitrary user secrets (PATs, API keys, SSH keys, …) stored in the **credential
store** (`keyring`, or the chmod-600 file fallback on headless boxes — same
`_cred_*` abstraction as the OAuth token) and injected into a session's container
as **env vars** or **mounted files** — never a plaintext secrets dotfile on the
host, never a value on the docker-run argv. The store buys *encrypted-at-rest
storage (when a real keyring backend is present) + no plaintext dotfile*; it does
**not** buy in-container secrecy — Claude and any code in the
`--dangerously-skip-permissions` container can read whatever is injected. That's
inherent and acceptable *because injection is opt-in per project* (see the gate
below). This first-classes what was already doable by hand with `--mount` a token
file + `--yolorc gh auth login --with-token`.

### Storage scheme — global + project scope

Two storage scopes: **global** and **project** (deliberately *not* config-dir —
that's the Claude-account axis — and *not* worktree — too ephemeral to store a
value in). At injection a referenced name resolves **most-specific-first: project,
then global** (`_resolve_secret_value`). A worktree session shares its main repo's
project scope, since the project key is the main repo root (`_project_key` follows
the shared `.git`).

- **Store service per (scope, name)** (`_secret_service`): global →
  `claude-yolo-secret-{name}`; project → `claude-yolo-secret-{project-hash8}-{name}`
  where `project-hash8` is the first 8 hex of the SHA-256 of the project key — the
  same hashing idiom as the per-config-dir token service. Upserted via `_cred_set`,
  mirroring `_store_oauth_token`. (On the macOS-keychain backend the value passes
  through keyring's Security-framework call, not *yolo's* own argv; on the file
  backend it's written straight to the chmod-600 file.)

- **Registry** `~/.claude-yolo/secrets.json` (`_read_secrets_file` /
  `_write_secret_entry` / `_remove_secret_entry`): keyed by service → `{scope,
  name, project_key, created, modified}`, **never the value**, host-side only,
  never mounted — same safety property as `tokens.json` / `projects.json`. It's
  what enumerates secrets across scopes and maps a hashed service back to its
  project (the hash is one-way). A re-set preserves the original `created` stamp.

This stored-value scope is **independent of** the *injection* scope: which
sessions get a secret is controlled by which config layer (global / project /
worktree) names it in the `secrets` key. The config layer decides *whether* a name
is injected here; the storage scope decides *which value* that name resolves to.

### Verbs (`secret set/list/rm`, mirroring the token verbs)

Dispatched before the config load (needs no yolo config; the project key comes from
git). The subcommand is the TOPIC, the secret NAME a trailing positional
(`do_secret` validates the shape).

- **`yolo secret set NAME [--project] [--clipboard]`** (`do_secret_set`) —
  store upsert + registry entry, **global by default** or **project scope** with
  `--project`. The value is **never a CLI argument** (that would leak it into shell
  history and the process argv visible in `ps`). Three input sources: **stdin** when
  piped (`... | yolo secret set NAME`), an **interactive no-echo prompt**
  (`getpass`) on a TTY, or **`--clipboard`** (`_read_clipboard`: the platform's
  clipboard CLI — `pbpaste` on macOS, `Get-Clipboard` on Windows, `wl-paste`/
  `xclip`/`xsel` on Linux — for the just-copied-from-a-web-page case; the clipboard
  is left as-is). A single trailing newline is stripped (so `echo … |` works).
  **NAME is validated as a shell identifier** (`[A-Za-z_][A-Za-z0-9_]*`) — it
  becomes an env var name in-container. Empty value refused.

- **`yolo secret list [--all]`** (`do_secret_list`) — registry-backed table
  (NAME / SCOPE / CREATED / STATUS reconciled against the store via
  `_keychain_has`, like `yolo tokens`). Shows global + the current project's
  secrets; **`--all`** spans every project (the cross-project counterpart, like
  `list --all`).

- **`yolo secret rm NAME [--project]`** (`do_secret_rm`) — delete the stored value
  (`_keychain_delete`) + registry row at the given scope.

### Config key — `secrets`, a spec list (name → target)

All secrets live in the credential store; the **config decides which a session gets and
how each is injected**. `secrets` (and the repeatable `--secret` CLI flag) is a
**list/concat dest** in `_CONCAT_DESTS`, accumulating across the layers and the
CLI. Each entry is a spec `NAME[:TARGET][!]`, parsed by `_parse_secret_spec` →
`(name, kind, target, ephemeral)`:

- **`NAME`** → env var `NAME`. **`NAME:ENVNAME`** (TARGET is an identifier) → env
  var renamed to `ENVNAME`. **`NAME:/abs`** or **`NAME:~/path`** (TARGET starts
  with `/` or `~`) → file mounted at that **container** path; `~` expands to the
  container home `/home/claude` (substituted explicitly — *not* via
  `os.path.expanduser`, which would wrongly hit the host `$HOME`). A trailing **`!`**
  marks an env target **ephemeral** (file targets reject it — `EBUSY` on a
  single-file mountpoint).

- **Concat/dedup** (`_resolve_secret_specs`): keyed by `(kind, target)`,
  lowest-precedence first, so exact dups collapse and a target collision (two specs
  hitting the same env name or mount path) is won by the higher layer.

- **Validation** at launch only (like mount/port resolution): the secret must exist
  in the store (else a pointed exit pointing at `yolo secret set`); a file target
  under the cwd / `~/.claude` mounts **warns** (`_warn_secret_file_target`) — it'd
  land plaintext in the host-visible tree. `--add-secret` / `--remove-secret` edit
  the stored list element-wise (modeled on prompts: exact-spec match), with the
  usual `--secret`-replaces-the-whole-list conflict guard.

This is the **opt-in gate**: a stored secret is injected only where a
config layer names it (same trust model as `--yolorc`/`--dockerfile` — the *key* is
host-side, so Claude can't grant its next session a new secret). The global
`secrets` list in `~/.yolo.json` is the "inject everywhere" escape hatch.

### Injection at launch — two mechanisms (`_stage_secrets`)

Both read from the credential store and stage **chmod-600 files in the per-session run dir**
(see [run dir](#temp-file-cleanup--the-per-session-run-dir) below); in neither case
does the value touch the docker-run argv (so not `docker inspect`'s `Config.Env`,
host `ps`, or tmux's pane command — the host-side surfaces an `-e` would leak it
into). An env-target value still lands in the consuming process's in-container
`/proc/<pid>/environ` — that's inherent to delivering an env var, and it's inside
the session's own trust boundary.

- **Env-target secrets** — *file transport, env by convention*. Each is written to
  `<run-dir>/secrets/<ENVNAME>` (file name = env var name, **no trailing newline**);
  the dir is bind-mounted **rw** at `/run/secrets`. A **baked loader**
  `/etc/yolo/load-secrets.sh` (in `DEFAULT_DOCKERFILE`, written with `printf` so it
  doesn't need BuildKit) loops the dir and `export`s each file; an ephemeral secret
  has a sibling `<NAME>.ephemeral` marker that makes the loader `rm` it right after
  export (why the mount is rw). **Kept by default** — the GC reclaims the rest; the
  rationale is that blanket self-delete would empty `/run/secrets` for a later `yolo
  shell` exec'd into the same container. The loader is **sourced from two places**
  (claude never runs `.bashrc`): the **claude launch wrapper** (extended from the
  `--yolorc` wrapper — sources the loader, runs any `clones`, sources the rc, then
  `exec "$@"`; triggered by env secrets, the OAuth token, a clone, *or* a yolorc)
  and the baked **`.bashrc`** (for
  `yolo shell`, sentinel-guarded by `YOLO_SECRETS_SOURCED`, before the `YOLO_RC`
  line so an rc can use the values). Nothing to load → no `/run/secrets` mount,
  loader is a no-op. **The Anthropic OAuth token is delivered through this exact
  path** in oauth-token mode (`_stage_secrets`' `extra_env`): staged as
  `<run-dir>/secrets/CLAUDE_CODE_OAUTH_TOKEN`, non-ephemeral (so a `docker exec`'d
  `yolo shell` re-reads it), which is why every oauth-token claude launch is now
  wrapped. See [Auth — oauth-token](#long-lived-oauth-token---auth-oauth-token-the-default).

- **File-target secrets** — each staged value is bind-mounted **read-only** at its
  container path. **Not** placed in `/run/secrets` (the loader ignores them) and
  **not** self-deleted: a single-file bind mount can't be unlinked from inside, so
  they persist for the session like `.credentials.json` and rely on the GC. (Both
  facts confirmed 2026-06-16 via `probes/mount-delete-probe.sh`.)

## Temp-file cleanup & the per-session run dir

A bind-mounted credential/secret file must exist for the **entire container
lifetime**, and yolo `os.execvp`s into docker (`_dispatch_launch`) — its process is
*replaced*, so there's no `finally`/`atexit` to delete anything. Cleanup is
therefore **out-of-band and parallel-safe**:

- **Per-session run dir**, keyed by the (final, suffix-laden) container name:
  `<run-dir>/<container>/` (`_session_run_dir`, mode **700**; files **chmod 600**,
  written via `_write_run_file` with `O_CREAT|0o600` so they're never briefly
  world-readable). Holds the staged secrets (`secrets/` subdir for env targets) plus
  the credential snapshot / throwaway mask.

- **Location: a per-user temp subdir** `claude-yolo-run/` (`_run_dir`). On **Linux**
  it prefers `$XDG_RUNTIME_DIR` when set (a per-user, mode-700 tmpfs); otherwise (and
  always on macOS) it uses `tempfile.gettempdir()` — chosen because the macOS per-user
  temp dir is mode 700, **excluded from Time Machine, and not in synced folders**
  (Dropbox/iCloud), so a session-long plaintext secret isn't copied off the machine.
  The per-container subdir is chmod-700 regardless of the root's mode. (The path
  resolves host-side; the files are bind-mounted to fixed container paths, so the
  opaque host path never matters in-container.)

- **GC at launch** (`_gc_run_dir`, called early in `launch_container`): removes only
  `<run-dir>/<dir>/` whose container is **not in `docker ps`**
  (`_running_container_names`) — crash/leftover sessions. **Never a blanket wipe**,
  which would nuke a *concurrently running* session's still-mounted files. Stays
  crash-proof (a `kill -9`'d session's dir is collected next launch, its container
  gone). Same start-of-launch philosophy as the `.yolo-status/<slug>.state` reset.

- **Retrofit:** `extract_credentials` / `_masking_credfile` now take the run dir and
  write into it (was `NamedTemporaryFile(delete=False)` in `$TMPDIR`, which leaked a
  file per launch forever), so they're collected by the same GC.

Rejected: *parent-waits-and-cleans* (drop `execvp`, `subprocess.run` + `finally`) —
works only for the non-tmux foreground case and forces yolo to own TTY/signal/
exit-code propagation for an interactive `-it` session, exactly what `execvp`
avoids. *tmpfs / FIFO / stdin* — none can carry a host *value* into a bind mount.

## Host-side config: `~/.yolo.json` + `~/.claude-yolo/projects.json` + per-worktree overlay

Config supplies defaults for most flags; `load_yolo_config` applies them via
`PARSER.set_defaults` *before* the re-`parse_args`, so explicit CLI flags still
win. Up to three layers, merged low→high, **all host-side only**:

1. **`~/.yolo.json`** (global) — a flat JSON object of config keys.
2. **`~/.claude-yolo/projects.json`** (per-project) — a JSON object mapping a
   **directory path** to a config object of the same keys. An entry applies when
   the *real* cwd (before any worktree `TOPIC` retargeting) is **at or under**
   the key path; when several keys match, the **longest wins** and only that one
   entry is used — the same nearest-wins rule the old in-directory search had,
   so running from a subdirectory picks up the project's entry. Written by
   `yolo config` (its only writer); a plain launch never touches it.
3. **`~/.claude-yolo/worktrees.json`** (per-worktree overlay) — a JSON object
   mapping a **worktree's absolute path** to a config object of the same keys,
   layered on *only* in worktree mode (`load_yolo_config(..., worktree_dir=…)`)
   as the most specific persisted layer (beats the project entry, still under the
   CLI). It's **auto-managed**, unlike `projects.json`: `yolo start TOPIC [config
   flags]` snapshots the explicitly-passed flags into it (via
   `_overlay_flags` = `_explicit_config_flags` minus `_OVERLAY_SKIP_KEYS`, so a
   resume relaunches with the same config without
   retyping; an empty `{}` is still written, symmetric with the worktree
   lifecycle), `yolo config TOPIC` edits it, and `yolo finish TOPIC` removes the
   entry. **`tmux`/`tmux-session` are *not* auto-snapshotted** (`_OVERLAY_SKIP_KEYS`):
   the `wip` dashboard launches a worktree with `--no-tmux` as a *mechanic* (it execs
   docker into the window it already made), and persisting that as `tmux:false`
   would then suppress tmux for a later `yolo shell <topic>`/`resume <topic>`. An
   explicit `yolo config TOPIC --no-tmux` still pins it (the `config` path doesn't
   go through `_overlay_flags`). **`yolo resume TOPIC [config flags]` also updates
   the overlay** — because resume restarts the container, flags passed to it both
   apply now and
   persist (`_merge_worktree_overlay`, same concat/override rule as the loader:
   `mounts`/`ports`/`prompts` accumulate onto the stored list with exact-dup
   specs dropped, scalars override; the merged result equals what the run already
   resolved, so persisted == live). `shell` is **excluded** from this — shelling
   into a *running* container can't change its mounts, so persisting there would
   mislead. `start` *creates* the overlay but deliberately does **not** consume
   one during the same run (so a stale same-path entry from a manual removal
   can't leak in); `resume`/`shell` consume it. Helpers mirror the projects ones
   (`_read_worktrees_file`/`_write_worktrees_file`/`_worktree_overlay_key`).

**An in-directory `.yolo.json` is deliberately no longer read.** It lives inside
the bind-mounted tree, so Claude in a container could edit it and grant its next
session new host access — extra `mounts`, or an arbitrary *read-write* host mount
via `config-dir` — and a `.yolo.json` committed in a cloned repo would apply
someone else's config the first time you ran yolo there. Host-side-only config
makes the safety property structural: nothing yolo reads is writable from inside
a container. (`~/.claude` *is* mounted rw, which is why the project store lives
under `~/.claude-yolo` — only `worktrees/<slug>/<topic>` dirs under it are ever
mounted, never `projects.json` **or `worktrees.json`**, which sits as a *sibling*
of the `worktrees/` dir, not inside any worktree. This matters because an overlay,
like a project entry, can grant host access — `mounts`, or an arbitrary rw mount
via `config-dir` — so it must stay container-unwritable.) A leftover `.yolo.json` found at/above the cwd
draws a **warning on every run** (never an error — the file is inert) naming the
migration path; `~/.yolo.json` itself is exempt from the walk.

Precedence overall: `~/.yolo.json` < `projects.json` entry < worktree overlay <
CLI flags. Per key the higher layer **overrides**, except `prompts`, `mounts`,
`ports`, and `secrets` (`_CONCAT_DESTS`), which **concatenate** across the layers
and then the CLI values (those lists accumulate; everything else replaces).

Keys mirror the flag names (dashes or underscores both accepted). Supported:
`config-dir`, `dockerfile`, `yolorc`, `auth` (one of `keychain`/`oauth-token`/`bedrock` —
validated against `AUTH_CHOICES` in `_parse_yolo_dict`, since `set_defaults`
bypasses argparse's `choices` check), `aws-profile`, `aws-region`,
`bedrock-model`, `claude-json`,
`ssh-agent`, `submodules`, `redirect-build-dirs`, `base`, `finish-action` (one of
`delete-if-merged`/`merge`/`push`/`keep` — validated against `FINISH_CHOICES` in
`_parse_yolo_dict`, same as `auth`), `finish-remote`,
`prompts` (string or list of strings; the pre-0.7 name
`append-system-prompt` draws a pointed rename error),
`mounts` (string or list, `PATH[:ro|:rw]`), `ports` (string or list,
`[HOST:]CONTAINER`), `secrets` (string or list, `NAME[:TARGET][!]`),
`plugin-dirs` (string or list of plugin dir/`.zip` paths),
`clones` (a `{url, dir[, depth]}` object or list of them),
`require-project-entry`, `tmux`, `tmux-session`.
Per-invocation **actions** — `--resume` and the verbs (with their `TOPIC`) — are
deliberately **not** config keys, and neither is `--dangerously-allow-home`
(CLI-only by design); any of them in a config file is a hard error (not in
`YOLO_KEYS`). `config-dir`, `dockerfile`, and `yolorc` get `~` expanded (a JSON file can't
lean on shell expansion). Booleans must be JSON `true`/`false`. A JSON **`null`** for any key
means "leave at the built-in default" (the loader skips it). Unknown keys, wrong
types, and malformed JSON all `sys.exit` naming the offending file/entry
(`_parse_yolo_dict` / `_read_projects_file` / `_read_worktrees_file`).

Every load also prints a one-line **provenance note** to stderr (suppressed by
`quiet=True`, which the long-lived `wip` dashboard passes when it re-reads config
each tick so the note/warnings don't scribble its frame) — e.g.
`config: ~/.yolo.json + projects.json[/Users/peter/hacks/foo]`, with a
`+ worktrees.json[<topic>]` tail when a (non-empty) worktree overlay applies, or
`config: built-in defaults (no project entry)` — and warns about **dangling
project keys** (entries whose directory no longer exists: the signature of a
moved/renamed project, whose config would otherwise *silently* fall back to the
global defaults — wrong account/profile being the real hazard). When the cwd
also has no matching entry — the rename case produces both at once — the warning
suggests re-running `yolo config` and removing the stale entry. This detection
only works because entries are never auto-created: a plain run in a fresh
directory configures nothing and writes nothing. The hard-mode version is the
`require-project-entry` guardrail (see above). Note `projects.json` keys are
**paths-as-identity**: renames must be hand-migrated (matching how Claude's
`~/.claude/projects/` buckets and the worktree slugs behave).

### `config` verb

`yolo.py config [TOPIC] [CONFIG FLAGS]` (`do_config`) shows or updates this
project's `projects.json` entry — or, with **`--global`**, `~/.yolo.json`
itself, or, with a **`TOPIC`**, that worktree's `worktrees.json` overlay
(`_do_config_worktree`) — then exits; it does **not** run a container. The
project entry key is the **main repo root** when inside a git repo (so
subdirectory runs and worktree sessions share it; `_project_key`), else the cwd.
Behavior à la `git config`:

- **With a `TOPIC`** — `yolo config fix-auth --add-mount ~/refdocs` — targets
  `worktrees.json[<worktree path>]` instead of the project entry, reusing the
  same `_apply_config_edits` machinery (whole-key sets, `--add-*`/`--remove-*`,
  `--unset`). Bare `yolo config TOPIC` shows the overlay (or "no overlay for
  TOPIC"); editing requires the worktree to **exist** (configuring a
  non-existent one is meaningless). `--global` and `--init` are
  project/global notions and **error** when combined with a `TOPIC`. Running
  `yolo config` from *inside* a worktree dir can't substitute: `_project_key`
  follows the shared `.git` back to the main repo root, so it would hit the
  project entry — the explicit `TOPIC` is the only handle on the overlay.

- **With config flags** — `yolo config --auth bedrock --mount ~/refdocs` —
  persists **exactly the explicitly-passed `YOLO_KEYS` flags** into the entry,
  per-key (other keys in the entry are left alone; re-running with one flag
  updates just that key). "Explicitly passed" is detected by a **sentinel
  re-parse** (`_explicit_config_flags`): a plain parse can't distinguish
  "defaulted" from "explicitly set to the default", and `config --auth oauth-token`
  must persist. List-kind dests use a fresh marker list, since argparse's append
  action copies the default before appending (identity survives exactly when the
  flag never appeared). `--mount` values are validated (exist + is-dir) *before*
  persisting, so a typo can't be pinned; the final entry is also re-validated so
  an unloadable entry is never written.
- **Bare `yolo config`** is **read-only**: prints the **complete effective
  config that would apply here** — the global `~/.yolo.json` values that aren't
  overridden, merged with this project's entry — not just the project entry,
  with **per-key provenance** (`_effective_config`). Each line is `key value
  [source]`, where `source` is `~/.yolo.json` or `projects.json` (or
  `~/.yolo.json + projects.json` for a concat key — `mounts`/`ports`/`prompts`/
  `secrets` — that both layers contribute to). Values are shown **raw** (paths
  un-expanded, so you see what's written), and an explicit `null` is skipped
  (it means "leave at the built-in default"). With nothing configured it prints
  `built-in defaults` (plus `(no project entry)`); it also prints the
  projects.json path and flags dangling keys. The merge mirrors
  `load_yolo_config`'s precedence but **doesn't** validate via `_parse_yolo_dict`
  (so an entry with a bad key still displays — fix it here with `--unset`), add
  the worktree overlay (that's `yolo config TOPIC`), or include built-in
  defaults. There is no scaffold/template behavior (and no `YOLO_INIT_DEFAULTS`
  anymore — built-in defaults live only in argparse).
- **Editing flags beyond whole-key sets** (all `config`-only, repeatable;
  applied by `_apply_config_edits`, the helper shared by the project and
  `--global` paths): **`--unset KEY`** deletes a key entirely (any *present*
  key may be unset, even one not in `YOLO_KEYS` — the repair path for an entry
  that breaks loading; an absent key errors). **`--add-mount PATH[:ro|:rw]` /
  `--remove-mount PATH`** edit single elements of `mounts` (vs `--mount`, which
  replaces the list): add validates via `_parse_mount_spec` and replaces a
  same-path element (so the mode can be flipped); remove matches by path
  (`_spec_path`: mode suffix stripped, `~` expanded, resolved) and deliberately
  *doesn't* require the dir to exist, so a stale mount is removable; an
  emptied list drops the key (for a concat key, `[]` ≡ absent).
  **`--add-prompt` / `--remove-prompt`** do the same for `prompts`
  (exact-string match; duplicate add is a no-op, absent remove errors).
  **`--add-port [HOST:]CONTAINER` / `--remove-port CONTAINER`** likewise for
  `ports`: add validates via `_parse_port_spec` and replaces a
  same-container-port element (so a `HOST:` pin can be added/dropped); remove
  matches by container port (`_port_container`: `HOST:` stripped, deliberately
  unvalidated so a malformed spec is removable).
  **`--add-secret NAME[:TARGET]` / `--remove-secret NAME[:TARGET]`** do the same for
  `secrets`, but modeled on `prompts` (the spec is an opaque string): add validates
  via `_parse_secret_spec` and dedups by **exact spec** (a name needed both env and
  file is two distinct specs); remove matches the exact spec.
  **`--add-plugin-dir PATH` / `--remove-plugin-dir PATH`** likewise for
  `plugin-dirs`, matched by **resolved path** (`_plugin_dir_key`, like mounts) so
  `~/x` and its absolute form are one entry: add validates via
  `_parse_plugin_dir_spec` (the path must exist) and is a no-op if already listed;
  remove needn't have an existing path, so a stale one is removable.
  **`--add-clone URL DIR [DEPTH]` / `--remove-clone DIR`** edit the dict-valued
  `clones` key (not a spec string — so its own `_take_clones_key` + an
  `_AddCloneAction` taking 2–3 tokens): add appends a `{url, dir[, depth]}` and
  **replaces a same-`DIR` entry** (so the url/depth can change), the optional 3rd
  `DEPTH` being a positive-int shallow clone; remove matches the stored `dir`.
  Contradictory instructions in one call (set + `--unset` of the same key,
  `--mount` with `--add/--remove-mount`, `-p` with `--add/--remove-prompt`,
  `--port` with `--add/--remove-port`, `--secret` with `--add/--remove-secret`,
  `--plugin-dir` with `--add/--remove-plugin-dir`, `--clone` with
  `--add/--remove-clone`)
  are errors, not silently ordered; sets apply first, then unsets, then list
  edits.
- **`yolo config --global`** targets the flat `~/.yolo.json` instead of the
  project entry, for both shows and writes (read raw + read-modify-write, so
  unknown keys can be `--unset` even though `_parse_yolo_file` would reject the
  file; a malformed file is a pointed error, never clobbered). Can't combine
  with `--init`.
- **`yolo config --init`** registers the project with an **empty entry** — no
  overrides, just enough to satisfy `require-project-entry` without pinning a
  config value the user never chose (bare `config` stays read-only, so an
  explicit flag is the only way to create one). Errors if the key already has
  an entry; can't combine with config flags, the editing flags, or `--global`;
  warns when the new (most-specific, empty) entry shadows an ancestor entry's
  config for this project. `--init` — like `--global`, `--unset`, and the
  `--add/--remove-*` flags — is a verb-only flag, validated like
  `--force`/`--new` in dispatch.

`config` is dispatched off the *first* `parse_args`, **before** the config files
are layered in — a broken config can't block fixing the config — and it reads
only `projects.json` (or, under `--global`, `~/.yolo.json`) itself, failing
with a pointed message on a malformed file (never clobbering it).

AWS sub-keys without `auth: bedrock` just **warn** (and are ignored) rather than
erroring, since the auth mode may legitimately be set to bedrock in a config file
and overridden back to `keychain`/`oauth-token` on the CLI over a file that also
set the AWS knobs.

## Workflow verbs

The opinionated front door. `start`/`resume`/`shell` take an **optional** `TOPIC`:
**with** a `TOPIC` they act on a git worktree of that name (the worktree workflow —
most work is meant to land on a branch that can be merged or PR'd); **without** one
they act on the **current directory** (no worktree), so the same verbs work whether
or not you want a branch. `finish` only makes sense against a worktree, so it still
**requires** a `TOPIC`. A bare `yolo` (no verb) is equivalent to `yolo start` (a
fresh session in the cwd). All run from inside a git repo (the cwd-mode verbs degrade
gracefully outside one — there's just no repo slug to label/find by).

- **`start [TOPIC]`** — *with `TOPIC`:* create a new worktree + branch `TOPIC` off
  `--base` (default `HEAD`; see `base` below) and launch a container with a fresh
  session named `<repo>:<TOPIC>` (the repo prefix distinguishes it from the same
  topic in another project, and from a cwd session named just after its directory);
  **errors if the worktree or branch already exists** (use
  `resume`). Any **explicit config flags** passed here are snapshotted into the
  worktree's `worktrees.json` overlay (see the config section), so a later
  `resume TOPIC` reuses them. *No `TOPIC`:* a fresh session in the current
  directory, named after the directory basename (`= --hostname`).
- **`resume [TOPIC]`** — continue the most recent session (`claude --continue`).
  *With `TOPIC`:* on that existing worktree (**errors if it doesn't exist** — use
  `start`), layering in that worktree's overlay config; any **explicit config
  flags** passed here update that overlay (add mounts/ports, change auth, …) and
  persist for next time, since the container restarts anyway. `--new` starts a
  fresh named session there instead. *No `TOPIC`:* in the current directory.
  `-r [ID]` (either mode) resumes a specific session / opens the picker. `--new`
  is worktree-only (for the cwd, a fresh session *is* `start`).
- **`shell [TOPIC]`** — a bash shell. If a container is **running** (label match —
  by worktree for `TOPIC`, by cwd otherwise) → `docker exec -it <id> /bin/bash`;
  otherwise a fresh ephemeral container with `--entrypoint /bin/bash`. Either way
  the prompt is yolo-flagged: every launch exports `YOLO_PS1` (`_ps1_env_args`),
  which the image's `.bashrc` adopts, giving `yolo:<dir>$`. In worktree mode PS1
  rewrites the long worktree prefix of `$PWD` at prompt time (via the
  `YOLO_WT_DIR`/`YOLO_WT_LABEL` env vars in a `${PWD/#…/…}` expansion) to a short
  label (`_worktree_ps1_label`): the `~/.claude-yolo/worktrees/` root and the
  prefix shared by *all* repo slugs under it are dropped, e.g.
  `claude-yolo/fix-auth`; with a single slug the label is just the topic. The
  exec'd case works because `docker exec` inherits the container's run-time env —
  so the env vars are stamped on *every* launch, not just `shell` ones.
- **`stop [TOPIC]`** (`do_stop`) — stop the running container for a worktree
  `TOPIC`, or the current directory. A terminal verb (no config, no launch),
  dispatched early like `dir`/`secret`. Finds the container by the same
  `yolo.worktree`/`yolo.cwd` labels `shell` uses (robust to the suffix-laden
  name), then `docker stop`s it. Containers run `--rm`, so the stop also *removes*
  the container — but the session transcript persists on the host, so `yolo
  resume` still works afterward. Nothing running is a **friendly no-op**, not an
  error (idempotent in spirit, safe to script), and a `TOPIC` doesn't require the
  worktree dir to exist (the match is by label, so an odd-state container is still
  stoppable). A session that's actively **`working`** is refused unless `--force`,
  so a stray `stop` can't cut off a running task — activity read from the same
  session-state file `ps`/`rebase` use, located via the container's *own*
  `yolo.config-dir`/`yolo.cwd` labels (so it's independent of this invocation's
  config). Unlike `rebase`, only `working` is guarded — `waiting`, a `yolo shell`,
  and a not-yet-started session (unknown `-`) all stop freely, since the point is
  just not to interrupt active work. The counterpart to `finish`, which refuses
  while a container runs: `stop` is how you clear that.
- **`finish TOPIC`** — `git worktree remove` the worktree, then handle the branch
  per **`--finish-action`** (config key `finish-action`, default `delete-if-merged`;
  `FINISH_CHOICES`). The four actions (dispatched at the tail of `do_finish`):
  - **`delete-if-merged`** (default, the prior behavior) — **delete the branch iff
    it's merged**: if reachable from `base` (the same `--base`/`base` ref as
    `start`/`list`, default `HEAD`; via `_branch_merged`) it's deleted (`git
    branch -d`) since nothing remains to preserve, otherwise it's **kept** with a
    message that it still needs to be merged or pushed (plus the pushed/unpushed
    note from `_branch_status_note`).
  - **`merge`** (`_finish_merge`) — `git merge TOPIC` into the **current
    checkout** (HEAD of the main repo, where `finish` runs — *not* `base`, which
    may be a remote ref you can't merge into), then `git branch -d` it. A merge
    failure (conflicts, dirty tree, unrelated histories) is **aborted** (`git
    merge --abort`) and the branch is **kept** — the worktree is already gone but
    the commits live on in the branch.
  - **`push`** (`_finish_push`) — `git push -u <remote> TOPIC` to the
    **`--finish-remote`** (config key `finish-remote`, default `origin`) and keep
    the branch **locally**. The `-u` sets up tracking (`<remote>/TOPIC`) since
    this action is for the open-a-PR flow, where a later bare `git push`/`git
    pull` on the branch should just work. A push failure keeps the branch
    locally too.
  - **`keep`** — leave the branch entirely alone (just clean up the worktree),
    with the `_branch_status_note` appended.

  All actions still refuse if a container is running, or on uncommitted changes
  (unless `--force`), and all remove the worktree's `worktrees.json` overlay
  entry. Leaves transcripts (they self-expire via `cleanupPeriodDays`). The
  removal goes through **`_remove_worktree`**, which handles git's refusal to
  `worktree remove` a tree containing **populated submodules** ("working trees
  containing submodules cannot be moved or removed" — an unconditional check that
  `--force` doesn't bypass): on *that* stderr it falls back to the documented
  manual workaround (`shutil.rmtree` the dir, then `git worktree prune` the stale
  admin entry), the dirty guard having already run. Any *other* git failure
  (e.g. a locked worktree) is surfaced verbatim — the rm fallback is scoped to
  the submodule case only.
- **`rebase TOPIC`** (`do_rebase`) — rebase a worktree's branch onto `base` (the
  same `--base`/`base` ref as `start`/`list`/`finish`, default `HEAD`). Resolves
  `base` to a concrete commit **in the main checkout** first (`git rev-parse` from
  the cwd, so a `HEAD` base means the main repo's tip, not the worktree's own
  branch — the same reason `_branch_merged` runs from the main repo), then runs
  `git -C <worktree> rebase <commit>`, streaming git's output. So commits landed
  on the base since the worktree branched are replayed under its work, exactly
  like `git rebase main` from the branch. Requires a `TOPIC`. A rebase that hits
  conflicts exits nonzero with a pointer to resolve (`git rebase --continue`) or
  abort (`git rebase --abort`) **in the worktree dir**, leaving it in-progress
  there. A terminal verb — no container; the `worktrees.json` overlay is
  untouched (the worktree lives on). **Running-container handling is
  session-aware, and lives in the `rebase_worktree` core** (not the `do_rebase`
  wrapper), so both the CLI and the dashboard's `r` enforce it identically — the
  same shape as `finish_worktree`. rebase only rewrites commits in a worktree that
  stays put — so only an *active* session is a real hazard, not a live container
  per se. A running container is checked against the **session-activity state file
  the hooks write** (`<config-dir>/.yolo-status/<cwd-slug>.state`), read via the
  **shared `_container_session_state` helper** that `stop`/`finish` also use: it
  resolves the state file through the container's *own* `yolo.config-dir`/`yolo.cwd`
  labels (not this invocation's `--config-dir`), so a session started under a
  different config dir is still read correctly. A `waiting`
  session (idle at a prompt) is rebased **through**; a `working` one — or an
  unknown state (`-`: a `yolo shell`, which runs no hooks, or a session that hasn't
  taken a turn yet) — is **refused unless `force`**. The one residual race (the
  user prompting the session in the gap between the check and the rebase) needs
  them driving the same session from two places at once, so it's a non-issue in
  practice. The **dirty-tree refusal is independent and absolute** — no `force`
  bypass, since `git rebase` needs a clean tree regardless (this is why `force`
  here gates *only* the running-container check, not the dirty check as it does in
  `finish`).
- **`diff TOPIC`** (`do_diff`) — `git -C <worktree> diff <base>...HEAD`, a
  **three-dot** diff against `base` (resolved to a commit in the main checkout
  first, like `rebase`/`list`, so a `HEAD` base is main's tip not the worktree's
  branch). Shows what the branch *adds* since it diverged — the PR-style review
  diff, matching the `↑ahead` of `list`'s COMMITS. Requires a `TOPIC`. Stdio is
  inherited so git pages it; no mutation and no session-state concerns, so unlike
  `rebase`/`finish` there's **no guard and no in-process core**. A terminal verb,
  dispatched after the config re-parse (it needs the config-resolved `base`),
  beside `rebase`. With **`--stat`** it instead opens the **interactive diff-stat
  picker** (`_diff_stat_picker`): `git diff --stat` (sized to the terminal),
  navigable, where Enter/Space on a file opens *that file's* `git diff` in a new
  tmux window — the file list is `--name-only` (exact paths) zipped by order with
  the `--stat` display lines (same diff order, so a truncated stat path still maps
  to the right file), the summary line non-selectable. The loop (`_diff_stat_loop`,
  under `_run_picker`, drawn by `_draw_diff_stat`) spawns each per-file window via
  `_spawn_window` (the generic tmux-window helper `_spawn_session_window` now wraps)
  with **`env={"LESS": "R"}`** — that stops git's pager auto-quitting a one-screen
  diff (git's default `LESS=FRX`; the `F` quits if it fits one screen), so the
  window stays until `q` for short *and* long diffs alike, with no extra Enter (the
  bug a small file like `_quarto.yml` exposed). `_tmux_window_command` prepends the
  env assignment to the git command only and still holds a *failed* window open.
  Needs a tty + tmux; without them `--stat` just prints the stat and returns. This
  is what the dashboard's `d` spawns (paged/interactive output can't live in the
  footer).
- **`list`** — the repo's worktrees as a table (TOPIC/STATUS/COMMITS/
  DIRECTORY). The
  TOPIC cell is just the topic, since yolo names the worktree's branch the same;
  it's only shown as `topic (branch: X)` when the worktree has a *different*
  branch checked out (someone switched it inside the container) — so there's no
  standing BRANCH column for what's almost always redundant.
  STATUS is `running`/`dirty`, else `merged`/`unmerged` (idle+clean) judged by
  whether the branch is reachable from **`base`** — exactly `git branch --merged
  <base>` (default `base` is `HEAD` = the main checkout; honours
  the `base` config key/`--base`) — or **`orphaned`** when git can't resolve the
  worktree's main repo at all (it was moved/deleted, so `git -C <wt> rev-parse`
  fails; `_worktree_rows` detects the nonzero rc and skips the merged/COMMITS
  computation, COMMITS shows `-`, and the REPO name is still recovered from the
  `.git` pointer). `do_list` prints a one-line footer when any are orphaned,
  pointing at `git worktree repair`; in `wip` the `orphaned` status is red (beats
  `running` in `_color_status`). **COMMITS** is the branch's `↓behind ↑ahead`
  counts vs `base` (GitHub's order — behind first), from `_branch_ahead_behind`'s
  `git rev-list --left-right --count base...branch`, carried on the `WorktreeRow`
  and shown by both `list` and the `wip` dashboard. So a fast-forward-merged or never-diverged branch reads
  `merged`; a *squash*-merge isn't reachable and reads `unmerged`. `do_list` runs
  the check in the main repo (not `git -C <worktree>`) so a `HEAD` base resolves
  to the main checkout, not the worktree's own branch. **`--all`** (verb-only,
  `all_repos`) instead lists every worktree under `~/.claude-yolo/worktrees`
  across all repos, with a leading **REPO** column — the cross-repo counterpart
  to a plain `list`, like `ps` is for running containers. Under `--all` the
  `merged` check is run in each worktree's *own* main repo (resolved via
  `_worktree_main_repo`: the shared `.git`'s parent), since the branch and a
  `HEAD` base only resolve there, not in the dir `list` was invoked from. The REPO
  cell is that repo's basename; when the main repo has been moved/deleted (git
  can't resolve it), the name is recovered from the worktree's `.git` pointer
  (`_worktree_repo_name`) so an orphaned worktree shows the repo name, not the slug.
- **`ps`** — every **running** yolo container, across **all** repos (the
  cross-repo counterpart to `list`), as a table
  (NAME/TOPIC/PORTS/CREATED/STATE) read from the `yolo.*` labels
  (`docker ps --filter label=yolo.cwd`); needs no git repo. CREATED is docker's
  own `{{.RunningFor}}` (how long ago the container was created). PORTS comes straight
  from docker ps's own column (free — no per-container `docker port` calls at the
  2s cadence), condensed by `_condense_ports` to `host->container` pairs
  (address/proto noise and the IPv6 twin dropped). STATE is read from each
  session's `<config-dir>/.yolo-status/<cwd-slug>.state` file
  (`_read_session_state`): `working <age>` (since the last `UserPromptSubmit` or
  `AskUserQuestion` answer), `waiting <age>` (since the `Stop` hook fired or an
  `AskUserQuestion` began blocking), or `-` (no file / older
  container). Both render via `_humanize_secs`. The config dir comes from the
  `yolo.config-dir` label (falls back to `~/.claude`); no extra docker calls.
  `--watch` redraws every `PS_WATCH_INTERVAL` (2s). It's an ordinary verb usable
  anywhere; run interactively *inside tmux* (stdin a TTY + `$TMUX` set),
  `--watch` is a **picker**: j/k/arrows move, Enter `select-window`s to the
  chosen container's window, q/ESC quits; otherwise it falls back to the
  passive redraw loop. (The tmux dashboard window is now `wip`, not `ps --watch`
  — see below.)
- **`wip`** — a tmux-resident **dashboard** for managing everything yolo, a
  superset of the `ps --watch` picker and the window-0 dashboard `--tmux`
  sessions now seed (`_ensure_tmux_session` runs `yolo wip --_dashboard`, the
  hidden flag that means "run the loop", vs a user-typed `yolo wip` that
  bootstraps the session + focuses the dashboard window via `_focus_tmux_window`,
  the shared focus helper extracted from `_launch_in_tmux`). Requires tmux.
  Sections (`_wip_items`): **running sessions** (`_wip_sessions` — one
  `docker ps` carrying the cid + labels, ordered by `_order_sessions`:
  unknown→waiting→working, each by *least-recent activity first* — unknown
  oldest-created first (its sortable `CreatedAt`, carried as the WipSession
  `created_at` field), waiting longest-idle first, working longest-working first,
  so reading down runs from least to most recently active) — which `_draw_wip`
  renders as **one SESSIONS table** (no blank lines between the groups; the
  SESSION/STATE color — grey unknown / green waiting / yellow working — is the
  group cue instead); then
  **worktrees** (the `_worktree_rows` extracted from `do_list` — *every* worktree,
  including ones with a running session, which also appear as a session row;
  `running_paths`, from the same single `docker ps`, both marks each `running` in
  STATUS and spares `_worktree_rows` its own per-worktree `docker ps` at the 2s
  cadence; this table carries the same **COMMITS**
  column `list` grows — `_branch_ahead_behind`'s `↓behind ↑ahead` counts vs
  `base`), and **projects** (`_wip_projects`: the
  `projects.json` keys **plus** the recent-projects registry — every launch stamps
  the project it opened (`_record_recent_project`, keyed by `_project_key`) into
  `~/.claude-yolo/recent-projects.json`, so a project shows up here even with no
  config entry; recent-only keys are dropped when their dir no longer exists,
  registered keys always shown — `registered` is still carried (the `a` key
  registers a recent one) but no longer surfaced as a marker). Rendered as a
  **REPO / DIRECTORY** table (the repo basename + the `~`-relative path), every
  row colored the same — the registered-vs-recent and active distinctions are
  not shown — the WORKTREES shape minus the extra columns. This registry is kept **separate from `projects.json`
  on purpose**: `projects.json` stays a deliberate, config-only ledger (`yolo
  config` is its only writer), so the dangling-key warning and `require-project-entry`
  keep the meaning that auto-stamping it would dilute. The loop (`_wip_loop`, under
  the cbreak `_run_picker` extracted from `_ps_picker`, selection tracked by a
  stable key like the ps picker) dispatches keys (`_wip_action`): `Enter`
  switches to a session's window / opens a worktree / **opens a project's
  session**. For both a worktree and a project, Enter first jumps to a live
  session window when one exists (`_session_window_for` resolves the running
  session at that path to its tmux window — the same `_focus_tmux_window` a session
  row uses), else it launches: a worktree `resume`s, a project `resume`s with a
  fresh-session fallback when the dir has none (see `_has_resumable_session`), so
  Enter "just opens" either one either way. The PROJECTS section also ends with a
  synthetic **`+` row** (kind `newsession`); Enter on it prompts (via
  `_PickerTerm.prompt_path` — a hand-rolled cbreak-mode line reader with `~`-aware
  directory Tab-completion through `_complete_path`, deliberately *not* readline,
  whose Tab never engages `input()` under libedit, the macOS Python uv ships) for a
  directory and `start`s a fresh session there, for a dir that isn't a listed
  project yet. **`N`** on a worktree or project starts a *fresh* session
  (`_wip_new_session`: `start` for a project, `resume TOPIC --new` for a worktree)
  rather than Enter's resume-the-latest, and **`R`** opens claude's session picker
  (`_wip_resume_pick`: `resume -r` / `resume TOPIC -r`) in a new window to resume a
  non-most-recent session — both refuse on a row with a live `window` (one session
  per dir/worktree; Enter switches instead) and otherwise shell out via
  `_wip_spawn_target`'s repo/name (same as Enter). `n` on a project prompts for a
  topic and starts a **new worktree** session
  there (`_wip_new_worktree`; topic validation is left to the spawned `yolo start
  <topic>`, surfacing in the new window like Enter's launch errors), `S` on a
  session row opens a bash shell in its container (`_wip_shell`: `docker exec -it
  <cid> /bin/bash` in a new `<name>-shell` window, like `yolo shell`), `b` browses a
  forwarded port (prompting if >1), `s`
  stops, `f` finishes, `r` rebases, `d` on a worktree row (or any worktree-backed
  session row, even a `working` one — diff is read-only) spawns `yolo diff
  <topic> --base <base> --stat` in a new window (`_wip_diff`; base from
  `_worktree_config`) — the interactive diff-stat picker, where Enter/Space on a
  file opens its diff in yet another window, `c` on a worktree or project row
  opens an **interactive config editor** (`_config_editor_loop`) for that worktree's
  overlay / project entry — a modal sub-screen (the `_diff_stat_loop` pattern) that
  shows the layer's current keys/values **plus the inherited lower layers**
  (read-only, dimmed, source-labeled — `_effective_config` minus the editable
  keys), where `Enter` edits the selected key (bools/choices via the j/k
  `_pick_one` picker, paths via the Tab-completing `prompt_path`, list keys drill
  into an add/remove element view `_config_list_loop` — mounts/plugin-dirs
  Tab-complete the dir; the dict-valued `clones` key gets its own
  `_config_clones_loop` that prompts url + dir + optional depth), `a` picks a
  not-yet-set key to add, `x` unsets one, and
  `e` is a raw-flags escape hatch. Every write composes a `yolo config [TOPIC]
  <flags>` subprocess (`_config_apply`, from the repo/project dir, reusing all of
  `yolo config`'s validation/persistence; bool keys emit `--key`/`--no-key`, list
  keys `--add-<stem>`/`--remove-<stem>`), so plain Enter then launches with the
  saved config (`_wip_config` is the launcher; reads are in-process via
  `_ConfigScope`). `a`
  registers a project (on a selected
  *recent-only* project it registers **that** one straight into `projects.json`;
  otherwise it prompts for a path via the same Tab-completing `prompt_path` the `+`
  row uses), `q` quits. `f`/`r`
  apply to any worktree row and to *waiting* session rows (a `working` session is
  never offered them); the running-session guard lives in the
  `finish_worktree`/`rebase_worktree` cores, so a worktree row with a `working`
  session refuses in the footer while an idle one is finished/rebased through —
  the CLI and dashboard share that one policy. **Quick ops run in-process** via the cores below and surface their
  result/`YoloError` in the footer; **launches shell out** into a fresh tmux
  window (`_spawn_session_window`: `new-window -c <repo>` running a fresh
  `yolo start/resume … --no-tmux`, so the inner yolo re-resolves that repo's
  config and execs docker straight into the window). The dashboard is long-lived
  *and* spans repos, so it does **not** carry a single `base`/`finish-action`/
  `finish-remote`: **each worktree resolves its own** from config at display and
  action time via `_worktree_config(home, main_root, worktree)` → `load_yolo_config(
  main_root, …, worktree_dir=worktree, quiet=True)` — that worktree's repo entry +
  its overlay + global `~/.yolo.json`, exactly what `yolo rebase TOPIC` from inside
  that repo would use. So `f`/`r` rebase/finish onto the *right* base per repo, the
  COMMITS/STATUS columns are judged per worktree (`_worktree_rows`' `base_resolver`
  hook; `do_list`/an explicit `--base` still pass a single `base`), and a `yolo
  config` edit reaches the *running* dashboard with no restart. (`quiet=True` keeps
  the provenance line and dangling-key warnings off the frame.) The `--_dashboard` loop only
  activates with a TTY + `$TMUX`; otherwise it degrades to `_ps_watch_passive`.
  **Coloring** is full "angry fruit salad", applied only in the draw layer (so the
  data layer / `yolo list` / `ps` stay escape-free): `_draw_table` runs each row
  through a per-section `_color_*_row` that SGR-wraps every cell (`_fg`) — session
  SESSION/STATE by status group, worktree STATUS by `dirty`/`running`/`unmerged`,
  COMMITS with nonzero behind red / ahead green / zeros grey, projects with
  REPO blue / DIRECTORY grey (uniform — like WORKTREES). `_format_table` measures **visible** width (`_visible_len` strips
  the SGR via `_SGR_RE`) so colored cells still align; the selected row is rendered
  as a plain reverse-video bar (ANSI stripped, then `\x1b[7m`) rather than tinted,
  which sidesteps the per-cell-color-vs-highlight clash and grey-on-grey.
- **`browse [TOPIC]`** — open the host browser at a running session's forwarded
  port (`do_browse`): find the container by the same label query `shell` uses
  (worktree label with a `TOPIC`, cwd label without), read its `yolo.ports`
  label for what was forwarded *at launch* (first = default; `--port N` selects
  another — read from the **first** parse's CLI-only values, so a config
  `ports` list can't masquerade as a selection), resolve the assigned host port
  via `docker port` (`_docker_port`), print `http://127.0.0.1:PORT/` (always —
  copy-pasteable), and `open` it (`_open_url`, the test seam; `--print`/`-n`
  skips it). No listening-poll on purpose: browse may legitimately run before
  the server starts. Pointed errors for no running container and for a
  container launched without ports (mappings can't be added live — exit and
  `resume`).
- **`dir [TOPIC]`** — print a session's working directory and exit (`do_dir`),
  for `cd "$(yolo dir TOPIC)"`. *With `TOPIC`:* the worktree's root dir
  (`_worktree_dir`), erroring if that worktree doesn't exist so the `cd` fails
  loudly rather than landing somewhere wrong. *No `TOPIC`:* the current
  directory. Only the path is written to **stdout** (errors go to stderr); it's
  dispatched *before* `load_yolo_config` specifically so the config provenance
  note doesn't pollute the command-substitution output. A terminal verb — no
  container.

Implementation shape:

- **Operational cores raise `YoloError`, not `sys.exit`** — so the `wip`
  dashboard can call them in-process without the process dying under it.
  `main()` is a thin wrapper that runs `_main()` and translates a `YoloError`
  back to `sys.exit(str(e))`, so the CLI behaves exactly as before. The
  cwd-coupled verbs were split into a context-explicit **core** + a thin
  cwd-resolving **wrapper**: `stop_session` (from `_stop_container`),
  `finish_worktree` (from `do_finish`; all git runs against an explicit
  `main_root` via `-C`, and the finish helpers `_finish_merge`/`_finish_push`/
  `_branch_status_note`/`_current_branch` likewise take a `repo`), `rebase_worktree`
  (from `do_rebase`; like `finish_worktree`, the core owns *all* its guards — the
  session-activity check (taking `slug`/`home`/`force`), the dirty-tree refusal,
  base-resolve, and the rebase itself — so the CLI and the dashboard's `r` enforce
  the same policy; `capture` folds git's output into the result for the dashboard's
  frame), `browse_session` (from `do_browse`; `_docker_port` now also
  raises `YoloError`), and `register_project` (from `config --init`). The cores
  **return** their result string rather than printing, so the wrapper prints it
  (CLI) and the dashboard shows it in the footer.
- **Dispatch is two-tier** (`main`). `config` runs off the *first* `parse_args`,
  before the config files are layered in, so a broken config can't block fixing
  the config (and its sentinel re-parse needs pristine parser defaults).
  Everything else re-parses with the config defaults layered in first
  (`dockerfile`, which just prints `DEFAULT_DOCKERFILE`, `dir`, which prints a
  path, `secret`, which manages the credential store, and `stop`, which `docker stop`s a
  container, are dispatched right after
  `config` — before that re-parse — since they need no yolo config at all; `dir`
  in particular keeps its stdout free of the config
  provenance note). The other
  terminal verbs (`list`, `ps`, `wip`, `tokens`, `forget-token`, `finish`, `rebase`,
  `setup-token`,
  and `shell`'s exec-into-running case) then handle-and-return — `setup-token` sits
  after the config-dir resolution specifically so it caches the token under the
  right per-dir service name, while `forget-token` is dispatched *before* the
  config-dir-must-exist check (forgetting a token for a deleted config dir must
  work). Launch verbs (`start`, `resume`, `shell`-fresh, and a
  bare `yolo`) pass the guardrails (home refusal, `require-project-entry` — see
  the orthogonal-flags section), then call `launch_container`; extra mounts and
  port specs are resolved only on these paths, so a stale mount path or
  malformed port spec can't break `list`/`finish`/`config`.
- **`launch_container`** is the single assembly path shared by every launch
  (extracted from the old inline `main`): it first finalizes the container name
  (the `-{config}`/`-{profile}` suffixes, up front) and **GCs + creates the
  per-session run dir** (keyed by that name; see the run-dir section), then
  assembles mounts (cwd + the extra `--mount` dirs), ssh-agent block, the
  credential/config/auth blocks (staging creds into the run dir; oauth-token mode
  hands the token to `_stage_secrets` as `extra_env` rather than `-e`), the
  **secret mounts** (`_stage_secrets`: the rw `/run/secrets` mount for env targets
  + the token, a ro mount per file target), labels, `--entrypoint` override, then
  hands the finished
  argv to `_dispatch_launch` (the run-it-here vs run-it-in-tmux seam — see the tmux
  section). It takes `container_base`,
  `command` (args after the image), optional `entrypoint`, and the resolved
  `mounts`/`ports`. For claude sessions (`entrypoint is None`) it also creates
  `<config-dir>/.yolo-status/` and **deletes the stale `<cwd-slug>.state` file**
  so a fresh session doesn't briefly show a prior one's wait time.
  `build_claude_args` builds the `claude` command (settings, built-in
  prompt, `--add-dir` per extra mount, `--continue`/`--resume`, `--name`). The
  built-in prompt's conditional lines depend on the launch: an SSH-agent line
  (local-only vs. push/fetch work), a forwarded-ports line (bind 0.0.0.0; reach it
  via `yolo browse`/`b`), and — when `cwd_mode` (a cwd session, `worktree_name is
  None`, passed by `main` to each call) — a caution that the working dir is the
  user's live host checkout, so destructive in-place changes to artifacts like
  `.venv` (that host tools/servers may depend on) should be avoided; a worktree is
  an isolated copy, so the line is omitted there.
- **Session-activity hooks** (`build_claude_args` + `_read_session_state`). The
  `--settings` overlay (which already disables the sandbox) also injects a
  `Stop` hook (writes `waiting <epoch>` to `/home/claude/.claude/.yolo-status/
  <cwd-slug>.state`) and a `UserPromptSubmit` hook (writes `working <epoch>`).
  Those two are *turn-boundary* events; a third case is **mid-turn waiting**: the
  `AskUserQuestion` tool blocks for the user's answer *without ending the turn*,
  so `Stop` never fires and the session would otherwise still read `working` while
  it actually waits. So a **`PreToolUse` hook matched to `AskUserQuestion`** writes
  `waiting` (the question is about to block) and a **`PostToolUse`** match writes
  `working` (the answer arrived). The matcher is honored for these two events
  (unlike `Stop`/`UserPromptSubmit`, where it's ignored), so only that one tool
  flips the state. (Plan-mode approval / `ExitPlanMode` is a known remaining gap —
  it fires no comparable hook, so a session sitting on a plan still reads
  `working`.) The absolute container path is baked into the hook command (no
  reliance on a `docker run -e` var reaching the hook subprocess). `--settings`
  *replaces* the whole `hooks` key (only `permissions` merges across scopes), so
  `_read_settings_hooks(config_dir, home)` reads the mounted
  `settings.json`/`settings.local.json` hooks and `build_claude_args`
  concatenates yolo's groups onto them (preserving the user's; enterprise-managed
  settings aren't covered). `ps` reads the state file (see below); the schema is
  the matcher-group-wrapped `{"hooks":{"Stop":[{"hooks":[{"type":"command",...}]}]}}`
  (matcher omitted for `Stop`/`UserPromptSubmit` — ignored for those events — and
  set to `AskUserQuestion` for the `Pre`/`PostToolUse` pair). The status file lives
  under the config dir because that's the only host-writable bind mount reachable
  from inside the container (`~/.claude-yolo` is deliberately never mounted).
- **Containers are found by docker label, not name.** Every launch is stamped
  `--label yolo.repo=<repo-slug>`, `--label yolo.cwd=<cwd>`,
  `--label yolo.config-dir=<host config dir>` (so the cross-repo `ps` can locate
  each session's `.yolo-status` file), and (for worktrees)
  `--label yolo.worktree=<topic>`. `running_container_for(slug, topic=None, *,
  cwd=None)` queries `docker ps --filter label=…`: by `yolo.worktree` for a worktree
  `shell`/`finish`/`list`, by `yolo.cwd` for a plain cwd `shell`. The cwd filter is
  what disambiguates a current-directory container from this repo's worktree
  containers (they share a repo slug but run under different paths). Robust to the
  `-{config}`/`-{profile}` name suffixes.
- **Verb dispatch / topic-optionality** (`main`). `finish` without a `TOPIC` errors;
  `start`/`resume`/`shell` without one run in the cwd. A bare invocation (no verb) is
  normalized to `start`. The single launch path then branches on whether a `TOPIC` is
  set: `_worktree_dir`/`setup_worktree` for a worktree, or `_repo_slug_or_none()` +
  `cwd.name` for the cwd.
- Verb-only flags: `--base REF` (config-backed via the `base` key; consumed by
  `start`, `list`, `finish`, and `rebase`), `--finish-action`/`--finish-remote`
  (config-backed like `base`, so ungated; consumed by `finish`), `--new`
  (resume, worktree-only), `--force` (`finish`, `rebase`, and `stop` — skips the
  uncommitted-changes guard for `finish`, the not-confirmed-idle running container
  for `rebase`, and the actively-`working` guard for `stop`),
  `--resume`/`-r` (resume), `--watch` (ps), `--all` (`list` and `secret list`),
  `--project`/`--clipboard` (`secret`), `--print`/`-n`
  (browse), and the
  `config` family — `--init`, `--global`, `--unset`,
  `--add-mount`/`--remove-mount`, `--add-prompt`/`--remove-prompt`,
  `--add-port`/`--remove-port`, `--add-secret`/`--remove-secret`,
  `--add-plugin-dir`/`--remove-plugin-dir`, `--add-clone`/`--remove-clone`.
  Each is validated against its verb in dispatch (e.g. `-r` outside `resume`,
  `--new` without a `TOPIC`, or `--new` with `-r` all error). (`--port` is the
  exception: a launch flag that doubles as `browse`'s selection.)

## tmux mode (`--tmux`) and the `ps` verb

Opt-in (`--tmux`, or `tmux: true` in config): instead of exec'ing the launch in
the invoking terminal, every session becomes a **window of one shared tmux
session** (default name `yolo`; `--tmux-session`/`tmux-session` overrides), so
parallel sessions live in one terminal window and tmux keys switch between
them. Windows, not panes, deliberately: Claude Code is a full-screen TUI and
panes would cramp it; tmux windows already are the navigation UX.

The mechanics, all funneled through two functions:

- **`_dispatch_launch`** is the seam at the tail of `launch_container` (and the
  `shell`-into-running `docker exec` path in `main`): tmux off → `os.execvp`,
  byte-for-byte the pre-tmux behavior; tmux on → `_launch_in_tmux`. It also
  decides **window reuse**: if `running_container_for` (same label query the
  `shell` verb uses) finds the matching container already running, the
  same-named existing window is focused instead of spawning a `docker run`
  doomed to the container-name conflict — but if no window matches (container
  started outside tmux mode), it spawns anyway and lets docker report the
  conflict in the window.
- **The already-running guard** (in `launch_container`, *before* `_build_image`,
  shared by both modes — same `running_container_for` query). A running container
  of this name means a live session for the worktree/cwd; you can't launch a
  second with the same name, so resuming/starting one "on top" is never valid.
  Handle it up front (so we never do the now-pointless, possibly-slow image build
  and then fail): **non-tmux** `sys.exit`s with guidance (switch to the terminal
  it's running in, or exit it and resume; `yolo shell` for another view) rather
  than building and dying on docker's raw name conflict. **tmux** instead
  **switches to the existing window** (resuming a live session = going back to it)
  and **warns** that the reused container keeps the image it was started with, so
  a changed Dockerfile / rebuilt image won't apply until the session is exited and
  resumed — the "it built a new image but launched the old one" surprise. The tmux
  no-window fall-through (container started outside tmux) still builds + spawns and
  lets docker report the conflict, unchanged. (Terminal verbs and `shell`-into-
  running are exempt: `shell` *wants* to attach to a running container, handled by
  the `docker exec` path in `main` before this is reached.)
- **`_launch_in_tmux`** ensures the session exists (`_ensure_tmux_session` —
  a fresh one is created detached with window 0 running the `yolo wip
  --_dashboard` dashboard, re-invoked via `_self_invocation`: sys.argv[0]
  resolved through
  `which()` and absolutized, since the tmux server's PATH/cwd differ), creates
  the window (`new-window -n <container-name> -P -F '#{window_id}'`), then
  focuses it (via `_focus_tmux_window`) — inside tmux (`$TMUX` set) by
  `select-window` + `switch-client`
  on the current client; outside by exec'ing into `tmux select-window \;
  attach-session`, so the invoking terminal becomes the tmux client. The
  outside case has one guard: if the session **already has a client attached**
  in another terminal (`_session_has_client`, via `tmux list-clients`),
  attaching a second client would make both terminals *mirror* the one session
  (tmux clamps every client to the smallest one's size and shows them the same
  window). So instead of attaching, it just `select-window`s — the new session
  appears in the already-attached terminal and the invoking one stays a normal
  shell.

Details that matter:

- **Window names are container names** (already unique among running
  containers, suffixes included); the exec'd-shell windows get a
  `-shell` suffix and never reuse (docker exec can't conflict — a second
  `yolo shell` deliberately opens a second window).
- **Window names are pinned** so the status bar keeps showing which
  container/topic each window is. Every window yolo creates (the dashboard and
  each session) gets `automatic-rename off` + `allow-rename off`
  (`_pin_tmux_window_name`); without it tmux would relabel the window with the
  foreground process name (node/python/bash) once it runs, turning the bottom
  bar's window list into a row of identical generic names.
- **The terminal title tracks the focused yolo window, labeled by kind**
  (`set-titles on` + a conditional `set-titles-string`, set by
  `_set_tmux_title_options`), so the OS window/tab title follows the focused window
  as you switch (incl. from the dashboard). The format branches on the window name
  `#W`: the `yolo-wip` dashboard → `#S wip`, a `<name>-shell` window → `#S · shell:
  <name>` (the `-shell` stripped via `#{s/-shell$//:#{window_name}}` — the substitute
  needs the full `#{window_name}`, not the `#W` alias, which expands empty inside
  it), else (a claude session) → `#S · session: #W`; `#S` is the session name
  (`yolo` by default, so e.g. `yolo · session: claude-yolo`). tmux's `set-titles` is
  off by default, so otherwise the title just keeps whatever it was before
  attaching. The options are (re-)asserted on **every**
  launch that touches a session yolo owns — `_ensure_tmux_session` sets them when it
  creates the session *and* re-sets them when the session already exists **but has
  the `yolo-wip` dashboard window** (the marker of a yolo session), so a long-lived
  session created before this feature heals itself without a `kill-server`. A
  *personal* session targeted via `--tmux-session` has no dashboard window, so its
  title config is never touched.
- The window command is `shlex.join(run_cmd)` (the argv contains `--settings`
  JSON and the OAuth token — quoting is load-bearing) wrapped by
  `_tmux_window_command`: on a **genuine-failure** exit it prints the code and
  waits for Enter, because tmux's default remain-on-exit off would otherwise
  vaporize the window before a fast `docker run` failure (name conflict, daemon
  down) can be read. Clean exits (0) close the window — and so do the two
  **intentional-stop** signals: `docker stop` (`yolo stop` / the dashboard `s`)
  makes the attached `docker run` exit **143** (SIGTERM) and Ctrl-C **130**
  (SIGINT), so the hold skips those (`[ $ec -ne 0 ] && -ne 130 && -ne 143`).
  Without that, every stopped session would leave a stale, same-named window that
  a later resume duplicates (which `_all_tmux_windows`' live-window preference then
  has to disambiguate). The same wrapper guards the dashboard window (a bad
  self-invocation can't kill the just-created session).
- **Everything interactive happens before tmux**: credential prompts
  (`ensure_oauth_token` consent/mint, `ensure_logged_in`) run in the invoking
  terminal — only the finished `docker run` argv moves into the window. The
  terminal verbs never touch tmux.
- The window command string is retained in tmux server state
  (`#{pane_start_command}`), but it **no longer contains any secret**: the OAuth
  token (and every `--secret`) now rides the `/run/secrets` file transport rather
  than `-e`/the argv, so the retained command — like `docker inspect` and host
  `ps` — is secret-free. (This is the hardening the old "`--env-file` follow-up"
  note anticipated; the file+loader approach goes further than `--env-file`, which
  would still populate `docker inspect`'s `Config.Env`.)
- All tmux commands go through the `_tmux()` wrapper — the test seam
  (`tests/test_tmux.py` fakes the server there and asserts on exact argv
  sequences).
- **The dashboard picker** (`_ps_picker` / `_ps_picker_loop` / `_draw_picker`):
  interactive `ps --watch` puts the terminal in cbreak mode (ISIG stays on, so
  Ctrl-C works; cursor hidden; everything restored in a `finally` — without it
  the dashboard window's shell is wrecked) and `select()`s on stdin with the
  refresh deadline as timeout, so keys are immediate while the 2s redraw
  cadence continues. Keys are read via `os.read` on the raw fd, NOT
  `sys.stdin` — Python's buffered reader can slurp the tail of an arrow-key
  escape sequence where `select()` can't see it (`_read_key`, which also
  distinguishes bare ESC by short timeout). The selection is tracked by
  container *name*, not row index, so a refresh can't silently move the
  highlight to a different session. Enter maps name → window via
  `_all_tmux_windows()` (all sessions, so it works from a personal tmux
  session too; cross-session adds `switch-client`). Window names aren't unique —
  a session that exits non-cleanly leaves its window open (the
  `_tmux_window_command` failure hold), so re-launching the same topic later opens
  a second same-named window — so `_all_tmux_windows` keys by name but, on a
  collision, prefers the window whose pane runs the container
  (`pane_current_command` == `docker`, the live session) over a stale shell;
  without this Enter would land on the dead same-named window. The picker keeps
  running — selection IS `select-window`; the dashboard persists. Containers
  without a window (started outside tmux mode) render with a `*` and Enter
  no-ops. The loop takes an injectable `wait_key` and is tested with scripted
  keys; only the terminal plumbing in `_ps_picker` is untested.

## The worktree mechanics (`setup_worktree`)

When a verb gets a `TOPIC`, this is what backs it. Orthogonal to the credential
modes (composes with any of them). `setup_worktree` creates a git worktree on a new
branch `TOPIC` (off `base`, default current `HEAD`, no upstream) at
`~/.claude-yolo/worktrees/<repo-slug>/TOPIC`, where `<repo-slug>` is the main repo
path slugified the way Claude names `~/.claude/projects/` buckets
(`re.sub(r"[^a-zA-Z0-9]", "-", path)`, factored into `_repo_paths`). `start` is its
sole caller and guards existence (`worktree.exists() or _branch_exists(topic)`)
*before* calling it, so `setup_worktree` always creates fresh — a single
unconditional `git worktree add -b`. `resume`/`shell` don't call it; they locate the
existing worktree via `_worktree_dir`. `main` then retargets `cwd` to the worktree
(so `-w` and the `{cwd}:{cwd}` mount point there) and **additionally mounts the
shared `.git` at its identical host path** — both same-path mounts are required
because a linked worktree stores *absolute* paths to its `.git` and back. The session
is named via `claude --name <repo>:<TOPIC>`. Durability is the point: commits land in the
host's shared `.git` and uncommitted edits live in the host worktree dir, so a
container exit loses nothing. Must be run from inside a git repo.

## Resuming a session (`resume`, `--resume [SESSION_ID]` / `-r`)

Resuming is the `resume` verb's job (there is no longer a bare `--continue`/`-c`
flag; it was retired in favour of the verb). A plain `resume` forwards
`claude --continue` (most recent session); `resume -r [ID]` forwards
`claude --resume [ID]`, opening Claude's interactive picker when given no ID (works
because we run `-it`). `-r` is **only** valid with `resume` (argparse default `None`;
dispatch errors otherwise). Resuming needs no new mounts: session transcripts live
in `~/.claude/projects/<slug>/*.jsonl`, which is already bind-mounted, and the
slug is derived from the project path — which matches host↔container because the
cwd is mounted at its identical path. So a session started in a yolo container
(or even on the host, same dir) is resumable. With a `TOPIC`, resume is keyed to the
worktree's path. The `--name` injection is **suppressed** when resuming, because
`claude` rejects `--name` alongside `--continue`/`--resume` (the session already has
its identity); `resume TOPIC --new` is the exception — it *does* name a fresh
worktree session and so omits the resume flags.

A plain `resume` (the `--continue` path) **falls back to a fresh session when
there's nothing to continue.** `claude --continue` *errors* if no transcript
exists for the dir — never started, or expired via `cleanupPeriodDays` — so before
launching, `_has_resumable_session` checks host-side for
`~/.claude/projects/<slug>/*.jsonl` (the same slug-from-the-bind-mounted-cwd that
makes resume work at all). Finding none, the launch path drops `--continue`, prints
a note, and builds a fresh session instead — *named* like `--new` in worktree mode
(`session_name` = `<repo>:<topic>`), or after the directory basename in cwd mode. This is what lets a "resume this
project" affordance be safe even when the dir has never had a session. Scoped to
the `--continue` path only: `-r [ID]` is left to `claude` (an explicit ID/picker
request shouldn't silently become a fresh session).

## Conventions / gotchas

- **macOS + Linux hosts (Windows via WSL2); Docker Desktop, OrbStack, or native
  Linux Docker as the engine.** The host-specific glue routes through the `_HOST`
  helpers (credential store, clipboard, ssh-agent socket, temp dir). **SSH agent
  forwarding** (off by default, enabled with `--ssh-agent`) picks its source socket
  per host via `_ssh_agent_sock_source`:
  - on **macOS / Windows** (Docker Desktop or OrbStack) it mounts the engine's
    `/run/host-services/ssh-auth.sock` — the VM-side socket the engine proxies to
    the host agent — NOT the raw host `$SSH_AUTH_SOCK`, whose listener lives in the
    host kernel and is unreachable from the container's Linux VM (the mounted inode
    is dead: `connect()` → ECONNREFUSED). That engine socket is `srw-rw---- root:root`,
    so the in-container `claude` user (uid = host uid, a non-root gid) can't
    `connect()` by default; `useradd -G root` puts it in group 0 for the socket's
    group-rw. No real privilege added (the user already has NOPASSWD sudo; the
    container is the sandbox).
  - on **native Linux Docker** the engine shares the host kernel, so it mounts the
    host's own `$SSH_AUTH_SOCK` directly — `connect()` works because the claude
    user shares the host uid that owns the socket (no group-0 trick needed there).

  Either way the host must have a running ssh-agent for forwarding to work.
- **Under `--ssh-agent`, GitHub HTTPS git is rewritten to SSH so it reuses the
  agent.** When the agent is forwarded, the launch sets run-time git config via
  `GIT_CONFIG_COUNT=1` / `GIT_CONFIG_KEY_0=url.git@github.com:.insteadOf` /
  `GIT_CONFIG_VALUE_0=https://github.com/` (highest-precedence env-based config, not
  baked into the image), so in-container git operations on `https://github.com/...`
  remotes (fetch *and* push) transparently route over SSH and authenticate via the
  forwarded ssh-agent — **no token ever enters the container**. It's conditioned on
  `--ssh-agent` deliberately: without an agent the rewrite would turn a public-repo
  HTTPS clone (which needs no auth) into an SSH URL that can't authenticate, so when
  the agent is off the rewrite is simply absent and plain HTTPS clones work. This is the only HTTPS-auth approach that keeps
  the secret-never-in-container property: HTTPS auth is a bearer token (the token
  must reach whoever makes the request), whereas SSH is challenge-response (the key
  stays on the host, the agent only signs). The host's `osxkeychain` credential
  helper is a macOS binary backed by the macOS Keychain — neither exists in the
  Linux container, which is the other reason plain HTTPS push can't work here. Host
  config is untouched (we never mount `~/.gitconfig`); remotes can stay HTTPS.
- **In-process sandbox is disabled deliberately — the *container* is the
  sandbox.** The container-only `--settings` overlay sets `sandbox.enabled:false`
  so that, when the mounted `~/.claude/settings.json` has `sandbox.enabled: true`,
  Claude doesn't warn at startup that `bubblewrap`/`socat`
  are missing and run unsandboxed. `--settings` is a container-only overlay (host
  settings untouched). Do NOT instead install `bubblewrap` to "fix" it — a default
  Docker container can't create unprivileged user namespaces (`bwrap: No
  permissions to create new namespace`), and granting that capability would weaken
  the very isolation this tool exists to provide. (A `/doctor` sandbox note may
  still appear; that's expected.) The **same overlay carries the session-activity
  hooks** (see `build_claude_args` under Workflow verbs) — and because `--settings`
  replaces each top-level key wholesale, the `hooks` key it sets must re-include
  the user's own mounted hooks (`_read_settings_hooks`), exactly as the `sandbox`
  key overrides the mounted `sandbox` setting.
- **Argument splitting:** `main` splits `sys.argv` on `--` *before* argparse
  sees it. Everything after `--` is appended to `docker run` last, so
  user-supplied flags win (last-one-wins).
- **`--prompt` / `-p`** is repeatable and is added *on top of* a built-in
  prompt telling Claude it's in an ephemeral Ubuntu container (it feeds
  claude's own `--append-system-prompt` flag, the option's pre-0.7 name).
- **Git identity is forwarded as env vars, not a mounted gitconfig.**
  `git_identity_args` reads the host's *effective* `user.name`/`user.email` (so a
  repo-local identity wins) and exports them as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.
  Mounting `~/.gitconfig` instead would drag in macOS-only bits (osxkeychain
  credential helper, GPG signing) that break commits in the Linux container. Note
  these env vars override any repo-local identity set *inside* the container.
- **`YOLO_SESSION` is exported into every container** (in `launch_container`'s
  shared arg list, so it covers claude sessions and `yolo shell` alike; a `docker
  exec`-ed shell inherits it too). It's a deterministic marker that code running
  inside — Claude, hooks, scripts — is in a yolo container, where the
  worktree/branch is already the unit of isolation, so committing on the current
  branch is fine. Its **presence** is the "am I in yolo?" test (`[ -n
  "$YOLO_SESSION" ]`); its **value names the session kind** — `worktree` for a
  worktree session, `cwd` for a current-directory one (`'worktree' if
  worktree_name else 'cwd'`) — so a script can branch on which it is. (It used to
  be the literal `1`; presence-based checks are unaffected, an exact `= 1` check
  would need updating.) Distinct from `YOLO_PS1` (a *presentation* var the
  `.bashrc` adopts for the prompt); `YOLO_SESSION` is the semantic flag.
- The container name is the cwd basename (or `{main_repo_name}-{TOPIC}` for a
  worktree), then suffixed with `-{config-dir-basename}` when
  `--config-dir` is set and `-{aws-profile-or-"bedrock"}` under `--auth bedrock`.
  Suffixes stack, so the axes compose in the name too. The final assembled name
  is run through `_sanitize_container_name` (in `launch_container`, after the
  suffixes) so a source with characters docker rejects — a cwd basename holding a
  space/unicode, a leading dot — still yields a valid `--name` rather than a
  cryptic `docker run` failure: every disallowed character (anything outside
  `[a-zA-Z0-9_.-]`) becomes `-`, leading `_.-` are stripped (docker requires an
  alphanumeric first char), and an entirely-invalid name falls back to `yolo`.
  The run dir, labels, and tmux window all key off this sanitized name.
- The `# https://claude.ai/chat/...` URL on line 2 and the upstream gist
  reference in git history are the script's provenance — this started as
  Migurski's gist.

## Development

`pyproject.toml` defines a **uv-managed project** whose one runtime dependency is
`keyring` (kept in sync with the PEP 723 `dependencies` block in `yolo.py`). Its
`dev` dependency group carries the tooling — `ruff` and `pytest`. The project *is*
packaged (hatchling build backend, `[project.scripts] yolo = "yolo:main"`, wheel
ships `yolo.py` + its `Dockerfile.default`/`Dockerfile.custom`/`container-prompt.txt`
data files) so it can `uv tool install`. `uv.lock` is committed; `.venv/`,
`dist/`, and the tool caches are gitignored.

```bash
uv sync                 # create/refresh .venv with the dev tools
uv run pytest           # run the test suite (tests/)
uv run ruff check .     # lint
uv run ruff format .    # format
uv build                # build the wheel/sdist into dist/ (for publishing)
uv run bump-my-version bump patch   # version bump (or minor/major): commit + tag
```

Version bumps are automated with **bump-my-version** (configured under
`[tool.bumpversion]` in pyproject.toml). One command updates the version in
`pyproject.toml` *and* the project's own entry in `uv.lock` (so the lockfile
doesn't go stale), then commits both and tags `v{new_version}`. It requires a
clean working tree. The pyproject search is line-anchored (regex) so it doesn't
also match the `current_version` line in the bumpversion config itself; the
uv.lock search is two-line (`name = "claude-yolo"\nversion = ...`) so it can't
hit a same-versioned dependency.

### Cutting a release

The steps, in order:

1. **Check the README and CHANGELOG are up to date.** Diff the commits since the
   last tag (`git log --oneline v{last}..HEAD`) against the docs and fill any
   gaps — a new flag/verb/key, changed defaults, renamed behavior. The README is
   feature-level user docs; the CHANGELOG is the per-version record.

2. **Consolidate the CHANGELOG `## Unreleased` section.** Each entry should
   describe *what changed since the last release* from a user's point of view —
   **not** the blow-by-blow of how we got there. Fold the intermediate commits
   for one feature into a single entry (e.g. several "fix the diff picker"
   commits become one "`yolo diff --stat` is an interactive picker" entry);
   drop churn that cancels out (a thing added then reworked is just its final
   shape).

3. **Retitle the section to the new version** — `## vX.Y.Z — YYYY-MM-DD`
   (today's date) — and commit that CHANGELOG edit on its own.

4. **Run the bump command** — `uv run bump-my-version bump {patch,minor,major}`
   (minor for new features, patch for fixes only). It updates `pyproject.toml` +
   `uv.lock`, commits them, and tags `v{new_version}` (annotated). Requires a
   clean tree, so commit the CHANGELOG/README first.

5. **Push from the host** — the yolo container has no SSH agent, so `git push`
   can't run here. Tell the user to run `git push origin main --follow-tags`.

If a doc gap surfaces *after* the bump (the tag isn't pushed yet), commit the fix
and move the annotated tag onto it (`git tag -f -a v{ver} -m "…"`, preserving the
original message) so the release ships accurate docs — don't leave the fix
dangling past the tag. Confirm it stayed annotated with `git cat-file -t v{ver}`
(a stray `git tag -f` without `-a` silently downgrades it to lightweight).

Tests load `yolo.py` via `importlib` **from its file path** (not a plain
`import yolo`) so each test gets a **fresh module instance** — `main()` mutates
the module-global `PARSER` through `set_defaults`, so isolation matters; loading
from the path also pins the tests to the source file regardless of any installed
`yolo`. They
never touch the host or Docker: `tests/conftest.py`'s `run_cli` fixture stubs
`build_docker_image`, `ensure_logged_in`, `extract_credentials`,
`ensure_oauth_token`, `git_identity_args`, and `os.execvp` (and points `_run_dir`
at the test home + no-ops the docker-ps `_gc_run_dir`), then asserts on the
captured `docker run` argv. It also defaults `running_container_for` to "nothing
running" (the launch-time already-running guard does a `docker ps`) — but only
when a test hasn't set its own stub, so the tmux/verb tests that patch it to a
truthy id before calling `run_cli` still win (identity check against the
freshly-loaded module's original). `test_config.py` covers config parsing/merging
(`~/.yolo.json` + `projects.json`), mount-spec parsing, the stale-state
warnings, the `dockerfile` config key (parse + `config`-verb persist/validate),
the `finish-action`/`finish-remote` keys (parse, the `FINISH_CHOICES` validation,
and `config`-verb persist), the `submodules` bool key (parse), the
`redirect-build-dirs` bool key (parse, non-bool rejection, `config`-verb persist of
the opt-out), and the `config`
verb; `test_cli.py` covers verb
dispatch and arg
assembly across the credential/config axes, extra mounts, the guardrails, the
build-dir redirect (`UV_PROJECT_ENVIRONMENT`/`CARGO_TARGET_DIR`/`PYTHONPYCACHEPREFIX`
present by default in a cwd launch, absent under `--no-redirect-build-dirs`; the
worktree-skips-it case is in `test_verbs.py`), the
`--dockerfile` override (content-addressed tag, the `HOST_UID` build-arg, the
missing-path error, the relative-vs-absolute path resolution against the session
cwd, and that the build context contains only the Dockerfile), the
`FROM ${YOLO_BASE}` layering (`_build_image` builds the base then the custom image
and folds the base tag into the final tag; `_verify_image_user` rejects a non-
`claude` image), and the `dockerfile` dump verb. The
tests locate the built image in the assembled argv by its
`claude-yolo:` repo prefix (the tag is now content-addressed, not a fixed constant).
It also covers the unconfigured-`Dockerfile.yolo` warning, the `--verbose`/`-v`
docker-command dump, and the **already-running guard** (non-tmux `resume` with a
running container `sys.exit`s before the build; the `repo`-based worktree variant
is in `test_verbs.py`, the tmux switch-and-skip-build variant in `test_tmux.py`).
`test_verbs.py` covers the worktree verbs against a
**real throwaway git repo** (so the actual `git worktree` machinery runs),
stubbing only `running_container_for` (docker) plus the `run_cli` side effects —
including the four `--finish-action` behaviors (`keep`, `merge` and its
conflict-abort/keep path, and `push` to a `--finish-remote` bare repo) alongside
the default `delete-if-merged`, the submodule-removal fallback (a real populated
submodule that plain `git worktree remove` refuses, which `finish` clears via
the rmtree+prune path), the `--submodules` population on launch (a real
file-protocol submodule checked out host-side when enabled, left empty by
default, and a no-op without `.gitmodules`), and the `rebase` verb (replaying a branch onto
the base's new commits, honouring `--base`; the required-topic/missing-worktree/
dirty-tree refusals; and the session-aware running-container handling — a
`waiting` session rebases through, `working`/unknown refuse, and `--force`
overrides — driven by a stamped `.yolo-status` state file read through the
container's own labels, including that a session under an alternate `--config-dir`
is still read correctly), the `diff` verb (required-topic/missing-worktree
refusals; the three-dot `base...HEAD` showing branch-only changes, not base's own,
via a real repo with commits on both; the `--stat` non-tmux fallback printing the
stat, the empty "No changes", and `--stat`-only-on-diff gating), and `list` (the
TOPIC-only columns with the `topic (branch: X)` fold-in only when the branch
diverges, the COMMITS column value for a branch one commit ahead, and `--all`
spanning two repos under one fake HOME, the REPO column,
the per-repo `merged` judgement run from a different repo, an orphaned worktree
(main repo moved → STATUS `orphaned`, `-` commits, repo name recovered from the
`.git` pointer, the `git worktree repair` footer hint), the empty case, and
the verb gating), the non-tmux already-running `resume` refusal, the
`resume`-with-no-session fallback to a fresh session (worktree mode names it
`<repo>:<topic>`, cwd mode after the directory; a seeded `projects/<slug>/*.jsonl`
transcript exercises the real `--continue` path), and the `stop`
verb (`docker stop`ping the worktree's container by label, the nothing-running
no-op, and the actively-`working` guard — refused without `--force`, stopped with
it; the cwd variant is in `test_cli.py`).
`test_worktree_config.py` covers the per-worktree overlay (also against a real
repo): `start` populating `worktrees.json` from explicit flags (and the empty
`{}`), `resume`/`shell` consuming it with project<overlay<CLI precedence and
concat-key accumulation, `resume` flags *updating* the overlay (lists accumulate
+ dedup, scalars override, no-flags no-op, persistence to the next resume) while
`shell` doesn't, the provenance tail, `yolo config TOPIC` show/edit (and the
`--global`/`--init`/missing-worktree errors, and a relative `--dockerfile`
validated against the worktree dir rather than the cwd), `finish` removing the
entry, and a malformed-file error.
`test_tokens.py` covers the token registry, the registry-sourced expiry warning
(`_token_minted`), the implicit-mint consent prompt, and the `tokens` /
`forget-token` verbs (the credential-store wrapping helpers stubbed).
`test_platform.py` covers the host-platform abstraction: the `_HOST`/`_is_*`
helpers, `_open_url` routing through `webbrowser`, the credential store
(file-fallback round-trip get/set/delete/exists + a faked keyring backend), the
per-host `_ssh_agent_sock_source` selection, the cross-platform `_read_clipboard`
command choice, and the `_run_dir` `$XDG_RUNTIME_DIR`/`$TMPDIR` location. A
conftest autouse fixture forces `YOLO_CREDENTIAL_STORE=file` so no test ever
touches a real keyring.
`test_tmux.py` covers tmux mode end-to-end against an in-memory fake tmux
server patched in at the `_tmux` seam (session creation + dashboard seeding,
window command quoting, inside-vs-outside `$TMUX` focusing, the
already-attached-client no-mirror guard, window reuse (including that a reused
running container **skips the image build** and warns, while the no-window
fall-through still builds), the config keys, the
terminal-title options (the per-window-kind `set-titles-string`, the bare-`yolo`
session target, set on a created session, re-asserted on an existing yolo session
that has the dashboard window, left off a personal session that lacks it), and the pinned
window names), the `ps` verb's table from canned `docker ps` output, and the
`--watch` picker loop via scripted `wait_key` events (selection movement and
clamping, Enter→select-window, cross-session switch-client, selection
surviving a refresh, orphan marking, the picker-vs-passive dispatch); the
`wip --_dashboard` seed of window 0 is asserted here too.
`test_wip.py` covers the `wip` dashboard: the data layer (`_order_sessions`
unknown→waiting→working grouping with unknown sorted oldest-`created_at`-first,
`_draw_wip` rendering the sessions as one SESSIONS table with *no* blank lines
between groups (distinguished by the green/yellow status-group color) and `(none)`
when empty, `_wip_items` listing *every* worktree
(running ones flagged, also shown as a session) against a real repo,
the `_worktree_rows` COMMITS counts (`_branch_ahead_behind` against a real
repo with commits added on the branch and the base) and that column's colorized
(red-behind/green-ahead) rendering in `_draw_wip`,
`_wip_projects`' active flag plus the recent-projects union and the `a`-registers-
selection flow, plus `_session_window_for` exact-path/subdir/no-window
resolution) and the `_wip_loop` event loop driven by a scripted `FakeTerm` with `_wip_items`/
`_draw_wip` and the action cores stubbed (navigation across sections, refresh
preserving selection by key, Enter→switch/resume-worktree/resume-project/focus-
active-project-or-worktree, `N` new-session on a worktree/project spawning
`resume TOPIC --new`/`start` (and refusing on a live `window` + the session-row
no-op), `R` resume-pick spawning `resume TOPIC -r`/`resume -r`, `n` on a
project prompting a topic then spawning `start <topic>` (and cancelling on an empty
topic), Enter on the `+` row prompting a directory then spawning `start --no-tmux`
there (cancel on empty, reject a non-dir), `S` shell on a session row spawning the
`docker exec` window (a worktree-row no-op), `b` browse incl. the
multi-port prompt, `s` stop with the working-session force + confirm/cancel,
`f`/`r` on worktrees and idle sessions (a running worktree row now defers to the
cores, which own the guard), `d` on a worktree *and* a worktree-backed session row
spawning `yolo diff <topic> --base … --stat` (a no-op on a plain cwd session), the
diff-stat picker (`_diff_stat_loop` navigating + Enter/Space spawning the per-file
`git diff` window, q quitting; `_draw_diff_stat`'s selected-file reverse bar and dim
summary), `c` opening the config editor (`_config_editor_loop` / `_config_scope`: the
raw-flags `e` hatch composing `yolo config [TOPIC] <flags>` from the right dir for
worktree and project scope, `x`+confirm unsetting a scalar, the `_config_list_loop`
add-mount path+ro/rw-pick and remove-element flows, the `_config_clones_loop`
add-with-depth (routed via `_config_edit_key`), add-without-depth, and remove-by-dir
flows, the error surfacing in the
editor frame, the current-values + inherited-pane display, and the units
`_config_value_flags` (bool `--key`/`--no-key`), `_prompt_config_value` kind
routing, and `_pick_one` navigation/cancel; a session row is the no-op message),
a raised `YoloError` landing in the
footer instead of killing the loop, `a` add-project), plus `do_wip` bootstrap
(focus the dashboard window, the no-tmux exit, the no-TTY passive fallback),
`_worktree_config` (a worktree's `base`/`finish-action`/`finish-remote` from a
freshly-edited global `~/.yolo.json` *and* from the worktree's own repo entry
overriding global; `None` home → built-in defaults), and `_complete_path`
(single-match full-fill, multi-match common-prefix + basename options, `~`
expansion, no-match unchanged) + `_wip_items` appending the `+` row.
`test_ports.py` covers the `--port`/`ports` axis (spec parsing, launch
assembly + the `yolo.ports` label + the 0.0.0.0 prompt line, layer
concatenation, the `config` port edits) and the `browse` verb (the docker
queries stubbed at `running_container_for`/`_container_label`/`_docker_port`
and the `_open_url` seam).
`test_plugin_dirs.py` covers the `--plugin-dir`/`plugin-dirs` axis: spec parsing
(`_parse_plugin_dir_spec` resolve/missing, `_resolve_plugin_dirs` dedup), launch
assembly (the read-only `-v` at the identical path + the `--plugin-dir` claude
arg, that it's *not* also an `--add-dir`, the missing-path exit that doesn't break
terminal verbs, layer concatenation), and the `config` verb (persist, validate,
`--add`/`--remove-plugin-dir` incl. idempotent add and stale remove, the
replace-conflict guard, and the verb gating).
`test_clones.py` covers the `--clone`/`clones` axis: `_resolve_clones`
(relative→sibling, absolute, `~`→container home, dedup-by-dest, the carried-through
`depth`), the config-file `{url, dir[, depth]}` object/list parse + bad-shape
rejection (incl. the config-only positive-int `depth` validation), launch assembly (the
`bash /etc/yolo/clone.sh <url> <resolved-dir>` in the launch wrapper + the bash
entrypoint, the `depth`→`--depth` 3rd arg, none by default, layer concatenation),
and the `config` verb (persisting the **object** form, the repeatable whole-list
set, `--unset clones`, the `--add-clone`/`--remove-clone` element edits incl. depth,
same-dir replace, missing-remove error, bad-depth rejection, the `--clone`-conflict
guard, and the config-only gating). The `wip` `c` editor's clones loop is in
`test_wip.py`.
`test_status.py` covers the session-activity feature: the injected
`Stop`/`UserPromptSubmit` hooks in the assembled `--settings` (schema + baked
status path) plus the `AskUserQuestion`-matched `Pre`/`PostToolUse` waiting/working
pair (mid-turn waiting), the `yolo.config-dir` label, the stale-status-file reset (and that
`shell` does neither), the user-hook merge (`_read_settings_hooks` +
concatenation), and the `_humanize_secs`/`_read_session_state` rendering; the
`ps` STATE column itself is exercised in `test_tmux.py`.
`test_yolorc.py` covers the `--yolorc` axis: `_resolve_yolorc`
(relative/absolute/`~`), the `yolorc` config-key parse + `~` expansion, the launch
wiring (the read-only mount at `_YOLORC_CONTAINER_PATH` + the `YOLO_RC` env), the
claude-launch source wrapper (bash entrypoint, reconstructed `claude
--dangerously-skip-permissions … "$@"`) vs the `.bashrc` path a `yolo shell` uses
(not command-wrapped), the missing-file guard, the unwrapped default launch, the
baked `.bashrc` sourcing line, and the `config` verb persist/validate.
`test_secrets.py` covers the secrets feature and the run-dir GC (the credential
store forced to the file fallback by conftest; clipboard / input sources stubbed
like `test_tokens.py`): `_parse_secret_spec` for
both targets + the `/`-or-`~` discriminator + `~`→`/home/claude` expansion (and
that it does *not* use the host `$HOME`) + the ephemeral `!` marker (and that a
file target rejects it) + the collision/dedup rule; the scope-aware
`_secret_service` naming; the credential-store round-trip (`_cred_*` /
`_read_secret_value` byte-for-byte) and the OS-branched `extract_credentials`
(macOS `security` vs the Linux `.credentials.json` file read); the `secrets.json`
registry (read/write/remove, created
preserved, malformed-file error); the verbs (`set` via clipboard/stdin/prompt +
NAME validation + empty-value refusal + project-scope keying, `rm`, `list`'s
global+project filter / `--all` / stale marking, the `set` dispatch routing the
NAME through `main`); the launch wiring (env → the rw `/run/secrets` mount + the
loader-sourcing wrapper + the staged chmod-600 file with no trailing newline,
env-rename + ephemeral marker file, file → per-path ro mount, the missing-secret
exit, the host-visible-path warning, and that no value ever reaches the argv); the
`secrets` config-layer concatenation; the `config` verb `--add-secret`/
`--remove-secret` edits + spec validation + the replace-conflict guard; the
`--project`/`--clipboard`/extra-args verb gating; the **OAuth-token-via-/run/secrets**
delivery (staged file + rw mount + wrapper, token off the argv, coexisting with a
user secret, and absent under `--auth keychain`); and the docker-ps-scoped GC
(parallel-safety) + the `_session_run_dir` 700 mode + the credential/mask
run-dir retrofit (chmod 600, in the run dir). `test_cli.py`'s
`assert_token_via_run_secrets` helper is the shared check that oauth-token mode
keeps the token off the argv and stages it under `/run/secrets`; `test_tmux.py`
asserts the same for the retained pane command. Keep them green when changing
flags or mounts.
