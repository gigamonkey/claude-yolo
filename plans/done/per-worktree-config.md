# Plan: per-worktree overlay configuration

## Context

yolo's host-side config has two layers today (`load_yolo_config`, yolo.py:972),
merged low→high and then beaten by explicit CLI flags:

1. `~/.yolo.json` (global)
2. `~/.claude-yolo/projects.json` entry (per-project, keyed by directory path,
   nearest-wins via `_match_project_entry`, yolo.py:930)

Worktree sessions (`yolo start TOPIC`, `resume TOPIC`, `shell TOPIC`) currently
inherit only the *project* entry (they share it by design — `_project_key`
resolves to the main repo root, yolo.py:1104). So config flags passed at
`start` — `yolo start fix-auth --auth bedrock --mount ~/refdocs --port 8000` —
apply to that one launch and are **lost on resume**: you must retype them.

**Goal.** A worktree gets its own persisted overlay, the most specific config
layer:

- **Populated** at `yolo start TOPIC [config flags]` from the explicit CLI flags,
  so `yolo resume TOPIC` later relaunches with the same config, no retyping.
- **Editable** via `yolo config TOPIC [flags]` (the same show/set/`--add-*`/
  `--unset` UX as the project entry).
- **Removed** by `yolo finish TOPIC`, with the worktree.

**Layer precedence becomes:** `~/.yolo.json` < `projects.json` entry <
**worktree overlay** < CLI flags. A worktree is more specific than its project,
so it slots just under the CLI — and like `mounts`/`ports`/`prompts` elsewhere,
the concat keys *accumulate* across all layers (`_CONCAT_DESTS`, yolo.py:859).

## Decisions

- **Separate file `~/.claude-yolo/worktrees.json`**, mapping *worktree absolute
  path* → config object (same value shape as a `projects.json` entry, validated
  by the existing `_parse_yolo_dict`). Reasons not to reuse `projects.json`:
  - `projects.json` is a deliberate, auditable ledger that **only `yolo config`
    ever writes**; the dangling-key warning (`_warn_dangling_keys`, yolo.py:947)
    and `require-project-entry` lean on that invariant. Worktree overlays are
    *auto-managed* (created by `start`, deleted by `finish`) — mixing them in
    would make every live worktree look like a project entry and muddy those
    warnings.
  - Different lifecycle, different file — cleaner separation.

  **Path-as-identity** is kept (consistent with `projects.json` keys, the
  `worktrees/<slug>/<topic>` state dir, and the docker labels): the full
  worktree path `~/.claude-yolo/worktrees/<slug>/<topic>` already encodes the
  repo (via slug) + topic, so two repos with a same-named topic don't collide.

- **Security invariant (load-bearing).** `worktrees.json` lives directly under
  `~/.claude-yolo/`, which is **never bind-mounted** into a container — only the
  individual `worktrees/<slug>/<topic>` *worktree dirs* are (CLAUDE.md: "only
  `worktrees/<slug>/<topic>` dirs under it are ever mounted, never
  `projects.json`"). This is essential: an overlay can grant host access
  (`mounts`, or an arbitrary rw mount via `config-dir`), so it must not be
  writable from inside the skip-permissions container. Storing it as a sibling
  of `worktrees/` — not inside any worktree — preserves the structural property
  that nothing yolo reads for a launch is container-writable. **Do not** store it
  inside the worktree dir.

- **Editing UX is `yolo config TOPIC`** (a new optional positional for the
  `config` verb, matching `start`/`resume`/`shell`/`finish`). Running `yolo
  config` *from inside* a worktree dir can't work: `_project_key` follows the
  shared `.git` back to the main repo root, so it would target the project entry,
  not the worktree. An explicit `TOPIC` is unambiguous and consistent.

- **Only `start` populates; `resume`/`shell` are read-only consumers.** Matches
  the spec ("initially populated"): the overlay is a deliberate snapshot taken at
  creation, then edited only via `yolo config`. A stray `resume TOPIC --port
  9000` applies for that run (CLI wins) but does **not** silently mutate the
  persisted overlay — predictable, and `config` stays the one writer of edits.

- **`start` always writes an entry, even an empty `{}`** (when no config flags
  were passed), so the overlay's lifecycle is symmetric with the worktree
  (created by `start`, removed by `finish`) and `yolo config TOPIC` always has
  something to show. *(Alternative considered: only write when ≥1 explicit flag,
  to avoid `{}` clutter. Rejected for asymmetry — a worktree would then
  sometimes have an overlay and sometimes not, complicating show/provenance. The
  empty entry is inert.)*

- **`base` is persisted like any other explicit `YOLO_KEYS` flag** (it's a config
  key) but is **inert on resume** — `base` is consumed only by `start`/`list`, so
  a persisted `base` never affects a resumed session. Accepted as a harmless
  wart rather than special-casing the population to drop it; keeps it identical to
  `_explicit_config_flags` / `yolo config`.

## Changes (all in `yolo.py` unless noted)

### 1. `worktrees.json` read/write helpers

Mirror the `projects.json` helpers, near `_read_projects_file` (yolo.py:917):

- `_worktrees_file(home) -> Path` → `home / ".claude-yolo" / "worktrees.json"`.
- `_read_worktrees_file(path) -> dict` — identical shape/validation to
  `_read_projects_file` (JSON object mapping path→object; pointed `sys.exit` on
  malformed), so a corrupt file fails loudly, never silently.
- `_write_worktrees_file(path, data)` — `mkdir(parents=True)` + `json.dumps(...,
  indent=2) + "\n"` (factor the existing inline writes if convenient).
- `_worktree_overlay_key(worktree_path) -> str` → `str(worktree_path.resolve())`
  (one definition shared by populate/edit/remove/load, so they always agree).

### 2. Layer the overlay in `load_yolo_config`

Extend the signature to `load_yolo_config(start, home, *, worktree_dir=None)`
(yolo.py:972). After the `projects.json` merge (yolo.py:1004-1006) and before the
provenance line:

```python
if worktree_dir is not None:
    wt = _read_worktrees_file(_worktrees_file(home))
    entry = wt.get(_worktree_overlay_key(worktree_dir))
    if entry is not None:
        merge(_parse_yolo_dict(entry, f"worktrees.json [{worktree_dir.name}]"))
        layers.append(f"worktrees.json[{worktree_dir.name}]")
```

`merge` already does concat-vs-override correctly (yolo.py:988), so the overlay
overrides scalars from lower layers and *concatenates* `mounts`/`ports`/`prompts`
onto them — exactly the desired precedence. The provenance line then reads e.g.
`config: ~/.yolo.json + projects.json[…] + worktrees.json[fix-auth]`.

`_parse_yolo_dict` reuse means an overlay with a bad key/type fails with the same
pointed message as the other layers.

### 3. Thread `worktree_dir` from `main`

In `main`, compute the worktree dir up front for the launch verbs and pass it to
the config load (yolo.py:3131):

```python
worktree_dir = None
if topic and verb in ("start", "resume", "shell"):
    worktree_dir, _, _ = _worktree_dir(topic, home)   # path only; doesn't create
config_defaults, matched_project_key = load_yolo_config(cwd, home, worktree_dir=worktree_dir)
```

On a fresh `start` the file has no entry yet → no-op (the CLI flags carry the run,
and step 4 writes the overlay afterward). On `resume`/`shell` the overlay is
layered under the CLI. `browse`/`finish`/`dir`/`config` don't take this path
(`browse`/`finish` read labels / remove; `config` is dispatched earlier).

### 4. `start` populates the overlay

In the `start` branch, right after `setup_worktree` reassigns `cwd` to the
worktree dir (yolo.py:3269):

```python
explicit = _explicit_config_flags(script_argv)   # {dash-key: value}, CLI-only
where = f"worktrees.json [{topic}]"
_parse_yolo_dict(explicit, where)                 # never persist an unloadable entry
wt = _read_worktrees_file(_worktrees_file(home))
wt[_worktree_overlay_key(cwd)] = explicit         # may be {}
_write_worktrees_file(_worktrees_file(home), wt)
```

`_explicit_config_flags` (yolo.py:1114) is the same sentinel re-parse `yolo
config` uses, so population captures *exactly* the flags the user typed (not
defaults), in the dash-keyed shape `worktrees.json` stores. The launch already
validated `mounts`/`ports` via `_resolve_mounts`/`_resolve_ports`, so the values
are sound; the `_parse_yolo_dict` call is the same belt-and-suspenders guard
`do_config` applies before writing.

### 5. `yolo config TOPIC` — show/edit the overlay

- **Allow the positional for `config`**: add `"config"` to the topic-allowed verb
  tuple (yolo.py:3077) so `yolo config fix-auth …` isn't rejected as an
  unexpected argument.
- **Thread `topic` into `do_config`** (dispatched at yolo.py:3112-3113): change
  to `do_config(script_argv, home, cwd, parsed, topic)`.
- In `do_config` (yolo.py:1276), when `topic` is set, target `worktrees.json`
  instead of `projects.json`:
  - Resolve `worktree, _, _ = _worktree_dir(topic, home)`; key =
    `_worktree_overlay_key(worktree)`.
  - **Guards:** `--global` with a topic → error ("`--global` edits ~/.yolo.json;
    it can't target a worktree"); `--init` with a topic → error ("`--init`
    registers a project entry, not a worktree overlay"). Place beside the existing
    `--init`/`--global` guards (yolo.py:1309-1316).
  - For **edits**, require the worktree to exist (`worktree.is_dir()`, pointed
    error pointing at `yolo start TOPIC`) — editing config for a non-existent
    worktree is meaningless.
  - **Show** (no flags): print the overlay entry (or "no overlay for `TOPIC`").
  - **Set/edit**: read-modify-write via the *existing* `_apply_config_edits`
    (yolo.py:1173) against `dict(wt.get(key, {}))`, re-validate with
    `_parse_yolo_dict`, write back — structurally identical to the project path
    (yolo.py:1374-1381), just a different file + key. Factor the
    project/worktree write paths to share one helper if it reads cleanly.

### 6. `finish` removes the overlay

In `do_finish` (yolo.py:2432), after the worktree is successfully removed
(yolo.py:2454-2455):

```python
wt = _read_worktrees_file(_worktrees_file(home))
if wt.pop(_worktree_overlay_key(worktree), None) is not None:
    _write_worktrees_file(_worktrees_file(home), wt)
```

Removal happens only on the success path; an aborted `finish` (running container,
dirty without `--force`) leaves both the worktree and its overlay intact.

## Tests

New `tests/test_worktree_config.py` (using the real-git-repo fixture from
`test_verbs.py`, since worktree creation must actually run; `run_cli` side
effects + `running_container_for` stubbed as there):

- **Populate:** `start fix-auth --auth bedrock --mount <dir> --port 8000` writes
  `worktrees.json[<worktree path>]` with exactly `{auth, mounts, ports}` (dash
  keys), and an empty `start bare-topic` writes `{}`.
- **Resume consumes:** `resume fix-auth` (overlay present) → assembled `docker
  run` argv reflects the overlay (the extra `-v` mount, the `-p` forward,
  bedrock env), with **no** flags retyped.
- **Precedence:** a CLI flag on `resume` overrides the overlay; the overlay
  overrides a `projects.json` entry; concat keys (`mounts`/`ports`/`prompts`)
  accumulate project + overlay + CLI (assert all present, order low→high).
- **Provenance:** stderr line includes `worktrees.json[fix-auth]` when the
  overlay applies.
- **`config TOPIC` show/edit:** bare `config fix-auth` prints the overlay;
  `config fix-auth --add-mount <dir>` adds one element; `--mount` replaces the
  list; `--unset auth` drops a key; `config TOPIC` for a missing worktree errors;
  `config TOPIC --global` and `config TOPIC --init` error.
- **Finish cleanup:** `finish fix-auth` removes the `worktrees.json` entry (and a
  no-overlay finish doesn't choke).
- **Malformed file:** a corrupt `worktrees.json` fails with a pointed message on
  the launch path.

Keep `test_config.py`/`test_cli.py` green (the `load_yolo_config` signature gains
a keyword-only arg with a default, so existing callers are unaffected).

## Docs

- **CLAUDE.md** — "Host-side config" section: document the third layer
  (`worktrees.json`), the new precedence (`~/.yolo.json` < `projects.json` <
  worktree overlay < CLI), the populate-on-`start` / read-on-`resume` /
  edit-via-`config TOPIC` / remove-on-`finish` lifecycle, and the security note
  (sibling of `worktrees/`, never mounted — same property as `projects.json`).
  Update the `config` verb description (new `TOPIC` form) and the `start`/`finish`
  verb descriptions (populate/cleanup).
- **CHANGELOG.md** — UNRELEASED bullet describing per-worktree overlay config.

## Verification

- `uv run pytest` — full suite green; `uv run ruff check . && uv run ruff format .`.
- Manual: `yolo start wt-cfg --port 8000 --mount ~/refdocs` in a repo →
  `~/.claude-yolo/worktrees.json` has the entry; exit; `yolo resume wt-cfg` →
  the printed `docker run` shows the `-p`/`-v` without retyping; `yolo config
  wt-cfg --add-prompt "be terse"` updates it; next resume picks it up; `yolo
  finish wt-cfg` drops the entry.
- Confirm `worktrees.json` never appears in any container mount (it's under
  `~/.claude-yolo/`, not a worktree dir).
