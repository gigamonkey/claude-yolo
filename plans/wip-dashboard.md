# Plan: `yolo wip` — a tmux dashboard for managing everything yolo

## Goal

A new verb `yolo wip` that opens a full-screen, tmux-resident dashboard for
managing all yolo work in one place. It is a superset of today's
`yolo ps --watch` picker:

- **Running sessions** — every running yolo container across all repos (today's
  `ps --watch` view), with Enter to switch to its tmux window.
- **Inactive worktrees** — every worktree under `~/.claude-yolo/worktrees`
  across all repos that *isn't* currently running (a la `yolo list --all`), with
  Enter to **resume** it into a new tmux window.
- **Known projects** — the directories registered in
  `~/.claude-yolo/projects.json`, with a key to **start** a fresh session or a
  new named worktree there.
- **Lifecycle actions** — **stop** a running session, **finish** an inactive
  worktree, **add** a new project, all without leaving the dashboard.

The dashboard is the natural "home base" you keep open in tmux while several
yolo sessions run in sibling windows.

## Why this fits the existing design

The pieces already exist and only need to be generalized and wired together:

- `_ps_picker` / `_ps_picker_loop` / `_draw_picker` (yolo.py:4302–4389) already
  implement an interactive, tmux-only, cbreak-mode picker with a 2s refresh, an
  injectable `wait_key` (so it's unit-testable), and Enter→`select-window`/
  `switch-client`. `wip` is this picker grown into multiple sections with more
  keybindings.
- `_ensure_tmux_session` (yolo.py:3132) already seeds a shared tmux session whose
  window 0 (`TMUX_DASHBOARD_WINDOW`, yolo.py:3064) runs the dashboard command
  (currently `yolo ps --watch`). `wip` becomes that window-0 command.
- `_launch_in_tmux` (yolo.py:3157) already knows how to attach/switch a client to
  a window, including the "another terminal already attached → don't mirror" case.
  The `wip` bootstrap reuses this focusing logic, and the dashboard's launch
  actions reuse the new-window machinery.
- Data sources already exist: `_ps_rows` (yolo.py:4168), `do_list`'s worktree
  row-building (yolo.py:4040), `_read_projects_file` (yolo.py:1591).
- Mutating actions already exist as verbs: `do_stop` / `_stop_container`
  (yolo.py:3960/3994), `do_finish` (yolo.py:3662), `start`/`resume` launch paths.

## Design decisions (resolved)

1. **`wip` replaces `ps --watch` as the seeded tmux dashboard window.**
   `_ensure_tmux_session` seeds window 0 with the `wip` dashboard instead of
   `yolo ps --watch`, so the shared `--tmux` session and `yolo wip` share one
   home window. `ps`/`ps --watch` stay as standalone lightweight verbs (usable
   outside tmux, scriptable).

2. **Quick mutating ops run in-process; launches shell out to a fresh `yolo`.**
   The two are different in kind:

   - **Quick ops (stop, finish, add-project)** run **in-process**. They're
     blocked today only by (a) cwd-coupling and (b) `sys.exit` on the error path,
     both of which we fix cleanly (see below): split each verb into a
     context-explicit **core** that takes the paths/cid the dashboard already
     holds and **raises `YoloError`** instead of exiting, plus a thin cwd-resolving
     wrapper for the CLI. The dashboard calls the core directly and surfaces the
     result (or the caught `YoloError`) in the footer.

   - **Launches (start, resume, new-worktree)** **shell out** — the new tmux
     window runs a fresh `yolo start/resume TOPIC` (via `_self_invocation`) with
     its working directory set to the target repo/worktree (`new-window -c`). A
     launch's result is always a new window running a long-lived session process,
     so the dashboard birthing a self-contained `yolo` there (which re-resolves
     config/credentials in its own cwd) is both simpler and more robust than
     threading a synthesized `parsed`+chdir through the whole `launch_container`
     pipeline. The dashboard itself only makes tmux calls — it never blocks on a
     subprocess.

3. **`wip` requires tmux** (like the interactive `--tmux` paths). Outside tmux /
   without a TTY it errors with guidance, rather than falling back to a passive
   table (that's what `ps --watch` already is).

### The `YoloError` refactor (enables in-process quick ops)

Introduce `class YoloError(Exception)`. Migrate the **cores the dashboard calls**
to raise it instead of `sys.exit`, and split each cwd-coupled verb into core +
wrapper:

- `stop_session(cid, where, home, *, force)` ← extracted from `_stop_container`
  (yolo.py:3994), raises on the active-`working` refusal and docker failure;
  `do_stop` resolves `cid` from cwd/topic, calls it, and translates `YoloError`
  to `sys.exit`.
- `finish_worktree(worktree, main_root, slug, topic, home, base, *, force,
  action, remote)` ← extracted from `do_finish` (yolo.py:3662), takes the
  explicit paths the dashboard already has (no `_repo_paths()`/cwd), raises on the
  dirty-tree/running-container/merge-conflict paths; `do_finish` resolves context
  from cwd+topic and calls it.
- `register_project(project_key, home)` ← extracted from `do_config`'s `--init`
  branch, raises if an entry already exists.
- `browse_session(cid, *, select=None, print_only=False)` ← extracted from
  `do_browse` (yolo.py:4434), takes the cid the dashboard already has; raises on
  no-ports / unknown-port; resolves the host port and opens it host-side.
- `rebase_worktree(worktree, topic, base, home, *, force=False)` ← extracted from
  `do_rebase` (yolo.py:3837), keeps the session-aware running-container guard;
  raises on the dirty-tree / unconfirmed-`working` / conflict paths.

CLI behavior is preserved by catching `YoloError` in the verb wrappers (or
centrally in `main()`'s dispatch: `except YoloError as e: sys.exit(str(e))`). The
dashboard catches it per-action and shows the message in the footer. Scope the
migration to these cores now; the central catch lets others move over later.

## Architecture

### New verb & dispatch

- Add `"wip"` to the verb `choices` (yolo.py:2414) and the help text.
- Dispatch it as a terminal verb after the config load (near `ps`, yolo.py:4714),
  so it can honour config if needed but launches no container itself. Add the
  usual verb-gating (no stray TOPIC/flags).
- `do_wip(home)` is the entry point. Two roles, distinguished by **where it
  runs**:
  - **Bootstrap** (user typed `yolo wip`): ensure the shared tmux session exists
    (`_ensure_tmux_session`, which seeds the dashboard window), then focus the
    dashboard window using `_launch_in_tmux`'s focusing logic (switch-client if
    inside tmux, attach if no client, no-mirror switch if attached elsewhere).
  - **Dashboard loop** (the window-0 command tmux runs): run the interactive
    loop. Distinguish from bootstrap with a private flag on the seeded command
    (e.g. `yolo wip --_dashboard`, hidden from help), mirroring how the seed
    today is the literal `yolo ps --watch`. The loop only activates when
    `stdin.isatty()` and `$TMUX` is set (same guard as `_ps_picker`).

### Data layer — one unified, sectioned item list

Add `_wip_items(home, base)` returning an ordered list of typed items grouped
into three sections. Each item carries everything an action needs (so the loop
never re-derives cwd from the dashboard's own cwd):

```
Item = {
  kind: "session" | "worktree" | "project",
  label fields for rendering (name/topic/repo/status/ports/state/dir),
  cid:        container id (sessions),
  window:     tmux window id or None (sessions — for Enter→switch),
  repo_dir:   the dir to run `yolo` from for actions,
  topic:      worktree/branch name (worktree items),
}
```

Build it by **reusing and lightly refactoring** existing code:

- **Sessions**: from `_ps_rows(home)` + `_all_tmux_windows()` (already used by the
  picker). These are the running containers. For grouping/sorting (below) the
  item needs the **raw** activity state and age, not just the humanized display
  string `_ps_rows` produces: split `_read_session_state` (yolo.py:4144) into a
  `_session_activity(path, now) -> (state, age_secs) | None` core (returning
  `("waiting"|"working", secs)` or `None` for unknown/`-`) and keep
  `_read_session_state` as a thin formatter over it. `_wip_items` reads the raw
  pair so it can order the section.
- **Worktrees**: refactor `do_list` (yolo.py:4040) to split row-building from
  printing — extract `_worktree_rows(home, base, all_repos=True)` returning
  structured rows (repo name, topic, status, dir, **running bool**, worktree
  path, main-repo path via `_worktree_main_repo`). `do_list` prints from it
  (no behavior change); `wip` filters to **not running** for the inactive
  section. A worktree whose container *is* running already appears in the
  sessions section, so it's excluded here.
- **Projects**: enumerate `_read_projects_file(...)` keys (the registered project
  dirs). Optionally annotate each with whether it currently has a running session
  (from the sessions list) so the section reads as "places you can start work".

### Render layer — sectioned picker

Generalize `_draw_picker` (yolo.py:4366) into a sectioned renderer:

- Section headers (`RUNNING SESSIONS`, `INACTIVE WORKTREES`, `PROJECTS`), each
  followed by its rows; empty sections show a muted "none".
- **Running-sessions ordering** (the section that changes moment to moment):
  group by activity state and sort within each group by how long it's been in
  that state, **longest first** — so the sessions most likely to need attention
  rise to the top:
  1. **`waiting`** sessions, longest-waiting first (idle the longest = readiest
     for you to pick up).
  2. **`working`** sessions, longest-working first.
  3. **unknown / `-`** (a `yolo shell`, or a session that hasn't taken a turn
     yet) last, in a stable order (e.g. by name).

  This ordering is recomputed on every 2s refresh, but selection is tracked by
  stable key (below), so a session crossing from `working` to `waiting` — and
  thus jumping groups — never drags the highlight with it.
- Reuse `_format_table` for column alignment within a section (or per-section
  tables, since columns differ by kind).
- The selected item is highlighted (reverse video, as today). Selection is
  tracked **by a stable key** (`kind:repo:topic` / container name), not row
  index, so a 2s refresh that adds/removes items doesn't move the highlight —
  same invariant the current picker keeps.
- Footer shows context-sensitive keybindings for the selected item kind.

### Interaction layer — loop + keybindings

Add `_wip_loop(home, base, session, wait_key)` modeled on `_ps_picker_loop`
(yolo.py:4327) — same cbreak setup via a shared `_run_picker(...)` helper
(factor the terminal plumbing out of `_ps_picker` so both loops use it), same
`wait_key`/deadline refresh structure, same injectable key source for tests.

**Auto-refresh:** like `ps --watch`, the dashboard redraws itself every
`PS_WATCH_INTERVAL` (2s) with no keypress — the `wait_key(deadline - now)` →
`None` branch re-runs `_wip_items` and resets the deadline, exactly as the
current picker does. So states, ages, the waiting/working ordering, and the
running-vs-inactive split all update live; an action (stop/finish/launch/add)
also forces an immediate refresh rather than waiting for the next tick.

Keybindings (the footer shows only the ones valid for the selected item, so the
state-dependent cases below aren't guesswork on screen):

| Key            | Applies to                                  | Action |
|----------------|---------------------------------------------|--------|
| `j`/`k`/arrows | all                                         | move selection across sections |
| `Enter`        | running session                             | switch to its tmux window |
| `Enter`        | inactive worktree                           | resume → new tmux window |
| `Enter`        | project                                     | start a session (cwd, no worktree) → new tmux window |
| `n`            | project                                     | prompt for a topic, `start TOPIC` (new worktree) → new tmux window |
| `b`            | running session **with forwarded ports**    | `browse` the forwarded port (prompt to pick if >1) |
| `s`            | running session                             | stop (confirm if `working`) |
| `f`            | inactive worktree                           | finish |
| `f`            | **waiting** session                         | stop the (idle) container, then finish |
| `r`            | **waiting** session / inactive worktree     | `rebase` the branch onto `base` |
| `a`            | (global)                                    | add a project (prompt for a path) |
| `q`/`ESC`      | (global)                                    | quit |

**Working sessions are guarded.** `f` and `r` are offered only on *waiting*
sessions and inactive worktrees, never on a `working` one — the same
not-interrupt-active-work stance `yolo stop`/`yolo rebase` already take (to act
on a working session, `s` stops it first, with the active-work confirm). `b` is
state-independent (opening a port doesn't disturb the session). Manual refresh
isn't needed — the 2s auto-refresh plus the post-action immediate refresh cover
it, which frees `r` for rebase.

Action mechanics:

- **Switch** (session Enter): existing `select-window` + cross-session
  `switch-client` (yolo.py:4359–4363). Pure in-process tmux calls.
- **Resume / start / new-worktree** (launches): **shell out** — create a tmux
  window whose command is a fresh `yolo <verb> [TOPIC]` (via `_self_invocation`)
  with `new-window -c <repo_dir>` so the spawned yolo resolves its own config in
  the right cwd, then `os.execvp`s docker into that window. Factor a
  `_spawn_session_window(repo_dir, argv_tail, window_name, session)` helper that
  reuses `_launch_in_tmux`'s new-window + pin + focus machinery but takes a
  `yolo` re-invocation as the command. Non-blocking; the dashboard window
  persists and refreshes on the next tick. (No `--tmux` on the inner call — the
  window *is* the session; the inner yolo just execs docker run there.)
- **Browse** (`b`): read the session's forwarded container ports from its
  `yolo.ports` label (`_container_label(cid, "yolo.ports")`). None → footer
  "no forwarded ports". Exactly one → browse it. More than one → prompt to pick
  (numbered list). Then call `browse_session(cid, select=port)` **in-process**
  (core extracted from `do_browse`, raising `YoloError`; resolves the host port
  via `_docker_port` and opens it with `_open_url` on the host — the dashboard
  runs host-side, so the browser opens normally). Catch `YoloError` → footer.
- **Stop** (`s`): confirm with a y/n keypress (cbreak), then call
  `stop_session(cid, where, home, force=...)` **in-process** with the item's cid
  (`--force` only if the user confirmed an active-`working` stop). Catch
  `YoloError` → show in the footer; on success show "stopped …".
- **Finish** (`f`): confirm, then call `finish_worktree(...)` **in-process** with
  the item's explicit worktree/main-root/slug/topic (honours the resolved
  `finish-action`/`base`). For a **waiting session** (a running but idle
  container, which `finish` would otherwise refuse), first
  `stop_session(cid, …)` — a waiting session stops freely — then
  `finish_worktree(...)`; abort and show the message if either raises
  `YoloError`. Catch `YoloError` → footer.
- **Rebase** (`r`): call `rebase_worktree(worktree, topic, base, …)`
  **in-process** (core extracted from `do_rebase`, raising `YoloError`). It keeps
  `do_rebase`'s session-awareness — a *waiting* session rebases through; the
  dashboard only offers `r` on waiting/inactive items, so the working-session
  refusal never trips here. Stream/collect git's output and surface conflicts in
  the footer (leaving the rebase in-progress in the worktree, as `do_rebase`
  does). Catch `YoloError` → footer.
- **Add project** (`a`): prompt for a path → validate it's a directory (resolve
  its repo root via `git -C <path> rev-parse --show-toplevel`, else use the path
  as-is) → call `register_project(project_key, home)` **in-process**. Catch
  `YoloError` (e.g. already registered) → footer.
- **New worktree** (`n` on a project): prompt for a topic name, then the same
  `_spawn_session_window(project_dir, ["start", TOPIC], …)` launch path.

**Line-input prompts in cbreak mode**: add a `_prompt_line(prompt)` helper that
temporarily restores the saved cooked termios, reads a line with `input()`, then
re-enters cbreak and redraws. Used for the topic name and the project path. Empty
input cancels.

**Confirmations**: a `_confirm(prompt)` helper that draws a footer prompt and
reads a single y/n key in cbreak (no echo) — used before stop/finish.

### Bootstrap & seeding changes

- `_ensure_tmux_session` (yolo.py:3132): change the seeded window-0 command from
  `["…", "ps", "--watch"]` to the `wip` dashboard command (`["…", "wip",
  "--_dashboard"]`). Keep `TMUX_DASHBOARD_WINDOW` and the keep-open-on-failure
  wrapper. Update the docstring.
- `do_wip` bootstrap reuses the focusing block from `_launch_in_tmux`
  (yolo.py:3202–3239). Consider extracting that focus logic into a small
  `_focus_tmux_window(session, window_id)` helper shared by `_launch_in_tmux` and
  `do_wip` to avoid duplication.

## Testing

Mirror `test_tmux.py`'s approach (fake tmux at the `_tmux` seam, canned
`docker ps`, scripted `wait_key`):

- **Data layer**: `_wip_items` / `_worktree_rows` against a real throwaway git
  repo with a couple of worktrees (as `test_verbs.py` does) + stubbed
  `running_container_for` / `_ps_rows`, asserting the three sections, the
  running-vs-inactive split, and project enumeration from a fake `projects.json`.
- **Cores**: `stop_session`/`finish_worktree`/`register_project` raise `YoloError`
  on the refusal paths (active-`working` stop without force, dirty/running
  finish, already-registered project) and succeed otherwise — tested directly,
  plus that the verb wrappers still `sys.exit` (CLI behavior unchanged).
- **Loop**: `_wip_loop` with scripted keys — selection movement across sections
  and clamping, Enter→switch for a session, Enter→`_spawn_session_window` for a
  worktree (assert the `yolo` re-invocation argv + `new-window -c <dir>` via the
  fake `_tmux`), `n`/`a` prompt flows (inject the line input), `s`/`f`
  confirm→in-process core call (stub the core, assert args; assert a raised
  `YoloError` lands in the footer rather than killing the loop), refresh
  preserving selection by key, quit.
- **Bootstrap/seeding**: `_ensure_tmux_session` now seeds the `wip` window;
  `do_wip` focuses it (inside-tmux switch, no-client attach, attached-elsewhere
  no-mirror), reusing the `_launch_in_tmux` assertions.
- Keep the existing `ps`/`ps --watch` picker tests green (those paths are
  unchanged except for the shared `_run_picker` refactor).

## Documentation

- `CLAUDE.md`: add a `wip` section (the dashboard, its sections and keys, the
  tmux-dashboard seeding change, the requires-tmux note) and add `wip` to the
  verb list / synopsis block. Note the cross-repo subprocess-with-cwd action
  model. (Use the `update-claude-md` skill after implementation.)
- `README` synopsis lines: add `./yolo.py wip`.
- `CHANGELOG` + version bump (`uv run bump-my-version bump minor`) — new feature.

## Suggested implementation phases

1. **Refactors (no behavior change):** introduce `YoloError` and the
   core/wrapper split for `stop_session`/`finish_worktree`/`register_project`/
   `browse_session`/`rebase_worktree` (with the central `main()` catch); split
   `_read_session_state` into the raw `_session_activity` core + formatter;
   extract `_worktree_rows` from `do_list`;
   extract `_run_picker` terminal plumbing from `_ps_picker`; extract
   `_focus_tmux_window` from `_launch_in_tmux` and `_spawn_session_window` from
   its new-window machinery. Land with tests still green.
2. **Read-only dashboard:** `wip` verb + dispatch + bootstrap/seeding +
   `_wip_items` + sectioned renderer + `_wip_loop` with navigation, Enter
   (switch / resume / start), and quit. This already delivers the core "see
   everything + jump to or launch sessions" value.
3. **Lifecycle actions:** `s` stop, `f` finish (with confirmations), `n` new
   worktree (with topic prompt), `a` add project (with path prompt).
4. **Docs + changelog + version bump.**

## Open questions for review

None outstanding — the design, action model, keybindings (`Enter`/`n`/`b`/`s`/
`f`/`r`/`a`/`q`), session grouping, and auto-refresh are all settled. Remaining
choices (exact footer wording, the multi-port pick UI) are implementation-time
details.
