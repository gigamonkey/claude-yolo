# Plan: preserve session startup output in a run-dir log

Goal: when the `wip` dashboard (or any yolo-spawned tmux window) launches a
session, the startup chatter — worktree setup, credential/secret staging
messages, the `docker build` output, the `--verbose` docker-run line — scrolls
away once the Claude Code TUI takes over. Today it survives only in the tmux
pane's scrollback, which Claude's constant redrawing slowly pushes past
`history-limit`. Capture it to a file so the user can read it any time during
the session.

---

## Where the output actually goes (why the design is what it is)

Two different launch shapes put the startup output in two different places:

- **wip-spawned sessions** (`Enter`/`n`/`r` in the dashboard) go through
  `_spawn_session_window` (`yolo.py:3846`): a fresh tmux window runs a full
  inner `yolo … --no-tmux` invocation. *Everything* — config resolution,
  worktree creation, image build, mount messages — prints into that new pane,
  and then the inner yolo `execvp`s into `docker run` (`_dispatch_launch`,
  `yolo.py:3880-3881`) and the TUI replaces it.

- **plain `yolo --tmux` from a shell** builds the docker command in the
  *invoking* terminal (all startup output prints there, where the user can
  still scroll) and only the `docker run` itself goes into the new window
  (`_launch_in_tmux`, `yolo.py:3750`).

So the problem is specific to windows yolo spawns that run an **inner yolo**:
the pane is born with the launch, fills with startup output, and the TUI buries
it. That pane's history, at the moment just before the exec, is *exactly* the
startup log — nothing before it (the window is fresh), nothing after it yet.

## Design: one-shot `capture-pane` at the exec boundary, not a streaming pipe

The obvious tool is `tmux pipe-pane`, but a streaming pipe has a fatal
bookkeeping problem: **nobody is left to turn it off.** The inner yolo replaces
itself with `docker run`, so after the exec no host-side yolo process exists,
and the pipe would keep streaming the Claude TUI's escape-heavy redraw stream
into the file forever — unbounded growth, unreadable content. Turning it off
*before* the exec is possible, but then a pipe captures strictly less than a
snapshot does (it misses everything printed before the pipe started, i.e.
worktree setup and any output emitted before the run dir exists to hold the
file), while adding an extra tmux child process and on/off bookkeeping.

A one-shot snapshot at the same boundary strictly dominates:

- `tmux capture-pane -p -e -S - -t "$TMUX_PANE"` returns the pane's entire
  history (`-S -`) with colors (`-e`), rendered — so `docker build`'s
  `\r`-progress spam collapses to its final lines instead of thousands of
  raw frames.

- The inner yolo runs it at the last moment before `_dispatch_launch`
  (`yolo.py:4315`), when the run dir already exists (`_session_run_dir`,
  created at `yolo.py:4009-4010`, well before the image build at 4247) and
  every host-side startup line has been printed.

- No moving parts survive the exec; nothing to clean up beyond the file, which
  the existing run-dir GC (`_gc_run_dir`, `yolo.py:951`) already reclaims when
  the container dies.

What a snapshot at this boundary *misses* is the in-container pre-claude
wrapper output (secrets sourcing, `clone.sh`, `--yolorc`) printed after the
exec but before the TUI. That's a small, separable follow-up (see "Not in
scope").

## Implementation

### 1. Gate: only yolo-spawned windows

Capturing `-S -` from an arbitrary pane would hoover up whatever shell history
preceded the launch — noise at best, the user's unrelated scrollback at worst.
Only capture when yolo created the pane, so its history is exactly this launch:

- `_spawn_session_window` (`yolo.py:3846`) passes
  `env={"YOLO_STARTUP_LOG": "1"}` through `_spawn_window` → the existing `env`
  prepend in `_tmux_window_command` (`yolo.py:3583`), so the inner yolo
  inherits it.

- The capture helper runs only when **both** `YOLO_STARTUP_LOG` and
  `TMUX_PANE` are set. A hand-run `yolo` inside tmux, or any non-tmux launch,
  is untouched (the invoking terminal's own scrollback already serves those).

### 2. Capture helper

A small function near the tmux helpers:

```python
def _snapshot_startup_pane(run_dir: pathlib.Path) -> None:
    """Snapshot this pane's history to <run_dir>/startup.log, best-effort."""
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("YOLO_STARTUP_LOG"):
        return
    log = pathlib.Path(run_dir) / "startup.log"
    print(f"Startup log: {log}", file=sys.stderr)   # before capture → self-describing
    res = _tmux("capture-pane", "-p", "-e", "-S", "-", "-t", pane)
    if res.returncode != 0:
        print("warning: could not capture startup log; continuing.", file=sys.stderr)
        return
    _write_run_file(run_dir, "startup.log", res.stdout.encode())
```

Called from `launch_container` immediately before `_dispatch_launch`
(`yolo.py:4315`) — after the `--verbose` docker-run print so that line lands in
the log too. Best-effort by construction: a tmux failure warns and the launch
proceeds.

Notes:

- `_write_run_file` (`yolo.py:969`) gives the chmod-600-from-creation
  guarantee, consistent with everything else in the run dir.

- The log is **not** mounted into the container (unlike its run-dir siblings
  `credentials.json` / `secrets/`). Keep it that way — it's a host-side
  convenience, and there's no reason to hand the container a rendered copy of
  its own launch pane.

- The path is printed *before* capturing, so the log's last line names its own
  location and the user sees where to look while the pane is still scrollable.

### 3. Viewing: `l` on a session row in `wip`

`l` is unused in the dashboard key dispatch (`_wip_action`, taken keys today:
`a b s S f r m d c n N R` + Enter). On a `session` row, spawn a pager window
the same way the diff-stat picker does per-file diffs:

```python
if key == "l" and kind == "session":
    _spawn_window(home, ["less", "-R", str(_run_dir() / name / "startup.log")],
                  f"log:{name}", session, env={"LESS": "R"})
```

`-R` renders the preserved colors; `q` closes the window (`_tmux_window_command`
holds it open only on real failure — and `less` exiting 1 on a missing file
lands on the "press Enter to close" path with less's own error visible, which
is adequate for the edge where a session predates the feature or wasn't
yolo-spawned). Add the key to the dashboard footer/help line.

### 4. Lifetime

Deliberately tied to the session: the run-dir GC removes the log when the
container is gone. That matches the stated need ("see the output after the
Claude session has started") and keeps the run dir's crash-proof cleanup story
untouched — no new persistence surface, no growth over time. If a post-mortem
log ever becomes a need, it's a different feature with a different home
(`~/.claude-yolo/…`), not an extension of this one.

## Tests

Per the existing seams (`run_cli` stubs the exec; `_tmux` is stubbable — see
`test_tmux.py` patterns):

- With `TMUX_PANE` + `YOLO_STARTUP_LOG` set: launch calls
  `capture-pane -p -e -S - -t <pane>`, writes `startup.log` (mode 600) with
  the stubbed capture output into the session's run dir, and still dispatches
  the docker run.

- With either env var missing: no capture-pane call, no file.

- Stubbed capture-pane failure (rc≠0): warning on stderr, launch proceeds,
  no file.

- `_spawn_session_window` includes `YOLO_STARTUP_LOG=1` in the window command.

- `wip`: `l` on a session row spawns a `less -R …/startup.log` window; `l` on
  a worktree/project row is a no-op.

## Docs

- README: a short paragraph under the tmux/wip section — startup output is
  saved to `<run-dir>/<name>/startup.log` for the life of the session; `l` in
  `wip` views it.

- CHANGELOG `## Unreleased` entry.

- ARCHITECTURE: mention `startup.log` in the run-dir contents list and the `l`
  key in the wip key table.

## Not in scope (noted for later)

- **In-container wrapper output** (secrets → clones → rc, printed between the
  exec and the TUI). Capturing it would mean the wrapper teeing its own output
  to a container-side file (e.g. `~/.yolo-startup.log`, readable via
  `yolo shell`) — a separate, container-side change; don't bind-mount a
  writable log back into the run dir for it.

- **True streaming `pipe-pane`**: rejected above — no process survives the
  exec to turn it off, so it either floods the file with TUI redraws or
  captures a strict subset of what the snapshot gets.
