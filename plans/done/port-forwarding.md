# Port forwarding: a `ports` config key + `yolo browse`

## Problem

Today the only way to reach a server running inside a yolo container from the
host is the raw docker passthrough (`yolo -- -p 8000:8000`). That has three
problems:

1. **It can't be persisted.** A project whose dev server always runs on 8000
   has to retype the `--` args on every `start`/`resume`; there's no config key
   for it the way there is for `mounts` or `ssh-agent`.
2. **Fixed host ports collide across sessions.** The whole point of the
   worktree workflow is running several sessions of the same project in
   parallel — but two containers can't both publish host port 8000. Each
   session needs its *own* host port, which a static config value can't
   express.
3. **Dynamic ports aren't discoverable.** If the host port varies per session,
   the user needs a way to find "the URL for *this* session" without reading
   `docker port` output by hand.

## Desiderata

- A per-project config key (`projects.json` / `~/.yolo.json`) declaring which
  container port(s) the project's server uses.
- Multiple concurrent sessions (different worktrees, same project) must each
  get a working, non-conflicting host port with **zero per-session
  configuration**.
- `yolo browse [TOPIC]` opens the host browser at the right host port for the
  given session (worktree by `TOPIC`, else the cwd session) — the
  discoverability counterpart, analogous to how `shell` targets a session.
- Safe defaults: ports bound to loopback only (this is a skip-permissions
  container; its server should not be on the LAN), and nothing published
  unless configured.

## Design overview

The core trick: **let docker allocate the host port** (`-p 127.0.0.1:0:8000` —
host port `0` means "pick a free ephemeral port") and **let docker be the
registry** (`docker port <container> 8000` reports what it picked). yolo never
has to allocate, persist, or garbage-collect port numbers; there is no state
file to go stale and no race against other processes grabbing ports. Parallel
sessions can't collide because the kernel hands each one a distinct free port.
The cost — the host port differs per session and per restart — is exactly what
`yolo browse` exists to absorb.

This mirrors the existing architecture: like containers being found by docker
*label* rather than by yolo-side bookkeeping, the port mapping is found by
docker *query* rather than yolo-side bookkeeping.

## The `ports` config key / `--port` flag

- **`--port SPEC`**, repeatable, mirrored by a `ports` config key (string or
  list of strings), exactly parallel to `--mount`/`mounts`.
- **Spec format: `CONTAINER` or `HOST:CONTAINER`** (integers 1–65535).
  - `"8000"` (the normal form) → `-p 127.0.0.1:0:8000`: docker picks a free
    host port, loopback-bound. This is what projects should configure.
  - `"8000:8000"` (explicit pin) → `-p 127.0.0.1:8000:8000`: a stable,
    bookmarkable host port for people who run only one session at a time. A
    second concurrent session fails at `docker run` with address-in-use —
    docker's error is clear and immediate, so we don't pre-check.
  - No `:ro`-style suffixes, no protocol option (tcp only), no host-IP option.
    `0.0.0.0` exposure is deliberately not expressible here; anyone who truly
    wants the container's server on the LAN can still use the raw `-- -p`
    passthrough, which keeps the foot-gun outside the config file.
- **Concat semantics**: `ports` joins `prompts` and `mounts` in
  `_CONCAT_DESTS` — global config + project entry + CLI flags accumulate, exact
  duplicates deduped. On a same-container-port conflict (e.g. global says
  `8000`, project says `8000:8000`) the higher layer wins, matching the
  mounts ro/rw rule.
- **Validation** in `_parse_port_spec` (the `_parse_mount_spec` analogue):
  integer parts, port ranges, at most one `:`. Config-file values validated in
  `_parse_yolo_dict` like everything else; bad CLI specs are argparse errors.
- **`config` verb support**: `--add-port SPEC` / `--remove-port CONTAINER`
  element-wise editors via `_apply_config_edits`, exactly like
  `--add-mount`/`--remove-mount` (remove matches on the container port with
  any `HOST:` prefix stripped; emptied list drops the key). `--port` with
  `--add-port`/`--remove-port` in one call is a contradiction error, same as
  the mounts family.

## What a launch does with it

In `launch_container`, for each parsed port spec:

1. Append `-p 127.0.0.1:{host or 0}:{container}` to the docker args. Only on
   the launch paths (`start`/`resume`/`shell`-fresh/bare `yolo`) — like extra
   mounts, port specs are never resolved for terminal verbs, so a bad spec
   can't break `list`/`finish`/`config`.
2. Stamp a **`yolo.ports=8000,3000`** label (comma-joined container ports, in
   config order). This is how `browse` and `ps` later know which container
   ports are meaningful and which one is "primary" (the first) without
   re-reading config — consistent with `shell` joining a running container
   with the mounts *it was launched with*: the label describes the actual
   container, config describes the next launch.
3. Extend the built-in system prompt: *"Container port(s) 8000 are forwarded
   to the host. A server must listen on 0.0.0.0 (not 127.0.0.1) to be
   reachable from the host browser; the user can open it with `yolo browse`."*
   This kills the single most common failure mode — dev servers defaulting to
   loopback inside the container, where the docker port-forward can't reach
   them — at the source, by telling the agent that runs the server.

Note that `docker exec` paths (`shell` into running) need no changes: port
mappings, like mounts, are fixed at `docker run` time, and exec'd shells just
join them.

## The `browse` verb

`yolo browse [TOPIC]`, a terminal verb (no container launch, no git required
in cwd mode — same degradation as `ps`):

1. **Find the container** with the existing `running_container_for` query: by
   `yolo.worktree` label when `TOPIC` is given, by `yolo.cwd` otherwise.
   No running container → pointed error ("no running session for …; start one
   with `yolo start …`").
2. **Pick the container port**: the first entry of the `yolo.ports` label, or
   `--port N` (verb-only flag, validated in dispatch like `--watch`/`--force`)
   to select another. Empty/missing label → error explaining the session was
   launched without `ports` config and that mappings can't be added to a
   running container (exit and `resume` after configuring).
3. **Resolve the host port**: `docker port <id> <container_port>` (new
   `_docker_port` helper — a subprocess seam the tests can stub, like
   `_tmux`). Parse the first `127.0.0.1:NNNNN` line (the command can emit an
   IPv6 line too).
4. **Print the URL, then open it**: always print `http://127.0.0.1:NNNNN/`
   (so it's copy-pasteable even when a browser already has it open), then
   `open <url>` on macOS. `--print`/`-n` skips the `open` for scripting.
   We do *not* poll for the server to be listening — `browse` may legitimately
   run before Claude has started the server, and a browser tab at a
   not-yet-listening port is self-explanatory and refreshable.

`browse` reads only docker state (labels + port query), so it works regardless
of which terminal/tmux window the session lives in, and `yolo browse TOPIC`
from anywhere inside the repo does the right thing for that worktree's
session.

## `ps` / `list` / picker integration (nice-to-have, same PR if cheap)

`yolo ps` (and the `--watch` dashboard/picker) grows a PORTS column rendering
`55001→8000` per mapping (host port via the same `_docker_port` helper,
batched or lazily — the 2s refresh budget matters), empty for sessions without
ports. This makes the dynamic allocation legible at a glance and gives the
tmux dashboard the full "which session is on which port" map. `list` stays
as-is (it shows worktrees, not containers).

## Implementation sketch

| Piece | Where |
| --- | --- |
| `--port` flag, `ports` in `YOLO_KEYS` + `_CONCAT_DESTS` | argparse setup, `_parse_yolo_dict` |
| `_parse_port_spec(spec) -> (host: int \| None, container: int)` | next to `_parse_mount_spec` |
| `-p` args + `yolo.ports` label + prompt addition | `launch_container` / `build_claude_args` |
| `browse` verb + `--port`/`--print` verb flags | `main` dispatch (terminal-verb tier) |
| `_docker_port(container_id, container_port) -> int` | new helper, subprocess seam |
| `config --add-port/--remove-port` | `_apply_config_edits` |
| `ps` PORTS column | `do_ps` / `_draw_picker` |

## Testing

- `test_config.py`: spec parsing (valid/invalid), concat across layers,
  same-container-port override rule, `config --add-port/--remove-port`
  including the contradiction errors and remove-by-container-port matching.
- `test_cli.py`: `-p 127.0.0.1:0:8000` and the `yolo.ports` label in the
  assembled `docker run` argv; pinned form; prompt addition present iff ports
  configured; terminal verbs untouched by a bad spec.
- `test_verbs.py` (or a new `test_browse.py`): `browse` dispatch — container
  found by the right label for TOPIC vs cwd, `--port` selection, the
  no-container and no-label errors, URL formation — with
  `running_container_for` and `_docker_port` stubbed and `open` captured
  (assert on argv, never spawn a browser; same philosophy as `_tmux`).

## Alternatives considered

- **yolo-side port allocation** (deterministic hash of worktree path, or
  scan-for-free-port at launch): gives stable-ish numbers, but means yolo owns
  an allocation registry with collision handling, staleness, and a race
  between "checked free" and "docker binds it". Docker-assigned + query has
  none of that, and `browse` makes the instability irrelevant. The explicit
  `HOST:CONTAINER` pin covers the "I want a bookmarkable port" case for
  single-session use.
- **`--network host`**: no per-port control, exposes everything the container
  binds, needs a Docker Desktop opt-in setting, and bypasses the loopback-only
  default. Stays available via raw passthrough; not config-expressible on
  purpose.
- **A `ports` state file under `~/.claude-yolo/`**: rejected for the same
  reason containers are found by label, not name — docker already knows; a
  second source of truth can only disagree.

## Out of scope (possible later)

- UDP / multiple protocols.
- A health-check/poll in `browse` ("waiting for the server…").
- `browse` printing all mapped ports as a menu when several are configured and
  `--port` is omitted (v1: first-is-primary).
- A generic `docker-args` config key (the security review it would need —
  arbitrary docker flags from a config file include `-v` — is exactly why
  `ports` is its own narrow, validated key).
