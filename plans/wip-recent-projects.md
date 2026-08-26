# Plan: show only recently-used projects on the wip dashboard

Goal: the PROJECTS section of `yolo wip` should naturally shrink to the
projects you're actually working in, instead of listing every entry ever
registered in `projects.json` plus every directory ever opened. Project
*configuration* is untouched — entries stay in `projects.json` and keep
layering into launches — this is purely a display filter, with an in-dashboard
affordance to reveal the full list.

---

## What exists today

All the data needed for a recency filter is already collected; nothing new
needs to be recorded.

- **`_wip_projects(home, sessions)`** builds the section's rows: every
  registered `projects.json` entry (alphabetical), then every
  recent-projects-registry dir not already covered by a registered entry
  (most-recently-opened first). Each row gets an `active` flag — true when a
  running session's cwd is at or under the row's path.

- **The recent-projects registry** (`~/.claude-yolo/recent-projects.json`,
  `_record_recent_project`) maps a project key (main repo root, else cwd) →
  `{"last_opened": iso}` and is stamped on **every** launch. Today
  `_wip_projects` uses it only to *discover* unregistered dirs and to order
  them; registered rows never look at their own stamp. Entries whose resolved
  path matches a registered `dir` are dropped from the recent list (exact
  `Path.resolve()` match).

- **`_wip_items(home)`** computes the worktree rows (each carries
  `main_root`, the worktree's main repo root) *before* calling
  `_wip_projects`, so worktree information is available at the right moment
  for free.

- **`_wip_loop`** owns per-view state (the viewport `top`, the selection key)
  and re-calls `_wip_items` on every 2s tick and after every action.
  `_wip_action` handles world-mutating keys; pure view-state keys (j/k, q)
  are handled in the loop itself.

## Design decisions

### 1. The visibility rule

A project row is shown by default when **any** of:

- it is `active` (a running session at or under its dir — already computed);

- it has at least one worktree — some worktree row's `main_root` is the
  row's path or a parent of it (a project dir is normally the repo's main
  root; the parent case covers a project dir inside a repo);

- its `last_opened` stamp is within `WIP_RECENT_DAYS` days (new module
  constant, **14**; no config key until someone actually wants to tune it).

The rule applies uniformly to registered and recent-only rows. Recent-only
rows exist *because* of a stamp, so for them it's effectively just the
recency cutoff — old one-off dirs age out of the dashboard, which is half the
point. A registered project with an active worktree stays visible however
stale its stamp: outstanding work is exactly what wip is for.

### 2. Recency lookup for registered rows

`_wip_projects` already reads the registry; per registered row, look up the
stamp whose key resolves to the row's resolved `dir` (the same exact-match
rule the existing dedup uses). Parse `last_opened` with
`datetime.datetime.fromisoformat` under `try/except` — the registry is
lenient by design, so an unparseable stamp just counts as "not recent".

Known limitation (pre-existing, same as the dedup): a project whose `dir` is
a *subdirectory* of its repo gets stamped under the repo root, so its stamp
won't match. Such a project shows via the worktree/active rules or the
reveal toggle; not worth a containment heuristic now.

### 3. Where the filter runs

`_wip_projects` grows keyword args (defaults preserve the current behavior
for direct test callers):

- `worktree_roots: set[Path]` — the `main_root`s of the worktree rows,
  passed by `_wip_items`;

- and computes per-row `hidden: bool` per the rule above, **returning all
  rows** with the flag rather than filtering itself.

`_wip_items(home, all_projects=False)` then drops `hidden` rows unless
`all_projects`, so the one place that assembles WipItems also owns how many
there are, and the hidden count is available for the toggle row.

### 4. Revealing the full list: a "… N more" toggle row

When any rows are hidden, append a selectable row (kind `"allprojects"`,
stable key `"allprojects:toggle"`) just above the trailing `+` row:

- collapsed: cols `("… N more", "")` — Enter reveals all projects;

- expanded: cols `("… show fewer", "")` — Enter collapses again.

The expanded/collapsed state (`all_projects`) lives in `_wip_loop` alongside
`top`/`selected`, and Enter on the toggle row is handled **in the loop**, not
`_wip_action` — it's view state, like scrolling, and the loop already
re-fetches sections after every keypress. All `_wip_items` call sites in the
loop pass the flag. Selection stability is free: the toggle row's key doesn't
change across states, and if the selected row disappears on collapse the loop
already falls back to the first row.

Add an `_WIP_HINTS["allprojects"]` entry (e.g. `"Enter show all configured
projects"` / the row text makes the collapse case obvious) and teach
`_color_project_row` the new kind (render it grey like an affordance, mirroring
the `+` row's green). While expanded, rows that would be hidden render grey
(dim) — the payload carries `hidden`, and `_color_project_row` receives the
whole item — so the revealed tail is visually distinct from the live set.

### 5. A freshly registered project must not vanish

`a` (register) on a prompted-for path writes a `projects.json` entry for a
dir that may have no stamp — under the filter it would be invisible the
moment it's created. Fix at the source: `register_project` (the shared core
behind the dashboard's `a` and `config --init`) also stamps the dir's
resolved path via `_record_recent_project`. Registering *is* use.

(`a` on a recent-only row needs nothing: that row was visible because its
stamp is recent, and the registered row inherits the same dir/stamp.)

### 6. Out of scope

- `yolo list`, `ps`, and the config verb are untouched — this is wip-only
  display behavior.

- No pruning of `recent-projects.json` itself; old entries are harmless and
  the file stays a complete record.

- No config key for the cutoff; `WIP_RECENT_DAYS = 14` is a constant.

## Implementation steps

1. **`WIP_RECENT_DAYS = 14`** constant near the other wip constants.

2. **`_wip_projects`**: add `worktree_roots=frozenset()` kwarg; compute per
   row `last_opened` (registered rows: registry lookup by resolved dir;
   recent rows: their own entry), `has_worktree`, and from those + `active`
   the `hidden` flag. Return all rows.

3. **`_wip_items`**: grow `all_projects=False` kwarg; pass the worktree
   `main_root`s into `_wip_projects`; filter hidden rows unless
   `all_projects`; carry `hidden` into each project WipItem's payload; append
   the `allprojects` toggle row (with the hidden count) above the `+` row
   whenever the unfiltered list has hidden rows.

4. **`_wip_loop`**: `all_projects = False` state; thread it through every
   `_wip_items` call; handle Enter on kind `"allprojects"` in the loop
   (toggle + immediate refresh) before dispatching to `_wip_action`.

5. **`_color_project_row` + `_WIP_HINTS`**: color the toggle row; dim
   `hidden` rows when they're shown (expanded mode).

6. **`register_project`**: stamp `_record_recent_project(home, resolved)`
   after a successful write.

7. **Tests** (`tests/test_wip.py`, plus the `_wip_items` project-row tests in
   `test_multi_repo.py`):

   - visibility rule: registered project with no stamp/worktree/session is
     hidden; shown when (a) stamped within 14 days, (b) a worktree row's
     `main_root` matches, (c) a session is running there; recent-only dir
     older than the cutoff is hidden.

   - toggle row: appears with the right count only when something is hidden;
     absent when nothing is; a FakeTerm loop test drives Enter on it and
     asserts the full list appears (with "show fewer") and collapses again.

   - `register_project` stamps the registry.

   - **Existing tests will need stamps**: several tests register projects
     and expect them listed (`test_wip_items_lists_named_project_rows`,
     `test_wip_projects_unions_recent_registry`, the project-cols tests, the
     config-editor entry tests, …). Registering now stamps recency (step 6),
     so most pass untouched; any that write `projects.json` directly should
     either stamp `last_opened` or assert against the unfiltered rows.

8. **Docs**: README's wip section (default = recently-used projects, the
   "… N more" reveal, the 14-day window); ARCHITECTURE.md's wip dashboard
   section (visibility rule, where the state lives); CHANGELOG `## Unreleased`
   entry.

## Verification

`uv run pytest tests/test_wip.py tests/test_multi_repo.py` plus the full
suite; then the `verify` skill (docker-free CLI/dashboard harness) to drive a
real dashboard: seed a throwaway HOME with a mix of stamped/stale projects
and confirm the collapsed view, the toggle, and that `a` on a fresh path
leaves the new project visible.
