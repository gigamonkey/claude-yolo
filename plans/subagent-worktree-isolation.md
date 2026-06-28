# Plan: enable Claude Code subagent worktree isolation inside yolo sessions

Goal: let a Claude session running **inside a yolo container** use the Agent/Task
tool's `isolation: "worktree"` option, so subagents run in parallel each in their
own git worktree — instead of falling back to sequential work in the shared tree
with the message:

> Worktree isolation isn't available in this container (the main repo path isn't
> writable here). I'll run the agents sequentially in the shared tree instead.

This was reported from a yolo **worktree** session, and the root cause is specific
to how yolo mounts a worktree session — but the fix should be written so it works
for cwd sessions too.

---

## Root cause

In a yolo **worktree** session, `launch_container` bind-mounts only two things that
matter here:

- the worktree dir itself, as the cwd — `~/.claude-yolo/worktrees/<slug>/<topic>`
  (rw) — `yolo.py:3899`
- the **shared `.git`** (`common_git`) at its real host path, e.g.
  `/Users/peter/hacks/<repo>/.git` (rw) — `yolo.py:4001`

It does **not** mount the **main checkout root** (`main_root`, the parent of that
`.git`). Docker still has to materialize `/Users/peter/hacks/<repo>/` to host the
`.git` mount, so it auto-creates that parent as a **root-owned stub containing only
`.git`** — no working tree, not writable by the `claude` user.

Claude Code's worktree-isolation feature creates each subagent worktree at
`<repository root>/.claude/worktrees/<name>`. When the cwd is itself a linked
worktree, it resolves "repository root" to the **main checkout** (the parent of the
shared `.git`) — exactly the root-owned stub. Writing there fails → "the main repo
path isn't writable here."

(Whether Claude Code resolves to the main root vs. the current worktree's toplevel
in the nested-worktree case is **undocumented**; the error message is the empirical
proof that it lands on the main root. The fix below sidesteps the resolution
entirely, so it doesn't matter.)

---

## Requirements — all three must hold

For `git worktree add` to succeed **and** the agents to actually be able to work:

1. **A writable directory** at the location where each agent worktree is created.
2. **A base ref that resolves offline.** The "base ref" is the git revision a new
   worktree's branch is started from. "Offline-resolvable" means git can turn it
   into a concrete commit from the local `.git` alone, with no network call. Claude
   Code defaults to `worktree.baseRef: "fresh"` = `origin/HEAD` (the remote's
   default branch), which generally implies a `git fetch` and isn't even guaranteed
   to be set in a given checkout. The container has no network and no git creds
   (ssh-agent is off by default), and yolo worktrees track no upstream — so "fresh"
   can fail to resolve or resolve to something stale. It needs
   `worktree.baseRef: "head"` (local `HEAD`, always present locally). Only
   `"fresh"`/`"head"` are accepted; arbitrary refs are not. (Claude Code reportedly
   falls back to local `HEAD` when `origin/HEAD` doesn't resolve, so "fresh" may
   limp along — but setting `"head"` makes it deterministic.)

   Note `worktree.baseRef` is a **Claude Code setting** (in its `settings.json`),
   **not** git config — it only governs Claude Code's own worktree-isolation
   feature. Git has its own unrelated `worktree.*` config section (e.g.
   `worktree.guessRemote`); there is no `worktree.baseRef` in git itself, just a
   `worktree` prefix collision. This is why yolo can inject it through the
   `--settings` overlay rather than touching git config inside the container.
3. **Edits inside the agent worktree must not be blocked.** Claude Code issue
   #47134: the built-in deny-write on `.claude/**` blocks Edit/Write to files
   *inside* worktrees that live under `.claude/worktrees/`, so an agent can create
   the worktree but can't write in it. Putting the worktrees **outside** any
   `.claude/` path avoids this.

Plus one housekeeping concern:

4. **Stale admin entries.** Any worktree git registers writes admin entries into
   the host's real `.git` (`common_git` is mounted rw), so a crashed agent leaves
   `git worktree` entries pointing at paths that no longer exist. `git worktree
   prune` cleans them.

---

## Design

Inject two things through yolo's existing **container-only `--settings` overlay**
(the same overlay that already sets `sandbox.enabled:false` and the
session-activity hooks in `build_claude_args`). The overlay already merges
user-defined hooks via `_read_settings_hooks` and doesn't touch a `worktree` key,
so this composes with whatever the user has.

1. **`worktree.baseRef: "head"`** in the overlay → satisfies requirement (2) and
   the wrong-base problem.

2. **A baked `WorktreeCreate` hook** that fully overrides the worktree location →
   satisfies (1) and (3). The hook receives `worktree_name` / `base_branch` /
   `target_path` on stdin; the command form creates the worktree and prints the
   absolute path it created, exit 0. It runs roughly:

   ```sh
   # /etc/yolo/worktree-create.sh   (baked into Dockerfile.default)
   name="$(jq -r .worktree_name)"            # from stdin JSON
   dest="/home/claude/.yolo-agent-worktrees/$name"
   git worktree prune                         # clear stale entries from a prior crash
   git worktree add "$dest" -b "worktree-$name" HEAD >&2
   printf '%s\n' "$dest"
   ```

   (Exact stdin field names and output schema are only lightly documented —
   **verify by trying** before committing to them. The HTTP-hook form returns
   `worktreePath` in `hookSpecificOutput`; the command form prints the path.)

### Where the agent worktrees go

Use a **container-local dir off the bind mount**, e.g.
`/home/claude/.yolo-agent-worktrees/`:

- writable by the `claude` user; avoids `.claude/**` entirely (req. 3);
- ephemeral — discarded at container exit, no host clutter. The agents'
  *commits/branches* still persist because those live in the shared `.git`, which
  is what gets merged back into the parent session's branch;
- cost: the stale-admin-entry issue (req. 4), handled by `git worktree prune` in
  the hook (and/or at launch).

### Rejected alternative: just mount the main checkout rw

Adding `-v {main_root}:{main_root}` in worktree mode would make the main-repo path
writable and let Claude Code's default behavior run — but the worktrees would then
land in `<main_root>/.claude/worktrees/`, straight into the #47134 deny-write trap,
so agents still couldn't edit in them. It also re-exposes the main checkout to the
worktree container, partly defeating the isolation a worktree session exists to
provide. The hook approach is strictly better.

(If #47134 turns out to be already fixed in the running Claude Code version, then
mount-main-root + `baseRef:head` *might* suffice — but don't rely on it.)

---

## Implementation sketch

- **`Dockerfile.default`** — bake `/etc/yolo/worktree-create.sh` (written with
  `printf`, like the existing `/etc/yolo/load-secrets.sh` / `clone.sh`, so it needs
  no BuildKit). Ensure `jq` is available (or parse the JSON without it).
- **`build_claude_args`** (the `--settings` overlay) — add the `worktree` key
  (`baseRef: "head"`) and concatenate a `WorktreeCreate` hook group onto the user's
  hooks, exactly as the `Stop`/`UserPromptSubmit`/`AskUserQuestion` groups are
  added today. Remember `--settings` replaces each top-level key wholesale, so the
  `worktree` key here is the whole `worktree` config (fine — users don't typically
  set it) and the hook group must be merged with `_read_settings_hooks`.
- **Gating** — decide whether this is always-on or behind a flag/config key. It's
  low-risk and only activates when a session actually requests worktree isolation,
  so **default-on** is reasonable; a `--no-subagent-worktrees` opt-out (and config
  key) is cheap insurance if it ever misbehaves.
- **Scope** — applies to both worktree and cwd sessions. In a cwd session the cwd
  *is* writable, so the only thing strictly needed there is `baseRef: "head"`; the
  hook is harmless and keeps behavior uniform (and keeps agent worktrees out of the
  user's live checkout, which is a plus for cwd sessions).

---

## Validation

- **No-code spike first.** In a worktree session, add to the worktree's
  `.claude/settings.json` (host-editable, flows through the overlay):

  ```json
  { "worktree": { "baseRef": "head" },
    "hooks": { "WorktreeCreate": [ { "hooks": [
      { "type": "command", "command": "/path/to/wt-create.sh" } ] } ] } }
  ```

  with `wt-create.sh` creating the worktree under
  `/home/claude/.yolo-agent-worktrees/` and printing the path. Launch a couple of
  parallel `isolation: "worktree"` agents and confirm they run concurrently and can
  edit files. This pins down the real hook I/O schema before baking anything.

- **Then** move it into the Dockerfile + overlay and add tests in the
  `test_status.py` / `test_cli.py` style: assert the assembled `--settings` carries
  the `worktree.baseRef` and the `WorktreeCreate` hook group (alongside the existing
  status hooks), and that the user's own `worktree`/hooks survive the merge.

---

## Open questions

- Exact `WorktreeCreate` stdin field names and success/output contract (docs thin;
  resolve in the spike).
- Whether Claude Code cleans up (`git worktree remove`) agent worktrees on normal
  completion, or whether yolo should prune on every launch as a backstop. The hook
  prunes defensively regardless.
- Whether to prune the shared host `.git` of stale `worktree-<name>` entries at
  `yolo` launch (cheap, keeps the host repo tidy after crashes).
