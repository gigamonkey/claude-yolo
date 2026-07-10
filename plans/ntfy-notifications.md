# Plan: ntfy.sh notifications on session state changes

Goal: a configurable feature that publishes an [ntfy.sh](https://ntfy.sh)
notification when a session's activity state changes — most usefully
`working → waiting` ("Claude finished / is blocked on you"), so the user can
walk away from a long-running session and get pinged on their phone/desktop
when it needs them.

---

## Where state changes already live

yolo already tracks exactly the transitions we want to notify on. The
session-activity hooks injected via `--settings` (`build_claude_args`,
`yolo.py:3447-3477`) write `"<state> <epoch>"` to
`<config-dir>/.yolo-status/<cwd-slug>.state` on four events:

- `Stop` → `waiting` (turn ended, Claude is at the prompt)
- `UserPromptSubmit` → `working`
- `PreToolUse[AskUserQuestion]` → `waiting` (blocked mid-turn on a question)
- `PostToolUse[AskUserQuestion]` → `working`

`ps`/`wip` read that file back host-side (`_read_session_state`,
`yolo.py:5297`). The hooks are the single source of truth for state changes,
they run *inside* the container (which has network access and `curl` — it's in
`Dockerfile.default:8`, and custom images inherit it via `FROM ${YOLO_BASE}`),
and they already fire at exactly the right moments.

**Design: publish from the state-marking hook itself.** No host-side daemon
(yolo `exec`s into `docker run`, so no host process survives launch to watch
the state files), no polling, no new event source that could drift from what
`ps`/`wip` display.

---

## Design decisions

### 1. Replace the inline `printf` hook with a baked script

Today `_mark(state)` (`yolo.py:3461`) emits an inline
`printf '<state> %s' "$(date +%s)" > <file>`. The notify logic (read previous
state, dedupe, filter, curl in the background) is too much for a readable
one-liner in JSON, so bake a script into the image following the
`/etc/yolo/clone.sh` pattern (`Dockerfile.default:39`):

```
/etc/yolo/mark-state.sh <state> <state-file>
```

The hook command becomes `bash /etc/yolo/mark-state.sh waiting <target>`
(target still `shlex.quote`d, still an absolute baked-in path — the existing
"no dependence on docker `-e`" property for the *file path* is preserved).

Use the script **unconditionally**, whether or not ntfy is configured — one
code path, and with no `YOLO_NTFY_TOPIC` in the environment it degrades to
exactly the old `printf`. This is safe because the image is content-addressed:
a yolo that emits the new hook command always builds (or reuses) an image whose
Dockerfile contains the script, and custom Dockerfiles build `FROM ${YOLO_BASE}`
so they inherit it too. The Dockerfile edit shifts the image hash → one-time
rebuild on upgrade, which is expected and harmless.

Script behavior (order matters — state tracking must never be blocked or
broken by notification failures):

1. Read the previous `"<state> <epoch>"` from the state file (missing/garbled
   → empty).

2. Write the new `"<state> <epoch-now>"` to the state file — always, first.

3. Exit 0 unless `$YOLO_NTFY_TOPIC` is non-empty.

4. Skip unless the state actually changed (`prev_state != new_state` — dedupes
   repeated same-state events).

5. Skip unless the new state is in `$YOLO_NTFY_STATES` (space-separated list).

6. If transitioning to `waiting` and `$YOLO_NTFY_MIN_WORK_SECS` > 0: skip when
   `now - prev_epoch` is under the threshold. This is the anti-noise knob —
   when the user is actively chatting, every turn ends in `waiting`; the
   threshold limits pings to "Claude worked a while and now needs you", the
   walked-away case.

7. Publish, silently and without delaying the turn boundary:

   ```sh
   curl -fsS -m 10 \
     ${YOLO_NTFY_TOKEN:+-H "Authorization: Bearer $YOLO_NTFY_TOKEN"} \
     -H "Title: yolo: $YOLO_NTFY_LABEL" \
     -d "$new_state (was $prev_state ...)" \
     "$YOLO_NTFY_SERVER/$YOLO_NTFY_TOPIC" >/dev/null 2>&1 &
   ```

   Backgrounded (`&`) so a slow/unreachable server never adds latency to the
   Stop hook; all failures are silent (a notification is best-effort).

Message content: `$YOLO_NTFY_LABEL` identifies the session — computed
host-side at launch as the worktree label (reuse `_worktree_ps1_label`,
`yolo.py:3504`, e.g. `claude-yolo/fix-auth`) in worktree mode, else the cwd
basename. Body says the new state, e.g. `waiting for input (worked 4m)`.

### 2. Config surface: flat keys, standard layering

Follow the existing flat-key style (`aws_profile`, `tmux_session`, …) rather
than a nested object — it drops straight into `YOLO_KEYS` (`yolo.py:1516`),
the config layering (global `~/.yolo.json` < `projects.json` < `worktrees.json`
< CLI), and the `yolo config` verb with no new machinery:

| key | kind | default | meaning |
|---|---|---|---|
| `ntfy_topic` | str | *(unset — feature off)* | topic to publish to; empty string in a higher layer disables a lower layer's setting |
| `ntfy_server` | str | `https://ntfy.sh` | server base URL (self-hosted ntfy) |
| `ntfy_states` | list | `["waiting"]` | which new states notify (`waiting`, `working`) |
| `ntfy_min_work_secs` | int | `0` | suppress `waiting` pings when the preceding working stretch was shorter than this |

CLI flags: `--ntfy-topic`, `--ntfy-server`, `--ntfy-state` (append, like
`--port`), `--ntfy-min-work-secs`. Register them with the `_UNSET` sentinel
default like the other config-writable flags so `yolo config --ntfy-topic mine`
persists per-project (follow the `finish_remote` plumbing as the model; check
`yolo config --show` renders them).

Notes:

- `ntfy_states` uses **override** semantics (do *not* add it to
  `_CONCAT_DESTS`, `yolo.py:1544`) — a project setting replaces the global
  list, which is what you want for a preference.

- `kind == "int"` doesn't exist yet in `_parse_yolo_dict` (`yolo.py:1550`);
  add it (non-negative int, reject bools — mirror the `_ok_depth` care in the
  clones validation).

- Validate `ntfy_states` values against `{"waiting", "working"}` at parse time
  so a typo fails loudly host-side instead of silently never notifying.

### 3. Topic transport: the secrets file channel, not argv

The claude args (including the `--settings` hooks JSON) are passed positionally
on the `docker run` argv, so anything baked into the hook command is visible in
`docker inspect`, host `ps`, and tmux's retained pane command. An ntfy topic on
the public server is a capability — anyone who knows it can subscribe and
publish — so treat it like the OAuth token: ride the existing chmod-600
`extra_env` file transport (`_stage_secrets`, `yolo.py:1199`, → `/run/secrets`
+ the baked loader).

At launch, merge into the `extra_env` dict passed at `yolo.py:4210` (for all
auth modes, not just oauth-token):

- `YOLO_NTFY_TOPIC`, `YOLO_NTFY_SERVER`, `YOLO_NTFY_STATES` (space-joined),
  `YOLO_NTFY_MIN_WORK_SECS`, `YOLO_NTFY_LABEL`

The loader exports them before `exec`ing claude, and hook commands are claude
subprocesses, so they inherit the environment. Only stage them when
`ntfy_topic` is set (don't force the bash-wrapper launch path for everyone).

Server/states/label aren't secret, but sending them down the same channel is
simpler than splitting transports, and it keeps *everything* ntfy off the argv.

Bonus, nearly free: the script honors `$YOLO_NTFY_TOKEN` (Authorization:
Bearer) if present, so a reserved/protected topic on a self-hosted server works
today via the existing secret store — `yolo secret set YOLO_NTFY_TOPIC` /
`YOLO_NTFY_TOKEN` plus `--secret YOLO_NTFY_TOKEN` — no new auth machinery.
Document it; don't build more.

### 4. Accepted tradeoffs (call these out in docs)

- **The container can read the topic** (hooks run in-container). A rogue
  session could send spoofed/spam notifications — but not gain any host
  access, so this doesn't violate the host-side-only config property (the
  container still can't grant itself mounts or ports). Recommend a
  hard-to-guess topic per ntfy's own guidance.

- **No notification on container death.** Hooks can't fire when the container
  is stopped/killed/crashes. A host-side "session ended" ping from
  `stop`/`finish` is a possible follow-up, out of scope here.

- **ExitPlanMode remains a gap** — it fires no hook (pre-existing, documented
  at `yolo.py:3454`); plan-approval waits won't notify.

---

## Implementation steps

1. **`Dockerfile.default`**: add the `/etc/yolo/mark-state.sh` heredoc-via-
   `printf '%s\n'` RUN block next to `clone.sh` (real `\n`, not `\\n` — see
   `test_secrets_loader_uses_real_newlines`). Logic as in §1.

2. **`yolo.py` — config**: `YOLO_KEYS` entries, `"int"` kind + states
   validation in `_parse_yolo_dict`, argparse flags, `yolo config`
   plumbing/`--show` rendering.

3. **`yolo.py` — launch**: compute the label, build the `YOLO_NTFY_*` dict in
   the launch path, merge into `_stage_secrets`'s `extra_env`.

4. **`yolo.py` — `build_claude_args`**: `_mark` emits
   `bash /etc/yolo/mark-state.sh <state> <target>`; update the surrounding
   comment (the notify behavior, and that the path-baked-in property still
   holds).

5. **Tests** (all host-only, no docker, per the existing suite conventions):

   - `test_status.py`: update the exact-string hook-command asserts to the new
     script invocation.

   - `test_config.py` (+ `test_worktree_config.py` if it enumerates keys): new
     keys' validation, layering/override, `yolo config` round-trip; `int` kind
     rejects bools/negatives/strings.

   - New `tests/test_ntfy.py`: with `ntfy_topic` configured, the `YOLO_NTFY_*`
     files are staged in the run dir (chmod 600) and `/run/secrets` is mounted;
     no ntfy value appears anywhere on the captured `docker run` argv;
     unconfigured → nothing staged, no wrapper forced; label is
     worktree-label vs cwd-basename per mode; states join & min-work-secs
     formatting.

   - Script behavior test (in `test_ntfy.py` or `test_data_files.py`): extract
     the `mark-state.sh` RUN block from `DEFAULT_DOCKERFILE` (strip the
     `RUN mkdir -p /etc/yolo && ` prefix, rewrite the `/etc/yolo/...` redirect
     target to a tmp path, run through `bash -c` — i.e. reproduce what docker
     build does), then drive the resulting script with a stub `curl` on PATH:
     writes the state file correctly; no curl without topic; curls on
     `working → waiting`; dedupes same-state; respects `YOLO_NTFY_STATES` and
     `YOLO_NTFY_MIN_WORK_SECS`; correct URL/headers/body.

   - `test_data_files.py`: add a `mark-state.sh` marker to
     `test_default_dockerfile_markers`.

6. **Docs**:

   - `README.md`: a "Notifications (ntfy)" section — the four config keys,
     `yolo config --ntfy-topic <topic>` quickstart, phone-subscription pointer,
     the `min_work_secs` noise knob, the token-via-secret-store recipe, the
     tradeoffs from §4.

   - `ARCHITECTURE.md`: extend the session-activity section (hooks now route
     through `mark-state.sh`; the ntfy env transport) and the per-file test
     coverage map (`test_ntfy.py`).

   - `CHANGELOG.md`: `## Unreleased` entry.

7. **Manual verification** (host-side, outside the test suite): subscribe with
   `curl -s ntfy.sh/<random-topic>/json`, run
   `yolo --ntfy-topic <random-topic>`, give Claude a prompt, confirm a ping on
   Stop and none while working; confirm `--ntfy-min-work-secs 60` suppresses
   quick turns.

## Invariant checklist (from CLAUDE.md)

- Secrets/capabilities off the docker argv — topic rides the `/run/secrets`
  file transport. ✔

- Host-side-only config — ntfy keys live in the standard host config files;
  the container learning the topic grants no host access. ✔

- Build context stays Dockerfile-only; script is inline `printf`, no
  `COPY`/`--secret`. ✔

- Content-addressed image tag — hash shifts once by design; nothing assumes a
  fixed tag. ✔

- `--settings` wholesale-replace — hooks structure unchanged, user hooks still
  folded via `_read_settings_hooks`. ✔

## Out of scope / possible follow-ups

- Host-side notifications for lifecycle events (`stop`, `finish`, container
  crash).

- Per-state ntfy priority/tags, click-through URLs.

- Notification on `wip` dashboard events or `ExitPlanMode` (blocked on
  upstream hook support).
