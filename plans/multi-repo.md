# Multi-repo projects

Let a project span multiple git repos. When a worktree session starts, yolo
creates a same-named worktree+branch in *each* of the project's repos and
mounts every one of them into the container exactly the way the single repo is
mounted today (worktree + shared `.git`, both at their identical host paths).
The worktree verbs (`finish`, `rebase`, `merge`, `diff`) then operate across
the whole set.

The user-facing unit is a **multi-repo project**: a *named, saved launch
config* — a primary repo plus the extra repos — that you can start a worktree
session from, both on the CLI and from the `yolo wip` dashboard. It is a
launch template only, not a new kind of identity: once a topic is started, the
worktree overlay carries its repo set and the saved config is never consulted
again for that topic.

## User-visible behavior

```bash
# Define the multi-repo project once (from inside the primary repo, whose
# root is inferred as `dir`; --dir overrides, and is required outside a repo):
cd ~/work/app
yolo config --project chat --add-repo ~/work/lib --add-repo ~/work/proto

# Start a worktree session from it (works from any directory):
yolo start fix-auth --project chat
```

- Creates branch `fix-auth` and a worktree in **app** (the primary), **lib**,
  and **proto**, each at `~/.claude-yolo/worktrees/<slug(repo)>/fix-auth`.

- Launches one container. Working dir = app's worktree (exactly as a
  single-repo `yolo start` from `~/work/app` would). lib's and proto's
  worktrees (plus their shared `.git` dirs) are bind-mounted at their
  identical host paths and announced to claude via `--add-dir`, with a system
  prompt line explaining the layout ("this session spans repos X, Y, Z; each
  has a worktree on branch fix-auth at ...").

- The effective repo set is stamped into the worktree overlay
  (`worktrees.json`) at start. From then on the topic is self-describing:
  `yolo resume fix-auth` / `finish` / `rebase` / `merge` / `diff` (run from
  the primary repo, as today) read the overlay — editing or deleting the
  saved config later doesn't change live topics.

- `yolo finish fix-auth` stops the session, then removes all three worktrees
  and applies the branch action in each repo.

- In `yolo wip`, `chat` appears as a project row; `n` on it starts a
  multi-repo worktree session, and the resulting session/worktrees show up in
  the dashboard as they do today.

Meanwhile, plain `yolo start fix-auth` from `~/work/app` stays single-repo:
defining a multi-repo project does **not** make ordinary sessions on its
member repos multi-repo. Two lower-level entry points also exist for
completeness: ad-hoc `--repo PATH` flags on `start`, and a `repos` key on a
directory's own project entry for a project that should *always* be
multi-repo. All three funnel into the same mechanism (resolve the set at
start, stamp the overlay).

## Design decisions

### 1. A multi-repo project is a saved config, not a new identity

Stored host-side in `~/.claude-yolo/multirepos.json` (name TBD):

```json
{
  "chat": {
    "dir": "~/work/app",
    "repos": ["~/work/lib", "~/work/proto"]
  }
}
```

`dir` (required) names the primary repo; `repos` the extras. The value is
otherwise an ordinary config object (validated by `_parse_yolo_dict`, same
`YOLO_KEYS`), so a saved config may also carry e.g. `ports` or `prompts` —
it's "a saved way to launch," with multi-repo as the motivating use.

It is consulted **only at start** (`--project NAME`, or the dashboard's `n`):
it layers between the primary dir's projects.json entry and the CLI flags,
the start runs as if invoked from `dir`, and the resolved keys are stamped
into the topic's worktree overlay. Nothing else ever resolves the name — the
container labels, session name, secrets project scope, and all verbs still
key off the primary repo directory and worktree path exactly as today. That
keeps the entity from becoming a second project concept that every
directory-keyed feature would have to reconcile with; renaming or deleting a
saved config never strands a topic.

Why a named entity at all (rather than only per-invocation `--repo` flags):
`wip` is flag-less — every affordance launches a plain `yolo <verb>` in some
directory, so a not-always-on repo set must be saved host-side somewhere the
dashboard can list and start from. A saved config is exactly that place.

This is host-side-only config by construction (like projects.json /
worktrees.json, never mounted), so the "nothing a container can edit grants
new host access" invariant holds — an extra repo path is exactly the kind of
mount grant that must stay host-side.

### 2. The worktree overlay is the per-topic source of truth

`start` resolves the repo set (saved config + project entry + `--repo`
flags), creates the worktrees, and writes the resulting `repos` list into the
topic's overlay in `worktrees.json` (extending the existing
`_overlay_flags` snapshot, which today captures only explicit CLI flags).
Every later verb — `resume`, `shell`, `finish`, `rebase`, `merge`, `diff` —
derives the set from the overlay.

Implementation note: `_main` currently layers the overlay into config only
for `resume`/`shell` (yolo.py:7377); `finish`/`rebase`/`merge`/`diff` don't
read it. The repo-set helper (`_topic_repo_set`, below) must read the topic's
overlay explicitly rather than relying on the ambient config load.

Resolution is **strict at start/resume** (a path that isn't a git repo errors
before anything is created or launched) and **tolerant at finish** (a repo
whose path has vanished is skipped with a warning rather than stranding the
removable worktrees).

### 3. Each repo's worktree lives under its own slug (no new state)

Extra worktrees go to `~/.claude-yolo/worktrees/<slug(extra-repo)>/<topic>` —
the same scheme the primary already uses, keyed by each repo's own path.

Why not one directory holding all the worktrees (cwd = a parent dir)? That
would break the load-bearing assumption that the session cwd *is* a git
worktree (`_repo_paths` from cwd, session naming, the status-file slug,
orphan recovery, the dashboard's rows, `git worktree` bookkeeping), for no
mount-side benefit — same-path mounting means the container sees all
worktrees at their host paths regardless of who is cwd.

Consequences we get for free:

- `list --all`, the `wip` dashboard, and orphan detection already walk
  `~/.claude-yolo/worktrees/*/*` and keep working untouched.

- Same-topic worktrees in different repos can't collide on disk.

### 4. Scope: worktree sessions only (v1)

Multi-repo affects only TOPIC sessions — the feature *is* "create worktrees
in each repo". A cwd launch (`yolo start` with no topic) with `repos`
configured prints a stderr note that `repos` is ignored for cwd sessions and
that `mounts` is the way to mount the live sibling checkouts. (Mounting the
live extra checkouts rw would just duplicate `mounts` with fuzzier
semantics.)

### 5. One container, primary-keyed identity (unchanged)

Container name, hostname, labels (`yolo.repo`, `yolo.worktree`, `yolo.cwd`),
status file, overlay key — all keyed off the primary worktree exactly as
today. `stop`/`ps`/`browse` need no changes. Niceties: the session name for a
saved-config start can use the project name (`chat:fix-auth` instead of
`app:fix-auth`), and one new label `yolo.extra-repos=<slug>,<slug>` is
stamped for observability (nothing reads it in v1).

## Implementation

### Phase 1 — `repos` config plumbing

1. `YOLO_KEYS["repos"] = ("repos", "list")` (yolo.py:1543) and add `"repos"`
   to `_CONCAT_DESTS` (yolo.py:1571).

2. PARSER: `--repo` with `action="append"`, `dest="repos"`, default `[]`
   (mirroring `--mount`, yolo.py:3175).

3. `config` verb edits: `--add-repo` / `--remove-repo` flags, wired through
   `_apply_config_edits` (yolo.py:2143) and the flag-vs-verb guard list in
   `_main` (yolo.py:7322); add `"repos": "repo"` to `_LIST_FLAG`
   (yolo.py:6597) so the wip config editor's list loop handles it.

4. `_resolve_repos(specs) -> list[tuple[common_git, main_root, slug]]`:
   expanduser + resolve each path, `git -C <path> rev-parse
   --path-format=absolute --git-common-dir` to find its main root (so a path
   *inside* a repo normalizes to the repo), error on non-repos, dedupe by
   main root, drop the primary. Called only on paths that need it (launch
   and worktree verbs), like `_resolve_mounts` — a stale path can't break
   `list`/`config`.

### Phase 2 — saved multi-repo projects

5. Store: `~/.claude-yolo/multirepos.json`, `{name: {dir, ...config}}`.
   Reader validates names (no path-like keys), requires `dir`, and runs the
   value through `_parse_yolo_dict` (which now knows `repos`). Never mounted,
   like every other config file.

6. CLI: `--project NAME` on `start` (and `config`). On `start`, yolo chdirs
   to (or treats as cwd) the saved `dir`, layers the saved config between
   that dir's projects.json entry and the CLI flags, and proceeds as a
   normal worktree start. Reject `--project` combined with a conflicting
   in-repo invocation? No — the cwd is simply ignored in favor of `dir`
   (document it).

7. Creation/editing: `yolo config --project NAME [--dir PATH] --add-repo ...`
   writes the entry (reusing `_apply_config_edits` with a new
   `_ConfigScope`); `yolo config --project NAME` alone shows it. On
   creation, `dir` is inferred from the cwd's main repo root
   (`_main_root_or_none`) when run inside a repo; an explicit `--dir`
   overrides the inference, and is required when run outside a repo (error
   otherwise, naming the flag). `--dir` is a config-verb-only flag, guarded
   like the other `--add-*` flags in `_main`'s flag-vs-verb checks.

8. Overlay stamping: extend the start-path snapshot (yolo.py:7544) so a
   saved-config start writes the saved keys merged with explicit CLI flags
   into the topic's overlay — the topic must be self-describing (decision 2).

### Phase 3 — start: create N worktrees, mount them

9. Generalize worktree creation: give `setup_worktree` (yolo.py:1508) a
   `repo: pathlib.Path | None` parameter (run `git -C <repo> worktree add`,
   compute slug from that repo) — or add a sibling helper; either way the
   single-repo call sites stay green.

10. In `_main`'s start path (yolo.py:7534):

    - Resolve extras strictly. **Pre-flight every repo first** — primary and
      extras: error if the worktree dir or branch `topic` exists in any of
      them (needs a `-C`-capable `_branch_exists`), naming the offending
      repo. Nothing is created until all repos pass.

    - Create the primary worktree (unchanged), then each extra. On any
      creation failure, best-effort rollback: `_remove_worktree` (force) the
      ones already created, then re-raise.

11. `launch_container` (yolo.py:4029): new kwarg `extra_repos` — a list of
    `(worktree_path, common_git)` — emitting `-v wt:wt` and `-v git:git`
    pairs next to the existing primary `common_git` mount (yolo.py:4236),
    plus the `yolo.extra-repos` label.

12. Claude args: in `_main`, append the extra worktree paths to the dirs
    passed as `add_dirs` (they become `--add-dir`, yolo.py:3530), and add a
    `multi_repo_dirs` param to `build_claude_args` that emits one
    system-prompt line naming the sibling worktrees and their shared branch,
    so Claude knows commits in each go on branch `topic` and all survive on
    the host.

13. `--submodules`: loop `_init_submodules` (yolo.py:4003) over the extra
    worktrees too.

14. cwd launches with a non-empty resolved `repos`: stderr note (decision 4).

### Phase 4 — resume / shell

15. `resume TOPIC` reads the repo set from the overlay (already layered in
    for resume/shell) and passes the same `extra_repos` to
    `launch_container`. An extra repo whose worktree is **missing** at
    resume (e.g. added via `yolo config TOPIC --add-repo` mid-topic) gets it
    created then, with a note — same guards as start. `shell TOPIC` into a
    *running* container is untouched; a fresh shell container gets the same
    mounts as resume.

### Phase 5 — worktree verbs across the set

16. New helper `_topic_repo_set(topic, home)` returning
    `[(worktree, main_root, slug), ...]` — primary first, then extras from
    the topic's **overlay** (read explicitly; see decision 2) — used by all
    the verbs below.

17. `finish` (do_finish / finish_worktree, yolo.py:4493): restructure into a
    topic-level core:

    - Guard phase across the whole set before touching anything: stop the
      container (primary labels, as today), then dirty-check **every**
      worktree; any dirty repo aborts the lot (unless `--force`), naming the
      repo.

    - Act phase per repo: `_remove_worktree` + the branch action
      (`delete-if-merged`/`merge`/`push`/`keep`), each running `-C` its own
      main root; collect per-repo messages. For `--finish-action merge`, do
      all merges before any removal (same failed-merge-keeps-everything
      contract as today, extended to the set: first failure aborts with
      already-merged repos reported).

    - Overlay removal unchanged (keyed by the primary worktree path) — and
      it's what dissolves the topic's multi-repo-ness, correctly last.

18. `rebase` (yolo.py:4753): container-activity guard once (it's one
    container), then per repo: dirty guard, resolve `base` in that repo's
    main root, rebase its worktree. Report per repo; a conflict stops at
    that repo with the standard resolve-in-`<worktree>` guidance.

19. `merge` (yolo.py:4857): per repo, the existing base-must-be-the-checkout
    check runs against that repo's own checkout; merge sequentially, stop at
    first failure, report what landed.

20. `diff` (yolo.py:4927): concatenate per-repo diffs under a `== <repo> ==`
    header; `--stat` runs the existing stat view per repo sequentially.

21. `dir TOPIC` keeps printing the primary worktree (it feeds
    `cd $(yolo dir ...)`; one path only).

### Phase 6 — `wip` dashboard

22. PROJECTS section (`_wip_projects`, yolo.py:5909): add a row per saved
    multi-repo project — NAME plus the primary dir and repo count (e.g.
    `chat  ~/work/app +2 repos`). Its `n` action spawns
    `yolo start TOPIC --project NAME` (via the same `_wip_new_worktree`
    prompt flow).

23. Creating one from the dashboard: `a` becomes a two-way `_pick_one`
    (yolo.py:6699) — "directory project" (the existing `_wip_add_project`
    prompt, unchanged) or "multi-repo project", which prompts for a name
    (`prompt_line`) and the primary repo path (`prompt_path`,
    Tab-completing; git root inferred as `_wip_add_project` does), creates
    the entry by shelling out to `yolo config --project NAME --dir PATH`
    (reusing all of Phase 2's validation), then drops straight into the
    config editor on the new entry so the extra repos are added right there
    via the existing list-edit loop (`repos` is in `_LIST_FLAG` from
    Phase 1). Update the PROJECTS `_WIP_HINTS` line accordingly.

24. Editing one: `c` on a saved-config row opens the same editor — a new
    `_ConfigScope` kind (yolo.py:6606) with `store="multirepos.json"`,
    `config_args=["config", "--project", NAME]`, `entry_key=NAME`, and
    `base_cwd=dir` so the inherited pane shows the global + primary-dir
    layers the saved config sits on. `read()` gains a branch for the new
    store file.

25. Worktree rows: extra repos' worktrees already appear as ordinary rows
    under their own repos. The **primary** row's `f` routes through the new
    topic-level finish core (reads the overlay, cleans up the whole set); an
    extra-repo row's `f` finishes just that worktree. A grouped per-topic
    display is a follow-up.

### Phase 7 — tests and docs

26. New `tests/test_multi_repo.py` (conftest's `run_cli` fixture + tmp git
    repos):

    - config: `repos` parses (string or list), concatenates across layers,
      `--add-repo`/`--remove-repo` edit projects.json, editor `_LIST_FLAG`
      entry.

    - saved configs: file parse/validation (bad name, missing `dir`,
      unknown keys), `--project` layering order, start-from-anywhere, the
      overlay stamp containing the saved `repos`.

    - start: worktree + branch created in every repo; captured `docker run`
      argv has `-v` for each worktree and each common `.git`, `--add-dir`
      for each extra, the extra-repos label, and the layout prompt line.

    - start guards: existing branch in *one* extra repo → error, **zero**
      worktrees created anywhere; creation failure mid-set rolls back.

    - resume: mounts re-derived from the overlay (not the saved config — a
      mutated saved config must not change a live topic); a missing extra
      worktree is created.

    - finish: all worktrees removed, per-repo branch handling; a dirty
      extra repo blocks the whole finish without `--force`; a vanished repo
      path warns and skips.

    - rebase/merge/diff: iterate the set; merge stops at first failure.

    - cwd session with `repos` configured: warning, no repo mounts.

    - wip (in `tests/test_wip.py`'s existing stub style): saved-config rows
      render in PROJECTS; `n` on one spawns `start TOPIC --project NAME`;
      the `a` picker's multi-repo branch creates the entry and opens the
      editor on it.

27. Docs: README feature section (saved-config example + verb behavior),
    ARCHITECTURE.md subsection under the worktree machinery (the
    template-not-identity rule, overlay-as-source-of-truth,
    strict/tolerant resolution), CHANGELOG `## Unreleased` entry, and the
    per-file test-coverage map.

## Edge cases to handle explicitly

- `repos` naming the primary (or two entries resolving to the same repo):
  deduped silently in `_resolve_repos`.

- Same basename repos (`~/a/app`, `~/b/app`): fine — paths and slugs are
  full-path-derived; container name/hostname come from the primary only.

- A configured path pointing *inside* a repo: normalized to that repo's main
  root (same as `_project_key` does for the cwd).

- A saved config whose `dir` no longer exists / isn't a repo: `start
  --project` errors cleanly; live topics are unaffected (overlay-driven).

- Two saved configs sharing a primary `dir`: fine — they're just different
  launch templates; topics record their own set.

- Branch diverged inside the container (someone switched branches in an
  extra worktree): the finish/rebase cores already operate on the worktree's
  actual branch state per repo; `list` already surfaces `topic (branch: X)`.

- `base` (`--base`/config) is resolved per repo (each `-C` its own main
  root), so the default `HEAD` means each repo's own tip; a named ref like
  `main` must exist in every repo or that repo's step fails with a clear
  message.

## Out of scope (follow-ups, not v1)

- Grouped multi-repo rows in `wip` / `list` (one row per topic spanning
  repos).

- Per-repo `base` overrides in the `repos` entries (would need
  `{path, base}` objects, like `clones`).

- A `--no-repos` escape hatch for starting a one-off single-repo topic on a
  directory whose own project entry sets `repos` always-on (saved configs
  make that always-on pattern rare; add the flag if it turns out to be
  wanted).
