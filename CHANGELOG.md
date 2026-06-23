# Changelog

Notable changes to claude-yolo, per tagged version. Versions are tagged
`v{version}` and tracked in `pyproject.toml`.

## Unreleased

- **The built-in system prompt tells Claude more about the yolo environment.** It
  now notes that the container is ephemeral (the bind-mounted working dir and any
  writable mounts persist; the rest is discarded), that `sudo` and package installs
  are available, and that
  `YOLO_SESSION=1` marks a yolo session — and, when no SSH agent is forwarded (the
  default), that it's working locally only with no GitHub access (so it won't
  attempt pushes/fetches that can't work); when `--ssh-agent` is on, a line says
  push/fetch do work.

## v0.20.0 — 2026-06-22

- **`yolo wip`'s `c` is now an interactive config editor.** Instead of a single
  blind "type a line of flags" prompt, `c` opens a modal editor showing the
  selected worktree's/project's current config — plus the inherited lower layers,
  read-only — where `Enter` edits a key (bool and choice values via a `j/k` picker;
  directory paths Tab-complete), `a` adds a not-yet-set key, `x` removes one, and
  `e` is still the raw-flags escape hatch. Every change runs through `yolo config`,
  so all its validation and persistence are reused unchanged; plain Enter on the
  row then launches with the saved config.

- **The WORKTREES `DIRECTORY` column drops the shared `~/.claude-yolo/worktrees/`
  prefix**, showing just `<repo-slug>/<topic>` — the part that actually
  distinguishes one worktree from another — in both `yolo list` and the `wip`
  dashboard.

- **A session that asks a question mid-turn now shows as `waiting`, not `working`.**
  The `ps`/`wip` STATE was driven only by turn-boundary hooks (`Stop` →
  waiting, `UserPromptSubmit` → working), so the `AskUserQuestion` tool — which
  blocks for your answer *without* ending the turn — left the session reading
  `working` while it was really waiting on you. yolo now also flips the state via a
  `PreToolUse`/`PostToolUse` hook matched to that tool. (Plan-mode approval still
  reads `working` — it fires no comparable hook.)

- **The built-in Dockerfiles and container prompt now live in data files** beside
  `yolo.py` (`Dockerfile.default`, `Dockerfile.custom`, `container-prompt.txt`)
  instead of inline strings. No behavior change for `uv tool install` or a repo
  symlink (the files ship in the wheel and sit beside the script); only a bare copy
  of `yolo.py` alone — never a supported install — would now miss them.

- **`--plugin-dir PATH` loads a local Claude Code plugin into every yolo session.**
  The clean way to give yolo sessions their own **skills** without those skills
  leaking into your host Claude sessions: Claude Code discovers skills only at
  fixed paths (and yolo mounts your whole `~/.claude`, so anything dropped in
  `~/.claude/skills` shows up on the host too), but a plugin loaded via
  `--plugin-dir` is session-only — present in every yolo session, never in a plain
  host session, with your regular `~/.claude/skills` still available. Package the
  skills as a local plugin kept outside `~/.claude` and point yolo at it; the path
  (a directory or `.zip`) is bind-mounted read-only and passed to claude as
  `--plugin-dir`. Repeatable; also settable as `plugin-dirs` in config (a
  concatenating list like `mounts`/`ports`/`secrets`), with `yolo config
  --add-plugin-dir`/`--remove-plugin-dir` to edit it element-wise. Unlike `mounts`,
  a plugin dir isn't also announced as an `--add-dir` working directory.

## v0.19.0 — 2026-06-21

- **`yolo wip`'s PROJECTS section is now a REPO / DIRECTORY table.** It matches the
  WORKTREES table's shape (the repo basename and its `~`-relative directory),
  instead of the old single-column path list. Every row is colored the same — REPO
  blue, DIRECTORY grey, like WORKTREES — with no `(active)`/`(recent)` markers (the
  distinction wasn't worth surfacing; `a` still registers a recently-opened
  project).

- **Sessions get clearer names.** The Claude session name (shown on the divider
  above the prompt and in `claude --resume`) is now set for every session: a cwd
  session is named after its directory (the container hostname), and a worktree
  session is named `<repo>:<topic>` instead of just `<topic>` — so it's distinct
  both from a cwd session and from the same topic in another project. (Names are
  set on a fresh session only; Claude rejects `--name` with `--continue`/`--resume`,
  so an already-running session you resume keeps its original name.)

- **`yolo wip` can set a worktree/project's config inline.** Press `c` on a
  worktree or project row, type a line of yolo flags (e.g. `--mount ~/refdocs
  --port 8000 --auth bedrock`), and they're saved to that worktree's overlay (or
  the project's `projects.json` entry) — it just runs `yolo config [TOPIC] <flags>`
  for you, so all the usual parsing and validation apply. Plain Enter then launches
  with the saved config (and it persists for next time).

- **`yolo diff TOPIC` shows a worktree branch's diff against its base.** A
  three-dot `git diff base...branch` (PR-style — what the branch adds since it
  diverged, the same `↑ahead` `list`'s COMMITS column counts), paged as usual.
  It's read-only, so it works while a session is running. With `--stat` it opens an
  *interactive* `git diff --stat` instead: navigate the changed files and press
  Enter/Space to open the selected file's diff in a new tmux window (`q` closes).
  In `yolo wip`, `d` on a worktree row (or a worktree's session row) opens that
  interactive stat picker in a new tmux window.

- **`yolo wip` can open a session in any directory.** The PROJECTS section ends
  with a `+` row; `Enter` on it prompts for a directory — with shell-style Tab
  completion (fills the common prefix and lists matches) and `~` expansion — and
  starts a fresh session there, so you can launch in a directory that isn't a
  registered or recently-opened project yet. The `a` (register a project) prompt
  Tab-completes the same way.

## v0.18.0 — 2026-06-20

- **`yolo wip` — a colorized, tmux-resident dashboard for managing everything
  yolo.** A full-screen dashboard (window 0 of the shared `--tmux` session, which
  it now seeds — superseding the old `ps --watch` seed; `ps`/`ps --watch` stay as
  standalone verbs) that refreshes every 2s, in three sections:

  - **Sessions** — every running yolo session in one table (SESSION / TOPIC /
    CREATED / PORTS / STATE), grouped unknown → waiting → working and, within each
    group, ordered by least-recent activity first (unknown oldest-created, waiting
    longest-idle, working longest-working). The groups read apart by color.

  - **Worktrees** — every worktree (à la `list --all`, including ones with a
    running session), with a STATUS column and a **COMMITS** column showing how far
    the branch has diverged from its base as `↓behind ↑ahead`.

  - **Projects** — the projects configured in `projects.json` *plus* ones you've
    simply opened (stamped into a new `~/.claude-yolo/recent-projects.json` and
    flagged `(recent)`), so a project shows up with no `yolo config` step.

  Navigate with `j`/`k`/arrows. `Enter` switches to a session's window, or opens a
  worktree/project — jumping to its live session window if one is running, else
  resuming (falling back to a fresh session) or starting one. `n` starts a new
  worktree in a project, `b` browses a forwarded port, `s` stops a session, `f`
  finishes, `r` rebases, `a` registers a project, `q` quits. Quick actions run
  in-process (results/errors in the footer); launches open a fresh tmux window.
  base / finish settings (for `r`/`f` and the COMMITS/STATUS columns) and a
  launched session's config all resolve from **each worktree/project's own config**
  (its `projects.json` entry + worktree overlay + global `~/.yolo.json`), live — so
  a `yolo config` edit takes effect without restarting the dashboard. Requires tmux.

- **`yolo list` gained a COMMITS column.** Each worktree row now shows how far its
  branch has diverged from `base` as `↓behind ↑ahead` (behind-first, as GitHub
  orders it), from `git rev-list --left-right --count base...branch`.

- **`yolo resume` falls back to a fresh session when there's nothing to continue.**
  A plain `resume` runs `claude --continue`, which *errors* when no prior session
  exists for the directory (never started, or expired via `cleanupPeriodDays`).
  yolo now checks host-side for a transcript first and, finding none, starts a
  fresh session (named after the worktree topic in worktree mode) instead of
  letting the error blow up inside the container.

- **`yolo finish` stops an idle session for you.** It used to refuse outright while
  a container was still running for the worktree; now it stops the session first —
  exactly as `yolo stop` would — so a quiescent session can be closed and its
  worktree cleaned up in one step. An actively `working` session is still refused
  unless you pass `--force`.

- **`yolo rebase` reliably reads a session's idle/working state.** It located the
  session-activity file from the invoking command's `--config-dir` rather than the
  container's, so a session started under a different config dir was read as
  unknown and an idle one wrongly refused without `--force`. It now reads the state
  through the container's own labels, like `stop`/`finish` do.

## v0.17.0 — 2026-06-17

- **Runs on Linux now (and Windows via WSL2), not just macOS.** The container was
  always Linux; this ports the host-side glue (credential store, clipboard,
  ssh-agent socket, temp dir) off macOS-only assumptions, gated through new
  `_HOST` helpers. Native Windows without WSL is out of scope.

- **Credentials now go through [`keyring`](https://pypi.org/project/keyring/), the
  one new runtime dependency.** The OAuth token and stored `secret`s previously
  lived in the macOS Keychain via the `security` CLI; they now use keyring — the
  macOS Keychain, Secret Service (libsecret) on Linux, or the Windows Credential
  Manager, all encrypted at rest. On a **headless** box with no keyring backend,
  yolo falls back to a `chmod 600` file store under `~/.claude-yolo/credentials`
  (force it anywhere with `YOLO_CREDENTIAL_STORE=file`). uv provisions the
  dependency automatically in both the standalone and installed run modes, so the
  single-file property is preserved. (The previously-advertised "stdlib-only / no
  runtime dependencies" property is intentionally dropped.)

  - **Seamless upgrade for existing macOS users.** A token/secret left in the
    login Keychain by a pre-keyring version is read back through `security` on
    first use and migrated into keyring — so you won't be prompted to re-mint. This
    is a temporary shim that will be removed in a later release.

- **`keychain` auth mode works on Linux.** It reads the host's rotating Claude Code
  credentials — the macOS Keychain via `security`, or the `.credentials.json` file
  Claude Code keeps on Linux.

- **`yolo tokens` / the expiry warning read the mint date from the registry.**
  keyring exposes no per-item modification date (the macOS Keychain did), so the
  estimate now comes solely from `~/.claude-yolo/tokens.json`; the old "re-minted
  outside yolo" status is gone.

- **Smaller cross-platform fixes:** `yolo browse` opens the browser via Python's
  `webbrowser` instead of macOS `open`; `secret set --clipboard` reads the system
  clipboard via `pbpaste` / `Get-Clipboard` / `wl-paste` / `xclip` / `xsel`;
  `--ssh-agent` mounts the Docker-Desktop/OrbStack VM socket on macOS/Windows but
  your own `$SSH_AUTH_SOCK` on native Linux Docker; the per-session run dir prefers
  `$XDG_RUNTIME_DIR` on Linux.

- **Version string** Drop the `g` prefix from the sha part of the version string
  because it just looked like part of the SHA.

## v0.16.0 — 2026-06-17

- **New `stop` verb.** `yolo stop` stops the running session's container in the
  current directory; `yolo stop TOPIC` stops a worktree's. The container is found
  by the same labels `shell` uses and `docker stop`ped (which also removes it,
  since containers run `--rm`); the session transcript is kept, so `yolo resume`
  still works afterward. Nothing running is a friendly no-op. A session that's
  actively **working** is refused unless you pass `--force`, so a stray `stop`
  can't cut off a running task (idle/shell/not-yet-started sessions stop freely).
  It's the counterpart to `finish` (which refuses while a container is running) —
  `stop` is how you clear that.

- **Resuming/starting a session that's already running is now handled up front**,
  instead of building an image and then failing (or silently reusing the old one).
  A container of that name already running means a live session for the
  worktree/cwd; you can't launch a second with the same name. yolo now detects
  this before the build, in both modes: **non-tmux** refuses with guidance (switch
  to the terminal it's running in, or exit it and resume again; `yolo shell` for
  another view) rather than dying on docker's raw name-conflict error; **tmux**
  switches you to the existing window (resuming a live session = going back to it)
  and **warns** that the reused container keeps the image it was started with — so
  a changed `Dockerfile.yolo` / rebuilt image won't apply until you exit and resume
  the session. This is also the fix for the confusing "it built a new image but
  launched the old container" surprise in tmux mode.

- **The assembled `docker run` command is no longer printed before launch.** It
  was long and rarely legible. Pass **`--verbose`/`-v`** to bring it back for
  debugging. (It carries no secrets — the OAuth token and any `--secret` ride a
  file mount, not the argv.)

- **A launch now warns about an unconfigured `Dockerfile.yolo`.** The custom-image
  feature is opt-in via the `dockerfile` config key, so a `Dockerfile.yolo` left
  sitting in the session dir without that key was silently ignored (yolo built
  the default image). yolo now warns when `cwd/Dockerfile.yolo` exists but no
  `dockerfile` is configured, pointing at `yolo config --dockerfile
  ./Dockerfile.yolo`.

- **Bare `yolo config` now shows the complete *effective* config**, not just the
  project entry: the global `~/.yolo.json` values that aren't overridden, merged
  with this project's entry, each line annotated with where the value comes from
  (`~/.yolo.json`, `projects.json`, or both for a concat key like `mounts`). So
  you can see at a glance what's inherited versus pinned, instead of only the
  project's own overrides.

- **`yolo finish --finish-action push` now pushes with `-u`**, so the local
  branch tracks `<remote>/<topic>`. The `push` action is for the open-a-PR flow,
  where a later bare `git push`/`git pull` on the branch should just work;
  previously it left the branch with no upstream.

## v0.15.0 — 2026-06-16

- **New `secret` verb family + `--secret` flag (`secrets` config key) — store
  arbitrary secrets in the macOS keychain and inject them into a session.**
  `yolo secret set NAME` stores a value (the value is never a CLI argument — it
  comes from stdin when piped, a hidden interactive prompt, or `--clipboard` via
  `pbpaste`), at **global scope** or, with `--project`, scoped to the current
  repo. `yolo secret list` (`--all` spans every project) and `yolo secret rm`
  round out the verbs, mirroring `tokens`/`forget-token`. Storage is the keychain
  (encrypted at rest, no plaintext secrets dotfile) plus a host-side
  `~/.claude-yolo/secrets.json` registry that's never mounted, exactly like
  `tokens.json`. A name resolves project-scope first, then global.

  Injection is opt-in per the host-side `secrets` config key / repeatable
  `--secret` flag (a concat list like `mounts`/`ports`) — a stored secret is
  injected only where a config layer names it, the same trust model as
  `--yolorc`/`--dockerfile`. Each spec is `NAME[:TARGET][!]`: bare `NAME` → env
  var `NAME`; `NAME:ENVNAME` → renamed env var; `NAME:/path` or `NAME:~/path` →
  a file bind-mounted at that container path (`~` is the *container* home,
  `/home/claude`). A trailing `!` on an env target makes it ephemeral (deleted
  right after it's read). **No secret value ever reaches the docker-run argv** —
  and so not `docker inspect`'s `Config.Env`, host `ps`, or tmux's retained pane
  command: env-target secrets transit a private `/run/secrets` file mount read
  by a baked loader (sourced from the claude launch wrapper and `.bashrc`),
  file-target secrets a read-only bind mount.

- **The Anthropic OAuth token no longer rides the docker command line.** In the
  default `oauth-token` auth mode, `CLAUDE_CODE_OAUTH_TOKEN` was passed with
  `-e`, which put it on the host docker-run argv (yolo even printed it), in
  `docker inspect`'s `Config.Env`, and in tmux's retained pane command. It now
  reuses the same `/run/secrets` file transport as `--secret`, so it's off all
  three. (It still appears in claude's in-container process environment — inherent
  to delivering it as an env var, and inside the session's own trust boundary.) A
  side effect: every `oauth-token` claude launch is now started through the bash
  wrapper that sources the secrets loader.

- **Temp-file cleanup hardening.** yolo previously left a credential file in
  `$TMPDIR` on every launch (`extract_credentials`/`_masking_credfile` used
  `NamedTemporaryFile(delete=False)` and `os.execvp` left no chance to unlink
  it). Staged credential and secret files now live in a per-session run dir
  under `$TMPDIR/claude-yolo-run/<container>/` (mode 700, files chmod 600 from
  creation) — `$TMPDIR` because the macOS per-user temp dir is excluded from
  Time Machine and synced folders. A GC at the start of each launch removes only
  run dirs whose container is no longer in `docker ps`, so it's parallel-safe
  (never touches a concurrently-running session's files) and crash-proof.

## v0.14.0 — 2026-06-16

- **New `--yolorc PATH` flag (`yolorc` config key, default unset)** sources a
  shell file *inside* the container before the session starts — for per-session
  setup that keeps secrets out of Claude's transcript, e.g. `gh auth login
  --with-token < tokenfile`. Path resolution mirrors `--dockerfile`: a relative
  path resolves against the session working dir (the worktree dir in worktree
  mode), an absolute path (incl. `~`) is used as-is for an out-of-tree rc the
  container can't edit. The file is bind-mounted read-only at
  `/home/claude/.yolorc` with `YOLO_RC` pointed at it; a claude launch is
  command-wrapped to `. "$YOLO_RC"; exec claude …` (claude isn't a shell), while
  `yolo shell` sources it via the baked `.bashrc`. `source` (not run), so the
  rc's `export`s reach the session env; a nonzero rc warns but doesn't block.
  Opt-in by design (a key, not presence-detection): a repo's `.yolorc` is inert
  unless this key points at it, so cloning-and-running can't auto-execute it.

- **`--mount` now accepts a file, not just a directory.** The previous
  directory-only check was really an existence guard (docker auto-creates a
  missing bind-mount source as a root-owned dir); a file satisfies it equally.
  Mounted directories are still forwarded to claude as `--add-dir`; a mounted
  file is bind-mounted but not (—`--add-dir` is dir-only). This makes mounting a
  single token file for `--yolorc` work directly.

## v0.13.0 — 2026-06-16

- **New `--submodules` flag (`submodules` config key, default off)** populates a
  session's git submodules — `git submodule update --init --recursive`, run
  host-side just before launch — so they're checked out in the bind-mounted
  working dir before Claude starts. Neither `git merge` nor `git worktree add`
  checks out submodule contents, so without this you'd populate them by hand
  inside each container. Run host-side on purpose: it needs the host's git
  credentials and network. git (2.53, tested) gives each worktree its own
  submodule git dir and clones fresh from the remote rather than reusing a
  sibling worktree's or the shared `.git/modules` objects, so populating
  generally fetches; the host has the creds/network for that, whereas an
  in-container clone of a private submodule would fail with the ssh-agent off.
  A no-op when there's no `.gitmodules`, and best-effort (a failure warns but
  doesn't block the session).

- **`yolo finish` now handles worktrees containing submodules.** git refuses to
  `git worktree remove` a tree with populated submodules ("working trees
  containing submodules cannot be moved or removed" — an unconditional check that
  `--force` doesn't bypass), which left such worktrees un-finishable. `finish`
  now falls back to the documented manual removal (delete the directory, then
  `git worktree prune`) on that specific error, after its usual dirty-tree guard;
  any other git failure is still surfaced verbatim.

- **`yolo list --all`** lists every worktree under
  `~/.claude-yolo/worktrees` across all repos (with a leading REPO column), not
  just the current repo's — the cross-repo counterpart to a plain `list`. The
  `merged`/`unmerged` status is judged in each worktree's own repo (its branch
  and `--base` only resolve there).

- **`yolo list` drops the BRANCH column.** yolo names a worktree's branch the
  same as its topic, so the column was almost always redundant; the branch is
  now folded into TOPIC as `topic (branch: X)` *only* when the worktree has a
  different branch checked out (switched inside the container).

- **Every container now exports `YOLO_SESSION=1`**, a deterministic marker that
  code running inside (Claude, hooks, scripts) is in a yolo container. Covers
  claude sessions and `yolo shell` (a `docker exec`-ed shell inherits it too).
  Unlike `YOLO_PS1` (a presentation var for the bash prompt), this is the
  semantic "am I in a yolo session?" flag.

- **New `yolo rebase TOPIC` verb** rebases a worktree's branch onto `--base`
  (the same ref as `start`/`list`/`finish`, default `HEAD`), replaying the
  branch's commits on top of work that landed on the base since it branched —
  i.e. `git rebase main` from the branch. `base` is resolved to a commit in the
  main checkout first (so a `HEAD` base means the main repo's tip, not the
  worktree's own branch). It refuses on uncommitted changes (`git rebase` needs
  a clean tree). A *running* container is handled by session activity rather
  than refused outright (unlike `finish`, which removes the worktree): the
  hooks' `.yolo-status` state file is consulted, and an idle (`waiting`) session
  is rebased through, while an active (`working`) or unknown-state one is
  refused unless invoked with `--force`. A rebase that hits conflicts is left
  in-progress in the worktree to `git rebase --continue` or `git rebase
  --abort`.

- **`yolo finish` branch handling is now configurable** via `--finish-action`
  (config key `finish-action`, default `delete-if-merged` — the prior behavior).
  Four modes: **`delete-if-merged`** deletes the branch iff it's reachable from
  `--base`, else keeps it with the merged/pushed note; **`merge`** merges the
  branch into the current checkout (HEAD of the main repo, not `base`) and then
  deletes it, aborting and keeping the branch on a merge failure; **`push`**
  pushes the branch to a remote (**`--finish-remote`**, config key
  `finish-remote`, default `origin`) and keeps it locally; **`keep`** leaves the
  branch alone. All modes still remove the worktree (and its overlay entry) and
  refuse on a running container or uncommitted changes (unless `--force`).

## v0.12.0 — 2026-06-15

- **Base image bumped to Ubuntu 26.04 LTS** (from 24.04). Almost nothing in the
  built-in Dockerfile depends on the distro version — Node comes from NodeSource
  (a codename-independent `nodistro` repo), `uv`/`uvx` from the astral image, and
  Claude from the native installer — so the bump just keeps the base current with
  a longer support window for tools Claude installs on demand. The
  content-addressed image tag changes with the Dockerfile text, so the new base
  builds as a distinct image automatically.

- **`--dockerfile` relative paths now resolve against the session's working
  directory.** A relative path — the common per-project case, a Dockerfile
  committed in the repo — is now resolved against the worktree dir in worktree
  mode (else the cwd), so the same checked-in `./Dockerfile.yolo` works in the
  main checkout and in every worktree, and a topical worktree can carry its own
  that differs from the others'. An absolute path (including a `~` path) is used
  as-is, for a generic image kept in a central collection. This makes
  `yolo config TOPIC --dockerfile ./Dockerfile.yolo` work even when run from the
  main repo: the path is validated (and later built) against the worktree's copy,
  not the directory you ran the command from. Previously a relative path was
  resolved against the launch directory at both validation and build time, so a
  worktree-local Dockerfile couldn't be referenced.

- **`yolo finish` now deletes a merged branch.** Previously `finish` always kept
  the branch. It now checks whether the branch is reachable from `base` (the same
  `--base`/`base` ref `start` and `list` use, default `HEAD`): if it's merged (or
  never diverged), the branch is deleted along with the worktree, since there's
  nothing left to preserve; if it's *not* merged, the branch is kept and `finish`
  says it still exists and needs to be merged or pushed (with the same
  pushed/unpushed note as before).

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
  by `yolo finish TOPIC`. **`yolo resume TOPIC [config flags]` also updates the
  overlay** — since the container restarts, flags passed to resume both apply now
  and persist (lists like `mounts`/`ports` accumulate, scalars override), so you
  can add a mount or port to an existing worktree session on the fly. Stored
  host-side in `~/.claude-yolo/worktrees.json`, keyed by worktree path — a sibling
  of the `worktrees/` dir, so (like `projects.json`) it's never mounted into a
  container and can safely grant host access.

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
