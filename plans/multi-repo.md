# Multi-repo projects

Let a project span multiple git repos. When a worktree session starts, yolo
creates a same-named worktree+branch in *each* of the project's repos and
mounts every one of them into the container exactly the way the single repo is
mounted today (worktree + shared `.git`, both at their identical host paths).
The worktree verbs (`finish`, `rebase`, `merge`, `diff`) then operate across
the whole set.

## User-visible behavior

```bash
cd ~/work/app                                # the primary repo
yolo config --add-repo ~/work/lib --add-repo ~/work/proto
yolo start fix-auth
```

- Creates branch `fix-auth` and a worktree in **app** (as today), **lib**, and
  **proto**, each at `~/.claude-yolo/worktrees/<slug(repo)>/fix-auth`.

- Launches one container. Working dir = app's worktree (unchanged). lib's and
  proto's worktrees (plus their shared `.git` dirs) are bind-mounted at their
  identical host paths and announced to claude via `--add-dir`, with a system
  prompt line explaining the layout ("this session spans repos X, Y, Z; each
  has a worktree on branch fix-auth at ...").

- `yolo finish fix-auth` (run from app) stops the session, then removes all
  three worktrees and applies the branch action in each repo. `rebase` /
  `merge` / `diff` likewise iterate the repo set, reporting per repo.

- `yolo resume fix-auth` relaunches with the same repo set (the config
  snapshot in the worktree overlay preserves explicit `--repo` flags, same as
  `--mount` today).

## Design decisions

### 1. Config key `repos`, primary repo = where you run yolo

A new config key `repos` — a list of host paths to the *additional* repos —
usable at every layer (global `~/.yolo.json`, project entry, worktree overlay)
plus a repeatable CLI flag `--repo PATH`. It concatenates across layers like
`mounts` (`_CONCAT_DESTS`). The **primary** repo stays implicit: it's the repo
containing the invocation cwd (`_repo_paths()`), which is also the projects.json
key. A `repos` entry that resolves to the primary itself is deduped out, so a
shared global list can't double-mount.

This is host-side-only config by construction (projects.json / worktrees.json
are never mounted), so the "nothing a container can edit grants new host
access" invariant holds without new work — an extra repo path is exactly the
kind of mount grant that must stay host-side.

### 2. Each repo's worktree lives under its own slug (no new state)

Extra worktrees go to `~/.claude-yolo/worktrees/<slug(extra-repo)>/<topic>` —
the same scheme the primary already uses, keyed by each repo's own path.

Why not one directory holding all the worktrees (cwd = a parent dir)? That
would break the load-bearing assumption that the session cwd *is* a git
worktree (`_repo_paths` from cwd, session naming, the status-file slug, orphan
recovery, the dashboard's rows, `git worktree` bookkeeping), for no mount-side
benefit — same-path mounting means the container sees all worktrees at their
host paths regardless of who is cwd.

Consequences we get for free:

- `list --all`, the `wip` dashboard, and orphan detection already walk
  `~/.claude-yolo/worktrees/*/*` and keep working untouched.

- No new state file: the repo set is re-derived from config at every verb,
  matching the existing "config describes the next launch" philosophy.

- Same-topic worktrees in different repos can't collide on disk.

### 3. Scope: worktree sessions only (v1)

`repos` affects only TOPIC sessions — the feature *is* "create worktrees in
each repo". A cwd launch (`yolo start` with no topic) with `repos` configured
prints a stderr note that `repos` is ignored for cwd sessions and that
`mounts` is the way to mount the live sibling checkouts. (Mounting the live
extra checkouts rw would just duplicate `mounts` with fuzzier semantics.)

### 4. Verbs resolve the repo set from config, tolerantly where it matters

`finish`/`rebase`/`merge`/`diff` re-resolve `repos` at invocation time:

- `start`/`resume` resolve **strictly** — a configured path that isn't a git
  repo is an error before anything is created or launched.

- `finish` resolves **tolerantly** — a configured repo whose path is gone, or
  which has no worktree for this topic, is skipped with a warning rather than
  stranding the removable worktrees. (A repo *removed from config* mid-topic
  leaves its worktree behind as a stray; `list --all` still shows it and a
  plain `yolo finish` from that repo still cleans it up individually.
  Documented, not solved, in v1.)

### 5. One container, primary-keyed identity (unchanged)

Container name, hostname, labels (`yolo.repo`, `yolo.worktree`, `yolo.cwd`),
session name, status file, overlay key — all keyed off the primary worktree
exactly as today. `stop`/`ps`/`browse` need no changes. One new label,
`yolo.extra-repos=<slug>,<slug>`, is stamped for observability (nothing reads
it in v1).

## Implementation

### Phase 1 — config plumbing

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
   main root, drop the primary. Called only on paths that need it (launch and
   worktree verbs), like `_resolve_mounts` — a stale path can't break
   `list`/`config`.

### Phase 2 — start: create N worktrees, mount them

5. Generalize worktree creation: give `setup_worktree` (yolo.py:1508) a
   `repo: pathlib.Path | None` parameter (run `git -C <repo> worktree add`,
   compute slug from that repo) — or add a sibling helper; either way the
   single-repo call sites stay green.

6. In `_main`'s start path (yolo.py:7534):

   - Resolve extras strictly. **Pre-flight every repo first** — primary and
     extras: error if the worktree dir or branch `topic` exists in any of
     them (needs a `-C`-capable `_branch_exists`), naming the offending repo.
     Nothing is created until all repos pass.

   - Create the primary worktree (unchanged), then each extra. On any
     creation failure, best-effort rollback: `_remove_worktree` (force) the
     ones already created, then re-raise.

   - The worktree overlay snapshot needs no work: `_overlay_flags` picks up
     any explicit `--repo` automatically once the key is in `YOLO_KEYS`.

7. `launch_container` (yolo.py:4029): new kwarg `extra_repos` — a list of
   `(worktree_path, common_git)` — emitting `-v wt:wt` and `-v git:git` pairs
   next to the existing primary `common_git` mount (yolo.py:4236), plus the
   `yolo.extra-repos` label.

8. Claude args: in `_main`, append the extra worktree paths to the dirs
   passed as `add_dirs` (they become `--add-dir`, yolo.py:3530), and add a
   `multi_repo_dirs` param to `build_claude_args` that emits one system-prompt
   line naming the sibling worktrees and their shared branch, so Claude knows
   commits in each go on branch `topic` and all survive on the host.

9. `--submodules`: loop `_init_submodules` (yolo.py:4003) over the extra
   worktrees too.

10. cwd launches with a non-empty resolved `repos`: stderr note (decision 3).

### Phase 3 — resume / shell

11. `resume TOPIC` re-resolves the repo set (project entry + worktree overlay
    + CLI, which the existing config layering already yields) and passes the
    same `extra_repos` to `launch_container`. An extra repo whose worktree is
    **missing** at resume (repo added to config mid-topic) gets it created
    then, with a note — same guards as start. `shell TOPIC` into a *running*
    container is untouched; a fresh shell container gets the same mounts as
    resume.

### Phase 4 — worktree verbs across the set

12. New helper `_topic_repo_set(topic, home, repos_specs)` returning
    `[(worktree, main_root, slug), ...]` — primary first, then extras —
    used by all the verbs below.

13. `finish` (do_finish / finish_worktree, yolo.py:4493): restructure into a
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

    - Overlay removal unchanged (keyed by the primary worktree path).

14. `rebase` (yolo.py:4753): container-activity guard once (it's one
    container), then per repo: dirty guard, resolve `base` in that repo's
    main root, rebase its worktree. Report per repo; a conflict stops at that
    repo with the standard resolve-in-`<worktree>` guidance.

15. `merge` (yolo.py:4857): per repo, the existing base-must-be-the-checkout
    check runs against that repo's own checkout; merge sequentially, stop at
    first failure, report what landed.

16. `diff` (yolo.py:4927): concatenate per-repo diffs under a `== <repo> ==`
    header; `--stat` runs the existing stat view per repo sequentially.

17. `dir TOPIC` keeps printing the primary worktree (it feeds
    `cd $(yolo dir ...)`; one path only).

18. `wip` dashboard: extra repos' worktrees already appear as ordinary rows
    under their own repos. v1 keeps that; the primary row's `f` routes
    through the new topic-level finish core (so it cleans up the whole set),
    while an extra-repo row's `f` finishes just that worktree. A grouped
    display is a follow-up.

### Phase 5 — tests and docs

19. New `tests/test_multi_repo.py` (conftest's `run_cli` fixture + tmp git
    repos):

    - config: `repos` parses (string or list), concatenates across layers,
      `--add-repo`/`--remove-repo` edit projects.json, editor `_LIST_FLAG`
      entry.

    - start: worktree + branch created in every repo; captured `docker run`
      argv has `-v` for each worktree and each common `.git`, `--add-dir`
      for each extra, the extra-repos label, and the layout prompt line;
      explicit `--repo` lands in the worktree overlay.

    - start guards: existing branch in *one* extra repo → error, **zero**
      worktrees created anywhere; creation failure mid-set rolls back.

    - resume: same mounts re-derived from config; a missing extra worktree
      is created.

    - finish: all worktrees removed, per-repo branch handling; a dirty extra
      repo blocks the whole finish without `--force`; a vanished repo path
      warns and skips; stray-worktree case (repo removed from config).

    - rebase/merge/diff: iterate the set; merge stops at first failure.

    - cwd session with `repos` configured: warning, no repo mounts.

    - strict vs tolerant resolution: bad path errors `start`, warns `finish`.

20. Docs: README feature section (config example + verb behavior),
    ARCHITECTURE.md subsection under the worktree machinery (repo-set
    resolution, strict/tolerant rule, stray-worktree caveat), CHANGELOG
    `## Unreleased` entry, and the per-file test-coverage map.

## Edge cases to handle explicitly

- `repos` naming the primary (or two entries resolving to the same repo):
  deduped silently in `_resolve_repos`.

- Same basename repos (`~/a/app`, `~/b/app`): fine — paths and slugs are
  full-path-derived; container name/hostname come from the primary only.

- A configured path pointing *inside* a repo: normalized to that repo's main
  root (same as `_project_key` does for the cwd).

- Branch diverged inside the container (someone switched branches in an extra
  worktree): the finish/rebase cores already operate on the worktree's actual
  branch state per repo; `list` already surfaces `topic (branch: X)`.

- `base` (`--base`/config) is resolved per repo (each `-C` its own main
  root), so the default `HEAD` means each repo's own tip; a named ref like
  `main` must exist in every repo or that repo's step fails with a clear
  message.

## Out of scope (follow-ups, not v1)

- Grouped multi-repo rows in `wip` / `list` (one row per topic spanning
  repos).

- Per-repo `base` overrides in the `repos` entries (would need
  `{path, base}` objects, like `clones`).

- Starting the multi-repo flow from a non-primary repo (v1: the repo you run
  yolo from is the primary; run it from the one whose project entry holds
  `repos`).
