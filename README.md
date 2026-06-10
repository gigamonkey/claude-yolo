# claude-yolo

Run [Claude Code](https://claude.com/claude-code) in full "yolo mode"
(`--dangerously-skip-permissions`) without giving it free rein over your laptop.

The whole point is **blast-radius containment**: `yolo` launches Claude
Code inside a throwaway Docker container. Claude can install packages, run
commands, and edit files unattended — but the only part of your host it can touch
is the directory you launch it from (which is bind-mounted in). Everything else
stays on the other side of the container wall.

It's a single self-contained Python script with no runtime dependencies beyond
the standard library. You can install it as a `yolo` command (see below) or just
run the file directly — either way it pulls in zero runtime dependencies. (The
repo also carries a small uv-managed test/lint setup for working on the script;
see [Development](#development).)

## Requirements

- **macOS.** Credential extraction reads from the macOS keychain via the
  `security` CLI. By default the script also forwards your running SSH agent
  into the container (disable with `--no-ssh-agent`).
- **A Docker engine** installed and running — either
  [Docker Desktop](https://www.docker.com) or [OrbStack](https://orbstack.dev).
  Both expose the host SSH agent at `/run/host-services/ssh-auth.sock`, which is
  how agent forwarding works (see [below](#why-forward-the-ssh-agent)).
- **[uv](https://docs.astral.sh/uv/)** installed. The script's shebang is
  `#!/usr/bin/env -S uv run --script`, so it self-runs under uv, which guarantees
  a Python ≥3.10 (it's still stdlib-only — uv just picks the interpreter, since
  macOS's system `python3` is often too old).
- **Claude Code** already set up on your host. The default auth mode mints a
  long-lived OAuth token via `claude setup-token`, which needs a
  **Pro/Max/Team/Enterprise plan**; alternatives are your keychain login
  credentials (`--auth keychain`) or **AWS credentials** (`--auth bedrock`).
  See [Authentication modes](#authentication-modes).

## Install on your PATH

The script installs as a `yolo` command. Two ways, depending on whether you want
a clean managed install or a copy that tracks the repo:

**Installed (recommended)** — `uv tool install` (or `pipx install`) builds it into
an isolated venv and drops a `yolo` executable on your PATH, with zero runtime
dependencies:

```bash
uv tool install git+https://github.com/gigamonkey/claude-yolo  # from the repo
uv tool upgrade claude-yolo                                    # later, to update
uv tool update-shell						# add yolo to your $PATH
```

Or just run the bundled **`./install-from-git`** script, which wraps that
`uv tool install` (re-run it any time to update; pass a tag/branch to pin one).

You can also run it once without installing: `uvx --from
git+https://github.com/gigamonkey/claude-yolo yolo`.

**Standalone** — the file self-runs under uv via its PEP 723 header, so you can
skip the build entirely and just symlink it; a symlink (not a copy) keeps it
tracking the repo, so `git pull` updates it:

```bash
chmod +x yolo.py
ln -s "$PWD/yolo.py" ~/.local/bin/yolo   # ~/.local/bin is on PATH if you use uv
```

You can also run it in place as `./yolo.py` (handy from a checkout).

## Usage

```bash
yolo                                   # default: long-lived OAuth token (minted on first run)
yolo --config-dir ~/.claude-work       # use an alternate config directory
yolo --auth keychain                   # snapshot of your keychain login creds instead
yolo --auth bedrock --aws-profile myprofile --aws-region us-west-2  # AWS Bedrock
yolo setup-token                       # mint+cache the long-lived OAuth token explicitly
yolo tokens                            # list the tokens yolo has minted (and when)
yolo forget-token                      # delete this config dir's token from the keychain
yolo --no-ssh-agent                    # don't forward the host SSH agent
yolo --mount ~/refdocs                 # also mount ~/refdocs, read-only, at its host path
yolo --mount ~/other-repo:rw           # extra mount, writable
yolo config --mount ~/refdocs          # persist flags as this project's config, then exit
yolo config                            # show this project's config entry, then exit
yolo --version                         # print the version and exit
yolo -- --network host                 # pass extra args to `docker run`

# verbs in the current directory (no worktree):
yolo                                   # == `yolo start`: a fresh session here
yolo resume                            # continue the most recent session here
yolo resume -r [SESSION_ID]            # pick / resume a specific session here
yolo shell                             # bash shell in this dir's container (or fresh)

# the worktree workflow (see below) — the same verbs, plus a TOPIC:
yolo start fix-auth                    # new worktree+branch, launch a session
yolo resume fix-auth                   # re-enter it, continue the session
yolo shell fix-auth                    # open a bash shell in its container
yolo finish fix-auth                   # remove the worktree, keep the branch
yolo list                              # show this repo's worktrees
```

Run it from the directory you want Claude to work in. That directory becomes the
container's working directory and is the only host path Claude can modify.

How Claude authenticates is one choice via `--auth`
(`oauth-token` [default] / `keychain` / `bedrock`); `--config-dir`, `--claude-json`,
`--ssh-agent`, and `--mount` are **orthogonal flags** that combine freely with it
and each other (see
[Authentication modes](#authentication-modes) and
[Configuration options](#other-configuration-options)). The
positional arguments are an optional verb
(`config`/`start`/`resume`/`shell`/`finish`/`list`/`setup-token`/`tokens`/`forget-token`)
and its topic name.
You can also
add `--append-system-prompt "..."` (or `-p "..."`, repeatable) to tack extra
instructions onto Claude's system prompt, and set defaults for most flags in
[host-side config files](#configuring-defaults-yolojson--yolo-config).

## The verbs (and the worktree workflow)

`start`, `resume`, and `shell` take an **optional** `TOPIC`. With no `TOPIC` they
act on the **current directory** (no worktree); a bare `yolo` is just `yolo start`:

```bash
yolo            # == yolo start: a fresh session in the current directory
yolo resume     # continue the most recent session here (yolo resume -r [ID] for a specific one)
yolo shell      # a bash shell in this dir's running container, or a fresh one
```

Give them a `TOPIC` and they switch to the **worktree workflow** instead. Most work
with `claude-yolo` is meant to land on a branch you can merge or open a PR from, and
the worktree verbs make that the path of least resistance — the `TOPIC` becomes both
the git worktree and the branch name, and you run from inside a repo:

```bash
cd ~/hacks/bells
yolo start fix-auth       # new worktree + branch `fix-auth`, fresh session
# ...work, exit the container...
yolo resume fix-auth      # back into it, continuing where you left off
yolo shell fix-auth       # a bash shell in that worktree (poke around)
yolo list                 # what worktrees exist, and which are running
yolo finish fix-auth      # done — remove the worktree, keep the branch to merge/PR
```

You can run several at once (`start fix-auth` in one terminal, `start
refactor-db` in another) on the **same repo** without them stepping on each other.

- **`start [TOPIC]`** with a `TOPIC` creates a git **worktree** on a new branch
  `TOPIC`, branched off `HEAD` by default (change with `--base REF`, e.g. `--base
  origin/main`, or set `"base"` in your config), and launches a fresh session named
  `TOPIC`; it errors if that topic already exists (use `resume`). With no `TOPIC` it
  just starts a fresh session in the current directory.
- **`resume [TOPIC]`** continues the most recent session — on the worktree `TOPIC`
  (errors if it doesn't exist — use `start`) or, with no `TOPIC`, in the current
  directory. `-r [SESSION_ID]` picks a specific session (or opens the picker);
  `--new` (worktree only) starts a fresh named session instead.
- **`shell [TOPIC]`** drops you into a bash shell: into the **running** container if
  one is up for that worktree (or, with no `TOPIC`, for this directory) — handy while
  a session works in another terminal — otherwise a fresh throwaway container. The
  prompt flags where you are (`yolo:<dir>$`), with the long worktree path shortened
  to a `<repo>/<topic>` label (e.g. `yolo:claude-yolo/fix-auth$`).
- **`finish TOPIC`** (worktree only) removes the worktree but **keeps the branch** (for you to
  merge or push). It refuses if a container is still running, or if there are
  uncommitted changes (override with `--force`).
- **`list`** shows the repo's worktrees (TOPIC / BRANCH / STATUS / DIRECTORY).
  STATUS is `running`, `dirty` (uncommitted changes), or — when a worktree is
  idle and clean — `merged`/`unmerged` depending on whether its branch is already
  contained in the base branch (`git branch --merged` semantics, so `merged`
  means it's ready to `finish`). The merge target is `--base`/`base` (default
  `HEAD`, i.e. the main checkout). Squash-merges read as `unmerged`.

The worktrees live in a central spot keyed by a slug of the repo path,
`~/.claude-yolo/worktrees/<repo-slug>/<TOPIC>`, so they clutter neither the repo
nor its parent. Because the worktree directory **and** the repo's shared `.git`
are both bind-mounted in, **nothing is lost when the container exits**: commits
land in the shared `.git` immediately, and uncommitted edits are on host disk.
Containers themselves are disposable (`docker run --rm`); `start`/`resume` just
launch a fresh one each time. `finish` is the cleanup `git worktree remove` +
keeping the branch, so you no longer have to do that by hand.

## How it works

When you run the script, it does five things:

### 1. Builds the Docker image

It writes an inline Dockerfile to a temp directory and builds it. The image is
Ubuntu 24.04 with `nodejs`, `npm`, `git`, `curl`, `jq`, and a handful of baked-in
amenities used across most projects — `ripgrep`, `fd` (the `fd-find` package,
symlinked to `fd`), `build-essential`, and `uv`/`uvx` — plus Claude Code installed
via the **native installer** (`curl https://claude.ai/install.sh | bash`, landing
at `~/.local/bin/claude`).

The image is rebuilt on every run, but Docker's layer cache makes that nearly
instant after the first time — so baked-in tools cost almost nothing per launch
and spare Claude from re-installing them inside each ephemeral container. Tools
you only need in one project are better left to on-demand `sudo apt` inside the
container than added here.

### 2. Matches your user ID

The Dockerfile creates a `claude` user whose UID is substituted at build time to
match your host UID (`os.getuid()`). This keeps file ownership straight across the
bind mounts: anything Claude writes in the working directory lands on the host
owned by *you*, and the container can in turn read host-owned files — including the
`chmod 600` credentials file and your mounted `~/.claude` config. (The user is also
added to group 0 so it can reach the SSH agent socket — see
[below](#why-forward-the-ssh-agent).)

### 3. Extracts your credentials

This describes the **keychain** auth mode (`--auth keychain`); the default
`oauth-token` mode and `--auth bedrock` work differently (see
[Authentication modes](#authentication-modes)).

Claude Code keeps its OAuth credentials in the macOS keychain. The script pulls
them out with the `security` CLI into a temporary, `chmod 600` file, then
bind-mounts that file to `.credentials.json` inside the container.

Before extracting, the script runs `claude auth status` on the host to confirm
you're actually logged in. If you're not, it offers to run `claude auth login`
for you (the browser OAuth flow) and then re-checks before launching — so a
logged-out host gets caught up front instead of dropping you into a container
that immediately prompts for `/login`. It checks login status rather than just
token expiry on purpose: an expired access token is refreshed automatically at
runtime via the stored refresh token, so expiry alone doesn't mean you're logged
out. (This check is skipped in Bedrock mode, which authenticates via AWS.)

The keychain entry is named `Claude Code-credentials` for the default config, or
`Claude Code-credentials-<hash8>` for an alternate config directory, where
`<hash8>` is the first 8 hex chars of the SHA-256 of the directory's path. This
mirrors how Claude Code itself names its keychain entries.

### 4. Wires up the container

It assembles the `docker run` arguments:

- Bind-mounts your current directory into the container at the same path and sets
  it as the working directory.
- Forwards your SSH agent socket so Claude can use your SSH keys (e.g. for
  `git push`) without copying any private keys into the container.
- Mounts your `~/.ssh/known_hosts` read-only so SSH host-key verification works.
- Forwards your git identity (`user.name`/`user.email`) so commits made in the
  container are attributed to you (see below).
- Mounts your config/credentials according to the mode (see below).
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

You can turn all of this off with `--no-ssh-agent`, which drops the agent socket,
`SSH_AUTH_SOCK`, and the `known_hosts` mount. With it off, in-container git
operations against GitHub won't authenticate (since the HTTPS→SSH rewrite relies
on the agent), so use it only when you don't need network git from inside.

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
Claude it's running in an ephemeral Ubuntu container (and any `-p` prompts you
added). When you exit, `--rm` cleans up the container.

The full `docker run` command is printed (between two dashed lines) before
launch, so you can see exactly what's happening.

## Authentication modes

`--auth` selects one of three mutually-exclusive ways for Claude to authenticate
(default `oauth-token`). The [config flags](#other-configuration-options) below
compose with whichever you pick.

| `--auth` | How it authenticates | Best for |
| --- | --- | --- |
| `oauth-token` *(default)* | A long-lived token in the `CLAUDE_CODE_OAUTH_TOKEN` env var | Everything, including long-lived and concurrent sessions |
| `keychain` | Mounts a snapshot of your rotating Claude.ai keychain credentials | Plans without `setup-token`; short, solo sessions |
| `bedrock` | AWS Bedrock credentials | Billing via AWS |

### `oauth-token` (default)

Authenticates with a long-lived token from `claude setup-token` — a **one-year
token that is never rotated and never written back** — forwarded into the
container as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable, with no
`.credentials.json` mount. Because nothing ever rewrites it, **any number of
concurrent containers (plus the host on its own keychain login) can use it at once**
with no interference, for as long as each session runs. That's why it's the
default: unlike keychain mode it has no failure mode that depends on how long
your sessions run or how many you run at once.

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
credentials, each `--config-dir` (≈ each account/profile) gets its *own* long-lived
token, rather than one global token silently authenticating as the wrong account.
yolo resolves the token in this order: an explicit `CLAUDE_CODE_OAUTH_TOKEN` in your
host environment wins (it's global by nature, for CI or self-managed tokens) → else
the yolo-managed keychain entry for the active config directory → else (interactive
launches only) offer to mint a fresh one and cache it there. `yolo setup-token`
honours `--config-dir` too, so it caches under the same name a matching launch will
read.

**Stored in the macOS keychain, extract-only.** The token is kept as a
generic-password entry in your login keychain — encrypted at rest, the same place
Claude Code stores its own credentials, never written to a dotfile. The service
name is `claude-yolo-oauth-token` for the default config directory, or
`claude-yolo-oauth-token-<hash8>` for an alternate `--config-dir`, where `<hash8>`
is the first 8 hex chars of the SHA-256 of the directory's resolved path (the same
hashing scheme the keychain login credentials use). yolo only ever *reads* this
entry to forward the token into the container — it never rotates or rewrites it,
so unlike the keychain login credentials there are no rotation hazards from sharing
it across sessions.

Trade-off: unlike the SSH-agent design (where the secret never enters the
container), this *does* put a bearer token in the container's environment — but
it's a scoped, inference-only token, and no worse than the rotating snapshot
keychain mode mounts.

#### Tokens & revocation

Minting a year-long credential deserves honest bookkeeping, so yolo keeps a
**registry** of every token it mints — service name, config directory, and the
exact mint timestamp — in `~/.claude-yolo/tokens.json` (metadata only; the token
itself lives in the keychain). Three things use it:

- **`yolo tokens`** lists what exists: per config dir, when it was minted, the
  estimated expiry (mint + 1 year), and whether the keychain entry is still
  present.
- **`yolo forget-token`** deletes the active config dir's token from your
  keychain and the registry. *Forget*, not *revoke* — see below.
- At launch, yolo warns when the active token is within a week of its estimated
  expiry (so it doesn't just silently start 401ing inside containers a year from
  now); re-mint with `yolo setup-token`.

**Revocation is the weak spot, and it's outside yolo's control.** There is no
API or CLI command to revoke a `setup-token` token — `claude auth logout` only
clears local state
([#34198](https://github.com/anthropics/claude-code/issues/34198)), and the CLI
has no list/revoke subcommands
([#48373](https://github.com/anthropics/claude-code/issues/48373), open feature
request). The only revocation path is manual:
**<https://claude.ai/settings/claude-code>**, one trash-icon click per token
([support article](https://support.claude.com/en/articles/10310342-how-do-i-log-out-of-all-active-sessions)).
In practice that page is rough: normal Claude Code usage mints tokens of its own,
so the list accumulates hundreds of near-identical entries with no bulk-revoke
([#59378](https://github.com/anthropics/claude-code/issues/59378)), and
revocation has been reported to lag by days
([#43801](https://github.com/anthropics/claude-code/issues/43801)). The mint
timestamps that `yolo tokens` records are your best handle for picking yolo's
token out of that list.

For perspective: if you use Claude Code's remote-control features at all, your
account already has a long list of these tokens from routine usage. The one yolo
mints is deliberately created with your consent, recorded with a timestamp, and
stored encrypted — it will likely be the best-tracked token on the page.

### `keychain`

Extracts your Claude.ai login credentials from the macOS keychain and mounts them
as `.credentials.json` (this is the mode described under
[How it works → Extracts your credentials](#3-extracts-your-credentials)). No
token mint, no plan requirement beyond being logged in on the host.

The catch is **token rotation.** Those credentials are a short-lived access token
(~8h) plus a **single-use refresh token**: when the access token expires, Claude
Code refreshes it and the refresh token *rotates* to a new one, invalidating the
old. yolo mounts a *snapshot* of the credential into each container, so the first
party to refresh — any container, **or the host** — silently invalidates every
other snapshot. The loser gets a `401` and is effectively logged out the next time
*it* tries to refresh. Concretely:

- A **single long session** that runs past the ~8h token life refreshes inside the
  container and knocks out your host login (and vice versa).
- **Concurrent** sessions (or a session overlapping host use) race the same way —
  one refreshes, the others break.
- A **short, solo** session that finishes before any refresh happens is fine.

This is why it's no longer the default: it behaves perfectly in a quick test
and then bites once sessions get long, parallel, or overlap host use. Use it
when your plan doesn't support `setup-token`, or when you specifically want
snapshot semantics and accept the rules above.

### `bedrock`

Authenticate and bill via **AWS Bedrock** instead of Claude.ai. Sets
`CLAUDE_CODE_USE_BEDROCK=1`, mounts `~/.aws` read-only, and skips the keychain
entirely. `--aws-profile` is optional (the AWS SDK's default credentials are used
otherwise), `--aws-region` defaults to `us-east-1`, and `--bedrock-model` sets the
model id. Composes with `--config-dir` (e.g.
`--auth bedrock --config-dir ~/.claude-bdr`).

## Other configuration options

These compose with any `--auth` mode:

| Flag | Default | Effect |
| --- | --- | --- |
| `--config-dir PATH` | `~/.claude` | Mounts `PATH` at `/home/claude/.claude`. In `keychain`/`oauth-token` modes the credential is keyed per directory (the hashed keychain-entry name described above), so separate config dirs ≈ separate profiles. |
| `--claude-json` / `--no-claude-json` | on | Whether to mount the host `~/.claude.json` (global config: MCP servers, project history/trust). Turn it off for a cleanly isolated profile alongside an alternate `--config-dir`. |
| `--ssh-agent` / `--no-ssh-agent` | on | Whether to forward the host SSH agent (see [above](#why-forward-the-ssh-agent)). |
| `--mount PATH[:ro|:rw]` | — | Bind-mount an extra host directory (reference docs, a sibling repo) into the container at its identical host path. **Read-only by default**; append `:rw` to make it writable. Repeatable. The directory must exist. Each mount is also passed to Claude as `--add-dir`, so it shows up as a working directory Claude knows about. |
| `--rebuild-image` | off | Pass `--no-cache` to `docker build`, forcing a full image rebuild from scratch. |
| `--require-project-entry` | off | Refuse to launch unless a `projects.json` entry matches the current directory. Set it in `~/.yolo.json` if you rely on per-project profiles: a renamed project then fails loudly instead of silently falling back to your global defaults. `--no-require-project-entry` overrides it for one run. |
| `--dangerously-allow-home` | — | By default yolo **refuses to launch with the working directory at or above `$HOME`** — that would mount your whole home directory (including `~/.ssh` and yolo's own config) read-write into a skip-permissions container. This flag overrides the refusal; it is deliberately CLI-only and cannot be set from a config file. |

## Configuring defaults (`~/.yolo.json` + `yolo config`)

Rather than re-typing the same flags every time, set their defaults in
**host-side config**. There are two layers:

1. **`~/.yolo.json`** — global defaults, a JSON object whose keys mirror the
   flag names:

   ```json
   {
     "auth": "oauth-token",
     "ssh-agent": false,
     "append-system-prompt": ["Prefer the standard library."]
   }
   ```

2. **`~/.claude-yolo/projects.json`** — per-project config, a JSON object
   mapping a project directory to the same kind of object. You don't edit it by
   hand to get started: run `yolo config` *with the flags you want to pin* from
   inside the project —

   ```bash
   yolo config --config-dir ~/.claude-work --mount ~/refdocs
   ```

   — and exactly those flags are saved as the project's entry (keyed by the repo
   root, so subdirectory runs and worktree sessions share it). Re-running with a
   flag updates just that key; a bare `yolo config` prints the entry that
   currently applies without writing anything. An entry applies to any directory
   at or under its key path; when several keys match, the most specific wins.

Precedence runs low to high: `~/.yolo.json` < the project's `projects.json`
entry < explicit CLI flags — so a flag always wins, and the project entry
overrides your global file per key (`append-system-prompt` and `mounts` are the
exceptions: those lists accumulate across all layers).

Supported keys: `config-dir`, `auth` (`keychain`/`oauth-token`/`bedrock`),
`aws-profile`, `aws-region`, `bedrock-model`, `claude-json`, `ssh-agent`, `base`
(the default branch point for `start`), `append-system-prompt` (a string or
list of strings), `mounts` (a string or list of `PATH[:ro|:rw]` specs), and
`require-project-entry`. A `null` value leaves a key at its built-in default.
The per-invocation actions (`--resume` and the verbs) are deliberately **not**
config keys, and neither is `--dangerously-allow-home`.

**Why is there no in-project config file?** Earlier versions read a `.yolo.json`
from the project directory. That file lives inside the tree that gets mounted
into the container — so Claude, running unattended inside one, could edit it and
quietly grant its *next* session more host access (an extra writable mount, say,
via `mounts` or `config-dir`); a `.yolo.json` committed to a repo you cloned
would likewise apply someone else's config to your machine. Both config files
now live outside everything a container can write, which makes the safety
property structural rather than policed. A leftover in-project `.yolo.json` is
ignored with a warning on every run (loud on purpose: if one *appears*, you want
to notice) telling you where to migrate it.

Because `projects.json` is keyed by directory path, **renaming or moving a
project orphans its entry** — the project would silently fall back to your
global defaults. yolo warns on every run about entries whose directory no longer
exists (and suggests the rename fix when the current directory has no entry of
its own); set `require-project-entry` in `~/.yolo.json` to upgrade that warning
to a hard refusal.

## Extra `docker run` arguments

Anything after a `--` separator is appended to the `docker run` command verbatim,
*after* the script's own arguments. Because Docker uses last-one-wins for
repeated flags, your arguments override the script's defaults:

```bash
yolo -- --network host --memory 4g
```

## Notes and gotchas

- **Login is checked up front (keychain mode).** In keychain mode (`--auth
  keychain`), claude-yolo copies credentials from the macOS keychain rather than
  authenticating inside the container. Before launching it runs
  `claude auth status`; if you're logged out it offers to run `claude auth login`
  for you and re-checks. Requires a host `claude` recent enough to have the `auth`
  subcommand — if it's missing, the check is skipped and the script falls back to
  erroring out when credential extraction comes up empty. (`--auth oauth-token`
  and `--auth bedrock` skip this check — they don't use the login keychain.)
- **The in-process sandbox is disabled on purpose** — the *container* is the
  sandbox. If your `~/.claude/settings.json` has `sandbox.enabled: true`, Claude
  would otherwise warn at startup that `bubblewrap`/`socat` are missing and run
  unsandboxed. claude-yolo suppresses that by passing
  `--settings '{"sandbox":{"enabled":false}}'` to Claude — a container-only
  override, so your host settings are untouched. Installing `bubblewrap` wouldn't
  help anyway: a default Docker container can't create unprivileged user
  namespaces, and granting that capability would weaken the very isolation this
  tool provides. (A `/doctor` sandbox note may still appear; that's expected.)
- **Don't switch to `npm install -g`.** The npm global install lands at
  `/usr/local/bin/claude`, which `/doctor` flags as a broken install and which
  self-update can't manage. The native installer is deliberate.
- **macOS-only as written**, because of the keychain and SSH-agent assumptions.

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

This started life as
[Michal Migurski's gist](https://gist.github.com/migurski/6d7b718b364dfa4e7c8c63cd643ede2c).
