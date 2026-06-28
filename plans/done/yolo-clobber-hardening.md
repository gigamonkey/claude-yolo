# Plan: hardening `yolo` against host-state clobbering

Goal: take the field notes in `plans/yolo-clobber-notes.md` and turn the things an
agent currently has to *remember* into things `yolo` *hardwires*, so a session
running in a **cwd** container (the user's live host checkout, bind-mounted in
place, Linux container over a macOS host) can't trash host state by accident.

The notes group into two kinds of fix:

- **Mechanical** — things the container can enforce regardless of what the agent
  does (env vars, mounts). These are the high-value ones; an agent can't forget
  them.
- **Behavioral** — judgment calls (`git add -A`, mutating live data) that can
  only be nudged through the system prompt. We sharpen the prompt and, where
  cheap, add a backstop.

The single highest-value item is **Part A** (redirect per-OS / build dirs off the
bind mount). Everything else is incremental.

---

## Design principles

1. **Hardwire beats remember.** Every shell in the container — claude itself, the
   launch wrapper, `yolo shell`, a `docker exec`, *and the agent's `Bash` tool
   subshells* — inherits the container's **process environment**. So a single
   `docker run -e VAR=...` reaches all of them. This sidesteps the whole pain the
   notes describe (having to patch `~/.bashrc`, `~/.profile`, *and* the rotating
   `~/.claude/shell-snapshots/*.sh` because the Bash tool sources a snapshot, not
   `.bashrc`). The snapshot only ever *adds* to the inherited env; it won't unset
   `UV_PROJECT_ENVIRONMENT`. **`docker -e` is strictly better than the three-places
   host hack** and is the mechanism we should lean on.

2. **Scope to where the hazard is — cwd sessions only.** The per-OS-artifact and
   live-data hazards are real in **cwd** sessions (`worktree_name is None`), where
   the bind mount is the user's actual checkout. A worktree session is an isolated
   copy created fresh by `git worktree add`, so it rarely carries a host-built
   `.venv`. Although the redirect would be harmless in a worktree too, we
   deliberately **scope it to cwd sessions** to keep worktree launches as simple as
   they are today — same gate as the existing `cwd_mode` block in
   `build_claude_args`.

3. **Don't change host behavior.** Nothing we add may write to the host outside
   the bind mount, and the redirect targets must be container-local (or a
   host-side dir under `~/.claude-yolo/`, never inside the project tree).

---

## Part A — redirect per-OS / build dirs off the bind mount (the big one)

### A1. Env-var redirect (primary mechanism)

Add `docker run -e` vars that point each ecosystem's per-OS / build directory at a
**container-local path off the bind mount**, so the container never clobbers the
host's copy and a host dev server's interpreter is never swapped out from under
it.

Concretely, in `launch_container` where the shared `-e` args are assembled
(`yolo.py` ~line 3657–3676, beside `YOLO_SESSION` / `_ps1_env_args`):

| Var | Redirects | Notes |
|-----|-----------|-------|
| `UV_PROJECT_ENVIRONMENT` | `./.venv` (uv) | The headline case from the notes. Absolute path → uv builds the Linux venv there, host `.venv` untouched. |
| `CARGO_TARGET_DIR` | `target/` (Rust) | Clean, well-supported. |
| `PYTHONPYCACHEPREFIX` | `__pycache__` trees | Mirrors the pycache tree under the prefix; keeps `.pyc` churn off the mount. |

Lower-value (caches, cross-platform-safe text/SQLite, optional): `PIP_CACHE_DIR`,
`npm_config_cache`, `RUFF_CACHE_DIR`, `PYTEST_*`. Not worth the surface area in v1.

This set is **hardcoded for v1.** Someday we may want a config mechanism to let a
project add/override entries (e.g. a `build-dir-redirects` map of `VAR -> path`);
note it as a future enhancement, don't build it now.

**Path scheme.** A fixed container-local directory — **no slug needed**:

```
/home/claude/.yolo-env/uv           -> UV_PROJECT_ENVIRONMENT
/home/claude/.yolo-env/cargo-target -> CARGO_TARGET_DIR
/home/claude/.yolo-env/pycache      -> PYTHONPYCACHEPREFIX
```

There's no collision to design against: each session is its own container and
`/home/claude/...` is container-local (discarded at exit), so two concurrent
sessions live in two separate filesystems. Within one container there's a single
primary project (the cwd), and a slug wouldn't even help the multi-project edge
case (a second `--mount`ed repo, or `cd`-ing away): `UV_PROJECT_ENVIRONMENT` holds
one value, so a slugged path can't point at two repos at once regardless. So the
slug buys nothing here — keep the path fixed. (`/home/claude/...` rather than
`/tmp` so it's clearly the user's and survives a `/tmp` sweep; both vanish at exit
either way.) The slug **does** earn its keep in the persistence variant below,
where one host dir is shared across sessions.

**The `UV_PROJECT_ENVIRONMENT`-is-global caveat (from the notes).** uv applies an
*absolute* `UV_PROJECT_ENVIRONMENT` to *every* project run in that shell. For the
dominant single-project session this is correct. If a session spans repos (extra
`--mount`ed project dirs, or someone `cd`s into another repo), two projects would
share one venv path and uv would thrash between them. A slug in the path can't fix
this (the var holds one value), so we accept it for v1, document it, and note the
cleaner fix is A2.

**Persistence (optional enhancement).** A container-local path is rebuilt every
session (clean, but costs a `uv sync` each launch). If we want the Linux venv to
persist across sessions, bind-mount a host-side dir
`~/.claude-yolo/envs/<slug>/` (created host-side with the right uid, so no
volume-ownership problem) to `/home/claude/.yolo-env/<slug>/` and point the vars
inside it. Off the project tree, owned correctly, persistent. Recommend shipping
the ephemeral version first and adding persistence only if the rebuild cost bites.

**Gating — on by default (opt-out).** This is the single highest-value fix, and
the safe default is to protect the host: unless the user has a specific reason not
to, the more-likely-to-work behavior is for the container not to fight the host
over a shared path. So it's **on by default** in cwd sessions (per principle 2),
with an explicit way to turn it off. The switch:

- `redirect-build-dirs` (bool, default `true`) — validated in `_parse_yolo_dict`,
  threaded through `load_yolo_config` / `PARSER`. A user who hits a case where the
  redirect is wrong (e.g. a tool that *needs* the in-tree `.venv`) disables it in
  `~/.yolo.json` or per-project via `yolo config`.
- CLI-overridable as `--redirect-build-dirs` / `--no-redirect-build-dirs`.

Note this defaults `true` unlike the other bool keys (`ssh-agent`, `submodules`,
which default `false`) — those *add* host exposure so default off; this *removes*
host risk, so default on.

Because the redirect is pure `-e` args, it composes with every auth mode and flag
and needs no new mount in v1.

### A2. Volume/overlay shadow (the thorough variant — covers `node_modules`)

Env vars can't relocate `node_modules` (npm has no equivalent of
`UV_PROJECT_ENVIRONMENT`). The notes' "even cleaner" idea is to **shadow** the
artifact path: give the container its own empty dir at `<cwd>/.venv`,
`<cwd>/node_modules`, etc., so the host's copy is invisible and the container
builds its own.

Mechanism and its catch:

- A plain anonymous **docker volume** mounted at `<cwd>/node_modules` initializes
  **root-owned**, so the `claude` user (HOST_UID, non-root) gets `EACCES` writing
  into it. A **tmpfs** mount can take `uid=<HOST_UID>` and avoids this, but is
  memory-backed (bad for large `node_modules`/`target`).
- Cleanest is the **host-side-dir bind mount** from the persistence note above:
  `~/.claude-yolo/envs/<slug>/node_modules` (pre-created host-side, correct uid)
  bind-mounted at `<cwd>/node_modules`. Host copy shadowed, container writes
  freely, correct ownership, persistent.

Downsides: needs an explicit per-ecosystem path list, only covers the project-root
copy (not nested `node_modules` in a monorepo), and shadowing means the container
can't see host-installed deps (it reinstalls — usually fine).

**Recommendation:** ship A1 first (covers Python/Rust, zero mount complexity).
Add A2 as an opt-in (`--shadow-dir <relpath>` repeatable, or a curated default set
behind the same `redirect-build-dirs` key) only if `node_modules` clobbering shows
up in practice. Keep it behind the same config key so there's one switch.

---

## Part B — sharpen the cwd system prompt

The behavioral items (notes §2, §3, §5, §6) can't be fully mechanized; tighten the
`cwd_mode` block in `build_claude_args` (`yolo.py` ~3118–3129) and/or
`container-prompt.txt`. Keep it concise — long prompts dilute. Proposed additions
to the existing "live checkout" line, cwd-only:

1. **Staging discipline (notes §2):** "The working tree is the user's live data,
   much of it uncommitted. Never `git add -A`/`git add .`/`git commit -a`; stage
   explicit paths only, and don't commit files you didn't author without asking."
2. **Mutate nothing live (notes §3):** "Test data-layer changes against a copy
   (`cp` to `/tmp`, or a test client on a copy DB), not the live DB/corpus or
   mutating requests to a running server; keep migrations additive and idempotent;
   never delete a file out from under a running process."
3. **Process hygiene (notes §5):** "Run servers detached (`setsid … &`), never in
   the foreground (it stomps the TTY); use the `[x]` bracket trick with `pkill` and
   put temp files in `/tmp`/the scratchpad, never the project dir." — this may be
   better as a short line than the full detail; weigh length.
4. **Confirm before irreversible/outward actions (notes §6):** already partly
   covered by the global guidance; a one-line reminder that blast radius is the
   user's real work.

If we ship Part A, **update the existing `.venv`/`node_modules` line** — it
currently says "make a throwaway venv rather than wiping the project's." With the
redirect in place the agent no longer needs to do that manually; reword to "build
artifacts are already redirected off the live checkout for the container, so `uv`/
`cargo` won't touch the host's — but still don't manually `rm -rf` them."

Note the existing prompt is assembled by joining `extra_system_prompt` with
`"... "` and fed to `--append-system-prompt`; new lines slot into that list.

---

## Part C — optional git backstop (defense in depth)

A prompt can't *prevent* `git add -A`. Two cheap, low-risk options to consider
(both optional, not required for v1):

- **A `pre-commit` hook** installed into the container's git config that aborts a
  commit whose staged set includes paths the session didn't author *and* looks
  data-shaped — too fuzzy/aggressive; likely more annoyance than value. Recommend
  *against*.
- A narrower **`core.hooksPath` pre-commit** that simply refuses if the commit was
  produced via `commit -a` is not detectable at hook time. Skip.

Conclusion: leave §2 as prompt-only. Document the decision so it isn't
re-litigated.

---

## Testing

Mirror the existing suites (see CLAUDE.md → Development):

- **`tests/test_cli.py`** — assert the `-e UV_PROJECT_ENVIRONMENT=…` (and
  `CARGO_TARGET_DIR`, `PYTHONPYCACHEPREFIX`) appear in the assembled `docker run`
  argv for a default cwd launch (the fixed `/home/claude/.yolo-env/...` paths);
  assert they're **absent** for a worktree launch (cwd-only scope) and **absent**
  when `--no-redirect-build-dirs` is passed.
- **`tests/test_config.py`** — `redirect-build-dirs` parses as a bool, rejects
  non-bool, round-trips through the `config` verb, and defaults correctly.
- Prompt assertions live wherever the `cwd_mode` block is currently checked
  (grep the tests for the existing "live checkout"/`.venv` prompt assertion and
  extend it).
- If A2 ships: assert the shadow bind mounts (`-v …/node_modules:…`) and their
  host-side pre-creation.

---

## Rollout

1. **Part A1** (env-var redirect, default on, `redirect-build-dirs` key) — the
   payload. One change in `launch_container`'s `-e` block + the config plumbing +
   tests.
2. **Part B** (prompt wording) — small, ship alongside A1 so the prompt matches
   the new behavior.
3. **Part C** — documented decision (likely "prompt-only"), no code.
4. **Part A2 / persistence** — defer; revisit if `node_modules` or per-session
   venv rebuild cost proves painful.

Then the usual: CHANGELOG `## Unreleased` entry, README (new flag/key + a short
"build-dir redirection" note in the cwd-session section), and a version bump.

---

## Decisions (resolved)

1. **Scope: cwd sessions only.** Worktree launches stay as simple as they are
   today; the redirect is gated on `cwd_mode` like the existing live-checkout
   prompt block. Prompt changes (Part B) are also cwd-only.
2. **Ephemeral first.** Fixed container-local `/home/claude/.yolo-env/...`,
   rebuilt per session. Persistent host-side bind mount is a deferred enhancement.
3. **Vars in v1:** `UV_PROJECT_ENVIRONMENT` + `CARGO_TARGET_DIR` +
   `PYTHONPYCACHEPREFIX`, hardcoded. A config mechanism to customize the set is a
   future enhancement; cache vars and `node_modules` (A2) are deferred.
4. **On by default (opt-out).** `redirect-build-dirs` defaults `true` — protecting
   the host is the more-likely-to-work behavior, so it's the default unless the
   user has a specific reason to disable it (`~/.yolo.json`, `yolo config`, or
   `--no-redirect-build-dirs` per run). Defaults `true` unlike `ssh-agent`/
   `submodules` (which add exposure and default off) because it *removes* risk.
