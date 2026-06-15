# Changelog

Notable changes to claude-yolo, per tagged version. Versions are tagged
`v{version}` and tracked in `pyproject.toml`.

## UNRELEASED

- **`--tmux` mode now labels sessions clearly.** Each window's name is pinned
  (`automatic-rename`/`allow-rename` off) so the status bar keeps showing which
  container/topic it is instead of degrading to the foreground process name
  (node/python/bash), and the OS terminal title is turned on
  (`set-titles` + `set-titles-string "yolo · #S · #W"`) so the window/tab title
  reflects the focused session+window. The title options are applied only when
  yolo *creates* the tmux session, so a pre-existing or personal
  (`--tmux-session`) session is never reconfigured.
- **Per-worktree overlay config.** A worktree now carries its own config layer,
  the most specific persisted one (`~/.yolo.json` < `projects.json` entry <
  worktree overlay < CLI flags). `yolo start TOPIC [config flags]` snapshots the
  explicit flags into it, so `yolo resume TOPIC` relaunches with the same config
  (mounts, ports, auth, …) without retyping. It's editable with `yolo config
  TOPIC` (same show/set/`--add-*`/`--unset` UX as the project entry) and removed
  by `yolo finish TOPIC`. Stored host-side in `~/.claude-yolo/worktrees.json`,
  keyed by worktree path — a sibling of the `worktrees/` dir, so (like
  `projects.json`) it's never mounted into a container and can safely grant host
  access.
- **GitHub HTTPS→SSH rewrite is now conditioned on `--ssh-agent`.** Previously
  the image unconditionally rewrote `https://github.com/` remotes to
  `git@github.com:`, which — with `--ssh-agent` off (the default) — turned a
  public-repo HTTPS clone (needing no auth) into an SSH URL that couldn't
  authenticate, so the clone failed. The rewrite now rides along with the agent:
  it's applied as run-time git config (`GIT_CONFIG_*` env) only under
  `--ssh-agent`, so without an agent plain HTTPS clones of public repos work,
  and with one authenticated fetch/push still routes over SSH token-free. It's
  no longer baked into the image, so a custom `--dockerfile` image gets it too.
- **`yolo dir [TOPIC]`**: print a session's working directory and exit — the
  worktree's root with a `TOPIC` (erroring if that worktree doesn't exist), or
  the current directory without one. Only the path is written to stdout, so it
  composes in command substitution, e.g. `cd "$(yolo dir my-topic)"`.

## v0.11.0 — 2026-06-13

- **Custom container images via `--dockerfile PATH` / the `dockerfile` config
  key**: build the session image from your own Dockerfile instead of the inline
  default. The recommended shape *layers on* the default rather than replacing
  it: a Dockerfile that references `YOLO_BASE` (`ARG YOLO_BASE` / `FROM
  ${YOLO_BASE}`) gets the built-in default built first and passed in as the
  `YOLO_BASE` build arg, so your image inherits the `claude` user, sudo,
  the native Claude install, the GitHub HTTPS→SSH rewrite, and the entrypoint,
  and only adds your own steps. A Dockerfile that doesn't reference `YOLO_BASE`
  is built as-is (the full-replacement escape hatch) and must itself create the
  `claude` user via `ARG HOST_UID`. Either way the image must end on `USER
  claude` — yolo refuses to launch an image that would run as root (it passes no
  `-u`, so a root image would write host files with the wrong owner). Image tags
  are now content-addressed (`claude-yolo:<hash8>` over the Dockerfile text +
  host UID), so concurrent sessions building different Dockerfiles can't race on
  a shared tag.

- **`yolo dockerfile`**: print the built-in default Dockerfile — a starting
  point for a custom one, or just to inspect what gets built. With `--custom`
  it instead prints a ready-to-edit template that layers on the default via
  `FROM ${YOLO_BASE}`, with a marked block for your own steps and the trailing
  `USER claude` yolo requires — so `yolo dockerfile --custom > Dockerfile.yolo`
  gives you a correct custom Dockerfile to fill in.

- **`--version` self-identifies live checkouts**: a version run from a git
  checkout now appends a suffix — `+editable` when sitting exactly on the clean
  release tag (a live checkout that would otherwise be indistinguishable from a
  wheel of that tag), `+dirty` when the release tag is checked out with local
  changes, or `+g<sha>[.dirty]` for commits past the release — so an editable /
  symlinked dev install is distinguishable from a wheel of a tagged release. A
  regular wheel install (outside any git repo) reports the bare version, so a
  bare version now means *only* an installed immutable wheel. The base
  version itself is read from an *adjacent* `pyproject.toml` when one is present
  (the editable/standalone case), falling back to the recorded package metadata
  only for a wheel — so an editable install reflects the live version after a
  `bump` without needing a reinstall.

- **`yolo ps`: dropped the DIRECTORY column; renamed UP → CREATED.** The
  directory column ate a lot of width; CREATED is what the underlying
  `{{.RunningFor}}` actually reports.

## v0.10.0 — 2026-06-13

- **`yolo ps` shows session activity (new STATE column)**: each running session
  reads `working <age>` while Claude is busy (time since your last prompt), or
  `waiting <age>` (e.g. `waiting 5m`) once Claude has finished responding and is
  sitting at the prompt (time since it stopped) — so the
  cross-repo listing and the tmux dashboard tell you at a glance which sessions
  need your attention. It's driven by Claude Code **hooks** that yolo injects
  into every session (a `Stop` hook records when Claude finishes, a
  `UserPromptSubmit` hook records when you reply), each writing a small
  timestamp file under `<config-dir>/.yolo-status/` that `ps` reads back — no
  extra docker calls, so it's cheap even at the 2s `--watch` cadence. A session
  that hasn't interacted yet (or one started by an older yolo) shows `-`.

  yolo injects these via the same container-only `--settings` overlay it
  already uses to disable the in-process sandbox. Since `--settings` replaces
  the whole `hooks` key rather than merging it, yolo reads the `hooks` from your
  mounted `settings.json`/`settings.local.json` and folds its own onto them, so
  your own hooks still fire inside the container (hooks from other settings
  sources, e.g. enterprise-managed, are not carried over).

- **Fix: oauth-token sessions no longer break with `/login`.** Claude Code 2.1.x
  changed its auth precedence so that a `~/.claude/.credentials.json` file is
  preferred over the `CLAUDE_CODE_OAUTH_TOKEN` env var. Because yolo bind-mounts
  `~/.claude` read-write and a container's Claude Code writes its file-store creds
  there, a prior session could leave a stale `.credentials.json` on the host that
  the next launch mounted back in — whose dead token then shadowed the valid env
  token and forced a `/login`. oauth-token (and bedrock) sessions now overlay a
  throwaway `.credentials.json` at that path, so the env token always wins and the
  container can't write creds back to your host `~/.claude`. yolo also warns at
  launch if a `~/.claude/.credentials.json` exists on the host, since on macOS it
  never should (the Keychain is the store).

- **Image: Node 24 instead of Ubuntu's Node 18.** The baked image now installs
  Node 24 from NodeSource rather than Ubuntu 24.04's `nodejs` apt package.

## v0.9.0 — 2026-06-12

- **Port forwarding via the `ports` config key / `--port [HOST:]CONTAINER`**:
  forward the project's server port(s) into the host, always bound to
  `127.0.0.1`. With a bare container port (`--port 8000`, the normal form)
  docker assigns a free host port per session, so parallel worktree sessions
  can each run their server on the same container port without colliding;
  `HOST:CONTAINER` pins a stable host port for single-session use. Lists
  concatenate across the config layers and the CLI, like `mounts`. When ports
  are forwarded, Claude is told in the system prompt to bind servers to
  `0.0.0.0` (a loopback-bound server is unreachable through docker's forward).

- **`yolo browse [TOPIC]`**: open the host browser at a running session's
  forwarded port — the discoverability counterpart to the docker-assigned
  host ports. Finds the session's container (worktree by `TOPIC`, else the
  current directory), asks docker which host port was assigned, prints the
  URL, and opens it. `--port N` selects among several forwarded ports;
  `--print`/`-n` prints the URL without opening a browser.

- **`yolo config --add-port` / `--remove-port`**: element-wise edits of the
  stored `ports` list, mirroring `--add-mount`/`--remove-mount` (`--add-port`
  replaces a same-container-port entry, so a host pin can be flipped;
  `--remove-port` matches by container port, ignoring any `HOST:` prefix).

- **`yolo ps` (and the tmux dashboard/picker) grew a PORTS column** showing
  each session's `host->container` mappings, so the dashboard doubles as the
  which-session-is-on-which-port map.

## v0.8.3 — 2026-06-12

- **README: new Limitations section.** The containers are Linux (Ubuntu), so
  despite yolo running on a Mac it's not much good for *Mac* development —
  inside the container Claude has no access to Xcode, Swift toolchains, macOS
  frameworks, simulators, or other Mac-specific tooling.

## v0.8.2 — 2026-06-12

- **README: documented the limits of containerization.** New "What the
  container does and doesn't protect" section: the container's job is to
  contain filesystem damage to the mounted directories; container escape is
  theoretically possible (Claude runs arbitrary code, and a container is not a
  hard security boundary); and most importantly, the container does nothing to
  constrain what Claude can do with credentials you hand it — via
  `--ssh-agent`, mounted directories containing credentials, or secrets pasted
  into a session.

## v0.8.1 — 2026-06-12

- **`--tmux` no longer mirrors the session into a second terminal**: launching a
  `--tmux` session from a terminal while another terminal is already attached to
  the same tmux session used to attach a *second* client, so both terminals
  mirrored the one session (tmux clamps every client to the smallest one's size
  and shows the same window). Now yolo detects the already-attached client
  (`tmux list-clients`) and instead just switches *that* terminal to the new
  window, leaving the invoking terminal a normal shell — so the new session
  shows up where your tmux session already lives.

## v0.8.0 — 2026-06-12

- **`--tmux` mode**: instead of exec'ing the session in your terminal, each
  session becomes a *window* of one shared tmux session (default name `yolo`;
  `--tmux-session`/`tmux-session` overrides), so parallel sessions live in one
  terminal and tmux keys switch between them. Window 0 of a fresh session runs a
  live `yolo ps --watch` dashboard. Enable per-invocation with `--tmux` or
  globally/per-project with `tmux: true` in config. Window reuse: relaunching a
  session whose container is already running focuses its existing window instead
  of colliding on the container name.
- **`yolo ps`**: list every running yolo container across *all* repos (the
  cross-repo counterpart to `list`) as a table (NAME/TOPIC/DIRECTORY/UP), read
  from the `yolo.*` container labels — needs no git repo. `--watch` redraws every
  2s.
- **`ps --watch` is an interactive session picker inside tmux**: run from within
  tmux (TTY stdin + `$TMUX` set), `--watch` becomes a picker — j/k/arrows move,
  Enter `select-window`s to the chosen container's window, q/ESC quits — while the
  2s redraw cadence continues. Outside tmux it stays the passive redraw loop.
  Containers started outside tmux mode render with a `*` and Enter no-ops.

## v0.7.0 — 2026-06-11

- **`--append-system-prompt`/`-p` renamed to `--prompt`/`-p`** (**breaking**),
  with the config key `append-system-prompt` → `prompts`. This makes the prompt
  family exactly parallel to the mount family (`--prompt` → `prompts` →
  `--add-prompt`/`--remove-prompt`, like `--mount` → `mounts` →
  `--add-mount`/`--remove-mount`). A config file still using the old key draws a
  pointed rename error naming the one-call migration
  (`yolo config --unset append-system-prompt --add-prompt …`); inside the
  container the merged prompts still feed claude's own `--append-system-prompt`.
- **`--ssh-agent` now defaults to off** (**breaking-ish**): forwarding the host
  ssh-agent lets Claude authenticate as you to *any* host your keys allow, not
  just GitHub — too much standing reach to grant a skip-permissions container by
  default. Opt in per-project with `--ssh-agent` or `ssh-agent: true` in config.
  When off (the default), the built-in system prompt tells Claude it can't
  `git push`, as before. To restore the old behavior globally:
  `yolo config --global --ssh-agent`.
- **`yolo config` is now a flexible editor**, à la `git config`:
  - **`--global`** shows or updates `~/.yolo.json` (the global layer) instead of
    the project's `projects.json` entry.
  - **`--unset KEY`** drops a key entirely so lower layers / built-in defaults
    apply. Any *present* key can be unset — even one yolo no longer recognizes —
    so a broken entry can be repaired without hand-editing the file.
  - **`--add-mount`/`--remove-mount`** and **`--add-prompt`/`--remove-prompt`**
    edit single elements of the list-valued keys, unlike `--mount`/`--prompt`,
    which replace the whole list. `--remove-mount` matches by path and doesn't
    require the directory to exist, so a stale mount is always removable.
  - Contradictory instructions in one call (set + `--unset` of the same key,
    `--mount` with `--add/--remove-mount`, `-p` with `--add/--remove-prompt`)
    are errors, not silently ordered.

## v0.6.1 — 2026-06-10

- **`yolo config --init`**: register the current project in `projects.json` with
  an *empty* entry — no overrides, just enough to satisfy `require-project-entry`
  without pinning a config value you never chose. Errors if the project already
  has an entry; warns when the new (most-specific) entry shadows an ancestor
  entry's config.

## v0.6.0 — 2026-06-10

- **`oauth-token` is the new default auth mode** (**breaking-ish**): a plain
  `yolo` now authenticates with the long-lived `claude setup-token` token
  instead of mounting a snapshot of the rotating keychain credentials. The
  keychain snapshot was an attractive nuisance — its single-use refresh token
  means any session that refreshes (long-running, concurrent, or overlapping
  host use) silently invalidates every other snapshot, including the host's
  login. The setup-token is never rotated, so none of that applies. On your
  next interactive launch yolo will explain and ask before minting the 1-year
  token (browser OAuth flow; needs a Pro/Max/Team/Enterprise plan);
  non-interactive launches with no cached token exit with guidance instead.
  To keep the old behavior: `echo '{"auth": "keychain"}' > ~/.yolo.json`, or
  per-project `yolo config --auth keychain`.
- **Token registry**: tokens yolo mints are recorded (service name, config
  dir, mint timestamp) in host-side `~/.claude-yolo/tokens.json`. The mint
  timestamp matters: the claude.ai token list shows almost no per-token
  metadata, so it's the only practical handle for identifying a token there.
- **`yolo tokens`**: lists the minted tokens with config dir, mint date,
  estimated expiry (mint + 1 year), and keychain status.
- **`yolo forget-token`**: deletes the active config dir's token from the
  keychain and the registry. Named *forget* deliberately — there is no
  revocation API (no CLI command, no OAuth endpoint), so the token stays
  valid server-side until it expires; the only revocation path is manual at
  <https://claude.ai/settings/claude-code>, and the command says so.
- **Expiry warning**: launches warn when the active token is within a week of
  its estimated 1-year expiry (read from the keychain entry's modification
  date, so it works for tokens minted before the registry existed), instead
  of letting it silently start 401ing inside containers.
- README: new "Tokens & revocation" section documenting the manual-only
  revocation reality, with links to the relevant claude-code issues
  (#34198, #48373, #59378, #43801).

## v0.5.0 — 2026-06-10

- **Extra mounts**: new repeatable `--mount PATH[:ro|:rw]` flag (and `mounts`
  config key) bind-mounts additional host directories — reference docs, sibling
  repos — at their identical host paths, read-only by default. Each mount is
  also forwarded to claude as `--add-dir` so it shows up as a working
  directory. Mount lists concatenate across config layers and the CLI.
- **Config is now host-side only**: defaults live in `~/.yolo.json` (global)
  and `~/.claude-yolo/projects.json` (per-project, keyed by repo root, longest
  matching key wins). An in-directory `.yolo.json` is **no longer read** — it
  sits inside the bind-mounted tree, where Claude in a container could edit it
  to grant its next session new host access; a leftover file warns on every
  run. **Breaking**: repos relying on an in-directory `.yolo.json` (including
  one setting `config-dir`) must migrate it to one of the host-side layers.
- **`config` verb replaces `init`** (**breaking**): `yolo config <flags>`
  persists exactly the explicitly-passed config flags into the project's
  `projects.json` entry, per-key; a bare `yolo config` prints the entry that
  currently applies without writing. There is no `.yolo.json` scaffold anymore.
- **Rename detection**: every run prints a one-line config provenance note and
  warns about `projects.json` entries whose directory no longer exists (a
  moved/renamed project would otherwise silently fall back to global
  defaults). Opt-in `require-project-entry` upgrades that fallback to a hard
  error.
- **Home-directory guard**: yolo now refuses to launch with the working
  directory at or above `$HOME` (which would mount the whole home dir —
  `~/.ssh`, yolo's own config — read-write into the container). Override per
  invocation with the deliberately CLI-only `--dangerously-allow-home`.

## v0.4.0 — 2026-06-09

- Fix `setup-token` caching a truncated OAuth token: the pty `claude
  setup-token` runs under is now resized wide so the ~108-char token can't
  hard-wrap, and a scrape that still looks wrapped is rejected (falling back
  to a manual paste) instead of silently caching a token that 401s at runtime.

## v0.3.1 — 2026-06-09

- yolo shells get a yolo-flagged PS1 (`yolo:<dir>$`), with long worktree paths
  shortened to a `<repo>/<topic>` label at prompt time.
- Version bumps automated with bump-my-version (updates `pyproject.toml` and
  `uv.lock` together, commits, and tags `v{version}`).
- README: document oauth-token scoping (per config dir) and keychain storage.

## v0.3.0 — 2026-06-09

- Add `--version`, reporting the version from package metadata or the adjacent
  `pyproject.toml` (single source of truth for both install modes).

## v0.2.0 — 2026-06-09

- **Worktree workflow verbs**: `start`/`resume`/`shell`/`finish`/`list`. With a
  `TOPIC` they manage a git worktree + branch under
  `~/.claude-yolo/worktrees/<repo-slug>/`; without one, `start`/`resume`/`shell`
  act on the current directory. The old `--worktree` flag is retired, and a
  bare `yolo` is now equivalent to `yolo start`. `list` shows a
  running/dirty/merged/unmerged status per worktree, judged against `--base`.
- **Single `--auth` choice** (`keychain` [default] / `oauth-token` /
  `bedrock`) consolidating the auth flags; the config axes (`--config-dir`,
  `--claude-json`, `--ssh-agent`) compose orthogonally with it.
- **`--auth oauth-token`**: authenticate containers with a long-lived token
  from `claude setup-token` (cached in the macOS keychain, forwarded as
  `CLAUDE_CODE_OAUTH_TOKEN`), making concurrent containers safe — unlike the
  rotating keychain-credential snapshots. Auto-minting is gated on an
  interactive tty.
- Renamed the script to `yolo` and packaged it as an installable console
  command (`uv tool install` / `pipx`), plus an `install-from-git` wrapper.
- `--rebuild-image` forces a no-cache Docker image rebuild.
- A no-SSH note is added to Claude's system prompt under `--no-ssh-agent`.
- README/docs cleanups, including OrbStack as a supported engine.

## v0.1.0 — 2026-06-07

Initial packaged version (starting from Migurski's gist):

- Runs Claude Code with `--dangerously-skip-permissions` inside an ephemeral
  Ubuntu container, with the working directory bind-mounted at its identical
  host path and the in-container user matching the host UID.
- Claude Code installed via the native installer; common tooling (ripgrep, fd,
  build-essential, vim, uv) baked into the image; in-container sandbox
  disabled (the container is the sandbox).
- Credentials extracted from the macOS keychain (per `--config-dir` profile),
  with a host login pre-flight check.
- SSH-agent forwarding via the Docker engine socket, GitHub HTTPS remotes
  rewritten to SSH so no token enters the container, and host git identity
  forwarded as env vars.
- `--continue`/`--resume` for resuming sessions; `--worktree NAME` for
  parallel sessions (later superseded by the verbs).
- Flag-based CLI with `.yolo.json` config defaults and an `init` scaffold
  (both later superseded); PEP 723 self-running script under uv; dev tooling
  (pytest suite, ruff).

[Unreleased]: https://github.com/gigamonkey/claude-yolo/compare/v0.7.0...HEAD
