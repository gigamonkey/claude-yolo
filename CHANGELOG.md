# Changelog

Notable changes to claude-yolo, per tagged version. Versions are tagged
`v{version}` and tracked in `pyproject.toml`.

## Unreleased

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

[Unreleased]: https://github.com/gigamonkey/claude-yolo/compare/v0.4.0...HEAD
