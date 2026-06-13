# Show how long a yolo session has been waiting for input (`ps` STATE column)

## Goal

In `yolo ps` (and the tmux dashboard/picker), show for each running session
whether Claude is **working** or **waiting for input**, and when waiting, how
long it's been waiting. Drive it off Claude Code **hooks**, which are the
documented, unambiguous signal — not transcript-tail heuristics.

## Signal: Stop / UserPromptSubmit hooks

- **`Stop`** fires once when the main agent finishes responding and is now
  waiting for input (does *not* fire on user interrupt or API error,
  doesn't fire for subagents — that's `SubagentStop`). → record "waiting" + now.
- **`UserPromptSubmit`** fires when the user submits a prompt, before Claude
  processes it. → record "working" + now (the transition out of waiting).

Both events ignore `matcher`; the settings shape is
`{"hooks": {"Stop": [{"hooks": [{"type":"command","command": "..."}]}], ...}}`.

## Where the state lives

A tiny status file the in-container hook writes and `ps` reads. The only
host-visible writable channel from inside the container is a bind mount; the
config dir (`/home/claude/.claude` ⇄ host `~/.claude` or `--config-dir`) is the
right one (the cwd/worktree is the project tree — don't pollute it;
`~/.claude-yolo` is deliberately never mounted).

- File: `<config-dir>/.yolo-status/<cwd-slug>.state`, where `<cwd-slug>` is the
  session's working dir run through the existing
  `re.sub(r"[^a-zA-Z0-9]", "-", path)` scheme. cwd is unique per running
  container (cwd mode: one per dir; worktree mode: distinct paths), so it keys
  cleanly and `ps` can recompute it from the `yolo.cwd` label.
- Content: `waiting <epoch>` or `working <epoch>` (plain text — trivial for a
  shell one-liner to write and for `ps` to parse).

**Bake the absolute container path into the hook command** rather than rely on a
`docker run -e VAR` reaching the hook subprocess: the path is fixed
(`/home/claude/.claude/.yolo-status/<slug>.state`) and known at launch, and this
sidesteps any env-propagation question. Hook commands run via a shell, so
`printf 'waiting %s' "$(date +%s)" > <path>` works.

## ps → file mapping

`ps` is cross-repo and sees containers with different config dirs, so it can't
assume `~/.claude`. Stamp a **`yolo.config-dir`** label at launch (the host
config-dir path); `ps` reads it (falling back to `~/.claude` for older
containers), computes `<config-dir>/.yolo-status/<slug(yolo.cwd)>.state`, reads
the state, and renders a **STATE** column: `waiting 5m` / `working` / `-`
(no file yet, or unparseable). Duration humanized `s`/`m`/`h`/`d`.

The state file comes from the same `docker ps` call (one extra label field), so
no per-container `docker` query — fine for the 2s `--watch` cadence.

## Preserving the user's own hooks (merge)

`claude --settings` **overrides** the whole `hooks` key from the mounted
settings files (only `permissions` merges across scopes; the existing
`sandbox` override is per-key). So to keep a user's own hooks working inside the
container, read `hooks` from the config dir's `settings.json` and
`settings.local.json` at launch and **concatenate yolo's Stop/UserPromptSubmit
groups onto them**, passing the union via `--settings`. Best-effort: a missing
or malformed file contributes nothing. (Enterprise-managed settings aren't
covered — rare, and managed settings outrank `--settings` anyway.)

## Lifecycle

- On each **claude** launch (not the `shell`/bash entrypoint), yolo creates
  `<config-dir>/.yolo-status/` host-side and **deletes the stale state file** so
  a fresh session doesn't briefly show a prior session's "waiting 3h".
- The file persists across container exits (it's on host disk); the next
  Stop/UserPromptSubmit overwrites it. `ps` only reads files for *running*
  containers, so leftovers for finished sessions are simply never shown.

## Implementation map

| Piece | Where |
| --- | --- |
| `_cwd_slug`, `_STATUS_DIR_NAME` | near `_repo_slug_or_none` |
| `_read_settings_hooks(config_dir, home)` | near `build_claude_args` |
| hooks + status path baked into `--settings` | `build_claude_args` (new `status_state_path`, `extra_hooks` params) |
| `yolo.config-dir` label + status dir create/reset | `launch_container` (claude sessions only) |
| compute container status path + read user hooks, pass to `build_claude_args` | `main` |
| `_humanize_secs`, `_read_session_state` | near `_ps_rows` |
| STATE column (format field + parse + render) | `_ps_rows`, `PS_HEADERS`, `_draw_picker` |

## Out of scope / caveats

- A hook injected via `--settings` replaces mounted `settings.json` hooks
  in-container; mitigated by the merge above (settings.json + settings.local.json
  only). Document it like the existing sandbox-override note.
- Working state shows just `working` (no elapsed) — the ask is waiting time.
- A session killed mid-work leaves the file at `working`; it just stops showing
  once the container is gone.
