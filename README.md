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

The script is a self-contained Python script with no runtime dependencies beyond
the standard library. You can install it with `uv` (see below) or just run the
file directly.

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

- **macOS.** Credential extraction reads from the macOS keychain via the
  `security` CLI.

- **Claude Code** on the host computer. Although Claude code sessions are run
  within a Docker container which has Claude Code installed, two of the main
  authentication methods require running `claude` on the host to either create
  an Oauth key or to log in to Claude.

- **A Docker engine** The obvious choices are either the classic [Docker
  Desktop](https://www.docker.com) or the new hotness,
  [OrbStack](https://orbstack.dev). The `docker` command line tools `yolo`
  depends on will use whichever one you are running.

- **[uv](https://docs.astral.sh/uv/)** installed. The script's shebang is
  `#!/usr/bin/env -S uv run --script`, so it self-runs under `uv`, which
  guarantees a Python ≥3.10 (it's still stdlib-only — uv just picks the
  interpreter, since macOS's system `python3` is often too old).

## Limitations

Although `yolo` is designed to run on a Mac, the containers it launches are
Linux (Ubuntu). So despite running on a Mac, it's not much good for *Mac*
development: inside the container Claude has no access to Xcode, Swift
toolchains, macOS frameworks, Apple's simulators, or any other Mac-specific
tooling — it can edit the source files in the mounted directory, but it can't
build or run anything that needs macOS. It's best suited to projects whose
toolchain runs on Linux: web apps, servers, CLI tools, libraries, and the like.

## Installation

The preferred way to install `yolo` is with `uv tool install` which builds it
into an isolated venv and puts a `yolo` executable on your PATH, with zero
runtime dependencies:

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
```

As a shorthand a bare `yolo` is the same as `yolo start`. `resume` continues the
most recent session (`-r` picks a specific one, opening Claude's interactive
picker when given no ID). `shell` joins the **running** container for this
directory if there is one — handy while a session works in another terminal —
and otherwise starts a fresh throwaway container; either way the prompt is
flagged so you know where you are (`yolo:<dir>$`).

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
yolo finish something                   # remove the worktree; delete the branch if merged
yolo finish something --finish-action merge   # ...or merge the branch into HEAD, then delete it
yolo finish something --finish-action push    # ...or push it to a remote, keep it locally
yolo list                               # show this repo's worktrees
yolo dir something                      # print its directory: cd "$(yolo dir something)"
```

Verb details:

- **`start TOPIC`** creates the worktree on a new branch `TOPIC`, branched off
  `HEAD` by default (change with `--base REF`, e.g. `--base origin/main`, or
  the `base` config key), and launches a fresh session named `TOPIC`. It errors
  if the topic already exists — use `resume`.

- **`resume TOPIC`** continues that worktree's most recent session (`-r` for a
  specific one); `--new` starts a fresh named session there instead.

- **`finish TOPIC`** refuses if a container is still running or if there are
  uncommitted changes (override with `--force`). What it does with the branch
  after removing the worktree is set by
  [`--finish-action`](#finish-action---finish-action-mode-default-delete-if-merged)
  (default: delete it if merged, else keep it).

- **`list`** shows TOPIC / BRANCH / STATUS / DIRECTORY, where STATUS is
  `running`, `dirty` (uncommitted changes), or — when idle and clean —
  `merged`/`unmerged` depending on whether the branch is already contained in
  the base branch (`git branch --merged` semantics, so `merged` means it's
  ready to `finish`; a squash-merge reads as `unmerged`).

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
```

What `--tmux` does on each launch:

- Makes sure the shared tmux session exists, creating it detached if not. A
  fresh session gets a **dashboard** as window 0: `yolo ps --watch`, a live
  table of every running yolo container (NAME / TOPIC / DIRECTORY / PORTS / UP /
  STATE) across all repos. The dashboard is also a **picker**: `j`/`k` or the arrow keys move
  the highlight, Enter switches to that session's window, `q` quits. A
  container with no tmux window to switch to (started outside tmux mode) is
  marked with `*`. (`ps` is an ordinary verb — useful on its own; run
  interactively inside tmux it's the picker, anywhere else `--watch` is just a
  self-refreshing table.)
- Opens a new window named after the container, running the same `docker run`
  the default mode would have exec'd. The window closes when Claude exits; a
  window whose command *fails* sticks around showing the error until you press
  Enter.
- Focuses it: outside tmux your terminal execs into `tmux attach` (so it
  becomes the tmux client, much as the default mode becomes the session);
  inside tmux your current client just switches to the new window.

If the matching container is already running — say you `yolo resume foo` twice
— yolo switches to its existing window instead of spawning a `docker run` that
would only die on the container-name conflict.

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

## Authentication modes

`--auth` (or the `auth` config key) selects one of three mutually-exclusive ways
for Claude to authenticate (default `oauth-token`). The [configuration
options](#configuration) below compose with whichever you pick.

| `--auth`                  | How it authenticates                                              | Best for                                                 |
|---------------------------|-------------------------------------------------------------------|----------------------------------------------------------|
| `oauth-token` *(default)* | A long-lived token in the `CLAUDE_CODE_OAUTH_TOKEN` env var       | Everything, including long-lived and concurrent sessions |
| `keychain`                | Mounts a snapshot of your rotating Claude.ai keychain credentials | Plans without `setup-token` (Claude Console); short sessions |
| `bedrock`                 | AWS Bedrock credentials                                           | Billing via AWS                                          |

### `oauth-token` (default)

Authenticates with a long-lived token from `claude setup-token` — a **one-year
token that is never rotated and never written back** — forwarded into the
container as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable, with no
`.credentials.json` mount. Because nothing ever rewrites it, **any number of
concurrent containers (plus the host on its own keychain login) can use it at once**
with no interference, for as long as each session runs. That's why it's the
default: there is no refresh boundary to cross, so nothing depends on when your
sessions happen to run, how long they last, or how many run at once.

The first launch per config directory has no cached token, so yolo offers to mint
one: it explains what's about to happen, asks for confirmation, then runs the
browser OAuth flow and caches the token in your keychain. After that one-time
step every launch is silent. You can also mint explicitly with
**`yolo setup-token`** (it asks nothing — running it is the consent), and if
`CLAUDE_CODE_OAUTH_TOKEN` is already set in your environment (e.g. CI), that
value is used as-is. In a non-interactive context with no cached token, yolo
exits with guidance instead of hanging on a browser flow nobody can drive.

Requires a **Pro/Max/Team/Enterprise plan** (that's what `claude setup-token`
needs); the token is scoped to inference only. If your plan doesn't support it,
set `"auth": "keychain"` in `~/.yolo.json` and read the keychain section below.

**Tokens are scoped per config directory.** Just like the keychain login
credentials, each `--config-dir` (≈ each account/profile) gets its *own*
long-lived token, rather than one global token silently authenticating as the
wrong account. `yolo` resolves the token in this order: an explicit
`CLAUDE_CODE_OAUTH_TOKEN` in your host environment wins (it's global by nature,
for CI or self-managed tokens) → else the `yolo`-managed keychain entry for the
active config directory → else (interactive launches only) offer to mint a fresh
one and cache it there. `yolo setup-token` honours `--config-dir` too, so it
caches under the same name a matching launch will read.

**Stored in the macOS keychain, extract-only.** The token is kept as a
generic-password entry in your login keychain — encrypted at rest, the same
place Claude Code stores its own credentials, never written to a dotfile. The
service name is `claude-yolo-oauth-token` for the default config directory, or
`claude-yolo-oauth-token-<hash8>` for an alternate `--config-dir`, where
`<hash8>` is the first 8 hex chars of the SHA-256 of the directory's resolved
path (the same hashing scheme the keychain login credentials use). `yolo` only
ever *reads* this entry to forward the token into the container — it never
rotates or rewrites it, so unlike the keychain login credentials there are no
rotation hazards from sharing it across sessions.

#### Tokens & revocation

Minting a year-long credential deserves some bookkeeping, so `yolo` keeps a
**registry** of every token it mints — service name, config directory, and the
exact mint timestamp — in `~/.claude-yolo/tokens.json` (metadata only; the token
itself lives in the keychain). Three things use it:

```bash
yolo setup-token    # mint+cache a token for the active config dir (re-mint when expired)
yolo forget-token   # delete the active config dir's token from the keychain
yolo tokens         # list all the tokens yolo has minted (and when)
```

- **`yolo forget-token`** deletes the active config dir's token from your
  keychain and the registry. *Forget*, not *revoke* — see below.
- At launch, yolo warns when the active token is within a week of its estimated
  expiry (so it doesn't just silently start 401ing inside containers a year from
  now); re-mint with `yolo setup-token`.

- **`yolo tokens`** lists what exists: per config dir, when it was minted, the
  estimated expiry (mint + 1 year), and whether the keychain entry is still
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

Claude Code on the host keeps its login credentials in the macOS keychain. In
the `keychain` auth mode `yolo` pulls them out with the `security` CLI into a
temporary, `chmod 600` file and bind-mounts that file to `.credentials.json`
inside the container. Thus no new tokens are created, and you don't need a plan
that allows creating long-lived tokens.

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

The keychain entry it reads is named `Claude Code-credentials` for the default
config directory, or `Claude Code-credentials-<hash8>` for an alternate
`--config-dir` — the same per-directory hashing described above, mirroring how
Claude Code itself names its keychain entries.

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
supports them: the per-config-dir credential (keychain entry or OAuth token,
hashed service names as described under [Authentication
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

### `dockerfile` (`--dockerfile PATH`)

Build the container image from your own Dockerfile instead of the built-in
default — handy when a project needs heavier or project-specific tools baked in
so Claude doesn't reinstall them in every ephemeral container. The default
Dockerfile stays inline in `yolo.py` (so the script remains a single
self-contained file); `--dockerfile` just points at different build
instructions.

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
  can't merge into), then delete it. If the merge fails (conflicts, a dirty tree,
  unrelated histories) it's aborted and the branch is kept — the worktree is gone
  but the commits live on in the branch.

- **`push`** — push the branch to a remote and keep it locally. The remote is the
  **`finish-remote`** key (`--finish-remote NAME`, default `origin`). A push
  failure keeps the branch locally too.

- **`keep`** — leave the branch alone (just clean up the worktree).

Every mode still refuses on a running container or uncommitted changes (unless
`--force`). Set it in config to make e.g. `merge` your default `finish`, or pass
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
rest of the entry alone. A bare `yolo config` is read-only: it prints the entry
that currently applies (and the path of `projects.json`) without writing
anything. `yolo config` is the only thing that writes `projects.json` — a plain
launch never does — so the file stays a deliberate, auditable record of
per-project grants.

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
extracts your login credentials from the macOS keychain into a `chmod 600` temp
file mounted into the container; `bedrock` mounts `~/.aws` read-only and sets
the Bedrock environment variables.

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
container or back on the host. (It's why you'll see a macOS-looking path like
`/Users/...` recorded as the `cwd` in a session file even though the container is
Linux — that genuinely *is* the working directory inside the container.) Mounting
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
repo, but it never gets to read the private key. (The socket the Docker engine
exposes is owned `root:root` with mode `srw-rw----`, so the container's `claude`
user is added to group 0 — root's group — to get the group-write permission that
`connect()` needs. This adds no real privilege: the user already has passwordless
`sudo`, and the container is the sandbox.)

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

The script is stdlib-only and needs nothing installed to run. For working on it,
the repo includes a [uv](https://docs.astral.sh/uv/)-managed dev setup
(`pyproject.toml`) with `ruff` and `pytest`:

```bash
uv sync                 # set up .venv with the dev tools
uv run pytest           # run the test suite (tests/)
uv run ruff check .     # lint
uv run ruff format .    # format
uv build                # build the wheel/sdist into dist/ (for publishing)
```

The tests stub out Docker, the keychain, and `os.execvp`, so they assert on the
`docker run` command the script *would* build without touching the host or
launching anything.

## Provenance

This started life as [Michal Migurski's
gist](https://gist.github.com/migurski/6d7b718b364dfa4e7c8c63cd643ede2c).
