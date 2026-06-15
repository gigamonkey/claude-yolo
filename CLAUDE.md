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
./yolo.py                          # default: long-lived OAuth token (consent-prompted mint on first run)
./yolo.py --config-dir ~/.claude-work          # alternate config dir
./yolo.py --auth keychain          # mount a snapshot of the rotating keychain creds instead
./yolo.py --auth bedrock --aws-profile myprofile --aws-region us-west-2 --bedrock-model some.model.id
./yolo.py --auth bedrock --config-dir ~/.claude-bdr # Bedrock + alternate config dir
./yolo.py --no-claude-json         # don't mount the host ~/.claude.json
./yolo.py --ssh-agent              # forward the host ssh-agent (off by default)
./yolo.py --mount ~/refdocs        # also mount ~/refdocs (read-only) at its host path
./yolo.py --mount ~/other:rw       # extra mount, writable
./yolo.py --dockerfile ./Dockerfile.yolo  # build the image from a custom Dockerfile
./yolo.py dockerfile               # print the built-in default Dockerfile (a starting point)
./yolo.py --port 8000              # forward container port 8000 (docker picks the host port)
./yolo.py --port 8000:8000         # ...or pin host port 8000 (single-session)
./yolo.py browse                   # open the browser at this session's forwarded port
./yolo.py browse fix-auth          # ...or at a worktree session's
./yolo.py setup-token              # mint+cache the long-lived OAuth token explicitly
./yolo.py tokens                   # list minted tokens (mint date, est. expiry, status)
./yolo.py forget-token             # delete this config dir's token (local only)
./yolo.py -- --network host        # extra docker run args
./yolo.py                          # == `yolo start`: fresh session in the cwd
./yolo.py resume                   # continue most recent session in this dir
./yolo.py resume -r                # interactive session picker (cwd)
./yolo.py resume -r SESSION_ID     # resume a specific session (cwd)
./yolo.py shell                    # bash shell in the cwd's container (or fresh)
./yolo.py start fix-auth           # new worktree+branch, launch a session (see verbs)
./yolo.py resume fix-auth          # re-enter that worktree, continue the session
./yolo.py shell fix-auth           # bash shell in that worktree's container
./yolo.py finish fix-auth          # remove the worktree; delete the branch if merged, else keep+warn
./yolo.py list                     # this repo's worktrees
./yolo.py dir fix-auth             # print that worktree's dir (cd "$(yolo dir fix-auth)")
./yolo.py ps                       # running yolo containers, across all repos
./yolo.py ps --watch               # ...refreshing every 2s (the tmux dashboard)
./yolo.py --tmux                   # spawn the session as a tmux window instead
./yolo.py --version                # print the version and exit
```

The **auth mechanism** is a single mutually-exclusive choice via `--auth`
(`oauth-token` [default] / `keychain` / `bedrock`). Everything else —
`--config-dir`, `--claude-json`, `--ssh-agent`, `--mount`, `--port`, `--tmux` —
is an **orthogonal flag** that composes freely with the chosen auth mode and
with each other. The only positional args are an optional `verb`
(`config`/`start`/`resume`/`shell`/`browse`/`finish`/`list`/`ps`/`dir`/
`dockerfile`/`setup-token`/`tokens`/`forget-token`) and its `TOPIC`; see [Workflow
verbs](#workflow-verbs).

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

`--version` (the argparse `version` action) prints `_version()`, which mirrors this
dual nature: it first reads the recorded package metadata
(`importlib.metadata.version("claude-yolo")`, present in the installed wheel), and
falls back to scraping `version` out of the **adjacent pyproject.toml** (resolving
the script's symlink, so the PATH-symlink standalone install works) — so both modes
trace back to the single source of truth, pyproject.toml, with no second copy of the
number in `yolo.py`. A stray copy with neither metadata nor pyproject reports
`unknown`.

## How it works

1. **Builds the image** (`build_docker_image`) from the inline
   `DEFAULT_DOCKERFILE` (a plain literal Dockerfile, no templating) written to a
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
   text + host UID (`_image_tag`) — so the inline default and each custom
   Dockerfile get *distinct* images. This matters because yolo runs sessions in
   parallel: a single fixed tag would let two concurrent builds (default vs.
   custom) race and one `docker run` pick up the other's image. The default
   Dockerfile stays inline (not a shipped file) to preserve the single-file
   property — `--dockerfile` is an *override*, not a relocation, though a custom
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
4. **Extracts credentials** (`extract_credentials`; keychain mode only) from the
   macOS keychain via the `security` CLI, into a chmod-600 temp file that gets
   bind-mounted to `.credentials.json`. In the default oauth-token mode this
   step is replaced by forwarding `CLAUDE_CODE_OAUTH_TOKEN` (see the oauth-token
   section). Service name is `Claude Code-credentials` by default,
   or `Claude Code-credentials-{hash8}` for a non-default config dir, where
   `hash8` is the first 8 hex chars of the SHA-256 of the resolved config path.
   This mirrors how Claude Code itself names keychain entries — if that scheme
   changes upstream, this breaks.
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
- **`--mount PATH[:ro|:rw]`** (repeatable; `mounts` in config) → bind-mount extra
  host directories ("reference" dirs) at their **identical host paths**, like the
  cwd. **Read-only by default**; `:rw` opts in. The path must exist (docker would
  otherwise create it root-owned on the host). Each mount is also forwarded to
  claude as `--add-dir`, so the dirs are working directories Claude actually knows
  about. Mount lists **concatenate** across the config layers and the CLI (exact
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
  (loopback-bound servers are unreachable through docker's forward) and that
  the user opens them with `yolo browse`. Like mounts, mappings are fixed at
  `docker run` time and resolved only on launch paths.
- **`--rebuild-image`** (default off) → pass `--no-cache` to `docker build`, forcing
  a full image rebuild from scratch (useful when a baked tool is stale or the
  Dockerfile changed).
- **`--dockerfile PATH`** (default unset; `dockerfile` in config) → build the
  container image from this Dockerfile instead of the inline `DEFAULT_DOCKERFILE`.
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
  starting point. **Caveat:** a `dockerfile` pointing at a file *inside*
  the bind-mounted working tree (e.g. `./Dockerfile.yolo`) is editable by Claude
  between runs, so Claude could alter the next image build. The *key* still lives
  in host-side `projects.json` (Claude can't add it), only the referenced file is
  in-tree — an accepted trade-off for an opt-in feature, but prefer an out-of-tree
  Dockerfile when the isolation matters.
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
  check**, adding `-e CLAUDE_CODE_OAUTH_TOKEN=…` and overlaying a throwaway `{}`
  `.credentials.json` (`_masking_credfile`) so a stale host creds file can't
  shadow the env token under Claude Code 2.1.x (see the precedence caveat below).
  It's the default because it has no refresh boundary, so it's safe regardless
  of session timing or concurrency. See
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
it simultaneously with no interference. It's delivered purely as the
`CLAUDE_CODE_OAUTH_TOKEN` env var. This is why it became the default in 0.6.0:
keychain mode was an attractive nuisance — fine in a quick test, with breakage
governed by an invisible refresh boundary rather than anything the user can see
or control.

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
  yolo-managed macOS keychain entry **for the active config dir** → else mint a
  fresh one interactively and cache it there. That last (auto-mint) step is
  **consent-prompted and gated on `sys.stdin.isatty()`**: interactively, yolo
  explains what's about to be minted (1-year token, keychain storage,
  `forget-token` / the claude.ai revoke page) and asks `Proceed? [Y/n]` before
  running the flow — minting a year-long credential the user didn't explicitly
  ask for was the original argument against making this mode the default, so it
  is never done silently (`yolo setup-token` skips the prompt: running the verb
  *is* the consent). A non-interactive launch with no cached token (script/cron/
  no TTY) exits with guidance to run `yolo setup-token` or set the env var,
  rather than hanging on a browser flow nobody can drive.
- **Per-config-dir, like the keychain creds.** The token is cached under
  `claude-yolo-oauth-token` for the default config dir, or
  `claude-yolo-oauth-token-{hash8}` for an alternate `--config-dir`, where `hash8`
  is the first 8 hex chars of the SHA-256 of the resolved path (`_oauth_service`) —
  the *same* hash Claude itself uses for its per-dir keychain entry. So each
  config dir (≈ each account/profile) gets its own long-lived token instead of one
  global token silently authenticating as the wrong account.
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
  keychain with `security add-generic-password -U`.
- **Storage rationale:** the keychain (not a dotfile) keeps the secret encrypted
  at rest, consistent with how Claude Code stores its own creds, and it's
  *extract-only* — never rotated, never written back — so none of the precedence/
  rotation hazards of the mounted `.credentials.json` apply.
- **Caveat:** this *does* put a bearer token inside the container env (a shift from
  the "secret never enters the container" SSH-agent philosophy), but it's a scoped,
  inference-only token — and no worse than the mounted refresh-token snapshot,
  which it replaces. Requires a Pro/Max/Team/Enterprise plan.

### Token bookkeeping: registry, expiry warning, `tokens` / `forget-token`

Because revocation is effectively out of our hands (verified 2026-06-10: no CLI
command, no documented OAuth revocation endpoint; the only path is manual at
<https://claude.ai/settings/claude-code>, whose token list shows near-zero
per-token metadata, accumulates entries from normal Claude Code usage, and has a
reported multi-day revocation lag — claude-code issues #34198/#48373/#59378/
#43801), yolo does its own bookkeeping:

- **Registry** (`~/.claude-yolo/tokens.json`; `_read_tokens_file` /
  `_write_token_entry` / `_remove_token_entry`): maps keychain **service name →
  `{config_dir, minted}`**. Non-secret metadata, host-side only, never mounted
  (same safety property as `projects.json`). Written by `_store_oauth_token`
  (the single funnel both mint paths go through); a re-mint replaces the entry
  and prints the *previous* mint timestamp, since the old token stays valid
  server-side. It exists for what the keychain can't do: enumerate yolo's tokens
  across config dirs, and map a service name back to its config dir (the hash8
  is one-way — the mapping is recorded at mint time or lost). The **mint
  timestamp is the practical point**: it's the only handle for identifying a
  token on the claude.ai page.
- **Expiry warning** (`_warn_token_expiry`, called from `ensure_oauth_token` on
  the cached-keychain-token path; skipped for env-supplied tokens, whose age is
  unknowable): warns at launch when the token is past or within
  `TOKEN_EXPIRY_WARN_DAYS` (7) of `mdat + TOKEN_LIFETIME_DAYS` (365 — an
  *assumption*; the token is opaque and states no expiry). The date source is
  the **keychain item's own `mdat`** (`_keychain_mdat`: `security
  find-generic-password` *without* `-w` — attributes only, no secret read —
  regex-parsed, falling back to `cdat`), not the registry: we upsert with
  `add-generic-password -U`, so mdat = last mint, which can't drift and covers
  tokens minted before the registry existed. Parse trouble → `None` → silently
  no warning (it's advisory).
- **`yolo tokens`** (`do_tokens`, terminal verb, registry-only — needs no config
  dir): table of SERVICE / CONFIG DIR / MINTED / EXPIRES~ / STATUS. STATUS
  reconciles against the keychain via `_keychain_has` (attributes-only
  existence check): `stale (not in keychain)` for a deleted item,
  `re-minted outside yolo` when keychain mdat disagrees with the registry mint
  by > 1 day, else `ok`. Footer points at the claude.ai page and the
  match-by-MINTED trick.
- **`yolo forget-token`** (`do_forget_token`, terminal verb): deletes the active
  config dir's keychain entry (`_keychain_delete`) and registry row, then is
  explicit that the token is only *forgotten*, not revoked — still valid
  server-side, revocable only at the claude.ai page, and probably impossible to
  identify there (reasons above, outside yolo's control). Named `forget-token`
  deliberately: the verb must not claim a power it doesn't have. Honours
  `--config-dir`/config-file `config-dir`, and is dispatched *before* the
  config-dir-must-exist check so a token for an already-deleted config dir can
  still be forgotten (`_oauth_service` only hashes the resolved path).

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
   `_explicit_config_flags`, so a resume relaunches with the same config without
   retyping; an empty `{}` is still written, symmetric with the worktree
   lifecycle), `yolo config TOPIC` edits it, and `yolo finish TOPIC` removes the
   entry. **`yolo resume TOPIC [config flags]` also updates the overlay** —
   because resume restarts the container, flags passed to it both apply now and
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
and `ports` (`_CONCAT_DESTS`), which **concatenate** across the layers and then
the CLI values (those lists accumulate; everything else replaces).

Keys mirror the flag names (dashes or underscores both accepted). Supported:
`config-dir`, `dockerfile`, `auth` (one of `keychain`/`oauth-token`/`bedrock` —
validated against `AUTH_CHOICES` in `_parse_yolo_dict`, since `set_defaults`
bypasses argparse's `choices` check), `aws-profile`, `aws-region`,
`bedrock-model`, `claude-json`,
`ssh-agent`, `base`, `prompts` (string or list of strings; the pre-0.7 name
`append-system-prompt` draws a pointed rename error),
`mounts` (string or list, `PATH[:ro|:rw]`), `ports` (string or list,
`[HOST:]CONTAINER`), `require-project-entry`, `tmux`, `tmux-session`.
Per-invocation **actions** — `--resume` and the verbs (with their `TOPIC`) — are
deliberately **not** config keys, and neither is `--dangerously-allow-home`
(CLI-only by design); any of them in a config file is a hard error (not in
`YOLO_KEYS`). `config-dir` and `dockerfile` get `~` expanded (a JSON file can't
lean on shell expansion). Booleans must be JSON `true`/`false`. A JSON **`null`** for any key
means "leave at the built-in default" (the loader skips it). Unknown keys, wrong
types, and malformed JSON all `sys.exit` naming the offending file/entry
(`_parse_yolo_dict` / `_read_projects_file` / `_read_worktrees_file`).

Every load also prints a one-line **provenance note** to stderr — e.g.
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
- **Bare `yolo config`** is **read-only**: prints the entry that currently
  applies (or "no entry for &lt;key&gt;") plus the projects.json path, and flags
  dangling keys. There is no scaffold/template behavior (and no
  `YOLO_INIT_DEFAULTS` anymore — built-in defaults live only in argparse).
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
  Contradictory instructions in one call (set + `--unset` of the same key,
  `--mount` with `--add/--remove-mount`, `-p` with `--add/--remove-prompt`,
  `--port` with `--add/--remove-port`)
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
  session named `TOPIC`; **errors if the worktree or branch already exists** (use
  `resume`). Any **explicit config flags** passed here are snapshotted into the
  worktree's `worktrees.json` overlay (see the config section), so a later
  `resume TOPIC` reuses them. *No `TOPIC`:* a fresh (unnamed) session in the
  current directory.
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
- **`finish TOPIC`** — `git worktree remove` the worktree, then **delete the
  branch iff it's merged**: if the branch is reachable from `base` (the same
  `--base`/`base` ref as `start`/`list`, default `HEAD`; via `_branch_merged`) it's
  deleted (`git branch -d`) since nothing remains to preserve, otherwise it's
  **kept** with a message that it still exists and needs to be merged or pushed
  (plus the pushed/unpushed note). Refuses if a container is running, or on
  uncommitted changes (unless `--force`). Removes the worktree's `worktrees.json`
  overlay entry. Leaves transcripts (they self-expire via `cleanupPeriodDays`).
- **`list`** — the repo's worktrees as a table (TOPIC/BRANCH/STATUS/DIRECTORY).
  STATUS is `running`/`dirty`, else `merged`/`unmerged` (idle+clean) judged by
  whether the branch is reachable from **`base`** — exactly `git branch --merged
  <base>` (default `base` is `HEAD` = the main checkout; honours
  the `base` config key/`--base`). So a fast-forward-merged or never-diverged branch reads
  `merged`; a *squash*-merge isn't reachable and reads `unmerged`. `do_list` runs
  the check in the main repo (not `git -C <worktree>`) so a `HEAD` base resolves
  to the main checkout, not the worktree's own branch.
- **`ps`** — every **running** yolo container, across **all** repos (the
  cross-repo counterpart to `list`), as a table
  (NAME/TOPIC/PORTS/CREATED/STATE) read from the `yolo.*` labels
  (`docker ps --filter label=yolo.cwd`); needs no git repo. CREATED is docker's
  own `{{.RunningFor}}` (how long ago the container was created). PORTS comes straight
  from docker ps's own column (free — no per-container `docker port` calls at the
  2s cadence), condensed by `_condense_ports` to `host->container` pairs
  (address/proto noise and the IPv6 twin dropped). STATE is read from each
  session's `<config-dir>/.yolo-status/<cwd-slug>.state` file
  (`_read_session_state`): `working <age>` (since the last `UserPromptSubmit`),
  `waiting <age>` (since the `Stop` hook fired), or `-` (no file / older
  container). Both render via `_humanize_secs`. The config dir comes from the
  `yolo.config-dir` label (falls back to `~/.claude`); no extra docker calls.
  `--watch` redraws every `PS_WATCH_INTERVAL` (2s) — that's
  the dashboard tmux mode seeds (see below), but it's an ordinary verb usable
  anywhere. Run interactively *inside tmux* (stdin a TTY + `$TMUX` set),
  `--watch` is a **picker**: j/k/arrows move, Enter `select-window`s to the
  chosen container's window, q/ESC quits; otherwise it falls back to the
  passive redraw loop.
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

- **Dispatch is two-tier** (`main`). `config` runs off the *first* `parse_args`,
  before the config files are layered in, so a broken config can't block fixing
  the config (and its sentinel re-parse needs pristine parser defaults).
  Everything else re-parses with the config defaults layered in first
  (`dockerfile`, which just prints `DEFAULT_DOCKERFILE`, and `dir`, which prints a
  path, are dispatched right after `config` — before that re-parse — since they
  need no config at all; `dir` in particular keeps its stdout free of the config
  provenance note). The other
  terminal verbs (`list`, `ps`, `tokens`, `forget-token`, `finish`, `setup-token`,
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
  (extracted from the old inline `main`): mounts (cwd + the extra `--mount`
  dirs), ssh-agent block, the credential/config blocks, labels, `--entrypoint`
  override, then hands the finished argv to `_dispatch_launch` (the run-it-here
  vs run-it-in-tmux seam — see the tmux section). It takes `container_base`,
  `command` (args after the image), optional `entrypoint`, and the resolved
  `mounts`/`ports`. For claude sessions (`entrypoint is None`) it also creates
  `<config-dir>/.yolo-status/` and **deletes the stale `<cwd-slug>.state` file**
  so a fresh session doesn't briefly show a prior one's wait time.
  `build_claude_args` builds the `claude` command (settings, built-in
  prompt, `--add-dir` per extra mount, `--continue`/`--resume`, `--name`).
- **Session-activity hooks** (`build_claude_args` + `_read_session_state`). The
  `--settings` overlay (which already disables the sandbox) also injects a
  `Stop` hook (writes `waiting <epoch>` to `/home/claude/.claude/.yolo-status/
  <cwd-slug>.state`) and a `UserPromptSubmit` hook (writes `working <epoch>`).
  The absolute container path is baked into the hook command (no reliance on a
  `docker run -e` var reaching the hook subprocess). `--settings` *replaces* the
  whole `hooks` key (only `permissions` merges across scopes), so
  `_read_settings_hooks(config_dir, home)` reads the mounted
  `settings.json`/`settings.local.json` hooks and `build_claude_args`
  concatenates yolo's groups onto them (preserving the user's; enterprise-managed
  settings aren't covered). `ps` reads the state file (see below); the schema is
  the matcher-group-wrapped `{"hooks":{"Stop":[{"hooks":[{"type":"command",...}]}]}}`
  (matcher omitted — ignored for these events). The status file lives under the
  config dir because that's the only host-writable bind mount reachable from
  inside the container (`~/.claude-yolo` is deliberately never mounted).
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
  `start`, `list`, and `finish`), `--new` (resume, worktree-only), `--force` (finish),
  `--resume`/`-r` (resume), `--watch` (ps), `--print`/`-n` (browse), and the
  `config` family — `--init`, `--global`, `--unset`,
  `--add-mount`/`--remove-mount`, `--add-prompt`/`--remove-prompt`,
  `--add-port`/`--remove-port`.
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
- **`_launch_in_tmux`** ensures the session exists (`_ensure_tmux_session` —
  a fresh one is created detached with window 0 running the `yolo ps --watch`
  dashboard, re-invoked via `_self_invocation`: sys.argv[0] resolved through
  `which()` and absolutized, since the tmux server's PATH/cwd differ), creates
  the window (`new-window -n <container-name> -P -F '#{window_id}'`), then
  focuses it — inside tmux (`$TMUX` set) by `select-window` + `switch-client`
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
- **The terminal title is turned on for yolo-created sessions** (`set-titles on`
  + `set-titles-string "yolo · #S · #W"`), so the OS window/tab title reflects
  the focused session+window (`#W` = the container name). tmux's `set-titles` is
  off by default, so otherwise the title just keeps whatever it was before
  attaching. These options are set **only when yolo creates the session**
  (`_ensure_tmux_session` returns early when it already exists), so a
  pre-existing session — including a personal one targeted via
  `--tmux-session` — is never reconfigured.
- The window command is `shlex.join(run_cmd)` (the argv contains `--settings`
  JSON and the OAuth token — quoting is load-bearing) wrapped by
  `_tmux_window_command`: on **nonzero** exit it prints the code and waits for
  Enter, because tmux's default remain-on-exit off would otherwise vaporize the
  window before a fast `docker run` failure can be read. Clean exits still
  close the window. The same wrapper guards the dashboard window (a bad
  self-invocation can't kill the just-created session).
- **Everything interactive happens before tmux**: credential prompts
  (`ensure_oauth_token` consent/mint, `ensure_logged_in`) run in the invoking
  terminal — only the finished `docker run` argv moves into the window. The
  terminal verbs never touch tmux.
- Caveat (accepted for now): the window command string — including
  `CLAUDE_CODE_OAUTH_TOKEN` — is retained in tmux server state
  (`#{pane_start_command}`). Not a new exposure class (the exec'd argv is
  visible in `ps` for the container's lifetime either way); an `--env-file`
  hardening would fix both and is a possible follow-up.
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
  session too; cross-session adds `switch-client`) and the picker keeps
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
is named via `claude --name TOPIC`. Durability is the point: commits land in the
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

## Conventions / gotchas

- **macOS only as written; Docker Desktop or OrbStack as the engine.** Credential
  extraction uses the macOS `security` CLI. SSH agent forwarding (off by default,
  enabled with `--ssh-agent`) mounts the Docker engine's
  `/run/host-services/ssh-auth.sock` (the VM-side socket the engine proxies to
  the host agent — both Docker Desktop and OrbStack expose it at that path), NOT
  the raw host `$SSH_AUTH_SOCK` — that socket's listener lives in the macOS kernel
  and is unreachable from the container's Linux VM (the mounted inode is dead:
  `connect()` → ECONNREFUSED). The host must have a running ssh-agent for
  forwarding to work. The engine socket is mounted `srw-rw---- root:root`, so the
  in-container `claude` user (uid = host uid, a non-root gid) can't `connect()` to
  it by default — `connect()` needs write perm on the socket inode, and the user
  is neither owner nor in group 0. Fix: `useradd -G root` puts `claude` in group 0,
  granting the socket's group-rw. No real privilege added (the user already has
  NOPASSWD sudo; the container is the sandbox).
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
- The container name is the cwd basename (or `{main_repo_name}-{TOPIC}` for a
  worktree), then suffixed with `-{config-dir-basename}` when
  `--config-dir` is set and `-{aws-profile-or-"bedrock"}` under `--auth bedrock`.
  Suffixes stack, so the axes compose in the name too.
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

Tests load `yolo.py` via `importlib` **from its file path** (not a plain
`import yolo`) so each test gets a **fresh module instance** — `main()` mutates
the module-global `PARSER` through `set_defaults`, so isolation matters; loading
from the path also pins the tests to the source file regardless of any installed
`yolo`. They
never touch the host or Docker: `tests/conftest.py`'s `run_cli` fixture stubs
`build_docker_image`, `ensure_logged_in`, `extract_credentials`,
`ensure_oauth_token`, `git_identity_args`, and `os.execvp`, then asserts on the
captured `docker run` argv. `test_config.py` covers config parsing/merging
(`~/.yolo.json` + `projects.json`), mount-spec parsing, the stale-state
warnings, the `dockerfile` config key (parse + `config`-verb persist/validate),
and the `config` verb; `test_cli.py` covers verb dispatch and arg
assembly across the credential/config axes, extra mounts, the guardrails, the
`--dockerfile` override (content-addressed tag, the `HOST_UID` build-arg, the
missing-path error, the relative-vs-absolute path resolution against the session
cwd, and that the build context contains only the Dockerfile), the
`FROM ${YOLO_BASE}` layering (`_build_image` builds the base then the custom image
and folds the base tag into the final tag; `_verify_image_user` rejects a non-
`claude` image), and the `dockerfile` dump verb. The
tests locate the built image in the assembled argv by its
`claude-yolo:` repo prefix (the tag is now content-addressed, not a fixed constant).
`test_verbs.py` covers the worktree verbs against a
**real throwaway git repo** (so the actual `git worktree` machinery runs),
stubbing only `running_container_for` (docker) plus the `run_cli` side effects.
`test_worktree_config.py` covers the per-worktree overlay (also against a real
repo): `start` populating `worktrees.json` from explicit flags (and the empty
`{}`), `resume`/`shell` consuming it with project<overlay<CLI precedence and
concat-key accumulation, `resume` flags *updating* the overlay (lists accumulate
+ dedup, scalars override, no-flags no-op, persistence to the next resume) while
`shell` doesn't, the provenance tail, `yolo config TOPIC` show/edit (and the
`--global`/`--init`/missing-worktree errors, and a relative `--dockerfile`
validated against the worktree dir rather than the cwd), `finish` removing the
entry, and a malformed-file error.
`test_tokens.py` covers the token registry, the `_keychain_mdat` parsing and
expiry warning, the implicit-mint consent prompt, and the `tokens` /
`forget-token` verbs (the `security`-wrapping helpers stubbed).
`test_tmux.py` covers tmux mode end-to-end against an in-memory fake tmux
server patched in at the `_tmux` seam (session creation + dashboard seeding,
window command quoting, inside-vs-outside `$TMUX` focusing, the
already-attached-client no-mirror guard, window reuse, the config keys, the
terminal-title options set only on a yolo-created session, and the pinned
window names), the `ps` verb's table from canned `docker ps` output, and the
`--watch` picker loop via scripted `wait_key` events (selection movement and
clamping, Enter→select-window, cross-session switch-client, selection
surviving a refresh, orphan marking, the picker-vs-passive dispatch).
`test_ports.py` covers the `--port`/`ports` axis (spec parsing, launch
assembly + the `yolo.ports` label + the 0.0.0.0 prompt line, layer
concatenation, the `config` port edits) and the `browse` verb (the docker
queries stubbed at `running_container_for`/`_container_label`/`_docker_port`
and the `_open_url` seam).
`test_status.py` covers the session-activity feature: the injected
`Stop`/`UserPromptSubmit` hooks in the assembled `--settings` (schema + baked
status path), the `yolo.config-dir` label, the stale-status-file reset (and that
`shell` does neither), the user-hook merge (`_read_settings_hooks` +
concatenation), and the `_humanize_secs`/`_read_session_state` rendering; the
`ps` STATE column itself is exercised in `test_tmux.py`.
Keep them green when changing flags or mounts.
