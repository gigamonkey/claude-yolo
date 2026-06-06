# claude-yolo

Run [Claude Code](https://claude.com/claude-code) in full "yolo mode"
(`--dangerously-skip-permissions`) without giving it free rein over your laptop.

The whole point is **blast-radius containment**: `claude-yolo.py` launches Claude
Code inside a throwaway Docker container. Claude can install packages, run
commands, and edit files unattended — but the only part of your host it can touch
is the directory you launch it from (which is bind-mounted in). Everything else
stays on the other side of the container wall.

It's a single self-contained Python script with no dependencies beyond the
standard library. There's no package to install and nothing to build.

## Requirements

- **macOS.** Credential extraction reads from the macOS keychain via the
  `security` CLI, and the script assumes an SSH agent is running
  (`SSH_AUTH_SOCK`).
- **Docker** installed and running.
- **[uv](https://docs.astral.sh/uv/)** installed. The script's shebang is
  `#!/usr/bin/env -S uv run --script`, so it self-runs under uv, which guarantees
  a Python ≥3.10 (it's still stdlib-only — uv just picks the interpreter, since
  macOS's system `python3` is often too old).
- **Claude Code** already set up on your host (so its credentials are in the
  keychain), or **AWS credentials** if you want to run against Bedrock.

## Install on your PATH

The script is self-contained — make it executable and symlink it somewhere on
your PATH to run `claude-yolo` from any directory:

```bash
chmod +x claude-yolo.py
ln -s "$PWD/claude-yolo.py" ~/.local/bin/claude-yolo   # ~/.local/bin is on PATH if you use uv
```

A symlink (not a copy) keeps it tracking the repo, so `git pull` updates it. The
examples below use `./claude-yolo.py`, but once it's on your PATH you can just say
`claude-yolo`.

## Usage

```bash
./claude-yolo.py                              # default ~/.claude credentials
./claude-yolo.py ~/.claude-work               # use an alternate config directory
./claude-yolo.py myprofile us-west-2 model.id # run against AWS Bedrock
./claude-yolo.py --worktree fix-auth          # run in a fresh git worktree (see below)
./claude-yolo.py -- --network host            # pass extra args to `docker run`
```

Run it from the directory you want Claude to work in. That directory becomes the
container's working directory and is the only host path Claude can modify.

You can also add `--append-system-prompt "..."` (or `-p "..."`, repeatable) to
tack extra instructions onto Claude's system prompt.

## Parallel sessions with `--worktree`

`--worktree NAME` lets you run several Claude containers on the **same repo at
once**, each in its own directory, without them stepping on each other — and
without risking work if a container exits at a bad moment:

```bash
cd ~/hacks/bells
./claude-yolo.py --worktree fix-auth      # terminal 1
./claude-yolo.py --worktree refactor-db   # terminal 2
```

Each invocation creates (or reuses) a git **worktree** on a new branch named
`NAME`, branched off your current `HEAD` with no upstream, and launches Claude in
it. The worktrees live in a central spot keyed by a slug of the repo path:
`~/.claude-yolo/worktrees/<repo-slug>/<NAME>` — so they clutter neither the repo
nor its parent directory.

Because the worktree directory **and** the repo's shared `.git` both live on the
host and are bind-mounted in, **nothing is lost when the container exits**:
commits land in the shared `.git` immediately, and even uncommitted edits are on
host disk. Merge or rebase the branch back into your main line locally whenever
you're done.

The session is also named `NAME` (`claude --name`), so it's labeled in the prompt
box and the `/resume` picker. To reattach, re-run with the same `--worktree NAME`,
or from the main repo use `/resume` → Ctrl+W ("all worktrees") or
`claude --resume NAME`. Cleanup is manual: `git worktree remove
~/.claude-yolo/worktrees/<repo-slug>/NAME` and `git branch -d NAME`.

## How it works

When you run the script, it does five things:

### 1. Builds the Docker image

It writes an inline Dockerfile to a temp directory and builds it. The image is
Ubuntu 24.04 with `nodejs`, `npm`, `git`, `curl`, and `jq`, plus Claude Code
installed via the **native installer** (`curl https://claude.ai/install.sh |
bash`, landing at `~/.local/bin/claude`).

The image is rebuilt on every run, but Docker's layer cache makes that nearly
instant after the first time.

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

## Three credential modes

The first positional argument decides the mode:

| You pass | Mode | What happens |
| --- | --- | --- |
| *(nothing)* | **Default** | Uses `~/.claude` + keychain credentials. |
| A path that **is a directory** | **Alternate config** | Mounts that directory at `/home/claude/.claude`; pulls credentials with the hashed keychain name. |
| A name that is **not a directory** | **AWS Bedrock** | Treats it as an AWS profile (with optional region and Bedrock model ID). Sets `CLAUDE_CODE_USE_BEDROCK=1` and mounts `~/.aws` read-only. No keychain extraction. |

The decision hinges purely on whether the first argument is an existing
directory.

## Extra `docker run` arguments

Anything after a `--` separator is appended to the `docker run` command verbatim,
*after* the script's own arguments. Because Docker uses last-one-wins for
repeated flags, your arguments override the script's defaults:

```bash
./claude-yolo.py -- --network host --memory 4g
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

## Provenance

This started life as
[Michal Migurski's gist](https://gist.github.com/migurski/6d7b718b364dfa4e7c8c63cd643ede2c).
The `https://claude.ai/chat/...` URL near the top of the script and the gist
reference in the git history are kept as a record of where it came from.
