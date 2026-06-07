# Plan: opinionated worktree workflow verbs

## Goal

Add a small set of verbs that make the **worktree-per-topic** flow the natural
way to use `yolo`, so most work lands on a branch that can be merged into `main`
or pushed as a PR. The verbs are sugar over primitives that already exist
(`--worktree`, `--continue`/`--resume`, `setup_worktree`); the heavy lifting is
already implemented and tested.

```
yolo start <topic>     # new worktree + branch <topic>, launch container, new named session
yolo resume <topic>    # launch a container on an existing worktree (default: --continue)
yolo shell <topic>     # bash shell in the worktree's running container, or a fresh one
yolo finish <topic>    # remove the worktree (keep the branch); cleanup
yolo list              # show this repo's worktrees, branches, and running containers
yolo init              # (unchanged) scaffold a .yolo.json
yolo                   # (unchanged) launch a container in the cwd, no worktree
```

`prune` is intentionally **out of scope for now** (see [Deferred](#deferred)).

## Container model (settled)

Containers stay **ephemeral** (`docker run -it --rm`). Durability comes from the
**worktree** (host disk) and the **session transcripts** (bind-mounted under
`~/.claude/projects/`), both of which outlive the container. So `resume` is
"fresh container + `claude --continue`", not a persisted container.

`shell` is the only verb that wants an *existing* container, and it gets one
opportunistically:

- If a container is **currently running** for the worktree → `docker exec -it
  <id> bash` into it (e.g. poke around while a session runs in another terminal).
- Otherwise → launch a **fresh ephemeral** container on the worktree with a bash
  entrypoint (same mounts a session would get), `--rm`.

This keeps the "no cruft" property of `--rm` and needs no container lifecycle
management.

### Identify containers by label, not name

Container *names* get suffixed by `--config-dir`/`--bedrock`, so reconstructing
them is fragile. Instead, stamp every launch with docker labels and query those:

- `--label yolo.repo=<repo-slug>` on every launch inside a repo.
- `--label yolo.worktree=<topic>` additionally on worktree launches.

`<repo-slug>` is the existing scheme from `setup_worktree`:
`re.sub(r"[^a-zA-Z0-9]", "-", str(main_root))`. Factor it into a `repo_slug()`
helper reused by the verbs.

Finding the running container for a topic:

```
docker ps --filter label=yolo.repo=<slug> --filter label=yolo.worktree=<topic> --format {{.ID}}
```

## CLI / argparse approach

Keep the current single-parser structure (all credential/config flags stay
global, the `--` passthrough split stays in `main`). Replace the single
`verb` positional (`choices=["init"]`) with **two positionals**:

```python
PARSER.add_argument("verb", nargs="?",
                    choices=["init", "start", "resume", "shell", "finish", "list"])
PARSER.add_argument("topic", nargs="?")   # required by start/resume/shell/finish
```

Plus a few new options:

- `--base REF` — for `start`: the git ref the new branch/worktree is created
  from. Defaults to `HEAD`. **Also a `.yolo.json` key** (`base`), so you can set a
  per-repo or global default (e.g. `"base": "origin/main"` for a PR-from-main
  flow). Consumed only by `start`; ignored by the other verbs.
- `--new` — for `resume`: start a fresh session in the worktree instead of
  continuing.
- `--force` — for `finish`: remove despite uncommitted changes.

`--new`/`--force` are transient and validated in dispatch (the way the AWS flags
are validated against `--bedrock` today). `--base` is config-backed: add `base`
to `YOLO_KEYS` (string) and `YOLO_INIT_DEFAULTS` (default `"HEAD"`) so it flows
through `load_yolo_config` like the other config options.

Rationale for staying with positionals rather than `argparse` subparsers: the
working arg-assembly path stays intact, bare `yolo` keeps working, and the
codebase already does manual cross-flag validation. (If per-verb flag scoping
grows, revisit subparsers with a shared parent parser.)

### Dispatch flow in `main`

Two classes of verbs:

- **Terminal / management** (`init`, `finish`, `list`, and the `shell` *exec*
  case): need the repo + topic but **not** credential config, so they run off the
  *first* `parse_args` (before `.yolo.json` is layered) and `return`.
- **Launch** (`start`, `resume`, `shell` *fresh*, and bare `yolo`): need the
  `.yolo.json` defaults, so they go through the existing load-config →
  `set_defaults` → re-parse path, then the assembly path.

```python
parsed = PARSER.parse_args(script_argv)          # first parse: built-in defaults
verb, topic = parsed.verb, parsed.topic

if verb == "init":   write_default_yolo(cwd); return
if verb == "list":   do_list(home, cwd); return
if verb == "finish": do_finish(topic, cwd, home, force=parsed.force); return
if verb == "shell":
    cid = running_container_for(topic, cwd)
    if cid:
        os.execvp("docker", ["docker", "exec", "-it", cid, "bash"]); return
    # else fall through to a fresh launch with a bash entrypoint

# launch path (start / resume / shell-fresh / bare):
PARSER.set_defaults(**load_yolo_config(cwd, home))
parsed = PARSER.parse_args(script_argv)
... assemble and exec docker run ...
```

## Refactor: extract the launch path

Pull the container-arg assembly + `os.execvp` out of `main` into a helper so
`start`/`resume`/`shell-fresh`/bare all share it:

```python
def launch_container(parsed, *, worktree_name, claude_args, entrypoint=None,
                     extra_labels=(), docker_args): ...
```

It does what `main` does today (mounts, ssh-agent block, credential blocks,
labels, build image, exec) but takes the worktree name, the trailing args, and
an optional `--entrypoint` override (bash for `shell-fresh`). The verbs become
thin translations into its parameters:

- `start <topic>`   → `worktree_name=topic`, `claude_args=["--name", topic, ...]`
- `resume <topic>`  → `worktree_name=topic`, `claude_args=["--continue"]` (or `-r`/`--new`)
- `shell <topic>`   → `worktree_name=topic`, `entrypoint="bash"`, `claude_args=[]`
- bare              → `worktree_name=None`, `claude_args` as today

This keeps the tested mount/credential logic in one place.

## Per-verb specifications

### `start <topic>`
- Must be inside a git repo (reuse `setup_worktree`'s repo check).
- **Error if the worktree OR branch `<topic>` already exists** —
  "already exists; use `yolo resume <topic>`". (Today `setup_worktree` silently
  reuses; add a create-only mode or pre-check.)
- Create the worktree: branch `<topic>` off `--base` (default `HEAD`), no
  upstream. Extend `setup_worktree` to take a `base` ref and pass it to
  `git worktree add -b <topic> <path> <base>` (optionally pre-validate with
  `git rev-parse --verify <base>` for a clean error). Note an `origin/...` base
  may be stale unless fetched first — we do not auto-fetch.
- Launch: worktree mounts + labels, `claude --name <topic>`.
- Composes with all credential/config flags and `.yolo.json`.

### `resume <topic>`
- **Error if the worktree does not exist** — "no such worktree; use
  `yolo start <topic>`".
- Launch on the existing worktree. Session selection:
  - default → `claude --continue` (most recent session for that path).
  - `--new` → fresh session (no `-c`/`-r`; `--name <topic>` subject to the
    [name-collision check](#open-questions)).
  - `-r [SESSION_ID]` → resume a specific session / open the picker (existing flag).
- Suppress `--name` when continuing/resuming (existing behaviour).

### `shell <topic>`
- Error if the worktree does not exist.
- If a container is running for the worktree → `docker exec -it <id> bash`.
- Else → fresh `--rm` container on the worktree, `--entrypoint bash` (full mounts,
  so the shell sees what a session would).

### `finish <topic>`
- Error if the worktree does not exist.
- **Refuse if a container is running** for the worktree (label match) —
  "exit the running container first". (Don't remove a dir out from under a live
  bind mount.)
- **Refuse if the worktree has uncommitted changes** (`git status --porcelain`
  non-empty: modified, staged, or untracked), unless `--force`. This is the only
  real data-loss vector, since the branch (and its commits) is kept.
- `git worktree remove <path>` (then `git worktree prune`). **Keep the branch.**
- **Keep the transcripts** — they self-expire via Claude Code's
  `cleanupPeriodDays` (default 30) and reusing a topic name is safe anyway
  (`--continue` always picks the newest session).
- Print an **informational** reminder (not a warning): e.g. "branch `<topic>`
  kept — N commits beyond main, M not pushed; merge or open a PR when ready." It
  naturally says nothing when there's nothing beyond main.

### `list`
- For the current repo, enumerate worktrees under
  `~/.claude-yolo/worktrees/<slug>/`, and for each show: branch, ahead/behind vs
  `main`, dirty flag (`git status --porcelain`), and whether a container is
  running (label query). One line per worktree. Pure read-only.

### `init` and bare `yolo`
- Unchanged.

## Composition with credential flags & `.yolo.json`

- The credential/config flags (`--config-dir`, `--bedrock`, `--claude-json`,
  `--ssh-agent`, `-p`) apply to `start`/`resume`/`shell-fresh` exactly as they do
  to a bare launch — they flow through `launch_container`.
- `.yolo.json` already rejects the action keys (`--worktree`, `--continue`,
  `--resume`); the new verbs are positionals, not in `YOLO_KEYS`, so they
  naturally can't appear in a config file. No change needed there.
- `base` is the one verb-related option that **is** a config key — a standing
  "branch new work off X" preference, not a per-invocation action. Add it to
  `YOLO_KEYS` and `YOLO_INIT_DEFAULTS` (default `"HEAD"`).
- `--worktree` stays as the underlying primitive (the verbs ultimately set the
  same internal `worktree_name`). Could be soft-deprecated later in favour of the
  verbs, but keep it working for now.

## Open questions

1. **`--name` collision (verify at implementation).** When `start` (or
   `resume --new`) runs `claude --name <topic>` and a prior session in that path
   bucket already carries that name (e.g. the topic name was used before
   `finish`), does Claude Code reject the duplicate name? If so, handle it (drop
   `--name`, or uniquify). Verify before relying on `--name`.

## Testing

Mirror the existing suite (`tests/`, which stubs `os.execvp` and asserts on the
assembled `docker run` argv):

- `start`/`resume`/`shell-fresh` → assert argv contains the right labels,
  entrypoint, `--name`/`--continue`, and that credential flags compose.
- `resume` errors on a missing worktree; `start` errors on an existing one.
- `start --base <ref>` (and `base` from `.yolo.json`) reaches
  `git worktree add -b <topic> <path> <ref>`; default is `HEAD`.
- `shell`-exec, `finish`, `list` → these shell out to `docker`/`git`; stub
  `subprocess.run`/`subprocess.check_output` and assert the commands (e.g.
  `finish` refuses on dirty/running, removes + keeps branch on clean).
- Keep `uv run pytest` and `uv run ruff check .` green.

## Docs to update

- `README.md` — a "Worktree workflow" section with the verb table and examples;
  note `shell`'s exec-or-fresh behaviour and `finish`'s guards.
- `CLAUDE.md` — document the verb set, the label scheme, the launch-path refactor
  (`launch_container`), the terminal-vs-launch dispatch split, and add `base` to
  the documented `.yolo.json` keys.

## Deferred

- **`yolo prune`** — find worktrees whose branches are merged to `main` and
  remove them. Parked because correct merge detection depends on the PR merge
  style: `git branch --merged main` misses **squash-merged** branches (the
  GitHub default), so it likely needs `gh pr list --state merged` and/or
  patch-id matching, plus a dry-run-by-default safety stance. Revisit as a
  follow-up once the merge-detection approach is decided.

## Implementation order

1. Add `repo_slug()` helper and labels to the current launch path (no behaviour
   change yet); add a `running_container_for(topic, cwd)` helper.
2. Extract `launch_container(...)` from `main`; re-run the suite to confirm the
   bare/worktree paths are unchanged.
3. Expand the `verb` positional choices, add `topic`, `--base` (+ the `base`
   config key/scaffold default), `--new`, `--force`; wire the dispatch flow
   (terminal vs launch).
4. Implement `start` and `resume` (translations into `launch_container`) + their
   existence checks.
5. Implement `shell` (exec branch + fresh branch).
6. Implement `finish` (guards → `git worktree remove` → reminder) and `list`.
7. Tests for every verb; ruff clean.
8. Update `README.md` and `CLAUDE.md`.
9. Resolve the `--name` collision open question during implementation.
```
