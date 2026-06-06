# Plan: parallel worktree sessions (`--worktree NAME`)

## Goal

Let the user spin up multiple Claude Code containers working on the **same repo
in parallel**, each in its own directory, with **no risk of losing work** if a
container exits at a bad moment.

The mechanism is a git **worktree per session**, created on the **host** (so it's
durable), mounted into the container alongside the repo's shared `.git`. One
name drives everything — the worktree directory, its branch, the container name,
and the Claude session's display name.

```bash
cd ~/repo
./claude-yolo.py --worktree fix-auth      # terminal 1
./claude-yolo.py --worktree refactor-db   # terminal 2
```

Each session works in `~/repo-fix-auth` / `~/repo-refactor-db`, on branch
`fix-auth` / `refactor-db`. Commits land in the shared object store immediately;
the work is later merged locally however the user likes.

## Decisions (settled with the user)

- **Host-managed worktree (not Claude's native `--worktree`, not in-container).**
  The worktree is created on the host with `git worktree add`, so both the
  working tree and `.git` live on host disk and are bind-mounted in. This is the
  only option that keeps **uncommitted** changes safe across container exit.
  (Claude Code *does* have a native `-w/--worktree`, but it would create the
  worktree inside the container at an undocumented path — likely ephemeral —
  reintroducing the data-loss window. We deliberately do not use it.)
- **Simple local branch, no `origin/main` tracking.** Create the branch from the
  current `HEAD` with `git worktree add -b NAME`, with **no upstream**. (The
  `worktree-<name>` / unset-upstream dance in the user's global CLAUDE.md exists
  only to patch `EnterWorktree` making branches track `origin/main` — which made
  `git push` go to `main`. That doesn't apply here: this branch tracks nothing,
  so a stray `git push` can't hit `main`, and the work is merged locally.)
- **Mount only the shared `.git`, not the whole main repo.** Surgical: parallel
  sessions can't touch the main checkout. (`git worktree list` will show the main
  worktree as a path with no files — harmless; see Edge cases.)

## Why this is simpler than a clone-based approach

No clone, no entrypoint changes. The existing ENTRYPOINT
(`claude --dangerously-skip-permissions`, `claude-yolo.py:31`) is untouched. The
feature is purely: create a host worktree, point the existing same-path
mount/`-w` machinery at it, add **one** extra mount for the shared `.git`, and
pass `--name` through to Claude. No Dockerfile edits.

## How the two mounts relate (the subtle part)

A linked worktree is split across **two locations**, joined by **absolute**
paths. From the live demo:

```
~/repo-fix-auth/.git                       <- a FILE, not a dir, containing:
    gitdir: /Users/peter/repo/.git/worktrees/fix-auth      (ABSOLUTE)

/Users/peter/repo/.git/worktrees/fix-auth/ <- this worktree's private state:
    HEAD, index, logs, ORIG_HEAD, COMMIT_EDITMSG
    gitdir    -> /Users/peter/repo-fix-auth/.git           (ABSOLUTE back-pointer)
    commondir -> ../..                                       (RELATIVE -> the shared .git)

/Users/peter/repo/.git/                    <- the SHARED store: objects, refs,
                                              config, packed-refs, and worktrees/
```

So the data is divided like this:

- **Working-tree files** + the `.git` *pointer file* → live in the **worktree
  directory** (`~/repo-fix-auth`).
- **Objects, refs, config** (the durable history) **and** this worktree's private
  `HEAD`/`index`/`logs` (under `worktrees/fix-auth/`) → live in the **shared
  `.git`** (`~/repo/.git`).

That means the container needs **two bind mounts**:

1. `-v {worktree}:{worktree}` — e.g. `~/repo-fix-auth` → working files + the
   `.git` pointer file. (This is just the existing same-path cwd mount, retargeted
   to the worktree.)
2. `-v {common_git}:{common_git}` — e.g. `~/repo/.git` → objects, refs, **and**
   `worktrees/fix-auth/` (so we get the private HEAD/index for free; no third
   mount needed).

**Why both must be mounted at their identical host paths** (this is exactly your
concern — yes, the worktree has the `.git` location baked into it, and vice
versa):

- `~/repo-fix-auth/.git` holds the **absolute** path
  `/Users/peter/repo/.git/worktrees/fix-auth`. That only resolves inside the
  container if mount #2 lands at `/Users/peter/repo/.git` — its real host path.
- The reverse pointer `worktrees/fix-auth/gitdir` holds the **absolute** path
  `/Users/peter/repo-fix-auth/.git`. That only resolves if mount #1 lands at
  `/Users/peter/repo-fix-auth` — its real host path.
- `commondir` is *relative* (`../..`), so it's fine as long as the `.git`
  internal tree is intact (it is — it's all under mount #2).

Two absolute pointers, one in each direction, are why **same-path mounting is
load-bearing** for this feature (not just a nicety as it is for cwd today). Mount
either location at a different path and git fails with "not a git repository" /
a dangling `gitdir`. The script already mounts same-path, so this falls out
naturally — we just have to do it for *both* paths.

The per-worktree metadata living *inside* the shared `.git` is the convenient
part: mounting `~/repo/.git` automatically brings `worktrees/fix-auth/` with it,
so HEAD/index/refs/objects are all present from a single second mount.

## Detailed changes

### 1. Argument parsing (`PARSER`)

```python
PARSER.add_argument(
    "--worktree",
    metavar="NAME",
    help="Create/reuse a git worktree NAME for this session (branch NAME), "
         "launch Claude in it, and name the Claude session NAME. For parallel "
         "sessions on one repo.",
)
```

(Optional future nicety: a separate `--name` to decouple the Claude session name
from the worktree name. Not needed for v1 — one name drives all.)

### 2. `main()` flow when `--worktree NAME` is set

All of this runs on the **host** before assembling `docker run`:

1. **Resolve the shared `.git` and main repo root** from the current directory
   (works whether launched from the main checkout or an existing worktree):
   ```python
   common_git = run("git rev-parse --path-format=absolute --git-common-dir")  # ~/repo/.git
   main_root  = pathlib.Path(common_git).parent                                # ~/repo
   ```
   Error out clearly if not in a git repo.
2. **Compute the worktree path** as a sibling of the main repo:
   ```python
   worktree = main_root.parent / f"{main_root.name}-{NAME}"   # ~/repo-fix-auth
   ```
3. **Create or reuse the worktree** on the host:
   - If `worktree` already exists and is registered → reuse (re-launching the
     same session).
   - Else if branch `NAME` exists → `git worktree add {worktree} {NAME}`.
   - Else → `git worktree add -b {NAME} {worktree}` (new branch off current HEAD,
     no upstream).
4. **Retarget the container working dir to the worktree** — set the `cwd`
   variable used for `-w {cwd}` and `-v {cwd}:{cwd}` to `worktree` instead of the
   process cwd.
5. **Add the shared-`.git` mount**:
   ```python
   args += ["-v", f"{common_git}:{common_git}"]   # read-write, same path
   ```
6. **Name things after NAME**: container name suffix `-{NAME}`; pass
   `--name {NAME}` to Claude (see #3).

### 3. Naming the Claude session

`claude` supports `-n, --name <name>` (verified via `claude --help`): "Set a
display name for this session (shown in the prompt box, /resume picker, and
terminal title)." Append it to the command Claude receives, alongside the
existing built-in `--append-system-prompt` (`claude-yolo.py:266`):

```python
# when --worktree NAME is set, add to the trailing claude args:
[..., DOCKER_IMAGE, "--name", NAME, "--append-system-prompt", joined]
```

Bonus synergy: sessions are bucketed per-cwd, so each worktree already gets its
own `/resume` list; `--name` just makes the entries human-readable ("fix-auth")
instead of first-prompt summaries. To reattach later: `cd ~/repo-fix-auth &&
claude --continue` (or re-run the script with the same `--worktree NAME`, which
reuses the worktree).

### 4. Composition with existing modes

`--worktree` is orthogonal to the positional credential modes, so it works with
default keychain creds and with an alternate config dir. Git identity and SSH
agent are already forwarded, so commits/pushes from the worktree work.

## Durability analysis (the whole point)

- **Committed work**: written to objects in the shared `.git`, which is on the
  host → durable the instant you commit.
- **Uncommitted work**: lives in the worktree directory, which is on the host →
  also durable; surviving container exit like any file on disk.
- So there is **no data-loss window**. This is the concrete improvement over the
  abandoned bare-repo-clone idea (where the clone was ephemeral and unpushed work
  vanished).

## Parallel-safety notes

Worktrees are designed for concurrent use:

- Each worktree has its **own** `HEAD`/`index` (under `worktrees/<name>/`), so
  sessions don't clobber each other's checkout state.
- The object store is content-addressed (concurrency-safe); ref updates are
  lockfile-guarded. Concurrent commits on different branches are safe.
- Git refuses to check out the same branch in two worktrees — a built-in
  guardrail against accidentally pointing two sessions at one branch.
- Low-risk caveat: `git gc --auto` could fire in one session while another reads;
  git is built to tolerate this. Not expected to bite; note it.

## Edge cases / risks

- **`.git`-only mount makes the main worktree look empty inside the container**:
  `git worktree list` will show `~/repo` (the main checkout) as a path with no
  files, because we didn't mount its working tree. Operations from *our* worktree
  don't need it, so this is cosmetic. If it ever matters, switch to mounting the
  whole main repo (the rejected option-3b).
- **Re-launch reuses the worktree** (step 3) — must detect an existing/registered
  worktree and not error on `git worktree add`.
- **Cleanup is the user's job** (out of scope): `git worktree remove ~/repo-NAME`
  and `git branch -d NAME` when done. Consider a future `--rm-worktree` helper.
- **`git` dubious-ownership**: with UID matching, the mounted paths are owned by
  the in-container `claude` user, so `safe.directory` likely isn't needed — but
  test, and add it in if it bites.
- **Branch from current HEAD**: the new branch forks from whatever HEAD you're on
  when you launch. Document this; could make it configurable (`--from <ref>`)
  later.

## Testing plan

1. In a scratch repo with a couple of commits:
   `./claude-yolo.py --worktree t1` → confirm `~/repo-t1` exists on host, branch
   `t1` checked out, container launches there, `git remote -v` / `git status`
   work, and `git rev-parse --git-common-dir` resolves to the mounted `~/repo/.git`.
2. Inside the container, make a commit; **kill the container** (`docker kill`)
   without any push; confirm on the host that the commit exists on branch `t1`
   (durability of committed work) and that uncommitted edits are still in
   `~/repo-t1` (durability of working tree).
3. Launch a second session `--worktree t2` while `t1` is running; confirm both
   work concurrently and commits from each land on their own branch.
4. Confirm the Claude session shows the name (prompt box / terminal title) and
   appears as `t1`/`t2` in `/resume`.
5. Confirm **default mode still works unchanged** (no `--worktree`).
6. Confirm `--worktree` composes with an alternate config dir.

## Out of scope

- Worktree/branch teardown automation (removal helper).
- Using Claude Code's native `--worktree`/`--tmux`.
- Mounting the main working tree.
- Non-macOS hosts.
