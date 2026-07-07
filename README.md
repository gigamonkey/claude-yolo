# claude-yolo

This tool exists to allow relatively safe use of Claude Code in “yolo mode”,
i.e. with `--dangerously-skip-permissions`. In particular it runs Claude Code in
a Docker container that mounts just what is needed to work on a project either
in the current directory or in a git worktree.

Within the container Claude can install packages, run commands, and edit files
unattended, but the only part of your host it can touch is either the directory
you launch it from or the worktree directory plus explicitly configured other
directories and a few specific directories Claude Code needs to work. Everything
else stays on the other side of the container wall.

It's a small Python tool — `yolo.py` plus a couple of sibling data files (the
built-in Dockerfiles and the container system prompt) — whose one runtime
dependency, [`keyring`](https://pypi.org/project/keyring/), is provisioned by
`uv` automatically. You install it with `uv` (see below); it also works
symlinked onto your `PATH`. It runs on **macOS and Linux**, and on **Windows
under WSL2** (which presents as Linux); native Windows without WSL is out of
scope.

## What the container does and doesn't protect

Note that running in a container only protects against _certain_ bad outcomes,
thus “relatively safe” above.

**What it's for:** the container keeps Claude from touching files on your host
outside the directories that are explicitly mounted into it. Claude can trash
its own container — install packages, edit anything, `rm -rf` the wrong thing —
and when the container exits, all of that evaporates except for changes to the
mounted directories. It also keeps Claude from inadvertantly reading data that
don’t want it to see and thus put into its chat history and send to Anthropic.

**Container escape is theoretically possible.** A Docker container is not a hard
security boundary the way a VM is—containers share the host kernel (or, on
macOS, the Docker/OrbStack Linux VM's kernel), and kernel or runtime
vulnerabilities that allow escapes do surface from time to time. Since Claude in
yolo mode runs arbitrary code by design, a sufficiently motivated (or
sufficiently confused) agent could in principle write or run code that exploits
one. This tool makes no attempt to harden against that beyond Docker's defaults;
it raises the bar from “any shell command touches your host” to “you need a
container escape”, which is a big practical improvement but not a guarantee.

**The container does nothing to constrain credentials you give Claude.** This is
the more important limitation in practice. If you hand Claude a credential that
gives it access to an external resource, running in a container doesn’t limit
what Claude can do with that credential any more than your laptop does. That
applies to:

- **`--ssh-agent`** — the agent will sign challenges for anything Claude asks,
  so Claude can authenticate to *any* host your keys can reach, not just
  GitHub. (This is why it's off by default.)

- **Mounted directories containing credentials** — mounting `~/.aws` (as
  `--auth bedrock` does), a directory with a `.env` file, service-account
  keys, kubeconfigs, etc. gives Claude full use of whatever those credentials
  can do.

- **Credentials pasted into a session** — an API key or password you paste
  into the conversation is one Claude can use, container or no container.
- **Injected secrets (`--secret`)** — a stored secret you inject into a
  session is one Claude can read and use. The credential store buys
  encrypted-at-rest storage and keeps the value off disk and out of the docker
  command line; it does *not* sandbox the value once it's in the container. Inject
  only what the session needs. See [`secrets`](#secrets---secret-nametarget-repeatable).
- And of course the Anthropic credentials that every mode forwards, which
  Claude needs to run at all.

The container has network access (it has to, to talk to the Anthropic API), so
“can use the credential” means “can use it against the real service.” Scope what
you hand over accordingly: prefer read-only mounts, narrowly-scoped tokens, and
leaving `--ssh-agent` off unless a project actually needs Claude to push.

**Custom Dockerfiles don't widen any of this.** If you point yolo at your own
Dockerfile with `--dockerfile` — even one sitting in the project directory where
Claude could edit it — the worst it can do is change *what's inside the
container*, not *what the container can reach on your host*: a Dockerfile can't
add host mounts and can't copy host files into the image. See
[`dockerfile`](#dockerfile---dockerfile-path) for the full reasoning.

## Requirements

- **macOS or Linux host** (or **Windows via WSL2**, which presents as Linux).
  Native Windows without WSL is out of scope. Credentials are stored via
  [`keyring`](https://pypi.org/project/keyring/) — the macOS Keychain, Secret
  Service (libsecret) on Linux, or the Windows Credential Manager — falling back
  to a `chmod 600` file store under `~/.claude-yolo/credentials` on a headless
  box with no keyring backend.

- **Claude Code** on the host computer. Although Claude code sessions are run
  within a Docker container which has Claude Code installed, two of the main
  authentication methods require running `claude` on the host to either create
  an Oauth key or to log in to Claude.

- **A Docker engine** The obvious choices are either the classic [Docker
  Desktop](https://www.docker.com) or the new hotness,
  [OrbStack](https://orbstack.dev) — and on a Linux host, the native Docker
  Engine. The `docker` command line tools `yolo` depends on will use whichever
  one you are running.

- **[uv](https://docs.astral.sh/uv/)** installed. The script's shebang is
  `#!/usr/bin/env -S uv run --script`, so it self-runs under `uv`, which
  guarantees a Python ≥3.10 (often newer than the system `python3`) and
  provisions its one dependency, `keyring`.

## Limitations

The containers `yolo` launches are always Linux (Ubuntu), regardless of host. So
on a Mac it's not much good for *Mac* development: inside the container Claude has
no access to Xcode, Swift toolchains, macOS frameworks, Apple's simulators, or any
other Mac-specific tooling — it can edit the source files in the mounted directory,
but it can't build or run anything that needs macOS. It's best suited to projects
whose toolchain runs on Linux: web apps, servers, CLI tools, libraries, and the
like.

## Installation

The preferred way to install `yolo` is with `uv tool install` which builds it
into an isolated venv and puts a `yolo` executable on your PATH (resolving its
`keyring` dependency into that venv):

```bash
uv tool install git+https://github.com/gigamonkey/claude-yolo  # from the repo
uv tool upgrade claude-yolo                                    # later, to update
uv tool update-shell                                           # add yolo to your $PATH
```

Or just run the bundled **`./install-from-git`** script, which wraps that
`uv tool install` (re-run it any time to update; pass a tag/branch to pin one).

You can also run it without installing using `uvx`:

```bash
uvx --from git+https://github.com/gigamonkey/claude-yolo yolo`
```

Alternatively, the file self-runs under `uv` via its PEP 723 header, so you can
skip the build entirely and just symlink it from somewhere in your path. This is
probably only useful if you are working on `yolo` itself.

```bash
chmod +x yolo.py
ln -s "$PWD/yolo.py" ~/.local/bin/yolo   # ~/.local/bin is on PATH if you use uv
```

Or you can just run `./yolo.py` directly. Either way, `yolo --version` confirms
what you've got.

## Usage

There are two modes for using `yolo`: current working directory and worktree.

In **current working directory** mode, it mounts the directory where you ran
`yolo` into the container. That means changes made by Claude are immediately
reflected back onto you host computer. This is sometimes convenient but does run
the risk of exposing files to Claude that aren't checked into git. It can
scribble over or delete untracked files and there's nothing you can do about it
and if there is any sensitive data anywhere under the current directory, it has
access to it.

In **worktree** mode, `yolo` creates a git worktree and then mounts the worktree
directory (plus the shared `.git` directory) into the container. In this mode
Claude can only see what has been checked into git and if it runs completely
amok, you can just throw away the worktree and its branch and all you lost was
some tokens. All work done in a worktree session is reflected in the worktree
directory which `yolo` creates for you under `~/.claude-yolo/worktrees` and in
the branch tied to the worktree. So when you are done you can merge the branch
or push it to Github to make a PR or whatever your workflow calls for.

One thing to know before your first launch: the default authentication mode
needs a long-lived OAuth token, so the very first run (per Claude config) will
explain that and ask before minting one — see
[Authentication modes](#authentication-modes).

### Current working directory mode

The main subcommands that `yolo` understands are verbs for managing and
interacting with `yolo` sessions. Run them from the directory you want Claude to
work in; that directory becomes the container's working directory and is the
only host path Claude can modify.

```bash
yolo start                             # launch a session in the current directory
yolo resume                            # resume the latest session in the current directory
yolo resume -r [SESSION_ID]            # resume a specific session (or pick from a list)
yolo shell                             # open a bash shell in this dir's container
yolo stop                              # stop the running session in this directory
```

As a shorthand a bare `yolo` is the same as `yolo start`. A fresh cwd session is
named after the directory (the name shown above Claude's prompt and in
`claude --resume`), mirroring a worktree's `<repo>:<topic>`. `resume` continues the
most recent session (`-r` picks a specific one, opening Claude's interactive
picker when given no ID); if there's nothing to continue it just starts a fresh
session. `shell` joins the **running** container for this
directory if there is one — handy while a session works in another terminal —
and otherwise starts a fresh throwaway container; either way the prompt is
flagged so you know where you are (`yolo:<dir>$`). `stop` stops the running
container for this directory (or a worktree's, with a name); the session
transcript is kept, so you can `yolo resume` it later. `stop` won't cut off a
session that's actively working unless you pass `--force` (an idle session, or
one you've only opened a `yolo shell` into, stops without complaint).

### Worktree mode

For worktree mode use the same verbs followed by a worktree name. The `finish`
command requires a worktree name and cleans up the worktree for you. What
happens to the branch is controlled by [`finish-action`](#finish-action---finish-action-mode-default-delete-if-merged)
— by default it's deleted if it's already merged and kept otherwise. And the
`list` command, run in a directory, shows the worktrees associated with that
repo, i.e. the worktrees started via `yolo start <name>`.

```bash
yolo start something                    # new worktree+branch, launch a session
yolo resume something                   # re-enter it, continue the session
yolo shell something                    # open a bash shell in its container
yolo stop something                     # stop its running session (transcript kept)
yolo rebase something                   # rebase its branch onto --base (e.g. main's new commits)
yolo merge something                    # merge its branch into --base, keep the worktree + branch
yolo diff something                     # git diff its branch against --base (PR-style three-dot)
yolo finish something                   # remove the worktree; delete the branch if merged
yolo finish something --finish-action merge   # ...or merge the branch into HEAD, then delete it
yolo finish something --finish-action push    # ...or push it to a remote, keep it locally
yolo list                               # show this repo's worktrees
yolo dir something                      # print its directory: cd "$(yolo dir something)"
```

Verb details:

- **`start TOPIC`** creates the worktree on a new branch `TOPIC`, branched off
  `HEAD` by default (change with `--base REF`, e.g. `--base origin/main`, or
  the `base` config key), and launches a fresh session named `<repo>:<TOPIC>` (the
  name shown above Claude's prompt and in `claude --resume`; the repo prefix keeps
  it distinct from the same topic in another project). It errors
  if the topic already exists — use `resume`.

- **`resume TOPIC`** continues that worktree's most recent session (`-r` for a
  specific one); `--new` starts a fresh named session there instead. If there's no
  session to continue (none was ever started, or it expired), it quietly starts a
  fresh one rather than erroring.

- **`rebase TOPIC`** rebases the worktree's branch onto `--base` (default
  `HEAD`, the same ref `start` branches off and `finish`/`list` judge against),
  replaying the branch's commits on top of work that landed on the base since it
  branched — exactly like running `git rebase main` from the branch. It refuses
  if the worktree has uncommitted changes (`git rebase` needs a clean tree). A
  *running* container is handled by session activity, not refused outright: if
  the session is idle (`waiting`, per `yolo ps`) the rebase goes through; if it's
  actively `working` (or can't be confirmed idle) it's refused unless you pass
  `--force`. A rebase that hits conflicts is left in-progress in the worktree for
  you to `git rebase --continue` or `git rebase --abort`.

- **`merge TOPIC`** merges the worktree's branch into `--base` (default `HEAD`)
  but **leaves the worktree and branch in place** — the difference from `finish
  --finish-action merge`, which merges and then removes them. Use it to fold a
  branch's work into your mainline while you keep iterating on the branch. The
  merge lands in the main checkout, so `--base` must be what it currently has
  checked out (`HEAD` always is; a base naming the checked-out branch resolves to
  the same commit) — a base that isn't checked out is refused rather than merged
  into the wrong place. A conflict is aborted cleanly and the branch is kept, so
  nothing is left half-merged. Unlike `rebase`, a running session in the worktree
  isn't a hazard (the merge only reads the branch's committed tip), so there's no
  idle guard.

- **`diff TOPIC`** shows `git diff --base...branch` for the worktree — a
  three-dot diff that shows what the branch *adds* since it diverged from `--base`
  (default `HEAD`), the PR-style review diff (so a commit the base made on its own
  doesn't show up as a deletion). git pages it as usual. It's read-only — no
  container, no locks — so it works while a session is running. With **`--stat`**
  it instead opens an interactive `git diff --stat`: arrow/`j`/`k` to move,
  Enter/Space to open the selected file's diff in a new tmux window, `q` to close
  (this is what `yolo wip`'s `d` uses, so it needs tmux).

- **`finish TOPIC`** stops a running container for you first — as `yolo stop`
  would — so a quiescent session can be closed and cleaned up in one step; an
  actively `working` session is refused unless you pass `--force`. It also
  refuses if there are uncommitted changes (override with `--force`). What it
  does with the branch after removing the worktree is set by
  [`--finish-action`](#finish-action---finish-action-mode-default-delete-if-merged)
  (default: delete it if merged, else keep it).

- **`list`** shows TOPIC / STATUS / COMMITS / DIRECTORY. STATUS is `running`,
  `dirty` (uncommitted changes), or — when idle and clean — `merged`/`unmerged`
  depending on whether the branch is already contained in the base branch (`git
  branch --merged` semantics, so `merged` means it's ready to `finish`; a
  squash-merge reads as `unmerged`), or `orphaned` when git can't resolve the
  worktree's main repo at all (it was moved or deleted — `list` then prints a hint
  to run `git worktree repair`). COMMITS shows how far the branch has diverged
  from its base as `↓behind ↑ahead`. The branch name is folded into TOPIC and shown
  only when it differs from the topic — e.g. if someone switched branches inside
  the container.

- **`dir [TOPIC]`** prints a session's directory — the worktree's root with a
  `TOPIC` (it errors if that worktree doesn't exist), or the current directory
  without one — and nothing else, so it composes in `cd "$(yolo dir TOPIC)"`.

Because the worktree directory **and** the repo's shared `.git` are both
mounted, **nothing is lost when the container exits**: commits land in the
shared `.git` immediately and uncommitted edits are on host disk. The containers
themselves are disposable (`docker run --rm`); `start`/`resume` just launch a
fresh one each time. And you can run several worktree sessions on the same repo
at once (`yolo start fix-auth` in one terminal, `yolo start refactor-db` in
another) without them stepping on each other.

There are also three token-management verbs — `setup-token`, `tokens`, and
`forget-token` — described under [Authentication modes](#authentication-modes),
and a `config` verb described under [Configuration](#configuration).

### Port forwarding and `yolo browse`

If the project runs a server you want to reach from a host browser, configure
which container port(s) it uses and let `yolo` handle the host side:

```bash
yolo config --add-port 8000       # this project's dev server listens on 8000
yolo start                        # ...every launch now forwards it
yolo browse                       # open the browser at this session's server
yolo browse fix-auth              # ...or at a worktree session's server
```

For each configured port, `yolo` publishes it with a **docker-assigned host
port**, bound to `127.0.0.1` (never the LAN). Letting docker pick the host port
is what makes parallel sessions work: `yolo start fix-auth` and `yolo start
refactor-db` can both run the dev server on container port 8000 without
fighting over host port 8000. The cost is that the host port differs per
session — which is exactly what `browse` absorbs: it looks up the running
session's container (by worktree name, or the current directory), asks docker
which host port was assigned, prints the URL, and opens it. `yolo ps` also
shows every session's port mappings, so the dashboard doubles as the "which
session is on which port" map.

Details:

- A session with several forwarded ports opens the first-configured one;
  `yolo browse --port 3000` picks another. `--print`/`-n` prints the URL
  without opening a browser.
- If you run only one session at a time and want a stable, bookmarkable port,
  pin the host side: `--port 8000:8000` (`HOST:CONTAINER`). A second
  concurrent session then fails at launch with address-in-use, as it must.
- The server inside the container has to listen on **`0.0.0.0`**, not
  `127.0.0.1` — docker's forward can't reach a loopback-bound server. Many dev
  servers default to loopback; `yolo` tells Claude this in the system prompt
  whenever ports are forwarded, so servers it starts should just work.
- Port mappings are fixed at container launch (docker can't add one to a
  running container), so after configuring a port, exit the session and
  `yolo resume`.

See the [`ports` config key](#ports---port-hostcontainer-repeatable) for the
config details.

### tmux mode

By default every `yolo` session takes over the terminal you launched it from,
so several parallel sessions mean several terminal windows. **tmux mode**
(`--tmux`, or `tmux: true` in config) instead collects them all in one place: a
shared tmux session (named `yolo` by default) where each `yolo` session — and
each `yolo shell` — is its own tmux window, so you switch between them with
tmux keys (`prefix n`, `prefix <number>`, `prefix w`) instead of hunting for
windows on your desktop.

```bash
yolo start --tmux                 # this session becomes a window of tmux session "yolo"
yolo start fix-auth --tmux        # so does this one, alongside it
yolo config --global --tmux       # make tmux mode the default everywhere
yolo ps                           # list running yolo containers, across all repos
yolo ps --watch                   # ...refreshing every 2 seconds
yolo wip                          # open the dashboard for managing everything
```

What `--tmux` does on each launch:

- Makes sure the shared tmux session exists, creating it detached if not. A
  fresh session gets the **`wip` dashboard** as window 0 (see [The `wip`
  dashboard](#the-wip-dashboard) below) — your home base for switching between,
  launching, and tidying up sessions.
- Opens a new window named after the container, running the same `docker run`
  the default mode would have exec'd. The window closes when Claude exits; a
  window whose command *fails* sticks around showing the error until you press
  Enter.
- Focuses it: outside tmux your terminal execs into `tmux attach` (so it
  becomes the tmux client, much as the default mode becomes the session);
  inside tmux your current client just switches to the new window.
- Binds **`prefix y`** ("y" for yolo — unbound in stock tmux) to jump to the
  `wip` dashboard window. The dashboard starts as window 0, but windows move
  and renumber, so the binding selects it *by name* and always lands on it. If
  you've bound `prefix y` to something of your own, yolo leaves your binding
  alone.

If the matching container is already running — say you `yolo resume foo` twice
— you can't launch a second one with the same name, so yolo handles it up front
rather than failing on docker's name conflict. In tmux it **switches to the
existing window** (resuming a live session just means going back to it) and warns
that the running container keeps the image it was started with — so if you
changed its `Dockerfile.yolo`, the new image won't apply until you exit that
session and resume it again. Without tmux there's no window to switch to (it's a
live session in another terminal), so yolo refuses with a short message: switch
to that terminal, or exit the session and resume again (or use `yolo shell` for a
second view into it).

The **STATE** column tells you which sessions need you: `working 12s` while
Claude is busy (time since your last prompt), or `waiting 5m` once it has
finished responding and is sitting at the prompt (time since it stopped). This is driven by Claude Code **hooks** that yolo
injects into each session — a `Stop` hook records when Claude finishes, a
`UserPromptSubmit` hook records when you reply — so it reflects the real
conversation state, not container CPU. A session that hasn't interacted yet (or
one started by an older yolo) shows `-`. Injecting these hooks has one
implication for your own hooks — see **Session-state hooks** under [Notes and
gotchas](#notes-and-gotchas).

Everything that *isn't* a session launch (`list`, `ps`, `config`, `finish`, the
token verbs, and interactive credential prompts) stays in the terminal you ran
`yolo` from. The session name is configurable with `--tmux-session NAME` / the
`tmux-session` config key — one global session is the point, but a per-project
entry can group sessions per project instead. tmux mode needs `tmux` installed
on the host (`brew install tmux`); `--no-tmux` overrides a config-file default
for one run.

### The `wip` dashboard

`yolo wip` opens a full-screen, color-coded, tmux-resident dashboard for managing
*everything* yolo — it's the window-0 dashboard a `--tmux` session opens onto, and
you can jump to it any time with `yolo wip` (it ensures the shared session exists
and focuses the dashboard window) or, from inside the tmux session, with
**`prefix y`** (which finds the dashboard by name, wherever it sits). It refreshes every 2 seconds like `ps --watch`,
in three sections:

- **Sessions** — every running yolo session across all repos, in one table
  (SESSION / TOPIC / CREATED / PORTS / STATE). Rows are grouped by state — unknown
  (a `yolo shell`, or a session that hasn't taken a turn) first, then **waiting**,
  then **working** — and within each group sorted by least-recent activity first,
  so reading top to bottom runs from "longest idle / least recently touched" toward
  "busy right now". The groups are told apart by color rather than blank lines.
- **Worktrees** — every worktree across all repos (a la `yolo list --all`),
  *including* ones with a running session (which also appear up in Sessions). The
  **COMMITS** column shows how far each branch has diverged from its base as
  `↓behind ↑ahead`.
- **Projects** — a REPO / DIRECTORY table of the projects registered in
  `projects.json` *plus* any you've simply opened (yolo remembers those), so a
  project shows up here without a `yolo config` step. The section ends with a `+`
  row for opening a session in any other directory (see `Enter` below).

Navigate with `j`/`k` or the arrow keys; the footer shows the keys that apply to
the selected row:

| Key     | On…                              | Does |
|---------|----------------------------------|------|
| `Enter` | a running session                | switch to its tmux window |
| `Enter` | a worktree                       | switch to its live session window if it's running, else resume it in a new window |
| `Enter` | a project                        | switch to its live session window if running, else open a session there (resuming, or fresh if there's nothing to continue) |
| `Enter` | the `+` row                      | prompt for a directory and start a session there — Tab completes the path (fills the common prefix, lists matches), `~` works like a shell |
| `N`     | a worktree or project            | start a **fresh** session here (not a resume of the latest) — `start` for a project, `resume TOPIC --new` for a worktree; refuses if one is already running (Enter switches to it) |
| `R`     | a worktree or project            | open Claude's interactive session picker (`resume -r`) in a new window, to resume a session **other** than the most recent |
| `n`     | a project                        | prompt for a topic, start a new worktree session there |
| `c`     | a worktree or project            | open an interactive editor of that worktree's/project's config — shows the current values (plus the inherited lower layers, read-only), `Enter` edits a key (bool/choice pickers; Tab-completed paths), `a` adds a key, `x` removes one, `e` for a raw-flags line; plain Enter on the row then launches with the saved config |
| `S`     | a running session                | open a bash shell in its container (`docker exec`) in a new tmux window |
| `b`     | a session with forwarded ports   | `browse` the port (prompts if there's more than one) |
| `s`     | a running session                | stop it (confirms; an active session needs a second confirm) |
| `f`     | a worktree / idle session        | finish it (stops an idle session first, then removes the worktree) |
| `r`     | a worktree / idle session        | rebase its branch onto its base |
| `m`     | a worktree (or worktree session) | merge its branch into its base, keeping the worktree and branch (confirms) |
| `d`     | a worktree (or worktree session) | open an interactive `git diff --stat` in a new window; Enter/Space on a file there opens that file's diff in another window (`q` closes) |
| `a`     | a project (or anything)          | register a project (the selected recent one, else prompts for a path — Tab-completes like the `+` row) |
| `q`     | anything                         | quit the dashboard — only once **no sessions are running** (the footer explains the refusal otherwise; stop them with `s` first). If the dashboard window is nonetheless gone while the tmux session lives on, `yolo wip` respawns it. |

`f` and `r` won't interrupt an actively `working` session: applied to a worktree
(or an idle `waiting` session) they go through, but a worktree whose session is
busy refuses in the footer — stop it with `s` first. This is the same
not-interrupt-active-work stance `yolo stop`/`yolo rebase` take on the command
line. Each worktree's base and finish settings come from **its own repo's config**
(that repo's `projects.json` entry + worktree overlay + global `~/.yolo.json`),
resolved live — so the COMMITS column, `r`, and `f` all use the right base per
repo, and editing a config takes effect without restarting the dashboard. Stops,
finishes, rebases, and project registration happen **in place** (their result, or
any error, shows in the footer); opening or starting a session **shells out** into
a new tmux window, where a fresh `yolo` resolves that project's own config.
Requires tmux. (`wip` replaced the old `ps --watch` dashboard, of which it's a
superset; `ps`/`ps --watch` remain as standalone verbs, handy outside tmux.)

## Authentication modes

`--auth` (or the `auth` config key) selects one of three mutually-exclusive ways
for Claude to authenticate (default `oauth-token`). The [configuration
options](#configuration) below compose with whichever you pick.

| `--auth`                  | How it authenticates                                              | Best for                                                 |
|---------------------------|-------------------------------------------------------------------|----------------------------------------------------------|
| `oauth-token` *(default)* | A long-lived token in the `CLAUDE_CODE_OAUTH_TOKEN` env var       | Everything, including long-lived and concurrent sessions |
| `keychain`                | Mounts a snapshot of your rotating Claude.ai login credentials    | Plans without `setup-token` (Claude Console); short sessions |
| `bedrock`                 | AWS Bedrock credentials                                           | Billing via AWS                                          |

### `oauth-token` (default)

Authenticates with a long-lived token from `claude setup-token` — a **one-year
token that is never rotated and never written back** — delivered into the
container as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable, with no
`.credentials.json` mount. Because nothing ever rewrites it, **any number of
concurrent containers (plus the host on its own login credentials) can use it at once**
with no interference, for as long as each session runs. That's why it's the
default: there is no refresh boundary to cross, so nothing depends on when your
sessions happen to run, how long they last, or how many run at once.

The token is **not** put on the `docker run` command line (`-e
CLAUDE_CODE_OAUTH_TOKEN=…`). It rides the same private file transport as injected
[secrets](#secrets---secret-nametarget-repeatable) — staged in a chmod-600 file,
bind-mounted at `/run/secrets`, and exported inside the container by a small baked
loader — so the token stays out of `docker inspect`, host `ps`, and tmux's saved
pane command. (It still ends up in Claude's own process environment inside the
container, which is unavoidable and harmless — that's where Claude reads it from.)

The first launch per config directory has no cached token, so yolo offers to mint
one: it explains what's about to happen, asks for confirmation, then runs the
browser OAuth flow and caches the token in your credential store. After that
one-time step every launch is silent. You can also mint explicitly with
**`yolo setup-token`** (it asks nothing — running it is the consent), and if
`CLAUDE_CODE_OAUTH_TOKEN` is already set in your environment (e.g. CI), that
value is used as-is. In a non-interactive context with no cached token, yolo
exits with guidance instead of hanging on a browser flow nobody can drive.

Requires a **Pro/Max/Team/Enterprise plan** (that's what `claude setup-token`
needs); the token is scoped to inference only. If your plan doesn't support it,
set `"auth": "keychain"` in `~/.yolo.json` and read the keychain section below.

**Tokens are scoped per config directory.** Just like the host login
credentials, each `--config-dir` (≈ each account/profile) gets its *own*
long-lived token, rather than one global token silently authenticating as the
wrong account. `yolo` resolves the token in this order: an explicit
`CLAUDE_CODE_OAUTH_TOKEN` in your host environment wins (it's global by nature,
for CI or self-managed tokens) → else the `yolo`-managed store entry for the
active config directory → else (interactive launches only) offer to mint a fresh
one and cache it there. `yolo setup-token` honours `--config-dir` too, so it
caches under the same name a matching launch will read.

**Stored in the credential store, extract-only.** The token is kept via
[`keyring`](https://pypi.org/project/keyring/) — the macOS Keychain, Secret
Service on Linux, or the Windows Credential Manager, all encrypted at rest (or, on
a headless box with no keyring backend, a `chmod 600` file under
`~/.claude-yolo/credentials`) — never written to a dotfile in your project. The
service name is `claude-yolo-oauth-token` for the default config directory, or
`claude-yolo-oauth-token-<hash8>` for an alternate `--config-dir`, where
`<hash8>` is the first 8 hex chars of the SHA-256 of the directory's resolved
path. `yolo` only ever *reads* this entry to forward the token into the container
— it never rotates or rewrites it, so unlike the rotating login credentials there
are no rotation hazards from sharing it across sessions.

#### Tokens & revocation

Minting a year-long credential deserves some bookkeeping, so `yolo` keeps a
**registry** of every token it mints — service name, config directory, and the
exact mint timestamp — in `~/.claude-yolo/tokens.json` (metadata only; the token
itself lives in the credential store). Three things use it:

```bash
yolo setup-token    # mint+cache a token for the active config dir (re-mint when expired)
yolo forget-token   # delete the active config dir's token from the credential store
yolo tokens         # list all the tokens yolo has minted (and when)
```

- **`yolo forget-token`** deletes the active config dir's token from your
  credential store and the registry. *Forget*, not *revoke* — see below.
- At launch, yolo warns when the active token is within a week of its estimated
  expiry (so it doesn't just silently start 401ing inside containers a year from
  now); re-mint with `yolo setup-token`.

- **`yolo tokens`** lists what exists: per config dir, when it was minted, the
  estimated expiry (mint + 1 year), and whether the store entry is still
  present.

**Revocation is the weak spot, and it's outside yolo's control.** There is no
API or CLI command to revoke a `claude setup-token` token — `claude auth logout`
only clears local state
([#34198](https://github.com/anthropics/claude-code/issues/34198)), and the CLI
has no list/revoke subcommands
([#48373](https://github.com/anthropics/claude-code/issues/48373), open feature
request). The only revocation path is manual:
**<https://claude.ai/settings/claude-code>**, one trash-icon click per token
([support
article](https://support.claude.com/en/articles/10310342-how-do-i-log-out-of-all-active-sessions)).

In practice that page is rough: normal Claude Code usage mints tokens of its
own, so the list accumulates hundreds of near-identical entries with no
bulk-revoke ([#59378](https://github.com/anthropics/claude-code/issues/59378)),
and revocation has been reported to lag by days
([#43801](https://github.com/anthropics/claude-code/issues/43801)). The mint
timestamps that `yolo tokens` records are your best handle for picking yolo's
token out of that list.

For perspective: if you use Claude Code's remote-control features at all, your
account already has a long list of these tokens from routine usage. The one yolo
mints is deliberately created with your consent, recorded with a timestamp, and
stored encrypted — it will likely be the best-tracked token on the page.

### `keychain`

In the `keychain` auth mode `yolo` snapshots the credentials the *host's* Claude
Code manages into a temporary, `chmod 600` file and bind-mounts that file to
`.credentials.json` inside the container. Thus no new tokens are created, and you
don't need a plan that allows creating long-lived tokens. Where the host keeps
those credentials is OS-specific: on **macOS** it's the login Keychain, read via
the `security` CLI; on **Linux** Claude Code has no Keychain and keeps them in a
`.credentials.json` file in the config dir, which yolo reads directly.

Before extracting, yolo runs `claude auth status` on the host to confirm you're
actually logged in. If you're not, it offers to run `claude auth login` for you
(the browser OAuth flow) and re-checks before launching — so a logged-out host
gets caught up front instead of dropping you into a container that immediately
prompts for `/login`. (Logging in from inside the container but is awkward since
it can't open your browser.) It checks login status rather than token expiry on
purpose: an expired access token is refreshed automatically at runtime via the
stored refresh token, so expiry alone doesn't mean you're logged out. (This
needs a host `claude` recent enough to have the `auth` subcommand; if it's
missing, the check is skipped and `yolo` just errors out if the credential
extraction comes up empty.)

On macOS the Keychain entry it reads is named `Claude Code-credentials` for the
default config directory, or `Claude Code-credentials-<hash8>` for an alternate
`--config-dir` — the same per-directory hashing described above, mirroring how
Claude Code itself names its Keychain entries. (On Linux there's no Keychain; yolo
just reads the config dir's `.credentials.json` file.)

The catch, and the reason `keychain` is not the default auth mode is **token
rotation.** Those credentials are an access token with a fixed expiry (~8h after
the last refresh) plus a **single-use refresh token**: when the access token
expires, Claude Code refreshes it, and the refresh token *rotates* to a new one,
invalidating the old. Since `yolo` mounts a *snapshot* of the credentials into
each container, every container — and the host keychain — holds the *same* pair,
so they all share one **refresh boundary**: the moment when that access token
expires. Whetever user of the token makes the first API call after the boundary,
either a `yolo` container or the host, refreshes and wins. Every other user is
left with an expired access token and a no-longer-valid refresh token. At that
point, the best thing to do is to exit any `yolo` containers, log back in on the
host and then `yolo resume` the sessions. Which is a PITA.

Note that the problem is not concurrent sessions or the length of sessions but
**whether anything is running when the refresh boundary arrives**. A session
that starts five minutes before the access token expires will either refresh and
break other logins or get broken by someone else.

The damage also outlives the sessions. When a container wins the refresh, the
new credentials land only in that container's mounted file — nothing writes
back to the host keychain. The host is left holding the dead refresh token: the
host CLI is effectively logged out as of the boundary, and every keychain-mode
yolo session started after it snapshots the same dead credentials, until you
run `claude auth login` on the host to mint a fresh pair. (The pre-launch login
check can't catch this: login *status* can't reveal whether a refresh token is
still live without spending it.)

This is why it's not the default. Probably the only reason to use `keychain`
mode is if your plan doesn't support `setup-token` (i.e. a Claude Console
account).

### `bedrock`

In this mode we don't need to authenticate to Claude.ai but to **AWS Bedrock**
Sets `CLAUDE_CODE_USE_BEDROCK=1`, mounts `~/.aws` read-only, and skips the
keychain entirely. `--aws-profile` is optional (the AWS SDK's default
credentials are used otherwise), `--aws-region` defaults to `us-east-1`, and
`--bedrock-model` sets the model id. Composes with `--config-dir` (e.g. `--auth
bedrock --config-dir ~/.claude-bdr`).

## Configuration

Every option below can be given as a CLI flag, and most can also be stored as a
default so you don't re-type it. Configuration comes from three places, lowest
to highest precedence:

1. **`~/.yolo.json`** — global defaults, a JSON object whose keys mirror the
   flag names (dashes or underscores both work). Edit it by hand or with
   `yolo config --global`:

   ```json
   {
     "ssh-agent": true,
     "prompts": ["Prefer the standard library."]
   }
   ```

2. **`~/.claude-yolo/projects.json`** — per-project defaults, a JSON object
   mapping a project directory to the same kind of object. You don't edit this
   one by hand: the [`config` verb](#the-config-verb) below writes it. An entry
   applies to any directory at or under its key path; when several keys match,
   the most specific wins.

3. **CLI flags** — always win over both files.

Per key, a higher layer overrides a lower one, except `prompts`, `mounts`, and
`ports`, whose lists *accumulate* across all the layers. A JSON `null`
leaves a key at its built-in default.

Both files live **outside directories a session in a container can write**, and
that's deliberate. If we allowed, for instance, a `.yolo.json` to live in a
project directory then Claude could edit it and quietly grant its *next* session
more host access (an extra writable mount, say). Similarly a `.yolo.json`
committed to a repo would then affect anyone who used `yolo` in that repo.

The supported keys, each with its CLI flag:

### `auth` (`--auth MODE`)

Which of the three authentication modes to use: `oauth-token` (the default),
`keychain`, or `bedrock`. See [Authentication modes](#authentication-modes).
A common use is pinning `auth: "bedrock"` (plus the AWS keys below) on a work
project while personal projects use the default.

### `config-dir` (`--config-dir PATH`)

Which Claude Code **config directory** to use (default `~/.claude`); it's
mounted at `/home/claude/.claude` in the container, the spot Claude Code reads.

Multiple config directories are a Claude Code feature, not a `yolo` one:
pointing `CLAUDE_CONFIG_DIR` somewhere else gives you a completely separate
Claude profile — its own login (so a different account), its own settings,
history, and memory. People keep one per account (work vs. personal, or a
client's Team account), or a stripped-down profile for experiments. `yolo` just
supports them: the per-config-dir credential (the credential-store entry or OAuth
token, hashed service names as described under [Authentication
modes](#authentication-modes)) is selected to match, and — the common case — you
can tie a project to its config dir once with `yolo config --config-dir
~/.claude-work` so every launch from that project uses the right account
automatically.

Pairs naturally with `--no-claude-json` (below) when you want the alternate
profile fully isolated.

### `claude-json` (`--claude-json` / `--no-claude-json`, default on)

Whether to mount the host `~/.claude.json` — Claude Code's *global* config file
(MCP servers, project history and trust), which lives at `$HOME/.claude.json`
no matter what the config dir is. Turn it off for a cleanly isolated profile
alongside an alternate `config-dir`.

### `ssh-agent` (`--ssh-agent` / `--no-ssh-agent`, default off)

Whether to forward the host SSH agent into the container. **Off by default**, so
you opt in deliberately: forwarding the agent effectively hands your SSH keys to
Claude Code — the keys themselves never leave the host, but the agent will sign
challenges for whatever Claude asks, so it can reach *any* host your keys allow,
not just GitHub. Turn it on with `--ssh-agent` (or `ssh-agent: true` in config)
on projects where you want Claude to push to GitHub itself. With it off,
in-container git operations against GitHub won't authenticate, and `yolo` tells
Claude in the system prompt that it can't `git push` — so it will generally let
you know when something needs pushing from your host. (See also [Why forward the
SSH agent](#why-forward-the-ssh-agent)).

### `redirect-build-dirs` (`--redirect-build-dirs` / `--no-redirect-build-dirs`, default on)

In a **cwd session** the container mounts your live host checkout in place, so a
per-OS build directory on it is a hazard: your `./.venv` was built for macOS, and
the moment a container command runs `uv run` (or `cargo`, or anything that touches
`target/`/`__pycache__`) the tool rebuilds that directory **for Linux** — corrupting
the copy your host tools use, and killing any running host dev server whose process
re-execs `./.venv/bin/python`. **On by default**, `yolo` heads this off by pointing
those directories at fixed container-local paths under `/home/claude/.yolo-env/`
(`UV_PROJECT_ENVIRONMENT`, `CARGO_TARGET_DIR`, `PYTHONPYCACHEPREFIX`), so the
container builds its own and never touches the host's. It works by setting
**container env vars**, which every shell inside inherits — including Claude's Bash
tool, which sources a snapshot file rather than `~/.bashrc`, so nothing else
reliably reaches it. It applies only to cwd sessions (a worktree is an isolated
copy, so there's no host copy to protect); turn it off with
`--no-redirect-build-dirs` (or `redirect-build-dirs: false` in config) if a tool
genuinely needs the in-tree `.venv`. Note `node_modules` has no equivalent env
knob and is not redirected.

### `mounts` (`--mount PATH[:ro|:rw]`, repeatable)

Extra host directories — reference docs, a sibling repo — bind-mounted into the
container at their identical host paths. **Read-only by default**; append `:rw`
to make one writable. The directory must exist. Each mount is also passed to
Claude as `--add-dir`, so it shows up as a working directory Claude knows
about. In config, a string or list of `PATH[:ro|:rw]` specs; the lists
concatenate across the layers and the CLI (on a same-path ro/rw conflict the
higher layer wins).

### `ports` (`--port [HOST:]CONTAINER`, repeatable)

Container ports the project's server listens on, forwarded to the host — see
[Port forwarding and `yolo browse`](#port-forwarding-and-yolo-browse). A bare
container port (`"8000"`, the normal form) gets a docker-assigned host port per
session, so parallel sessions never collide; `HOST:CONTAINER` (`"8000:8000"`)
pins a stable host port for single-session use. Forwards are always bound to
`127.0.0.1` — a host *address* is deliberately not expressible here, so a config
file can't put the skip-permissions container's server on your LAN (the raw
`-- -p` passthrough is the escape hatch if you truly want that). In config, a
string or list of specs; like `mounts`, the lists concatenate across the layers
and the CLI (on a same-container-port conflict the higher layer wins).

### `secrets` (`--secret NAME[:TARGET]`, repeatable)

Stored secrets — PATs, API keys, SSH keys — injected into a session
without ever writing a plaintext secrets file or putting the value on a command
line. There are two halves: **storing** a secret (the `secret` verb) and
**injecting** it (the `secrets` config key / `--secret` flag). Storing one does
*not* inject it anywhere — injection is opt-in per project, the same trust model
as [`dockerfile`](#dockerfile---dockerfile-path) and `yolorc` (the *key* lives in
host-side config, which Claude can't edit from inside a container, so Claude can't
grant its next session a new secret).

**Storing — the `secret` verb.** Values live in the same credential store as the
OAuth token (`keyring` — the macOS Keychain, Secret Service, or Windows Credential
Manager, encrypted at rest; a `chmod 600` file under `~/.claude-yolo/credentials`
on a headless box), with a host-side `~/.claude-yolo/secrets.json` registry that
records non-secret metadata (name, scope, timestamps) and is never mounted — the
same arrangement as the OAuth-token registry.

```bash
yolo secret set GH_TOKEN              # prompts (hidden), or reads piped stdin
gh auth token | yolo secret set GH_TOKEN     # from a pipe
yolo secret set GH_TOKEN --clipboard  # from the system clipboard (pbpaste / Get-Clipboard / wl-paste / xclip / xsel)
yolo secret set DB_PASSWORD --project # scoped to this repo, not global
yolo secret list                      # global + this project's secrets
yolo secret list --all                # across every project
yolo secret rm GH_TOKEN               # delete (store + registry)
```

The value is **never passed as a command-line argument** (which would leak it
into shell history and the process list) — it comes from stdin, a hidden prompt,
or `--clipboard`. Secrets have two **storage scopes**: **global** (the default)
and **project** (`--project`, keyed to the repo root). At injection a name
resolves project-scope first, then global, so a project can override a global
secret of the same name. The NAME must be a shell identifier
(`[A-Za-z_][A-Za-z0-9_]*`) because it can become an env var name.

**Injecting — the `secrets` key.** List the secrets a session should get; each is
a spec `NAME[:TARGET]` whose TARGET picks how it's delivered:

| Spec | Delivered as |
| --- | --- |
| `GH_TOKEN` | env var `$GH_TOKEN` |
| `DB_PASSWORD:PGPASSWORD` | env var `$PGPASSWORD` (renamed) |
| `DEPLOY_KEY:~/.ssh/id_ed25519` | a file at `/home/claude/.ssh/id_ed25519` |
| `TOKEN:/etc/token` | a file at `/etc/token` |

A TARGET that starts with `/` or `~` is a **file** path (and `~` is the
*container* home, `/home/claude`, not your host home); anything else is an env
var name. A trailing `!` on an env spec (`GH_TOKEN!`) makes it **ephemeral** —
deleted the instant it's read, for the rare secret you don't want lingering in
the session's environment for a later `yolo shell` to pick up. In config it's a
string or list of specs; like `mounts`/`ports` the lists concatenate across the
layers and the CLI (on a target collision — same env name or file path — the
higher layer wins; a secret you want both ways is just two specs).

```bash
yolo --secret GH_TOKEN --secret DEPLOY_KEY:~/.ssh/id_ed25519
yolo config --add-secret GH_TOKEN     # persist for this project
```

**No secret value ever reaches the `docker run` command line** — and so never
`docker inspect`'s env, host `ps`, or tmux's saved pane command. Env-target
secrets are written to chmod-600 files in a private per-session directory
bind-mounted at `/run/secrets` and exported by a small loader yolo bakes into the
image (sourced before your `yolorc`, so an rc can use the values — e.g. `echo
"$GH_TOKEN" | gh auth login --with-token`). File-target secrets are bind-mounted
read-only at their path. Either way the staged files live in a private per-user
temp dir (`$XDG_RUNTIME_DIR` on Linux, else `$TMPDIR` — on macOS that's excluded
from Time Machine and synced folders) and are reclaimed when the container exits. (An env-target value does end up in Claude's own process
environment inside the container — unavoidable for an env var, and within the
session's trust boundary; a file target avoids even that.) The default
`oauth-token` auth mode delivers the Anthropic token through this very same
transport, for the same reason.

The credential store buys **encrypted-at-rest storage and no plaintext secrets
dotfile** — it does not make the secret invisible to Claude. Anything you inject, Claude
(and any code in the skip-permissions container) can read; that's inherent, which
is exactly why injection is opt-in per project. See also [What the container does
and doesn't protect](#what-the-container-does-and-doesnt-protect).

### `plugin-dirs` (`--plugin-dir PATH`, repeatable)

Load a **local Claude Code plugin** into the session — the clean way to give every
yolo session a set of **yolo-specific skills** without those skills showing up in
your plain host Claude sessions. Claude Code only discovers skills at fixed paths
(`~/.claude/skills/<name>`, a project's `.claude/skills/`, and plugins), with no
"extra skills directory" setting; and yolo mounts your whole `~/.claude` into the
container, so a skill dropped in `~/.claude/skills` would appear on the host too. A
plugin loaded with `--plugin-dir` sidesteps that: it's **session-only** (a host
Claude session never passes the flag, so never loads it), while your regular
`~/.claude/skills` stay available in the container untouched.

Package the skills as a local plugin kept **outside `~/.claude`** (so the host
can't discover it):

```
~/.claude-yolo/skills-plugin/
├── .claude-plugin/plugin.json      # name, description, version
└── skills/
    ├── skill-a/SKILL.md
    └── skill-b/SKILL.md
```

Then point yolo at it — once globally for "every yolo session", or per project:

```bash
yolo --plugin-dir ~/.claude-yolo/skills-plugin     # one session
yolo config --global --plugin-dir ~/.claude-yolo/skills-plugin   # every session
yolo config --add-plugin-dir ./tools-plugin        # just this project
```

The path (a directory or a `.zip`) is bind-mounted **read-only at its identical
host path** and passed to claude as `--plugin-dir`. It must exist. In config it's a
string or list; like `mounts`/`ports`/`secrets` the lists concatenate across the
global / project / worktree layers and the CLI (exact-path duplicates collapse).
Unlike `mounts`, a plugin dir is **not** also added as a `--add-dir` working
directory — it's a plugin, not a source tree.

### `clones` (`--clone URL DIR`, repeatable)

Clone a git repo into the container when the session starts — handy for giving
Claude a reference or dependency repo alongside your project without copying it
into your working tree. On the CLI it takes two arguments; in config it's a list of
`{url, dir}` objects:

```bash
yolo --clone https://github.com/me/lib ../lib              # one session
yolo config --clone https://github.com/me/lib ../lib       # persist for this project
```

```json
{ "clones": [ { "url": "https://github.com/me/lib", "dir": "../lib" } ] }
```

**`DIR` is a path inside the container**, resolved against the working directory:
absolute as-is, `~` is the container home (`/home/claude`), otherwise relative — so
`../lib` is a **sibling** of the working dir. One thing to know: only the working
directory itself is bind-mounted, so a sibling (or any path outside it) lives in the
container's **ephemeral** filesystem and is re-cloned each session — which is usually
what you want for a throwaway reference clone, and keeps it out of your actual repo.
(A path *inside* the working dir, like `vendor/lib`, would land on the bind-mount and
persist on the host.)

The clone runs at session start after secrets are loaded but **before** your
`yolorc` and Claude — so a `yolorc` that starts a server can rely on the cloned repo
already being there. It skips if the destination already exists, and a failure just
warns rather than blocking the session. Public HTTPS URLs need no auth; with
`--ssh-agent` on, GitHub HTTPS URLs route over your forwarded agent. In config the
list concatenates across the layers and the CLI.

A clone may also carry an optional **`depth`** (a positive integer), which becomes
`git clone --depth` — a shallow clone, handy for a large reference repo where you
don't need the history. There's no `--clone … DEPTH` launch flag, but you can set it
per clone in config:

```json
{ "clones": [ { "url": "https://github.com/me/lib", "dir": "../lib", "depth": 1 } ] }
```

To edit clones a piece at a time (rather than replacing the whole list with
`--clone`), use the element flags — these also let you set `depth`:

```bash
yolo config --add-clone https://github.com/me/lib ../lib 1   # url, dir, optional depth
yolo config --remove-clone ../lib                            # remove by dir
```

The `yolo wip` dashboard's `c` config editor edits clones interactively too
(prompting url, dir, and an optional depth).

### `dockerfile` (`--dockerfile PATH`)

Build the container image from your own Dockerfile instead of the built-in
default — handy when a project needs heavier or project-specific tools baked in
so Claude doesn't reinstall them in every ephemeral container. The built-in
default lives in `Dockerfile.default` (shipped alongside `yolo.py`);
`--dockerfile` just points at different build instructions.

**The recommended way: layer on yolo's default with `FROM ${YOLO_BASE}`.** The
default image already sets up a lot of load-bearing detail — the `claude` user
with your host UID, passwordless sudo, the native Claude install, the GitHub
HTTPS→SSH rewrite, the prompt, the `PATH`, and the `claude
--dangerously-skip-permissions` entrypoint. Rather than reproduce all of that,
build *on top of* it. yolo builds its default as a base image and passes its tag
in as the `YOLO_BASE` build arg, so a custom Dockerfile can be as short as:

```dockerfile
ARG YOLO_BASE
FROM ${YOLO_BASE}
RUN sudo apt-get update && sudo apt-get install -y postgresql-client
```

Everything else — the entrypoint, `PATH`, the installed `claude`, the user — is
**inherited from the base** via `FROM`; you don't repeat any of it, and you
automatically pick up improvements when yolo's default changes. The `claude` user
has passwordless sudo, so `RUN sudo …` installs as root without leaving the user.
The one rule: the container's runtime user must end up as `claude` (yolo passes
no `-u` to `docker run`, so the image's final `USER` *is* the runtime user). If
you switch to `USER root` to do work, end with `USER claude` — otherwise the
container would run as root and your edits would land on the host owned by root.
yolo checks the built image's user and refuses to launch with a clear message if
it isn't `claude`, so you can't get this subtly wrong.

Run `yolo dockerfile` to print the built-in default — a handy starting point, and
the thing to read if you want to know exactly what the base provides.

A Dockerfile that does **not** reference `YOLO_BASE` is treated as a full
replacement and built as-is (the escape hatch for "I want to start over
entirely"). In that case you own all the boilerplate above — at minimum `ARG
HOST_UID` and a `claude` user created with it — and you don't inherit future
default changes. Prefer layering unless you genuinely need a different base.

Each distinct Dockerfile gets its own content-addressed image tag
(`claude-yolo:<hash>`), so projects with different images — and parallel sessions
— never clobber each other's build.

The feature is **opt-in** — yolo only uses a custom Dockerfile when the
`dockerfile` key (or `--dockerfile`) points at one. So if you just drop a
`Dockerfile.yolo` in the project and forget to wire it up, it's silently
ignored and you get the default image. To catch that, a launch **warns** when a
`Dockerfile.yolo` is present in the session directory but no `dockerfile` config
is set, pointing you at `yolo config --dockerfile ./Dockerfile.yolo`.

**Is a custom Dockerfile safe?** Mostly yes, and it's worth understanding why,
since the file usually lives in your project directory, where Claude could edit
it between runs. The short version is that a Dockerfile changes *what's in the
container*, not *what the container can reach on your host*:

- **A Dockerfile can't add host mounts.** Bind mounts are decided by `yolo` on
  the host side when it launches the container; there is no Dockerfile
  instruction that mounts a host path (`VOLUME` only makes anonymous volumes). So
  editing the Dockerfile can't grant the next session access to any host
  directory yolo didn't already mount.

- **A Dockerfile can't copy host files into the image either.** `COPY`/`ADD` can
  only read from the *build context*, and yolo's build context is a temporary
  directory containing nothing but the Dockerfile itself — there are no host
  files there to copy. (yolo also double-checks the context holds nothing else
  before building.)

- **What a Dockerfile *can* do** is run arbitrary commands at build time and bake
  whatever it likes into the image. But build-time commands run in Docker's build
  sandbox with no access to your host filesystem and no credentials present (yolo
  passes none to the build — no `--secret`, no `--ssh`), and anything baked into
  the image only runs later *inside the container*, where Claude already runs
  arbitrary code with the same mounts and the same forwarded Anthropic token. So
  a malicious image gains nothing the running container doesn't already have. The
  practical risks are just the ordinary ones of building any untrusted Dockerfile
  (it has network at build time) plus the fact that a baked-in backdoor is
  stealthier and persists until the next rebuild.

In other words, treat a Dockerfile you didn't write with the same caution as any
third-party Dockerfile, but it doesn't widen yolo's blast radius beyond the
mounts and credentials you already chose to hand over.

### `base` (`--base REF`, default `HEAD`)

The git ref worktree branches are created from (`yolo start TOPIC`) and judged
`merged`/`unmerged` against (`yolo list`). Set it to e.g. `"origin/main"` if
your worktrees should branch from the remote rather than whatever the main
checkout is on.

### `finish-action` (`--finish-action MODE`, default `delete-if-merged`)

What `yolo finish TOPIC` does with the branch after removing the worktree. Four
modes:

- **`delete-if-merged`** (default) — delete the branch if it's already reachable
  from [`base`](#base---base-ref-default-head) (merged or never diverged), since
  nothing remains to preserve; otherwise keep it, with a note that it still needs
  to be merged or pushed.

- **`merge`** — merge the branch into the current checkout (the `HEAD` of the
  main repo, where `finish` runs — not `base`, which may be a remote ref you
  can't merge into), then remove the worktree and delete the branch. The merge
  runs *before* the worktree is removed, so if it fails (conflicts, a dirty tree,
  unrelated histories) it's aborted and nothing is removed — the worktree and its
  branch are kept intact to retry from.

- **`push`** — push the branch to a remote (with `-u`, so the local branch tracks
  it) and keep it locally. The remote is the **`finish-remote`** key
  (`--finish-remote NAME`, default `origin`). Tracking is set up because this mode
  is for the open-a-PR flow, where a later bare `git push`/`git pull` on the branch
  should just work. A push failure keeps the branch locally too.

- **`keep`** — leave the branch alone (just clean up the worktree).

Every mode first stops a running container (refusing only an actively `working`
session unless `--force`) and refuses on uncommitted changes (unless `--force`).
Set it in config to make e.g. `merge` your default `finish`, or pass
`--finish-action` for a one-off.

### `prompts` (`--prompt` / `-p`, repeatable)

Extra instructions tacked onto Claude's system prompt, on top of a built-in one
telling Claude it's in an ephemeral Ubuntu container. In config, a string or
list of strings; prompts accumulate across the layers and the CLI.

### `aws-profile`, `aws-region`, `bedrock-model` (`--aws-profile NAME`, `--aws-region REGION`, `--bedrock-model ID`)

The AWS knobs for `auth: bedrock` (see
[Authentication modes](#authentication-modes)); ignored, with a warning, under
any other auth mode. `aws-profile` is optional (SDK default credentials
otherwise) and `aws-region` defaults to `us-east-1`.

### `require-project-entry` (`--require-project-entry`, default off)

Refuse to launch unless a `projects.json` entry matches the current directory.
Because `projects.json` is keyed by directory path, **renaming or moving a
project orphans its entry** and the project would silently fall back to the
global defaults — the wrong account or profile being the real hazard. `yolo`
always warns about entries whose directory no longer exists; setting this key in
`~/.yolo.json` upgrades the fallback itself to a hard refusal to launch if the
current project is not configured. (`--no-require-project-entry` overrides it
for one run).

For a project that needs no customization, register it with an empty entry:
`yolo config --init` (see [the `config` verb](#the-config-verb)). That
satisfies the guard without pinning any config values.

### `tmux`, `tmux-session` (`--tmux` / `--no-tmux`, `--tmux-session NAME`)

Spawn sessions as windows of a shared tmux session instead of in the invoking
terminal — see [tmux mode](#tmux-mode). `tmux` is a boolean (default off);
`tmux-session` names the shared session (default `yolo`). Set `tmux: true` in
`~/.yolo.json` to live in tmux mode by default and `--no-tmux` your way out for
one run.

### CLI-only flags

A few flags are deliberately *not* config keys:

- **`--rebuild-image`** — pass `--no-cache` to `docker build`, forcing a full
  image rebuild (useful when a baked-in tool such as Claude Code itself is
  stale).

- **`--verbose` / `-v`** — print the full `docker run` command before launching.
  It's hidden by default (it's long and rarely legible); this brings it back for
  debugging. It contains no secrets — the OAuth token and any `--secret` are
  passed via a file mount, not the command line.

- **`--dangerously-allow-home`** — by default `yolo` **refuses to launch with the
  working directory at or above `$HOME`**, which would mount your whole home
  directory (including `~/.ssh` and `yolo`'s own config) read-write into a
  skip-permissions container. This flag overrides the refusal for one run; it
  cannot be set from a config file, since a standing override would quietly
  defeat the guard.

- The per-invocation actions — the verbs and `--resume`/`-r`/`--new`/`--force` —
  are also CLI-only by design.

### The `config` verb

`yolo config` manages the stored config layers, à la `git config`. Run it from
inside the project *with the flags you want to pin*:

```bash
yolo config --config-dir ~/.claude-work --mount ~/refdocs
```

Exactly those flags are saved as the project's entry, keyed by the repo root
(so subdirectory runs and worktree sessions share it; outside a git repo, the
current directory). Re-running with a flag updates just that key, leaving the
rest of the entry alone. A bare `yolo config` is read-only: it prints the
**complete effective config that would apply here** — the global `~/.yolo.json`
values that aren't overridden, merged with this project's entry — with the
source of each value, so you can see what's inherited versus pinned:

```text
$ yolo config
projects file: /Users/you/.claude-yolo/projects.json
effective config for /Users/you/hacks/foo:
  ssh-agent   true               [~/.yolo.json]
  auth        "bedrock"          [projects.json]
  mounts      ["~/refdocs", "~/proj"]  [~/.yolo.json + projects.json]
  config-dir  "~/.claude-work"   [projects.json]
```

A concat key (`mounts`/`ports`/`prompts`/`secrets`) shows the values from both
layers and is attributed to both; everything else is the winning layer's value.
It writes nothing. `yolo config` is the only thing that writes `projects.json`
— a plain launch never does — so the file stays a deliberate, auditable record
of per-project grants.

With **`--global`**, the same invocations read and write `~/.yolo.json` — the
global layer — instead of the project entry (you can also just edit that file;
it's plain JSON):

```bash
yolo config --global --ssh-agent      # set a global default
yolo config --global                  # show the global config (read-only)
```

A few editing flags go beyond whole-key sets:

- **`--unset KEY`** (repeatable) deletes a key from the entry entirely, so it
  falls back to the lower layers / built-in default — handy because a flag like
  `--ssh-agent` can only set the key true or false, not remove it. Any key
  actually present can be unset, even one yolo no longer recognizes, so a
  broken entry can be repaired without hand-editing the file.
- **`--add-mount PATH[:ro|:rw]` / `--remove-mount PATH`** (repeatable) edit
  single elements of the stored `mounts` list. `--mount` replaces the whole
  list; these leave the rest alone. `--add-mount` validates the directory
  (and updates the `:ro`/`:rw` mode if the path is already listed);
  `--remove-mount` matches by path, ignoring any mode suffix, and doesn't
  require the directory to exist — so a stale mount can always be removed.
- **`--add-prompt PROMPT` / `--remove-prompt PROMPT`** (repeatable) do the same
  for the `prompts` list (removal is by exact string match).
- **`--add-port [HOST:]CONTAINER` / `--remove-port CONTAINER`** (repeatable)
  likewise for the `ports` list. `--add-port` replaces an existing entry for
  the same container port (so a `HOST:` pin can be added or dropped);
  `--remove-port` matches by container port, ignoring any pin.

Contradictory instructions in one call — setting and `--unset`ting the same
key, or `--mount` alongside `--add-mount`/`--remove-mount` — are errors.

To register a project that needs no customization if you are using
`require-project-entry: true` use:

```bash
yolo config --init
```

This writes an *empty* entry for the project: no overrides, just "yolo knows
about this project". It errors if the project already has an entry, and it
can't be combined with other config flags (an empty entry is the point). One
subtlety: because only the most specific matching entry applies, an empty entry
created inside a directory covered by some broader entry *shadows* that entry's
config for this project — yolo warns when that happens.

## Extra `docker run` arguments

Anything after a `--` separator in the `yolo` invocation is appended to the
`docker run` command verbatim, *after* the arguments `yolo` passes. Because
Docker uses last-one-wins for repeated flags, these arguments override anything
`yolo` passes. You can use this to set parameters like `--network host` or to
change the `--memory`.

```bash
yolo -- --network host --memory 4g
```

## How it works

When you run the script, it does five things:

### 1. Builds the Docker image

It writes the built-in Dockerfile to a temp directory and builds it. The image is
Ubuntu 26.04 with `nodejs`, `npm`, `git`, `curl`, `jq`, and a handful of baked-in
amenities used across most projects — `ripgrep`, `fd` (the `fd-find` package,
symlinked to `fd`), `build-essential`, and `uv`/`uvx` — plus Claude Code installed
via the **native installer** (`curl https://claude.ai/install.sh | bash`, landing
at `~/.local/bin/claude`). You can build from your own Dockerfile instead with
`--dockerfile` (see [`dockerfile`](#dockerfile---dockerfile-path)).

The temp directory is the entire **build context**, and it holds nothing but the
Dockerfile — that's what keeps a custom Dockerfile's `COPY`/`ADD` from reaching
host files (yolo asserts the context is otherwise empty before building). The
image tag is content-addressed (`claude-yolo:<hash>` over the Dockerfile text and
your UID), so the default and any custom Dockerfile get separate images and
parallel sessions never race on a shared tag.

The image is rebuilt on every run, but Docker's layer cache makes that nearly
instant after the first time — so baked-in tools cost almost nothing per launch
and spare Claude from re-installing them inside each ephemeral container. Tools
you only need in one project are better left to on-demand `sudo apt` inside the
container than added here.

### 2. Matches your host user ID

The Dockerfile creates a `claude` user whose UID matches your host UID
(`os.getuid()`), passed in at build time as the `HOST_UID` build arg (`docker
build --build-arg HOST_UID=…`, which the Dockerfile's `ARG HOST_UID` feeds to
`useradd`). This keeps file ownership straight across
the bind mounts: anything Claude writes in the working directory lands on the
host owned by *you*, and the container can in turn read host-owned files —
including the `chmod 600` credentials file and your mounted `~/.claude` config.
(The user is also added to group 0 so it can reach the SSH agent socket — see
[below](#why-forward-the-ssh-agent).)

### 3. Sets up credentials

How depends on the `--auth` mode (see
[Authentication modes](#authentication-modes)): the default `oauth-token` mode
forwards the cached long-lived token as an environment variable; `keychain`
extracts your host login credentials (the macOS Keychain, or the Linux
`.credentials.json` file) into a `chmod 600` temp file mounted into the container;
`bedrock` mounts `~/.aws` read-only and sets the Bedrock environment variables.

### 4. Wires up the container

It assembles the `docker run` arguments:

- Bind-mounts your current directory into the container at the same path and sets
  it as the working directory.
- *If you opted in with `--ssh-agent`* (off by default): forwards your SSH agent
  socket so Claude can use your SSH keys (e.g. for `git push`) without copying any
  private keys into the container, and mounts your `~/.ssh/known_hosts` read-only
  so SSH host-key verification works.
- Forwards your git identity (`user.name`/`user.email`) so commits made in the
  container are attributed to you (see below).
- Mounts your config/credentials according to the mode (see above).
- Sets the container hostname to the project directory name, so Claude Code's
  status line shows it.

#### Why mount at the same path?

The working directory isn't mounted at a tidy container-native location like
`/workspace` — it's bind-mounted at the **exact same absolute path** it has on the
host (`-v {cwd}:{cwd}`, with `-w {cwd}`). So if you launch from
`/Users/peter/hacks/claude-yolo`, that's also the path *inside* the container, and
it's where Claude starts.

This is deliberate: it keeps paths **consistent across the container boundary**.
File references, `git`, stack traces, clickable `file:line` links, and Claude
Code's own session transcript all line up whether you read them inside the
container or back on the host. (It's why you'll see a host path like
`/Users/...` — or `/home/...` on a Linux host — recorded as the `cwd` in a session
file even though the container is Linux: that genuinely *is* the working directory
inside the container.) Mounting
at `/workspace` instead would make every recorded path mismatch the host layout.

#### Why forward the SSH agent?

This is **off by default** — forwarding the agent lets Claude authenticate as you
to *any* host your keys allow, so it's a deliberate opt-in (`--ssh-agent`, or
`ssh-agent: true` in config) for projects where you want Claude to push to GitHub
itself. When you do opt in, here's the mechanism and why it's the safe way to do
it:

Working autonomously usually means Claude needs to talk to remote services over
SSH — most commonly `git pull`/`git push` against GitHub or another host. That
requires your SSH private key. But copying a private key into a throwaway
container is exactly the kind of secret leak this tool exists to avoid.

The **SSH agent** solves this. On your host, the agent is a background process
that holds your unlocked keys in memory and exposes a Unix socket
(`$SSH_AUTH_SOCK`). Any program that wants to authenticate hands the
*challenge* to the agent over that socket; the agent signs it with the key and
hands back the *signature*. The key itself never leaves the agent.

claude-yolo bind-mounts that socket into the container and sets `SSH_AUTH_SOCK`
inside it to point at the mount. So `ssh` (and `git` over SSH) inside the
container authenticates through your host agent — Claude can push to a private
repo, but it never gets to read the private key. Which socket gets mounted
depends on the engine: on **macOS/Windows** (Docker Desktop or OrbStack) the agent
lives outside the engine's Linux VM, so yolo mounts the VM-side proxy socket the
engine exposes at `/run/host-services/ssh-auth.sock` — it's owned `root:root` with
mode `srw-rw----`, so the container's `claude` user is added to group 0 (root's
group) to get the group-write permission that `connect()` needs. (This adds no
real privilege: the user already has passwordless `sudo`, and the container is the
sandbox.) On a **native Linux Docker** host the engine shares your kernel, so yolo
mounts your own `$SSH_AUTH_SOCK` directly and `connect()` works because the
in-container user shares your host uid.

The companion `~/.ssh/known_hosts` mount just lets SSH verify the remote host's
key fingerprint, so connections don't fail or hang on an unknown-host prompt.

The image also configures git to rewrite GitHub **HTTPS** remote URLs to SSH
(`git config --system url."git@github.com:".insteadOf "https://github.com/"`), so
`git` operations against `https://github.com/...` remotes transparently route over
SSH and authenticate through the forwarded agent — **no access token ever enters
the container**. (HTTPS auth is a bearer token that would have to be handed in;
SSH is challenge-response, so the key stays on the host.)

#### Why forward the git identity?

Being able to *push* is only half of letting Claude do git work — it also needs
an identity to *commit* under. A fresh container has no git config, so a commit
would fail with `Author identity unknown`.

So the script reads your effective `git config user.name` / `user.email` on the
host (repo-local value if you have one, otherwise the global one — the same
identity a commit from the host would use) and passes them into the container as
the `GIT_AUTHOR_*` / `GIT_COMMITTER_*` environment variables. Commits made inside
the container are then attributed to you, with no extra setup.

It forwards just the identity rather than mounting your whole `~/.gitconfig` on
purpose: a mounted gitconfig would also pull in macOS-only settings — the
`osxkeychain` credential helper, GPG commit signing — that don't exist in the
Linux container and would make commits error or hang. One caveat: because these
are environment variables, they take precedence over any repo-local identity set
*inside* the container.

### 5. Launches Claude

Finally it `os.execvp`s into `docker run -it --rm`, replacing itself with the
interactive container. The container's entrypoint is
`claude --dangerously-skip-permissions`, plus a built-in system prompt telling
Claude it's running in an ephemeral Ubuntu container (and any
`--prompt` additions you configured). When you exit, `--rm`
cleans up the container.

The full `docker run` command is printed (between two dashed lines) before
launch, so you can see exactly what's happening.

## Notes and gotchas

- **The in-process sandbox is disabled on purpose** — the *container* is the
  sandbox. If your `~/.claude/settings.json` has `sandbox.enabled: true`, Claude
  would otherwise warn at startup that `bubblewrap`/`socat` are missing and run
  unsandboxed. claude-yolo suppresses that by passing
  `--settings '{"sandbox":{"enabled":false}}'` to Claude — a container-only
  override, so your host settings are untouched. Installing `bubblewrap` wouldn't
  help anyway: a default Docker container can't create unprivileged user
  namespaces, and granting that capability would weaken the very isolation this
  tool provides. (A `/doctor` sandbox note may still appear; that's expected.)

- **Session-state hooks (the `ps` STATE column).** To know whether a session is
  `working 12s` or `waiting 5m`, claude-yolo injects two Claude Code hooks into every
  session via that same `--settings` overlay: a `Stop` hook that records when
  Claude finishes responding and a `UserPromptSubmit` hook that records when you
  reply. Each writes a tiny timestamp file under `<config-dir>/.yolo-status/`,
  which `ps` reads back. Because `--settings` *replaces* the whole `hooks` key
  rather than merging it (only `permissions` merges across settings sources),
  claude-yolo reads the `hooks` from the mounted `settings.json` /
  `settings.local.json` and folds yolo's onto them so your own hooks still fire;
  hooks from *other* sources (enterprise-managed settings, or a project
  `.claude/settings.json` that isn't your config dir) are **not** carried over.
  The hook commands are plain shell and run unattended, independent of
  `--dangerously-skip-permissions`.

- **Don't switch to `npm install -g`.** The npm global install lands at
  `/usr/local/bin/claude`, which `/doctor` flags as a broken install and which
  self-update can't manage. The native installer is deliberate.

## Development

The script needs only `uv`, which provisions its one runtime dependency
(`keyring`) automatically. For working on it, the repo includes a
[uv](https://docs.astral.sh/uv/)-managed dev setup (`pyproject.toml`) with `ruff`
and `pytest`:

```bash
uv sync                 # set up .venv with the dev tools
uv run pytest           # run the test suite (tests/)
uv run ruff check .     # lint
uv run ruff format .    # format
uv build                # build the wheel/sdist into dist/ (for publishing)
```

The tests stub out Docker, the credential store, and `os.execvp` (and force the
file-based credential fallback so they never touch a real keyring), so they assert
on the `docker run` command the script *would* build without touching the host or
launching anything.

For the deep, feature-by-feature internals — how auth, secrets, the config
layering, the `wip` dashboard, tmux mode, the worktree verbs, and the run-dir GC
actually work, plus the per-file test coverage map — see
[`ARCHITECTURE.md`](ARCHITECTURE.md). `CLAUDE.md` is the lean overview loaded into
Claude Code sessions.

## Provenance

This started life as [Michal Migurski's
gist](https://gist.github.com/migurski/6d7b718b364dfa4e7c8c63cd643ede2c).
