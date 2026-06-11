# tmux mode: spawn yolo sessions into a managed tmux session

## Goal

Optionally run every yolo-launched Claude session as a window inside a single
tmux session (default name `yolo`), so all sessions live in one terminal window
and you navigate with tmux keys instead of juggling desktop windows. Add a
dashboard that lists running yolo containers, suitable for keeping in a window
(or pane) of that tmux session. Strictly opt-in: default behavior — exec
`docker run` in the current terminal — is unchanged.

## Design summary

Two pieces:

1. **A launch seam + built-in tmux dispatcher.** Factor the tail of
   `launch_container` (today: print the command, `os.execvp("docker", run_cmd)`)
   into a small dispatch function. Default path is byte-for-byte today's
   behavior. With tmux mode enabled (`--tmux` flag or `tmux: true` config key),
   the dispatcher instead ensures the shared tmux session exists, creates a new
   window running the same `docker run` command, and attaches/switches to it.

2. **A `ps` verb + dashboard.** `yolo ps` lists *all* running yolo containers
   across repos (today's `list` is per-repo worktrees only), read from the
   `yolo.*` docker labels every launch already stamps. `yolo ps --watch`
   refreshes in a loop; tmux mode seeds window 0 of the session with it.

### Why built-in tmux rather than generic launcher hooks

The hook idea (a config key holding a command template that yolo hands the
docker command to, defaulting to "exec in this terminal") was considered. It's
deferred, not rejected:

- A good tmux experience needs more than "run this command elsewhere":
  ensure-session, window naming, attach-vs-switch-client depending on whether
  we're already inside tmux, dashboard seeding, dedup-to-existing-window. A
  template hook can't express that; users would each rebuild it in shell.
- Templates mean quoting hazards (the docker argv contains JSON in `--settings`
  and a bearer token) and losing exec semantics/error surfacing.
- The internal seam *is* the hook mechanism, minus the config surface. If a
  real second backend shows up later (zellij, kitty tabs, iTerm), we can expose
  the seam as a `launcher` config key then, with `tmux` as a built-in value.

### Windows, not panes

Each session gets a tmux **window** (tab-like, full-screen), not a pane split.
Claude Code is a full-screen TUI; two of them side-by-side in panes get cramped
fast, and tmux windows already give the "navigate between sessions" UX
(`prefix n`/`prefix <number>`/`choose-tree`). The dashboard is itself a window
by default. If you want the dashboard always visible, splitting it off into a
pane manually (or via a tmux hook of your own) stays easy because `yolo ps
--watch` is just a command. — *Flagged as an open question below.*

## CLI / config surface

- `--tmux` / `--no-tmux` (default off) — argparse pair like `--ssh-agent`.
- Config key `tmux` (bool) added to `YOLO_KEYS` → `"tmux": true` in
  `~/.yolo.json` makes it the default everywhere; per-project override in
  `projects.json`; explicit CLI flag wins, as everywhere else. Persistable via
  `yolo config --tmux` (it's just another `YOLO_KEYS` flag — the sentinel
  re-parse picks it up for free).
- Config key + flag `tmux-session NAME` (default `"yolo"`) — the shared tmux
  session name. One global session across all repos is the point (that's the
  "one window for everything" ask), but a per-project `tmux-session` lets
  someone group by project if they prefer.
- New verb `ps` (terminal verb, no TOPIC, no container launch) with a
  `--watch` flag. Verb-only flag validation in dispatch like `--force`/`--init`.

## Mechanics

### 1. The dispatch seam (`launch_container` tail)

Extract the last lines of `launch_container` into:

```python
def _dispatch_launch(run_cmd: list[str], parsed, *, window_name: str) -> None:
    if not parsed.tmux:
        print(sep); print(" ".join(run_cmd)); print(sep)
        os.execvp("docker", run_cmd)   # exactly today
    else:
        _launch_in_tmux(run_cmd, window_name, session=parsed.tmux_session)
```

`window_name` = the final container name (already computed with the
`-{config}`/`-{profile}` suffixes), which docker guarantees unique among
running containers — so tmux window names stay distinguishable too.

Everything *before* the exec stays in the invoking terminal, unchanged — in
particular `ensure_oauth_token`'s consent prompt / pty mint flow and
`ensure_logged_in`'s interactive login still talk to the TTY that ran `yolo`,
not to a tmux window. Only the assembled `docker run` moves into tmux.

### 2. `_launch_in_tmux`

```text
1. shutil.which("tmux") or sys.exit with install guidance.
2. Ensure session:  tmux has-session -t =<session>   (= forces exact match)
   else:            tmux new-session -d -s <session> -n yolo-ps <self> ps --watch
                    (window 0 = the dashboard, created with the session)
3. Create window:   tmux new-window -t =<session>: -n <window_name> \
                        -P -F '#{window_id}' -- <command string>
   Capture the printed window id for the select/switch step.
4. Focus it:
   - If $TMUX is set (we're inside some tmux already):
       tmux select-window -t <window_id>; tmux switch-client -t =<session>
     (covers both "already in the yolo session" and "in a different session")
   - Else: os.execvp("tmux", ["tmux", "attach", "-t", "=<session>", ";",
                              "select-window", "-t", <window_id>])
     so the invoking terminal becomes the tmux client, mirroring today's
     "this terminal becomes the session" feel.
```

- **Command string:** tmux runs the window command through a shell, so build it
  with `shlex.join(run_cmd)`. The argv contains JSON (`--settings`) and the
  OAuth token; `shlex.join` quotes both safely.
- **Exit/error visibility:** `docker run --rm` exiting kills the window
  (default `remain-on-exit off`), which is right for a clean `claude` exit but
  eats the error when `docker run` fails instantly (name conflict, daemon
  down). Wrap the command:
  `sh -c '<cmd>; ec=$?; [ $ec -eq 0 ] || { echo "yolo: exited $ec"; read -r _; }'`
  so a failed window sticks around until Enter. (Alternative considered:
  `set-option remain-on-exit` per-window; the wrapper is simpler and
  self-cleaning.)
- **Dedup:** before creating a window, if `running_container_for(...)` already
  matches (same check the `shell` verb uses), don't spawn a doomed duplicate
  `docker run` (it would die on the container-name conflict) — find the
  existing window by name (`tmux list-windows -F '#{window_id} #{window_name}'`)
  and just focus it, with a stderr note. Falls back to spawning if no window
  matches (container started outside tmux mode).
- **`<self>` for the dashboard window:** resolve how to re-invoke yolo as
  `sys.argv[0]` made absolute (works for the console script, the PATH symlink,
  and a bare `./yolo.py`). Plain `watch yolo ps` was rejected: `yolo` may not
  be on PATH under that name, and `--watch` in-process avoids depending on
  `watch` existing on the host (it's procps, present on macOS only via brew).

### 3. `yolo ps` (+ `--watch`)

Host-side, docker-only, no git required (works from any cwd — it's the
cross-repo dashboard):

```text
docker ps --filter label=yolo.cwd \
          --format '{{.ID}}\t{{.Names}}\t{{.RunningFor}}\t{{.Label "yolo.repo"}}\t{{.Label "yolo.worktree"}}\t{{.Label "yolo.cwd"}}'
```

Rendered as a table: `NAME / REPO / TOPIC / DIRECTORY / UP`. (`yolo.cwd` is
stamped on every launch, so the filter catches cwd-mode and worktree sessions
both; worktree-less rows show `-` for TOPIC.) `--watch` loops: clear screen
(`\x1b[H\x1b[2J`), print table + timestamp + hint line ("prefix+<n> to switch
windows"), `time.sleep(2)`, KeyboardInterrupt exits cleanly. Read-only v1 — no
interactive selection (see open questions).

`ps` dispatches with the other terminal verbs in `main` (after config load so
nothing is needed from it, actually before guardrails — it launches nothing,
so it's exempt from the home-dir and require-project-entry guards, like
`list`).

### 4. What else routes through tmux when the mode is on

- `start` / `resume` / bare `yolo` / fresh `shell`: yes — they all funnel
  through `launch_container`, so they get it via the seam automatically.
- `shell` into a *running* container (the `docker exec` path in `main`): yes,
  same treatment — window named `<container>-shell` via the same
  `_launch_in_tmux` (it takes any argv, not just `docker run`). Without this,
  half the shell verb would ignore the mode, which would feel broken.
- Terminal verbs (`list`, `ps`, `tokens`, `config`, `finish`, `setup-token`,
  `forget-token`): never — they run in the invoking terminal as today.
  (`setup-token`'s pty flow in particular must stay put.)

## Edge cases / notes

- **Token in tmux server state:** the window command string contains
  `CLAUDE_CODE_OAUTH_TOKEN`. Today's `os.execvp` already exposes the same argv
  in `ps` output for the life of the container, so this isn't a new class of
  leak, but tmux additionally retains the command string in server memory
  (`list-windows -F '#{pane_start_command}'`). Optional hardening, possibly a
  follow-up that benefits both modes: write `-e` vars to a chmod-600 tempfile
  and pass `--env-file` instead. Plan v1: note it, don't block on it.
- **Nested tmux:** Claude runs inside docker, not inside the tmux client's
  shell, so there's no TERM/nesting weirdness beyond ordinary tmux
  (`TERM=screen-256color` etc.) — docker allocates its own pty via `-it`.
- **`--` docker-args passthrough** rides along untouched: the seam receives the
  fully assembled `run_cmd`.
- **macOS tmux:** not in the base system; error message should say
  `brew install tmux`.
- **Window lifecycle:** windows close when their container exits; `finish`
  needs no tmux awareness (its "container still running" guard already covers
  the tmux case since the container is found by label, not by terminal).
- **`tmux` config key abuse surface:** none new — config is host-side-only by
  design, and the key is a bool + a session-name string, not a command.

## Implementation steps

1. Argparse: `--tmux`/`--no-tmux` (BooleanOptionalAction-style pair matching
   the existing flags), `--tmux-session`; add `tmux`, `tmux-session` to
   `YOLO_KEYS` (+ type validation in `_parse_yolo_dict`: bool / str).
2. Extract the `_dispatch_launch` seam from `launch_container`; thread
   `window_name` (the final container name) into it.
3. `_launch_in_tmux`: ensure-session, new-window (with the sh wrapper +
   `shlex.join`), dedup-to-existing-window, attach/switch logic.
4. `yolo ps` verb + `--watch`; wire verb dispatch + verb-only flag validation.
5. Dashboard seeding: create window 0 as `<self> ps --watch` when
   `_launch_in_tmux` creates the session.
6. Route the `shell`-into-running `docker exec` path through the seam too.
7. Tests (`tests/test_tmux.py`): stub `subprocess.run`/`os.execvp` and assert
   on captured tmux argvs — session-ensure, window creation command (incl.
   shlex quoting of `--settings` JSON), inside-vs-outside `$TMUX` branch
   (patch `os.environ`), dedup branch (stub `running_container_for`), config
   key merge/override (`~/.yolo.json` `tmux: true` + CLI `--no-tmux` wins),
   `ps` table rendering from canned `docker ps` output. Existing suites must
   stay green with tmux off (the default path is unchanged by construction).
8. Docs: README section ("tmux mode"), CLAUDE.md, `--help` text.

## Open questions (to iterate on)

1. **Windows vs panes** — plan says one window per session + a dashboard
   window. If you'd rather have the dashboard as an always-visible short pane
   at the top/bottom of every window, that's tmux-config territory
   (`pane-border-status`, or a `split-window` per new window) — doable, but
   opinionated; v1 keeps it a window. Confirm?
2. **Dashboard interactivity** — v1 is read-only `--watch`. A later version
   could be a picker (j/k + Enter → `tmux select-window`), or even lean on
   `tmux choose-tree` instead of a custom pane at all. Worth it?
3. **One global session vs per-repo** — default is one global `yolo` session
   (the stated goal); `tmux-session` as a per-project config key allows
   grouping. Is the per-project knob worth having in v1, or YAGNI?
4. **Attach behavior from outside tmux** — exec into `tmux attach` (proposed,
   matches today's "this terminal becomes the session") vs. just print
   "spawned in tmux session 'yolo'" and exit (leaves your shell usable;
   `tmux attach` when you want it). Could be a third state of the flag, but
   that smells like over-config for v1.
5. **Env-file hardening** for the token (see edge cases) — fold into this
   change or separate follow-up?
