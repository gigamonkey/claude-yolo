# Plan: an interactive config editor in `yolo wip`

## Goal

Replace the dashboard's current `c` action — a single blind prompt for a line of
yolo flags — with a **modal config editor** that:

1. **Shows the current config** for the selected row's scope (a worktree's overlay
   or a project's entry), with the inherited/global values as read-only context.
2. Lets the user **change an existing value** by selecting its key and being
   re-prompted with an input appropriate to that key's type.
3. Lets the user **add a new value** by **picking a key** from the ones not yet
   set, then being prompted for the value — with **Tab completion on directory
   names** when the key takes a path (the headline case: `mounts`, `plugin-dirs`).
4. Lets the user **remove** a key (or a single element of a list key).

All persistence still flows through `yolo config`, so every existing validation,
dedup, and conflict rule is reused unchanged — the editor is a *front-end* that
composes the right `yolo config` invocations, not a second writer.

## Context: the current `c` action

`_wip_action` (yolo.py:5550) dispatches `c` to `_wip_config` (yolo.py:5682),
which:

- Resolves the scope from the selected row: a **worktree** row edits
  `worktrees.json[<path>]` via `yolo config <topic> …` run from the main repo; a
  **project** row edits `projects.json[<key>]` via `yolo config …` run from the
  project dir. (Any other row kind is rejected.)
- Calls `term.prompt_line("config flags for <label> (e.g. --mount ~/x --port
  8000): ")`, `shlex.split`s the answer, and runs `[_self_invocation(), *args,
  *flags]` as a subprocess, surfacing its stderr in the footer on failure.

Limitations this plan removes:

- **You must know the flag syntax** (`--mount`, `:ro`, `[HOST:]CONTAINER`, …).
- **It doesn't show what's currently set** — it's a blind one-shot.
- **No per-value Tab completion**: the whole line goes through `prompt_line`
  (cooked `input()`), so directory completion for mounts/plugin-dirs is
  unavailable. (`prompt_path` + `_complete_path` already exist for the `+`/`a`
  prompts, but `c` doesn't use them.)

## Reuse: what already exists

- **`_PickerTerm`** (yolo.py:4870) is the terminal surface every wip prompt uses.
  It already offers `wait_key`, `prompt_line` (cooked line), `prompt_path` (raw
  cbreak line with directory Tab completion via `_complete_path`, yolo.py:4844),
  and `confirm` (one-key y/N). The editor needs no new terminal plumbing — it runs
  as a **nested loop on the same `term`**, exactly like `_diff_stat_loop`
  (yolo.py:4330) is a modal sub-screen reached from the dashboard's `d`.
- **`_effective_config(home, cwd)`** (yolo.py:2197) returns the merged
  global+project config with per-key provenance — the data behind a bare `yolo
  config`. Useful for the read-only "inherited" context pane.
- **`_read_projects_file` / `_read_worktrees_file`** read the raw stored entries
  (the *editable* layer) directly, in-process, no subprocess.
- **`YOLO_KEYS`** (yolo.py:1533) maps each key to `(dest, kind)` where kind ∈
  `{path, auth, finish, str, bool, list}`; **`_CONCAT_DESTS`** (yolo.py:1558)
  marks the list keys. `AUTH_CHOICES` / `FINISH_CHOICES` enumerate the choice
  keys. This metadata drives the per-key input type — no hand-maintained second
  list.
- **`yolo config`** (`do_config` → `_apply_config_edits`, yolo.py:~2024) already
  implements whole-key set, `--add-*`/`--remove-*` element edits, `--unset`, and
  all validation. The editor composes these.

## Design decisions

### D1 — Reads in-process, writes via `yolo config` subprocess

Display reads the raw stored entry (`_read_projects_file`/`_read_worktrees_file`)
and the effective merge (`_effective_config` / `load_yolo_config(...,
worktree_dir=...)`) directly. Each *write* runs one `yolo config [<topic>]
<flags>` subprocess (as `_wip_config` does today), so all validation/persistence
stays in one place and the editor can't desync from the loader. Writes are
infrequent (a keystroke per edit, not per frame), so subprocess cost is a
non-issue. **Rejected:** calling `_apply_config_edits` in-process — it's coupled
to an argparse `parsed` namespace and `do_config`'s file I/O; faking that is more
code and a second code path to validate.

### D2 — Scope is the selected row's own layer

A **worktree** row edits its overlay (`worktrees.json[path]`), a **project** row
its entry (`projects.json[key]`) — identical to today's `c`. The editor shows the
stored entry as the **editable** set and the inherited lower-layer keys as
**read-only context** (dimmed, labeled with their source), so the user sees the
full effective picture but edits only the layer this row controls. **Decided:**
the inherited pane is always shown. It's computed as `_effective_config(home,
base_cwd)` minus the keys already in the editable entry, where `base_cwd` is the
project path (project scope → inherited = global only) or the worktree's main repo
(worktree scope → inherited = global + project entry). Adding an
inherited-but-unset key via `a` overrides it in this layer. Editing the **global**
`~/.yolo.json` is out of scope for v1 (it's not tied to a row); note a possible
future `g`-to-edit-global toggle.

### D3 — Per-key input type, derived from `YOLO_KEYS[kind]`

| kind | keys | prompt |
|---|---|---|
| `bool` | claude-json, ssh-agent, submodules, require-project-entry, tmux | toggle `true`/`false` (a one-key pick, no free text) |
| `auth` | auth | pick from `AUTH_CHOICES` |
| `finish` | finish-action | pick from `FINISH_CHOICES` |
| `path` | config-dir, dockerfile, yolorc | `prompt_path` (Tab completion) |
| `str` | aws-profile, aws-region, bedrock-model, base, finish-remote, tmux-session | `prompt_line` |
| `list` | mounts, ports, secrets, plugin-dirs, prompts | drill into an element view (see D4) |

A small `_CONFIG_INPUT` table maps each key → an input strategy, defaulting off
`kind` so it stays in sync with `YOLO_KEYS`. The choice lists come straight from
`AUTH_CHOICES`/`FINISH_CHOICES`. **Decided:** bool and choice keys use a minimal
`j/k`+Enter vertical list picker (`_pick_one`) — `["true", "false"]` for a bool,
the choice list for `auth`/`finish-action` — not free-text entry.

**Flag construction note.** The bool keys are argparse `BooleanOptionalAction`, so
they persist via `--<key>` (true) / `--no-<key>` (false), *not* `--<key> <value>`.
The write helper emits `--ssh-agent` vs `--no-ssh-agent` for a bool, `--<key>
<value>` for choice/str/path, and `--add-<stem>` / `--remove-<stem>` for a list
element (the stem map: mounts→mount, ports→port, secrets→secret,
plugin-dirs→plugin-dir, prompts→prompt).

### D4 — List keys drill into an element view

Selecting a list key (or "add" when it's a list key) opens a sub-view listing the
current elements with **add element** / **remove element** actions. The
add-element prompt is per-key:

| list key | element spec | add-element prompt |
|---|---|---|
| `mounts` | `PATH[:ro|:rw]` | `prompt_path` for the dir, then a one-key `ro`/`rw` (default `ro`) |
| `plugin-dirs` | `PATH` | `prompt_path` |
| `ports` | `[HOST:]CONTAINER` | `prompt_line` |
| `secrets` | `NAME[:TARGET]` | `prompt_line` (stretch: offer stored names from `yolo secret list`) |
| `prompts` | free text | `prompt_line` |

Add → `yolo config --add-<key> <spec>`; remove → `yolo config --remove-<key>
<spec>`. This is exactly the existing element-edit surface, so dedup/validation is
free. (`prompts` uses `--add-prompt`/`--remove-prompt`; the dest↔flag mapping is a
tiny lookup.)

### D5 — Path completion stays directory-only for v1

`_complete_path` filters to directories (yolo.py:4858), which is right for
`mounts`/`plugin-dirs`/`config-dir` — the headline ask. `dockerfile`/`yolorc` are
*files*, so Tab won't complete them in v1 (the user types the full path; `yolo
config` still validates existence). **Stretch:** add a `files=True` option to
`_complete_path`/`prompt_path` that also globs non-dir entries, used for the
file-valued path keys. Called out, not built in v1.

### D6 — Keep a raw-flags escape hatch

Power users lose the "type `--mount ~/x --port 8000`" one-liner if the editor
fully replaces it. Keep it as an in-editor action (e.g. `e` → "enter raw flags")
that runs the old `prompt_line` → `shlex.split` → `yolo config` path. Cheap to
retain, and it covers anything the structured UI doesn't.

## UI flow

`c` opens the editor sub-screen (drawn over the dashboard frame, same cbreak
`term`); `q`/`Esc` returns. A mock for a worktree row:

```
 config · worktree fix-auth  (worktrees.json)                 ↑↓ move

   auth          bedrock              (set here)
   mounts        ~/refdocs:ro         (set here)
                 ~/data:rw
   ports         8000                 (set here)
 ▸ ssh-agent     true                 (set here)
   base          origin/main          (inherited: ~/.yolo.json)

   Enter edit · a add key · x remove · e raw flags · q done
   ─────────────────────────────────────────────────────────
   saved: ssh-agent = true
```

- **Navigation** `j`/`k`/arrows over the editable (set-here) keys.
- **`Enter`** on a scalar key → re-prompt its value (typed by kind). On a list key
  → open the element view (D4).
- **`a`** (add key) → a **key picker** of `YOLO_KEYS` not yet set in this scope;
  select one → prompt for its value (by kind) → write. For a list key, picking it
  jumps straight to "add element".
- **`x`** (remove) → on a scalar, `--unset KEY`; on a list element (in the element
  view), `--remove-<key> <spec>`; on a whole list key, `--unset KEY` after a
  `confirm`.
- **`e`** → raw-flags escape hatch (D6).
- Each write's result (or the subprocess stderr on failure) shows in the footer;
  the screen re-reads the stored entry and redraws. On return to the dashboard,
  plain `Enter` launches with the saved config (the dashboard already re-resolves
  config live per row).

## Implementation (all in `yolo.py` unless noted)

### 1. A scope record

A small helper `_config_scope(kind, payload, home)` → a record with:

- `read()` → the raw stored entry dict for this scope
  (`_read_projects_file[key]` or `_read_worktrees_file[path]`, `{}` if absent).
- `inherited()` → effective keys from lower layers for the read-only pane
  (`_effective_config` for a project; `load_yolo_config(main_root, …,
  worktree_dir=…, quiet=True)` minus the overlay for a worktree).
- `config_args` → the `yolo config` prefix (`["config"]` or `["config", topic]`).
- `cwd` → where to run the subprocess (project path, or the worktree's main repo).
- `label` → display label.

Mirrors the branch already in `_wip_config`; factor that resolution into this
record so the editor is scope-agnostic.

### 2. The input-type table

`_CONFIG_INPUT`: key → strategy, derived from `YOLO_KEYS` kinds + the choice
lists, plus the per-list-key element-prompt map (D4) and the path-completable set
(D5). One table, so adding a future config key needs only a `YOLO_KEYS` entry and
(if it's a path/list) a line here.

### 3. Value prompts

`_prompt_config_value(term, key)` → the new value string (or `None` if cancelled),
dispatching on `_CONFIG_INPUT[key]`:

- bool → `_pick_one(term, ["true", "false"])` (a tiny one-key chooser, or reuse
  `confirm` phrased as the key).
- choices → `_pick_one(term, AUTH_CHOICES / FINISH_CHOICES)`.
- path → `term.prompt_path(...)`.
- str → `term.prompt_line(...)`.

`_pick_one` is a minimal inline list-picker (j/k + Enter) for the small
bool/choice sets — or, simpler, cycle on a keypress. Decide during
implementation; a 2–4 item picker is trivial.

### 4. List-element view + prompt

`_config_list_loop(scope, key, term)` — the element sub-view: lists current
elements, `a` adds (via `_prompt_list_element(term, key)` per D4), `x` removes the
selected element, `q` returns. Each add/remove runs `yolo config --add-<key> /
--remove-<key>`.

### 5. The write helper

`_config_apply(scope, flags)` → runs `[_self_invocation(), *scope.config_args,
*flags]` from `scope.cwd`, returns `(ok, message)` (stderr on failure). The single
funnel for every editor write; the current `_wip_config` subprocess call collapses
into this.

### 6. The editor loop + draw

`_config_editor_loop(scope, term)` and `_draw_config_editor(scope, selected,
footer)`, modeled on `_diff_stat_loop`/`_draw_diff_stat`. The loop re-reads
`scope.read()` after each write so the screen reflects the new state. `_wip_config`
becomes a thin launcher: build the scope, call the loop, return a footer string
(e.g. "edited config for fix-auth — press Enter to launch with it").

### 7. Dispatch

`_wip_action`'s `c` branch (yolo.py:5550) still calls `_wip_config`; only its body
changes. The dashboard loop already runs inside `_run_picker`'s cbreak context, so
the nested editor loop reuses the same `term` with no extra setup/teardown
(again, exactly as `d`/`_diff_stat_loop` does).

## Testing (`tests/test_wip.py`)

The editor loops take an injectable `term` (a `FakeTerm` with scripted
`wait_key`/`prompt_line`/`prompt_path` returns), and writes go through
`_self_invocation`/`subprocess.run` — both already stubbed in the wip tests
(`_stub_config_run`). So:

- **Show**: `_draw_config_editor` renders the stored entry's keys/values and marks
  inherited keys read-only (against a real repo + a seeded `worktrees.json` /
  `projects.json`).
- **Edit scalar**: select a key, scripted Enter + value → asserts the composed
  `yolo config … --<key> <value>` argv (subprocess stubbed) and, with a real
  (unstubbed) `yolo config`, the resulting stored entry.
- **Add key**: `a` → key picker → value prompt → the right `--<key>`/`--add-<key>`
  call; per-kind prompt routing (bool/choice/path/str).
- **List element**: add (path via `prompt_path`, then ro/rw) and remove → the
  `--add-mount`/`--remove-mount` calls; ports/secrets/prompts element prompts.
- **Remove/unset**: scalar `--unset KEY`; list element `--remove-<key>`.
- **Errors**: a failing `yolo config` (e.g. a missing mount path) surfaces in the
  footer without crashing the loop (subprocess returncode≠0).
- **Raw-flags escape hatch** still works (the old path).
- `_complete_path` is already covered; add a case only if `files=True` (D5
  stretch) is built.

End-to-end the existing real-repo wip tests exercise that a saved edit then
launches with the new config (the dashboard re-resolves live), so no new
integration scaffolding is needed.

## Documentation

- **README** — the `wip` key table currently says `c` "prompt[s] for a line of
  yolo flags …"; rewrite it to "open an interactive editor of this
  worktree's/project's config (shows current values, edit/add/remove keys; Tab
  completes directory mounts)". Keep the "plain Enter then launches with it" note.
- **CLAUDE.md** — the `wip` section's `c` description and the test-file note for
  `test_wip.py` (add the editor cases).
- **CHANGELOG** — an Unreleased entry: "`yolo wip`'s `c` is now an interactive
  config editor (view current config, edit/add/remove keys, Tab-completed
  directory mounts) instead of a single raw-flags prompt."

## Risks / watch-items

- **Scope creep in the UI.** Three nested levels (editor → key picker → list
  element view) is the ceiling for v1. Keep `_pick_one` and the list view minimal;
  don't build mid-line editing into the prompts (the existing prompts append-only,
  which is fine).
- **Path normalization.** `_complete_path` expands `~` to an absolute path on
  completion, so a Tab-completed mount stores an absolute path rather than `~/x`.
  That's valid (config accepts absolute) but differs from a hand-typed `~/x`;
  acceptable, and `yolo config` validates either way. Note it so it isn't a
  surprise.
- **Subprocess-per-edit.** Fine for interactive use; if it ever feels slow, the
  writes could batch, but don't pre-optimize.
- **`prompts` free-text** can contain spaces/quotes; it goes through `prompt_line`
  → a single `--add-prompt <text>` arg (no `shlex` needed since it's one value),
  so quoting isn't a problem. The raw-flags hatch (`e`) is the only path that
  `shlex.split`s, as today.

## Summary of changes

- New: `_config_scope`, `_CONFIG_INPUT`, `_prompt_config_value`, `_pick_one`,
  `_prompt_list_element`, `_config_list_loop`, `_config_apply`,
  `_config_editor_loop`, `_draw_config_editor` (all in `yolo.py`).
- Changed: `_wip_config` (yolo.py:5682) becomes a thin launcher for the editor;
  the `c` dispatch in `_wip_action` is unchanged.
- Reused unchanged: `yolo config`/`_apply_config_edits` (all validation/persist),
  `_PickerTerm`/`prompt_path`/`_complete_path`, `_effective_config`,
  `_read_projects_file`/`_read_worktrees_file`, `YOLO_KEYS`/`_CONCAT_DESTS`,
  `AUTH_CHOICES`/`FINISH_CHOICES`, the `_diff_stat_loop` modal pattern.
- Tests in `tests/test_wip.py`; docs in README/CLAUDE.md/CHANGELOG.
- Stretch (deferred): file-aware `_complete_path` (`files=True`) for
  dockerfile/yolorc; a `g` toggle to edit global `~/.yolo.json`; secret-name
  completion from `yolo secret list`.
```
