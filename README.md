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
- **Docker** installed and running.
- **[uv](https://docs.astral.sh/uv/)** installed. The script's shebang is
  `#!/usr/bin/env -S uv run --script`, so it self-runs under uv, which guarantees
  a Python ≥3.10 (it's still stdlib-only — uv just picks the interpreter, since
  macOS's system `python3` is often too old).
- **Claude Code** already set up on your host (so its credentials are in the
  keychain), or **AWS credentials** if you want to run against Bedrock.

## Install on your PATH

The script installs as a `yolo` command. Two ways, depending on whether you want
a clean managed install or a copy that tracks the repo:

**Installed (recommended)** — `uv tool install` (or `pipx install`) builds it into
an isolated venv and drops a `yolo` executable on your PATH, with zero runtime
dependencies:

```bash
uv tool install git+https://github.com/gigamonkey/claude-yolo  # from the repo
uv tool upgrade claude-yolo                                    # later, to update
```

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
yolo                                   # default ~/.claude credentials
yolo --config-dir ~/.claude-work       # use an alternate config directory
yolo --bedrock --aws-profile myprofile --aws-region us-west-2  # AWS Bedrock
yolo --no-ssh-agent                    # don't forward the host SSH agent
yolo -c                                # resume the most recent session here
yolo -r [SESSION_ID]                   # pick / resume a session
yolo init                              # write a .yolo.json of defaults, then exit
yolo -- --network host                 # pass extra args to `docker run`

# the worktree workflow (see below):
yolo start fix-auth                    # new worktree+branch, launch a session
yolo resume fix-auth                   # re-enter it, continue the session
yolo shell fix-auth                    # open a bash shell in its container
yolo finish fix-auth                   # remove the worktree, keep the branch
yolo list                              # show this repo's worktrees
```

Run it from the directory you want Claude to work in. That directory becomes the
container's working directory and is the only host path Claude can modify.

`--config-dir`, `--bedrock`, `--claude-json`, `--ssh-agent`, and `--worktree` are
**orthogonal flags** — combine them however you like (see
[Configuration & credential options](#configuration--credential-options)). The
positional arguments are an optional verb
(`init`/`start`/`resume`/`shell`/`finish`/`list`) and its topic name. You can also
add `--append-system-prompt "..."` (or `-p "..."`, repeatable) to tack extra
instructions onto Claude's system prompt, and set defaults for most flags in a
[`.yolo.json` file](#configuring-defaults-with-yolojson).

## The worktree workflow

Most work with `claude-yolo` is meant to land on a branch you can merge or open a
PR from. The verbs make that the path of least resistance — each takes a `TOPIC`
(which becomes both the git worktree and the branch name) and runs from inside a
repo:

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

- **`start TOPIC`** creates a git **worktree** on a new branch `TOPIC`, branched
  off `HEAD` by default (change with `--base REF`, e.g. `--base origin/main`, or
  set `"base"` in `.yolo.json`), and launches a fresh session named `TOPIC`. It
  errors if that topic already exists — use `resume`.
- **`resume TOPIC`** re-enters an existing worktree and, by default, continues its
  most recent session. `--new` starts a fresh session instead; `-r [SESSION_ID]`
  picks a specific one. Errors if the worktree doesn't exist — use `start`.
- **`shell TOPIC`** drops you into a bash shell on the worktree: into the
  **running** container if one is up (handy while a session works in another
  terminal), otherwise a fresh throwaway container.
- **`finish TOPIC`** removes the worktree but **keeps the branch** (for you to
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

(`--worktree NAME` is still available as the underlying flag — `start`/`resume`
are sugar over it — and composes with all the credential options below.)

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
match your host UID (`os.getuid()`). This is what lets bind-mounted sockets — like
your SSH agent — actually be readable inside the container.

### 3. Extracts your credentials

Claude Code keeps its OAuth credentials in the macOS keychain. The script pulls
them out with the `security` CLI into a temporary, `chmod 600` file, then
bind-mounts that file to `.credentials.json` inside the container. (This step is
skipped in Bedrock mode, which uses AWS credentials instead.)

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
repo, but it never gets to read the private key. (This is also why step 2 matches
the container user's UID to yours: socket permissions are UID-based, so without
the matching UID the container couldn't open the agent socket.)

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

## Configuration & credential options

Four independent flags control where Claude's config comes from and how it
authenticates. They're **orthogonal** — any combination is valid (for example
`--bedrock --config-dir ~/.claude-bdr` runs Bedrock against a separate config
directory):

| Flag | Default | Effect |
| --- | --- | --- |
| `--config-dir PATH` | `~/.claude` | Mounts `PATH` at `/home/claude/.claude`. Credentials are pulled from the keychain entry for that directory (the hashed name described above). |
| `--bedrock` (+ `--aws-profile`, `--aws-region`, `--bedrock-model`) | off → keychain | Authenticate and bill via **AWS Bedrock** instead of the keychain. Sets `CLAUDE_CODE_USE_BEDROCK=1`, mounts `~/.aws` read-only, and skips keychain extraction. `--aws-profile` is optional (the AWS SDK's default credentials are used otherwise); region defaults to `us-east-1`. Override a config-set value with `--no-bedrock`. |
| `--claude-json` / `--no-claude-json` | on | Whether to mount the host `~/.claude.json` (global config: MCP servers, project history/trust). Turn it off for a cleanly isolated profile alongside an alternate `--config-dir`. |
| `--ssh-agent` / `--no-ssh-agent` | on | Whether to forward the host SSH agent (see [above](#why-forward-the-ssh-agent)). |

## Configuring defaults with `.yolo.json`

Rather than re-typing the same flags every time, put their defaults in a
`.yolo.json` file — a JSON object whose keys mirror the flag names:

```json
{
  "config-dir": "~/.claude-work",
  "ssh-agent": false,
  "append-system-prompt": ["Prefer the standard library."]
}
```

The script reads the **nearest `.yolo.json` at or above the directory you launch
from**, overlaid on a global **`~/.yolo.json`**. Precedence runs low to high:
`~/.yolo.json` < the project `.yolo.json` < explicit CLI flags — so a flag always
wins, and a project file overrides your global one per key (`append-system-prompt`
is the exception: prompts from all layers accumulate).

Supported keys: `config-dir`, `bedrock`, `aws-profile`, `aws-region`,
`bedrock-model`, `claude-json`, `ssh-agent`, `base` (the default branch point for
`start`), and `append-system-prompt` (a string or list of strings). A `null`
value leaves a key at its built-in default. The per-invocation actions
(`--worktree`, `--continue`, `--resume`, and the verbs) are deliberately **not**
config keys.

To get started, `yolo init` writes a `.yolo.json` of default values
into the current directory (it won't overwrite an existing one), which you can
then edit down to the settings you care about.

## Extra `docker run` arguments

Anything after a `--` separator is appended to the `docker run` command verbatim,
*after* the script's own arguments. Because Docker uses last-one-wins for
repeated flags, your arguments override the script's defaults:

```bash
yolo -- --network host --memory 4g
```

## Notes and gotchas

- **Login is checked up front.** claude-yolo copies credentials from the macOS
  keychain rather than authenticating inside the container. Before launching it
  runs `claude auth status`; if you're logged out it offers to run
  `claude auth login` for you and re-checks. Requires a host `claude` recent
  enough to have the `auth` subcommand — if it's missing, the check is skipped
  and the script falls back to erroring out when credential extraction comes up
  empty.
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
