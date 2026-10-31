# yolo's git polling starves the host: analysis for whoever fixes it

Written 2026-10-30 from a debugging session in the `bhs-cs` project. This is
observation plus recommendations; I could not see yolo's source, so the
"what yolo does" parts are inferred from process traces and should be
checked against the code.

## The symptom

`make deploy` in `bhs-cs` runs the full test gate on the host before `fly
deploy`. Today the gate failed twice and otherwise crawled. Git-heavy tests
ran 10–50× slower on the host than in a yolo container on the same machine:

| test (website/test/code-api.test.ts)        | host, pollers active | host, pollers quiet | container |
|---------------------------------------------|---------------------:|--------------------:|----------:|
| save, read back, revise                     |              6448 ms |              454 ms |    115 ms |
| code-commit writes many files as one commit |              6854 ms |              475 ms |    105 ms |
| a sync commit … baseSha-guarded             |              8884 ms |              610 ms |    119 ms |
| whoami (no git work)                        |              1468 ms |             1066 ms |   1576 ms |

Only the tests that commit to git slowed down. One lesson-planning test with
a deliberately tight git deadline (`LESSON_GIT_TIMEOUT_SECONDS=2`, meant to
bound a hanging ssh push) failed outright because the sandbox *clone* it
triggers took longer than 2 s under the load, and on the retry a Save that
should return at a 1 s push deadline took 16 s of local git work first.

"Pollers quiet" = the user stopped all yolo sessions but the active one, so
the dashboard had fewer worktrees to poll. Nothing else changed between the
slow and fast columns.

## What was running

```
88670 /Users/peter/.local/share/uv/tools/claude-yolo/bin/python3 /Users/peter/.local/bin/yolo wip --_dashboard
54212 /Users/peter/.local/share/uv/tools/claude-yolo/bin/python3 /Users/peter/.local/bin/yolo diff post-incorporate --project all-bhs-cs --base main --s…
```

plus five live sessions (`yolo start …`/`yolo resume …`, each a `docker run`).
The project `all-bhs-cs` spans three repos (bhs-cs, bhs-cs-content,
bhs-cs-lesson-plans), so five sessions ≈ fifteen worktrees to poll. Each
bhs-cs worktree is now large: 12,500 tracked files (3× what it was a week
ago — the course-content repo was merged into it on 2026-10-30), an 833 MB
pack, and a 187 MB `content/books` tree.

## What the traces showed

Endpoint Security exec log (`sudo eslogger exec`) over one ~2 minute run of
one test file, binary ← parent, counts:

```
  2207  [platform] /Applications/Xcode.app/Contents/Developer/usr/bin/git  <- git
  1331  [platform] /usr/bin/git  <- python3.13        ← yolo
   855  [platform] /usr/bin/git  <- node              ← the tests themselves
    15  /Applications/OrbStack.app/Contents/MacOS/xbin/docker-tools  <- python3.13
    15  /opt/homebrew/Cellar/tmux/3.5a/bin/tmux  <- python3.13
```

So in two minutes yolo's two Python processes launched git **1,331 times —
about 11/s — more than the test suite under measurement did**, plus one
`docker` and one `tmux` call every ~8 s. An earlier `fs_usage` capture during
a deploy (dashboard only, no diff viewer) showed the same shape at a lower
rate (~2–3 git/s), so the `diff` viewer appears to be the heavier of the two,
but the dashboard alone was enough to fail the gate.

Each `git status`-class command on a 12.5k-file worktree is a full index
read plus an `lstat` of every path (and an index rewrite when stat info is
refreshed); at 11/s across fifteen worktrees that is a continuous
disk-and-CPU load, and the tests' commits fsync (`F_FULLFSYNC` on macOS) on
every object and ref write, so they queue behind it.

Two macOS-specific amplifiers worth knowing about:

- **`/usr/bin/git` is a stub** that re-execs the Xcode copy (`…/Xcode.app/
  …/usr/bin/git`), so every git call is two process creations, and it is
  ~3× slower to launch than Homebrew's git (9 ms vs 3 ms, measured).
- **Spawning by name does a PATH walk**, and on this machine PATH has 35
  entries before `/usr/bin`. Python's `subprocess` (execvp) and Node's
  libuv both probe each entry; on macOS each miss briefly creates a child,
  and `syspolicyd` evaluates every one and logs `<private> with PID N
  violates validation category policy`. During the deploy syspolicyd sat
  at 70–78 % CPU with `trustd` at ~20 %, and logged 43,000 such lines.
  Measured per launch this is cheap (35 entries vs 2: same 10 ms), so it is
  a symptom of the spawn rate rather than a cause, but it is real CPU and
  it is the most visible thing in Activity Monitor — it cost the debugging
  session an hour of false leads.

## What I'd change in yolo

In rough order of payoff:

1. **Poll far less often, and adaptively.** A status dashboard does not need
   sub-second freshness. One tick every 2–5 s, backing off (10–30 s) while
   nothing has changed, would cut the load by an order of magnitude on its
   own. The `diff` viewer should re-run `git diff` only when something it
   depends on has moved, never on a timer alone.

2. **Check cheaply before running git at all.** Everything a status row
   needs changes only when the worktree or its refs change. Stat a handful
   of files per worktree — `.git/HEAD` (or the worktree's `gitdir` file →
   `HEAD`), `.git/index`, `.git/refs/`/`packed-refs`, `.git/logs/HEAD` —
   and skip the git calls when none of their mtimes moved since the last
   tick. Or use a filesystem watcher (`watchdog`/FSEvents) on those paths
   and poll nothing. Either way the steady state becomes zero git
   processes.

3. **Poll only what is on screen / alive.** Rows for stopped sessions, or
   worktrees scrolled out of view, do not need refreshing every tick. If
   the dashboard's tmux window is not the active one
   (`tmux display -p '#{window_active}'`), it can idle entirely.

4. **One git call per worktree, not several.** `git status --porcelain=v2
   --branch` gives branch, upstream, ahead/behind, staged, unstaged and
   untracked in one process. Add `--no-optional-locks` (or
   `GIT_OPTIONAL_LOCKS=0`) so a status poll never rewrites the index and
   never contends with a real git operation in that worktree. If untracked
   files are not displayed, `-uno` skips the directory walk, which is most
   of the cost on a tree like `content/`.

5. **Spawn git by absolute path, resolved once** (`shutil.which("git")` at
   startup), and prefer a Homebrew/`/opt/homebrew/bin/git` if present over
   the `/usr/bin/git` stub. This removes the PATH-probe spawn storm (and
   the syspolicyd noise) and halves process creations per call.

6. **Consider a "quiet while busy" rule.** The gate that failed is a long
   CPU/disk-heavy host job. A dashboard could pause polling when system
   load is high, or expose `yolo wip --pause`/a keybinding, or `yolo` itself
   could set a lock file that other yolo processes honour while a
   `make deploy`-class command is running. This is a nice-to-have once 1–4
   are in; the user does always have `yolo wip` open and has deployed
   fine for months — the repo growing 3× is what tipped it over.

## How to verify a fix

Count git launches by yolo's Python over a window (the terminal needs Full
Disk Access for `eslogger`):

```
sudo eslogger exec 2>/dev/null | python3 -c '
import sys, json, time
t0 = time.time(); n = 0
for line in sys.stdin:
    e = json.loads(line)
    if "python" in e["process"]["executable"]["path"] and e["event"]["exec"]["target"]["executable"]["path"].endswith("/git"):
        n += 1
        print(f"{n/(time.time()-t0):5.1f} git/s from python", end="\r")
'
```

With five sessions open and the dashboard idle on screen, the number today
was ~11/s with the diff viewer, ~2–3/s without. After the fix it should
read 0.0 when nothing in the worktrees is changing, and spike only briefly
after a commit or checkout.

`fs_usage` is not a good substitute: its trailing number is a thread id,
not a pid, and the unified log redacts paths as `<private>` with no
`private_data:on` switch on current macOS. `eslogger exec` is the tool
that gives pid, path and parent in one record.
