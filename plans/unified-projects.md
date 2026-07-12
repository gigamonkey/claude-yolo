# Unified projects: one named project type

> **Implemented** (see CHANGELOG "Projects are now named"), with three
> deviations: `--project` also works on `resume`/`shell` (the dashboard's
> Enter/N/R spawn resumes by name, so ambiguous dirs stay unambiguous);
> migration *merges* a saved multi-repo entry with a v1 path entry over the
> same dir into one project (two entries would have turned a working dir
> ambiguous); and `--multi-repo` was removed outright with no alias (never
> released) while `secret`'s shipped `--project` kept a one-release
> `--project-scope` translation shim.

Today there are two project concepts: path-keyed entries in `projects.json`
(per-directory config, matched by cwd containment, live at every launch) and
named saved multi-repo configs in `multirepos.json` (launch templates,
consulted only at `start --multi-repo`, copied into the topic's overlay).
After the `name` config key landed, the differences left are storage shape,
by-name launchability, and the template-vs-live semantics — none of them
inherently about spanning repos (a plain project entry can already carry
`repos`).

This plan collapses the two into **one project type**: a *named* entry with a
primary directory, zero or more extra repos, and ordinary config keys. It
builds on `plans/multi-repo.md` and deliberately revisits that plan's decision
\#1 ("a saved config, not a new identity"): projects *become* the identity, so
the template-freeze semantics that protected against a disposable template
being edited/deleted mid-topic is replaced by live layering plus lifecycle
guards.

## The model

One store — `~/.claude-yolo/projects.json` (format v2, migration below):

```json
{
  "chat": {
    "dir": "~/work/app",
    "repos": ["~/work/lib", "~/work/proto"],
    "ports": ["3000"]
  },
  "notes": { "dir": "~/notes" }
}
```

- **Keyed by name** (same validation as multi-repo names today: a short
  label, not a path — `_do_config_multirepo`'s check). Creating a project
  from inside a repo defaults the name to the repo root's basename;
  `--name NAME` overrides at creation, and renaming is an explicit config
  operation (below).

- **`dir` is the primary** — sessions start there, container identity
  (labels, slugs, status file, secrets scope) stays keyed to it exactly as
  today. `dir` may be a plain non-git directory (cwd-session projects like
  `~/notes` exist today as path entries); `repos` extras must be git repos.

- **`repos` lists the extras**, not the primary. Keeping `dir` + extras
  (rather than one list with `repos[0]` special) preserves the existing key
  semantics: `repos` is a `_CONCAT_DESTS` key that concatenates across
  global/project/overlay/CLI layers, and a concat list that must always
  contain the primary in position 0 can't merge sanely. User-facing surfaces
  (wip, `config` display) can still present "the repo list" as
  `[dir, *repos]`.

- **Session naming** derives from the project name (the machinery the `name`
  config key just introduced): container `<name>` / `<name>-<topic>`, Claude
  session `<name>:<topic>`. The unreleased `name` *config key* is absorbed:
  the entry key **is** the name, so the key is dropped from `YOLO_KEYS` and
  `--name` becomes the create/rename affordance instead. A directory with no
  project entry keeps deriving names from its basename.

### Lookup: by name or by cwd

Two indexes into the same store:

- **By name**: `yolo start [TOPIC] --project NAME` (flag naming below) chdirs
  to `dir` first, exactly like `--multi-repo` today — so any project is
  launchable from anywhere, worktree *or* cwd session (today's
  `--multi-repo` requires a TOPIC; that restriction lifts for free since a
  project's `dir` is a perfectly good cwd-session target).

- **By cwd**: a bare `yolo` / `yolo start TOPIC` in a directory matches the
  project whose `dir` contains the cwd (longest `dir` wins, same containment
  rule as `_match_project_entry` today). **Extras never claim a cwd**: running
  `yolo` inside `~/work/lib` uses the `lib` project if one exists, or no
  project — being listed in `chat.repos` changes nothing, matching today.

Multiple projects sharing a `dir` (allowed today across multirepos.json
entries, and a genuinely useful capability — two repo sets over the same
primary for different workstreams): keep it allowed, resolve cwd ambiguity
explicitly — if >1 project has the best-matching `dir`, error naming the
candidates and require `--project NAME`. (wip is unaffected: every row knows
its project.)

## The two questions

### Should per-repo config exist independently of projects?

I.e. should today's path-keyed entries survive as a separate layer *under*
named projects? Arguments:

**For keeping an independent per-dir layer:**

- Several projects sharing a primary could share base config (auth,
  config-dir, secrets list) in the dir layer with per-project deltas on top —
  exactly today's `projects.json entry < multirepos.json entry` layering.

- Zero migration; `require-project-entry`, dangling-key warnings, and the
  containment matcher keep their current shape.

- In principle an *extra* repo's dir could carry settings that apply whenever
  it's mounted — but this is already not true today (only the primary's entry
  is consulted; extras' entries are inert), so it's not a real capability
  being lost.

**Against (for collapsing to projects only):**

- Two persisted stores means four-deep layering
  (global < dir entry < project < overlay), two editors in wip, and
  double provenance to explain — the exact confusion this unification exists
  to remove. "Why does this session have that mount?" should have one answer
  per scope.

- The overwhelmingly common case is one project per repo, where the dir layer
  is pure redundancy: the project (matched by cwd containment on its `dir`)
  *is* the per-repo config.

- The sharing case is rare and has escape hatches: put shared keys in
  `~/.yolo.json`, or duplicate a couple of keys across the two project
  entries. If real demand appears, an `extends`/base mechanism can be added
  later without resurrecting path-keyed identity.

**Recommendation: collapse.** The named project subsumes the per-repo entry;
`require-project-entry` now means "cwd must resolve to a named project."
Things that stay path-keyed regardless, deliberately: secrets project scope
(`_project_key` — secrets belong to the checkout, and renaming a project must
not orphan them), the recent-projects registry (it records directories you
opened, project or not), worktree slugs, and Claude's own
`~/.claude/projects/` transcript buckets. None of these are config layers.

### Can topic launches pick up project-config changes live?

**Yes — there is no fundamental reason for the copy-at-start freeze, and the
unified model should drop it.** The freeze existed because a multirepos.json
entry was a *disposable template*: nothing after start could safely depend on
it (rename/delete had to be free), so the overlay had to carry everything —
"editing or deleting the entry never changes a live topic" was the guarantee
that made template-ness coherent. Once the project is a durable named
identity, the natural semantics is the one single-repo projects already have:
the project entry is a live layer, re-read at every container launch and by
`_topic_repo_set` for the worktree verbs. That machinery already exists and
is proven — a `repos` key on a projects.json entry is live today (a repo
added mid-topic gets its worktree created by the next `resume`; a vanished
repo is skipped tolerantly by `finish`).

What replaces the stamp:

- The overlay records a **project pointer** — `project: NAME` — plus the
  explicit CLI flags it always recorded. (This repurposes the overlay `name`
  stamp added by the naming fix: same slot, now a reference instead of a
  copy.) `resume`/`shell`/`finish`/`rebase`/`merge`/`diff` resolve the
  pointer and layer the project entry live; topics with no pointer (plain
  `start TOPIC` in a project dir) resolve by cwd containment as today, so
  the pointer only matters when the topic was started by name or the dir is
  ambiguous.

- Shadowing works as today: overlay (explicit per-topic flags) still beats
  the project entry per scalar key, lists concatenate. The behavior the user
  already relies on — stop, edit project config, resume, new config applies
  unless the topic pinned its own value — becomes uniform across both kinds.

Costs, and their mitigations:

- **Deleting a project with live topics** can no longer be free. Guard it:
  `yolo config --project NAME --delete` (or whatever the delete spelling is)
  refuses while worktrees exist for topics pointing at it, `--force` to
  override, in which case the topic degrades to cwd-resolution + overlay
  (single-repo behavior, extras' worktrees left for `finish`'s tolerant
  skip-with-warning path to report).

- **Renaming a project** rewrites the pointer in affected overlays (one file,
  `worktrees.json`, host-side — trivial), and session names change at each
  topic's next relaunch (harmless: containers are found by labels, and wip's
  window naming resolves through the same config, so nothing desynchronizes).

- **The old guarantee flips**: `test_saved_config_edit_never_changes_a_live_topic`
  inverts — growing a project's `repos` mid-topic now *does* create the new
  worktree at next resume (assert that), matching the dir-entry behavior.
  CHANGELOG must call this out as a deliberate semantic change.

## Flag naming

`--project NAME` is the right spelling, but `--project` is taken as
`secret set`/`rm`'s boolean scope flag — and that flag *has* shipped (v0.15.0),
so it gets a compatibility shim: rename it to **`--project-scope`** and, since
the verb is known before parsing (positional `verb`), pre-translate a bare
`--project` in a `secret` invocation to `--project-scope` with a deprecation
warning for one release. `--multi-repo` has never been in a release, so it is
**removed outright** — no alias. (`--project` is what plans/multi-repo.md
originally wanted before the collision forced `--multi-repo`.)

`config` surface after unification:

- `yolo config` (in a dir) — show/edit the cwd's project (creating nothing,
  as today).
- `yolo config --init [--name NAME]` — create the project for this repo,
  name defaulting to the root's basename.
- `yolo config --project NAME [--dir PATH] [flags]` — show/edit/create by
  name (the old `--multi-repo` path; `--dir` inference from cwd kept).
- `yolo config --rename NEW` (or `--name NEW` on an existing entry) — rename,
  updating overlay pointers.
- `--add-repo` / `--remove-repo` unchanged.

## Migration

One-time, automatic, on first load that finds an old-format file; back up to
`projects.json.bak` / leave `multirepos.json` in place but stop reading it
after migrating; print a one-line stderr note. Detection: v1 `projects.json`
keys are paths (start with `/` or `~`); v2 keys are names and values carry
`dir`.

- Each v1 path entry → `{basename(path): {dir: path, ...keys}}`. Name
  collisions (two dirs with the same basename) disambiguate by appending the
  parent dir's name (`app-work`, `app-oss`) — and print what was chosen.
- Each multirepos.json entry merges in under its existing name; a collision
  with a migrated basename keeps the explicit multirepo name and renames the
  basename-derived one (it was never user-chosen).
- Existing worktree overlays keep working unmigrated: a topic with stamped
  multirepo keys simply has them all in its overlay — the live-layer change
  only affects *new* topics' overlays. One wrinkle: `name` leaves `YOLO_KEYS`
  (replaced by `project`), and `_parse_yolo_dict` hard-errors on unknown
  overlay keys — but the `name` stamp is unreleased (this branch only), so
  ship the rename together with unification and no compat shim is needed;
  the multi-repo + naming + unification work lands as one release.

Nothing about the security posture changes: the store stays host-side-only,
never mounted, and `repos`/`dir`/`mounts` remain exactly the kind of host
access grant that must live there (CLAUDE.md invariant).

## wip

One PROJECTS section, one row shape: every named project (name, dir,
`+N repos` when extras exist), then recent unregistered dirs as today
(`a` registers one — a single add flow now: pick dir, name defaults to
basename, extras addable in the config editor; the two-way
directory-vs-multi-repo picker goes away). Enter resumes/switches in `dir`
for *every* project (the "a saved config isn't a running thing" refusal goes
away — a project has a dir, which is a launchable place); `n` prompts for a
topic and spawns `yolo start TOPIC --project NAME --no-tmux`; `c` opens the
one config editor. `_project_display_name` collapses into "the matched
project's name" — same resolution the launch does.

## Implementation phases

1. **Store + model.** v2 read/write of `projects.json` (name-keyed, `dir`
   required), migration of both files, name validation, cwd containment
   matcher over `dir` (with the ambiguity error), by-name lookup. All of
   `_read_projects_file` / `_match_project_entry` / `_multirepo_entry` /
   `load_yolo_config`'s project layer converge here. Tests: migration shapes,
   collision renames, ambiguity error, non-git `dir`.

2. **Config surface.** `--project NAME` (secret-flag rename + pre-translation),
   `--init`/`--name` creation, rename with pointer rewrite, delete guard,
   `_ConfigScope` consolidation (the multirepos scope merges into the project
   scope). Tests: each verb path, the deprecation shim.

3. **Launch + live layering.** `start [TOPIC] --project NAME` (cwd session
   allowed), overlay writes `project: NAME` + explicit flags only (no more
   key copying), pointer resolution in `resume`/`shell` and `_topic_repo_set`,
   flip the freeze tests, session naming from the matched project. Tests:
   live repos growth mid-topic, shadowing unchanged, deleted-project
   degradation, pointer-less topics.

4. **wip unification.** Merge row kinds, single add flow, Enter/n/c/N/R over
   project rows, drop the multirepo special cases. Tests: row rendering,
   spawn argvs, window-name correlation via project names.

5. **Docs.** README (Multi-repo projects section becomes "Projects";
   config-keys section; worktree-mode naming), ARCHITECTURE (config layering,
   multi-repo section, wip), CHANGELOG (consolidate with the unreleased
   multi-repo + naming entries — this all ships as one feature, so the
   template-freeze semantics never ships at all and the CHANGELOG describes
   only the final shape).

## Settled decisions

- **Delete guard**: deleting a project (a `--delete` affordance on
  `config --project NAME` / the in-dir match) **refuses** while worktrees
  exist for topics that resolve to it; `--force` overrides, degrading those
  topics to cwd-resolution + overlay. Renaming needs no refusal at all: the
  `dir` doesn't change, so cwd-matched topics are unaffected — the rename just
  rewrites overlay `project` pointers and session names change at each topic's
  next relaunch (containers are found by labels, so nothing strands).
- **Secrets scope**: stays repo-root-keyed. Re-keying by project name would
  make secrets survive directory moves but break on renames instead; deferred
  until there's a felt need.
- **`repos` is rejected at the global layer**: a `repos` key in `~/.yolo.json`
  (or `config --global --add-repo`) is a hard error — a global extras list
  would add worktrees to every project.
