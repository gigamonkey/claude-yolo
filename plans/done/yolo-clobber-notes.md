# Avoiding clobbering in a `yolo` session — field notes

Notes for whoever is improving the `yolo` tool / its system prompt. These are the
things I (Claude, working in a `yolo` **cwd** container) have to consciously do to
avoid trashing the user's host state, plus suggestions for what `yolo` could
hardwire so the model doesn't have to remember.

Context that makes this hard: in a `cwd` session the container bind-mounts the
user's **live host checkout** in place. Edits, and any tooling side effects, are
real and visible on the host. The container is Linux; the host is macOS. So
anything that's per-OS (virtualenvs, native modules) is a landmine when the same
path is shared by both.

---

## 1. The big one: the project virtualenv on the bind mount

**Hazard.** The project's `./.venv` lives on the bind mount and was built for the
host (macOS interpreter symlinks). The moment any container command runs `uv run`
(or `uv sync`, `pip install`, etc.), uv sees a venv for the wrong platform and
**deletes and recreates `./.venv` for Linux**. That:

- breaks the user's host venv (now full of Linux binaries — `uv run` on macOS then
  rebuilds it again; constant thrash), and
- kills any running dev server whose process re-execs `./.venv/bin/python` — e.g.
  Flask's debug reloader dies with `FileNotFoundError: .venv/bin/python` the next
  time a watched file changes, because the interpreter was swapped out from under
  it.

This bit us repeatedly: we "fixed" the server, but every `uv run` *I* typed for
tests/verification re-clobbered it, because those shells didn't have the redirect.

**What I do.**

- Point uv at a **container-local** environment off the bind mount, via
  `UV_PROJECT_ENVIRONMENT=/tmp/<project>-venv`. The host's `./.venv` is then never
  touched by the container.
- Set it in **three** places, because different shells get initialized
  differently:
  - the server launcher (`serve.sh`), guarded by the session kind;
  - the user's `~/.bashrc` / `~/.profile`;
  - **the Bash-tool shell snapshots** under
    `~/.claude/shell-snapshots/*.sh` — this is what the agent's `Bash` tool
    actually `source`s on each call, and it is NOT `~/.bashrc`. If you only edit
    `~/.bashrc`, the agent's shells won't pick it up.
- Backstop: for one-off scripts/tests, call the interpreter directly
  (`/tmp/<project>-venv/bin/python script.py`) instead of `uv run`, so uv is never
  invoked and `./.venv` can't be touched.
- Verify the protection: run a `uv` command, then confirm `readlink ./.venv/bin/python`
  is unchanged (still the macOS path).

**Gotchas.**

- The shell-snapshot files **rotate** (new ones get created during a session), so
  appending to the current ones is not durable — a freshly created snapshot won't
  have the export. This is exactly the kind of thing that should be in the
  environment, not patched in by the agent.
- `UV_PROJECT_ENVIRONMENT` is global to uv, so it affects every project you run uv
  in within that shell. Fine for a single-project session; scope it if the session
  spans repos.

**Suggested hardwiring in `yolo`:** in a `cwd` session, export a per-project
container-local env path into **every** shell the container spawns (login shells,
the agent's Bash tool, and any launched servers) — e.g.
`UV_PROJECT_ENVIRONMENT=/tmp/<slug>-venv`, and the equivalent for other ecosystems
that keep a per-OS dir on the bind mount: Node `node_modules` (consider a
container-local store or bind-mount exclusion), Python `.venv`, Rust `target/`,
`__pycache__`, `.tox`, etc. The general principle: **per-OS / build-artifact
directories on the bind mount should be redirected off it for the container**, so
the container and host never fight over the same path. A volume/overlay for those
paths would be even cleaner than env vars.

---

## 2. The bind mount is the user's live, uncommitted data — don't sweep it into commits

**Hazard.** The working dir contains the user's real, often-uncommitted data files
(here: a git-tracked corpus of `courses/*` markdown/TSV the user edits in the
running app, plus untracked files). `git add -A` / `git commit -a` will hoover all
of that into "my" commits. I did this once and had to `git reset --soft` and
un-stage it.

**What I do.**

- **Never** `git add -A` / `git add .` / `git commit -a`. Stage code/docs files by
  explicit path only.
- Before committing, diff the staged set and re-check that no data/asset files
  snuck in; leave the user's data files unstaged.
- Treat files I didn't create and that look like user data (corpora, DBs, exports,
  zips) as off-limits unless explicitly asked.

**Suggested hardwiring:** nothing `yolo` can fully automate here (it's judgment),
but the system prompt could state plainly: *the working tree is the user's live
checkout; never stage with `-A`/`-a`; stage explicit paths; never commit files you
didn't author without asking.*

---

## 3. Don't run destructive ops against live state; work on copies

**Hazard.** Verifying behavior by mutating the live DB or the live corpus
(`rm db.db`, POSTing save/import requests to the running server, `--replace`
imports) changes the user's data. Deleting `db.db` out from under a running server
also 500s it.

**What I do.**

- Migrations are **additive and idempotent** (`CREATE TABLE IF NOT EXISTS`,
  `ALTER TABLE ADD COLUMN` guarded by a `PRAGMA table_info` check, `ALTER ... DROP
  COLUMN` only when present). Never "drop and recreate the DB."
- Test data-layer changes on a **copy**: `cp db.db /tmp/t.db` and run against that;
  or use the web framework's **test client** pointed at a copy DB, rather than
  POSTing mutating requests to the live server.
- Verification hits **GET** endpoints; never fire mutating POSTs at the live app
  just to "see if it works."
- If I do mutate live state by accident (e.g. a stray test row), I undo it
  immediately and say so.
- Never delete a file out from under a running process; stop the process first.

---

## 4. Don't damage build artifacts to run tests

Matches the standing `yolo` guidance already in the environment ("make a throwaway
venv rather than wiping the project's"). Concretely: I create the throwaway env in
`/tmp`, never `rm -rf .venv`/`node_modules` to "get a clean state," and leave
build artifacts as I found them.

---

## 5. Process / TTY hygiene (servers, pkill)

**Hazards.** Killing the dev server can accidentally kill the agent's own shell;
foreground servers stomp the TTY the agent needs.

**What I do.**

- Run servers **detached**: `setsid <cmd> </dev/null >log 2>&1 &` (the project's
  `serve.sh -d` does this), idempotent so re-running doesn't pile up servers. Never
  run a long-lived server in the foreground from the agent.
- `pkill` for the server uses the bracket trick so the matcher can't match its own
  command line: `pkill -9 -f '[a]pp\.py'`. And I run that `pkill` in a command that
  contains **no other literal copy of the process name** — otherwise the pattern
  matches my own bash invocation and kills the shell (seen as exit 1/144).
- Use the scratchpad / `/tmp` for temp files, never the project dir.

---

## 6. Confirm before irreversible or outward-facing actions

Deleting a feature, dropping a column/table, removing a concept, rewriting many
files — I ask a scoped question first (or at least state the assumption) rather
than guessing, because in a live checkout the blast radius is the user's real work.

---

## TL;DR for the `yolo` system prompt / environment

1. **Redirect per-OS/build dirs off the bind mount for the container** (`.venv` via
   `UV_PROJECT_ENVIRONMENT`, `node_modules`, `target/`, …) and inject the env into
   **every** shell — including the agent Bash tool's snapshot, which is not
   `~/.bashrc`. This is the single highest-value fix; it removes a whole class of
   "the container fought the host over a shared path" bugs.
2. **The working tree is the user's live data.** Never `git add -A`/`commit -a`;
   stage explicit paths; don't commit files you didn't author.
3. **Test on copies, mutate nothing live.** Prefer test clients / `/tmp` copies;
   keep migrations additive+idempotent; never delete files under a running process.
4. **Servers detached; `pkill` with the `[x]` trick in its own command; temp files
   in `/tmp`/scratchpad.**
