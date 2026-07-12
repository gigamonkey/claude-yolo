"""Tests for tmux mode (--tmux / `tmux` config key) and the `ps` verb.

The tmux server is faked at the `_tmux` seam (every tmux command yolo issues
funnels through it), so these assert on the exact tmux argv sequences without a
tmux binary. Container launches still go through the stubbed `run_cli` harness;
the captured `os.execvp` argv shows whether yolo exec'd docker (default mode)
or `tmux attach` (tmux mode, invoked outside tmux).
"""

import json
import shlex
import subprocess
import types

import pytest


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["tmux"], rc, out, err)


class FakeTmux:
    """A minimal in-memory tmux server: tracks the session and its windows.

    Windows are (window_id, window_name) pairs, assumed to live in
    `session_name` — except entries with an explicit third element, the
    session, for cross-session picker tests (only `list-windows -a` shows
    those).
    """

    def __init__(self):
        self.calls = []
        self.has_session = False
        self.session_name = "yolo"
        self.windows = []  # (window_id, window_name[, session_name])
        self.attached = False  # a client is attached to the session
        self.prefix_keys = {}  # key -> its `list-keys` output line
        self.pane_dump = "build output line\nStartup log: somewhere\n"  # capture-pane content
        self.capture_rc = 0  # capture-pane exit code (a test can force failure)
        self._next = 10

    def __call__(self, *args):
        self.calls.append(list(args))
        verb = args[0]
        if verb == "has-session":
            return cp(0 if self.has_session else 1)
        if verb == "new-session":
            self.has_session = True
            self.windows.append(("@0", args[list(args).index("-n") + 1]))
            return cp(0)
        if verb == "new-window":
            wid = f"@{self._next}"
            self._next += 1
            self.windows.append((wid, args[list(args).index("-n") + 1]))
            return cp(0, out=wid + "\n")
        if verb == "list-windows":
            if "-a" in args:
                # wid \t session \t name \t pane_current_command
                lines = (
                    f"{w[0]}\t{self._session(w)}\t{w[1]}\t{self._pane_cmd(w)}\n"
                    for w in self.windows
                )
            else:
                lines = (f"{w[0]}\t{w[1]}\n" for w in self.windows)
            return cp(0, out="".join(lines))
        if verb == "list-clients":
            return cp(0, out="/dev/ttys001\n" if self.attached else "")
        if verb == "display-message":
            return cp(0, out=self.session_name + "\n")
        if verb == "list-keys":  # list-keys -T <table> <key>
            line = self.prefix_keys.get(args[-1])
            return cp(0, out=line + "\n") if line else cp(1, err=f"unknown key: {args[-1]}")
        if verb == "bind-key":  # bind-key -T <table> <key> <command...>
            self.prefix_keys[args[3]] = " ".join(args)
            return cp(0)
        if verb == "capture-pane":
            return cp(self.capture_rc, out=self.pane_dump)
        return cp(0)  # select-window / switch-client

    def _session(self, window):
        return window[2] if len(window) >= 3 else self.session_name

    def _pane_cmd(self, window):
        return window[3] if len(window) >= 4 else ""

    def named(self, verb):
        return [c for c in self.calls if c[0] == verb]


@pytest.fixture
def tmux(cy, monkeypatch):
    """Fake the tmux server and default to 'invoked outside tmux, no container'."""
    fake = FakeTmux()
    monkeypatch.setattr(cy, "_tmux", fake)
    monkeypatch.setattr(
        cy.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None
    )
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: None)
    monkeypatch.delenv("TMUX", raising=False)
    return fake


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def window_command(call):
    """The shell command a new-window/new-session call would run (its last arg)."""
    return call[-1]


def unwrapped(command):
    """Strip the keep-open-on-failure wrapper, back to the original argv."""
    return shlex.split(command.split("; ec=$?")[0])


# --- default off ------------------------------------------------------------


def test_tmux_off_by_default_execs_docker(cy, run_cli, tmux, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert argv[:2] == ["docker", "run"]
    assert tmux.calls == []


# --- spawning into tmux -----------------------------------------------------


def test_tmux_outside_creates_session_and_attaches(cy, run_cli, tmux, dirs):
    home, work = dirs
    argv = run_cli(["--tmux"], home=home, cwd=work)

    # fresh server: session created detached, window 0 = the wip dashboard
    (new_session,) = tmux.named("new-session")
    assert new_session[1:5] == ["-d", "-s", "yolo", "-n"]
    assert new_session[5] == cy.TMUX_DASHBOARD_WINDOW
    assert "wip --_dashboard" in window_command(new_session)

    # the claude window runs the same docker run the default mode would exec,
    # shell-quoted (the --settings JSON must survive the round trip) and wrapped
    # to hold a *failed* window open
    (new_window,) = tmux.named("new-window")
    assert new_window[new_window.index("-n") + 1] == "work"
    cmd = window_command(new_window)
    assert "; ec=$?" in cmd
    run_cmd = unwrapped(cmd)
    assert run_cmd[:2] == ["docker", "run"]
    settings = run_cmd[run_cmd.index("--settings") + 1]
    assert '"sandbox":{"enabled":false}' in settings  # survived the shlex round trip
    # the OAuth token rides /run/secrets, so it's NOT in the tmux pane command
    # (the retained-command exposure this transport exists to avoid)
    assert "sk-ant-oat-TESTTOKEN" not in cmd
    assert not any("CLAUDE_CODE_OAUTH_TOKEN" in a for a in run_cmd)
    assert any(a.endswith(":/run/secrets:rw") for a in run_cmd)

    # outside tmux, the invoking terminal becomes the client, on the new window
    assert argv == ["tmux", "select-window", "-t", "@10", ";", "attach-session", "-t", "=yolo"]


def test_tmux_outside_with_client_attached_does_not_mirror(cy, run_cli, tmux, dirs):
    home, work = dirs
    tmux.has_session = True
    tmux.attached = True  # another terminal is already attached to the session
    argv = run_cli(["--tmux"], home=home, cwd=work)

    # no second client attached (no exec into attach-session) — that would mirror
    assert argv is None
    assert tmux.named("attach-session") == []
    # instead the already-attached terminal is switched to the new window
    (new_window,) = tmux.named("new-window")
    assert ["select-window", "-t", "@10"] in tmux.calls


def test_tmux_inside_switches_client(cy, run_cli, tmux, dirs, monkeypatch):
    home, work = dirs
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    argv = run_cli(["--tmux"], home=home, cwd=work)
    assert argv is None  # no exec: the current client is switched instead
    assert ["select-window", "-t", "@10"] in tmux.calls
    assert ["switch-client", "-t", "=yolo"] in tmux.calls


def test_tmux_existing_session_not_recreated(cy, run_cli, tmux, dirs):
    home, work = dirs
    tmux.has_session = True
    run_cli(["--tmux"], home=home, cwd=work)
    assert tmux.named("new-session") == []
    assert len(tmux.named("new-window")) == 1


def test_tmux_new_session_enables_terminal_title(cy, run_cli, tmux, dirs):
    home, work = dirs
    run_cli(["--tmux"], home=home, cwd=work)
    opts = tmux.named("set-option")
    # bare session target, NOT `=yolo` — set-option rejects the `=` exact-match prefix
    assert ["set-option", "-t", "yolo", "set-titles", "on"] in opts
    # the title format branches by window kind (dashboard / shell / session)
    title = next(c[4] for c in opts if c[3] == "set-titles-string")
    assert cy.TMUX_DASHBOARD_WINDOW in title  # dashboard → "<session> wip"
    assert "shell:" in title and "session:" in title


def test_tmux_new_session_binds_wip_key(cy, run_cli, tmux, dirs):
    # prefix-y jumps to the dashboard *by name*, so it works at any window index
    home, work = dirs
    run_cli(["--tmux"], home=home, cwd=work)
    assert [
        "bind-key",
        "-T",
        "prefix",
        cy.TMUX_WIP_KEY,
        "select-window",
        "-t",
        f":={cy.TMUX_DASHBOARD_WINDOW}",
    ] in tmux.calls


def test_tmux_wip_key_respects_user_binding(cy, run_cli, tmux, dirs):
    # The user bound the key to something of their own — yolo leaves it alone.
    tmux.prefix_keys[cy.TMUX_WIP_KEY] = f"bind-key -T prefix {cy.TMUX_WIP_KEY} new-window"
    home, work = dirs
    run_cli(["--tmux"], home=home, cwd=work)
    assert tmux.named("bind-key") == []


def test_tmux_wip_key_rebinds_over_its_own_binding(cy, run_cli, tmux, dirs):
    # An existing binding that targets the dashboard window is ours — re-assert it
    # (same idempotent self-heal as the title options).
    tmux.prefix_keys[cy.TMUX_WIP_KEY] = (
        f"bind-key -T prefix {cy.TMUX_WIP_KEY} select-window -t :={cy.TMUX_DASHBOARD_WINDOW}"
    )
    home, work = dirs
    run_cli(["--tmux"], home=home, cwd=work)
    assert len(tmux.named("bind-key")) == 1


def test_tmux_personal_session_is_not_reconfigured(cy, run_cli, tmux, dirs):
    # A pre-existing session with no yolo dashboard window (a personal one aimed at
    # via --tmux-session) is left alone — its title config isn't touched.
    home, work = dirs
    tmux.has_session = True  # exists, but tmux.windows is empty → no yolo-wip window
    run_cli(["--tmux"], home=home, cwd=work)
    assert tmux.named("set-option") == []
    assert tmux.named("bind-key") == []


def test_tmux_existing_yolo_session_reasserts_title(cy, run_cli, tmux, dirs):
    # A session we own (it has the yolo-wip dashboard window) re-gets the title
    # options on launch, so a long-lived session heals itself without a kill-server.
    home, work = dirs
    tmux.has_session = True
    tmux.windows = [("@0", cy.TMUX_DASHBOARD_WINDOW)]
    run_cli(["--tmux"], home=home, cwd=work)
    opts = tmux.named("set-option")
    assert ["set-option", "-t", "yolo", "set-titles", "on"] in opts
    title = next(c[4] for c in opts if c[3] == "set-titles-string")
    assert cy.TMUX_DASHBOARD_WINDOW in title and "shell:" in title and "session:" in title
    # the wip jump key heals alongside the title options
    assert len(tmux.named("bind-key")) == 1


def test_tmux_window_names_are_pinned(cy, run_cli, tmux, dirs):
    # automatic-rename/allow-rename off so the container name stays in the bar
    home, work = dirs
    run_cli(["--tmux"], home=home, cwd=work)
    pinned = [c for c in tmux.named("set-window-option") if c[-2] == "automatic-rename"]
    assert ["set-window-option", "-t", "@10", "automatic-rename", "off"] in pinned
    assert all(c[-1] == "off" for c in tmux.named("set-window-option"))


def test_all_tmux_windows_prefers_live_docker_on_duplicate_name(cy, tmux):
    # Two windows share a name: a stale one (a dead shell, listed first) and the
    # live container window (pane running `docker`). `_all_tmux_windows` must pick
    # the live one, so the dashboard's Enter lands on the claude session, not the
    # stale shell. (window tuple: id, name, session, pane_current_command)
    tmux.windows = [
        ("@1", "repo-topic", "yolo", "bash"),  # stale, oldest
        ("@4", "repo-topic", "yolo", "docker"),  # the live session
    ]
    assert cy._all_tmux_windows()["repo-topic"] == ("@4", "yolo")
    # order-independent: live window listed first still wins
    tmux.windows.reverse()
    assert cy._all_tmux_windows()["repo-topic"] == ("@4", "yolo")
    # no docker window → fall back to first
    tmux.windows = [("@1", "solo", "yolo", "bash"), ("@2", "solo", "yolo", "zsh")]
    assert cy._all_tmux_windows()["solo"] == ("@1", "yolo")


def test_tmux_session_name_flag(cy, run_cli, tmux, dirs):
    home, work = dirs
    run_cli(["--tmux", "--tmux-session", "hacking"], home=home, cwd=work)
    (new_session,) = tmux.named("new-session")
    assert new_session[1:4] == ["-d", "-s", "hacking"]
    (new_window,) = tmux.named("new-window")
    assert new_window[new_window.index("-t") + 1] == "=hacking:"


def test_tmux_needs_tmux_on_path(cy, run_cli, tmux, dirs, monkeypatch):
    home, work = dirs
    monkeypatch.setattr(cy.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="tmux"):
        run_cli(["--tmux"], home=home, cwd=work)


# --- reusing a window for an already-running container ------------------------


def test_tmux_reuses_window_for_running_container(cy, run_cli, tmux, dirs, monkeypatch):
    home, work = dirs
    # the guard reads the running container's actual name; its tmux window carries it
    monkeypatch.setattr(cy, "_running_container_name", lambda *a, **k: "work")
    tmux.has_session = True
    tmux.windows.append(("@3", "work"))
    argv = run_cli(["resume", "--tmux"], home=home, cwd=work)
    # no duplicate docker run spawned; the existing window is focused instead
    assert tmux.named("new-window") == []
    assert argv == ["tmux", "select-window", "-t", "@3", ";", "attach-session", "-t", "=yolo"]


def test_tmux_spawns_when_container_runs_but_no_window_matches(
    cy, run_cli, tmux, dirs, monkeypatch
):
    # container started outside tmux mode: nothing to reuse, spawn under its real
    # name and let docker report the name conflict inside the (kept-open) window
    home, work = dirs
    monkeypatch.setattr(cy, "_running_container_name", lambda *a, **k: "work")
    tmux.has_session = True
    run_cli(["resume", "--tmux"], home=home, cwd=work)
    assert len(tmux.named("new-window")) == 1


def test_tmux_reuse_skips_image_build_and_warns(cy, run_cli, tmux, dirs, monkeypatch, capsys):
    # Reusing a running container keeps its image, so building a new one is
    # pointless — skip the build and warn that a changed Dockerfile won't apply
    # until the session is restarted (the reported "built it but ran the old one").
    home, work = dirs
    monkeypatch.setattr(cy, "_running_container_name", lambda *a, **k: "work")
    built = []
    monkeypatch.setattr(
        cy, "_build_image", lambda parsed, cwd: built.append(cwd) or "claude-yolo:x"
    )
    tmux.has_session = True
    tmux.windows.append(("@3", "work"))  # matching window exists → focus it
    run_cli(["resume", "--tmux"], home=home, cwd=work)
    assert built == []  # build skipped
    assert "already running" in capsys.readouterr().err


def test_tmux_no_window_still_builds(cy, run_cli, tmux, dirs, monkeypatch):
    # Running but no window (started outside tmux): fall through to build + spawn.
    home, work = dirs
    monkeypatch.setattr(cy, "_running_container_name", lambda *a, **k: "work")
    built = []
    monkeypatch.setattr(
        cy, "_build_image", lambda parsed, cwd: built.append(cwd) or "claude-yolo:x"
    )
    tmux.has_session = True
    run_cli(["resume", "--tmux"], home=home, cwd=work)
    assert built  # not short-circuited


# --- shell into a running container -------------------------------------------


def test_tmux_shell_into_running_container_opens_window(cy, run_cli, tmux, dirs, monkeypatch):
    home, work = dirs
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: "abc123def456")
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    argv = run_cli(["shell", "--tmux"], home=home, cwd=work)
    assert argv is None
    (new_window,) = tmux.named("new-window")
    assert new_window[new_window.index("-n") + 1] == "work-shell"
    assert unwrapped(window_command(new_window)) == [
        "docker",
        "exec",
        "-it",
        "abc123def456",
        "/bin/bash",
    ]


# --- config keys ---------------------------------------------------------------


def test_tmux_config_key_enables_and_cli_overrides(cy, run_cli, tmux, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"tmux": True, "tmux-session": "hacking"}))
    argv = run_cli([], home=home, cwd=work)
    assert argv[0] == "tmux"
    (new_session,) = tmux.named("new-session")
    assert new_session[1:4] == ["-d", "-s", "hacking"]

    tmux.calls.clear()
    argv = run_cli(["--no-tmux"], home=home, cwd=work)
    assert argv[:2] == ["docker", "run"]
    assert tmux.calls == []


def test_tmux_config_key_must_be_bool(cy, run_cli, tmux, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"tmux": "yes"}))
    with pytest.raises(SystemExit, match="true or false"):
        run_cli([], home=home, cwd=work)


def test_config_verb_persists_tmux_keys(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--tmux", "--tmux-session", "hacking"], home=home, cwd=work)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    e = next(v for v in projects.values() if v.get("dir") == str(work))
    assert {k: v for k, v in e.items() if k != "dir"} == {"tmux": True, "tmux-session": "hacking"}


# --- the ps verb -----------------------------------------------------------------


def fake_docker_ps(monkeypatch, cy, out):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(cy.subprocess, "run", run)
    return calls


def test_ps_renders_cross_repo_table(cy, monkeypatch, capsys, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # last field per line is the yolo.config-dir label (no status files exist here)
    out = (
        f"myrepo-fix\tfix\t{home}/.claude-yolo/worktrees/-x-myrepo/fix"
        f"\t127.0.0.1:55001->8000/tcp\t2 hours\t{home}/.claude\n"
        f"work\t\t{home}/hacks/work\t\t5 minutes\t{home}/.claude\n"
    )
    calls = fake_docker_ps(monkeypatch, cy, out)
    cy.do_ps(home, watch=False)
    printed = capsys.readouterr().out

    (ps_call,) = calls
    assert ps_call[:2] == ["docker", "ps"]
    assert "label=yolo.cwd" in ps_call  # the filter that finds yolo's containers

    lines = printed.splitlines()
    assert lines[0].split() == ["NAME", "TOPIC", "PORTS", "CREATED", "STATE"]
    # docker's PORTS blob is condensed to the bare host->container pair
    assert "myrepo-fix" in lines[1] and "fix" in lines[1] and "55001->8000" in lines[1]
    # cwd-mode row: "-" for no topic and no ports
    assert "work" in lines[2] and " - " in lines[2]


def test_ps_shows_waiting_time_from_status_file(cy, monkeypatch, capsys, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    status_dir = home / ".claude" / cy._STATUS_DIR_NAME
    status_dir.mkdir(parents=True)
    cwd = f"{home}/hacks/work"
    # written 120s ago, state "waiting" -> "waiting 2m"
    (status_dir / f"{cy._cwd_slug(cwd)}.state").write_text(f"waiting {int(cy.time.time()) - 120}")
    out = f"work\t\t{cwd}\t\t5 minutes\t{home}/.claude\n"
    fake_docker_ps(monkeypatch, cy, out)
    cy.do_ps(home, watch=False)
    assert "waiting 2m" in capsys.readouterr().out


def test_condense_ports_drops_noise_and_ipv6_twin(cy):
    raw = "127.0.0.1:55001->8000/tcp, 0.0.0.0:3000->3000/tcp, [::]:3000->3000/tcp"
    assert cy._condense_ports(raw) == "55001->8000,3000->3000"
    assert cy._condense_ports("") == ""


def test_ps_with_nothing_running(cy, monkeypatch, capsys, tmp_path):
    fake_docker_ps(monkeypatch, cy, "")
    cy.do_ps(tmp_path, watch=False)
    assert "No yolo containers running." in capsys.readouterr().out


def test_watch_only_applies_to_ps(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="--watch"):
        run_cli(["start", "--watch"], home=home, cwd=work)


# --- the ps --watch picker -------------------------------------------------------


@pytest.fixture
def ps_rows(cy, monkeypatch):
    """Canned _ps_rows, returned as a mutable list so tests can vary refreshes."""
    rows = [
        ["alpha", "-", "-", "2 hours", "working 12s"],
        ["beta-fix", "fix", "55001->8000", "5 minutes", "waiting 3m"],
    ]
    monkeypatch.setattr(cy, "_ps_rows", lambda home: [tuple(r) for r in rows])
    return rows


def keys(*pressed):
    """A scripted wait_key: plays the given keys, then quits the picker."""
    seq = list(pressed)

    def wait_key(timeout):
        return seq.pop(0) if seq else "q"

    return wait_key


def test_picker_enter_switches_to_selected_window(cy, tmux, ps_rows, tmp_path):
    tmux.windows += [("@1", "alpha"), ("@2", "beta-fix")]
    cy._ps_picker_loop(tmp_path, "yolo", keys("j", "\r"))
    # j moved the highlight from alpha to beta-fix; Enter selected its window —
    # same session, so no switch-client
    assert ["select-window", "-t", "@2"] in tmux.calls
    assert tmux.named("switch-client") == []


def test_picker_arrows_match_jk(cy, tmux, ps_rows, tmp_path):
    tmux.windows += [("@1", "alpha"), ("@2", "beta-fix")]
    cy._ps_picker_loop(tmp_path, "yolo", keys("down", "down", "up", "\r"))
    # down/down clamps at the last row, up returns to the first
    assert ["select-window", "-t", "@1"] in tmux.calls


def test_picker_switches_client_across_sessions(cy, tmux, ps_rows, tmp_path):
    tmux.windows += [("@1", "alpha"), ("@9", "beta-fix", "other")]
    cy._ps_picker_loop(tmp_path, "yolo", keys("j", "\r"))
    assert ["select-window", "-t", "@9"] in tmux.calls
    assert ["switch-client", "-t", "=other"] in tmux.calls


def test_picker_enter_noop_without_window(cy, tmux, ps_rows, tmp_path):
    tmux.windows += [("@1", "alpha")]  # beta-fix runs but has no tmux window
    cy._ps_picker_loop(tmp_path, "yolo", keys("j", "\r"))
    assert tmux.named("select-window") == []


def test_picker_selection_survives_refresh(cy, tmux, ps_rows, tmp_path):
    tmux.windows += [("@1", "alpha"), ("@2", "beta-fix")]
    script = [
        ("key", "j"),  # highlight beta-fix
        ("refresh", lambda: ps_rows.insert(0, ["zeta", "-", "-", "1 second", "working 1s"])),
        ("key", "\r"),  # must still target beta-fix, not whatever sits at index 1 now
    ]

    def wait_key(timeout):
        if not script:
            return "q"
        kind, value = script.pop(0)
        if kind == "refresh":
            value()
            return None
        return value

    cy._ps_picker_loop(tmp_path, "yolo", wait_key)
    assert ["select-window", "-t", "@2"] in tmux.calls


def test_picker_draw_highlights_and_marks_orphans(cy, tmux, ps_rows, tmp_path, capsys):
    tmux.windows += [("@1", "alpha")]  # beta-fix has no window -> the * mark
    cy._ps_picker_loop(tmp_path, "yolo", keys())
    frame = capsys.readouterr().out
    highlighted = [line for line in frame.splitlines() if line.startswith("\x1b[7m")]
    assert highlighted and "alpha" in highlighted[0]  # first row starts selected
    assert "beta-fix *" in frame
    assert "* no tmux window" in frame


def test_watch_outside_tty_or_tmux_is_passive(cy, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cy, "_ps_watch_passive", lambda home: called.append("passive"))
    monkeypatch.setattr(cy, "_ps_picker", lambda home: called.append("picker"))
    monkeypatch.delenv("TMUX", raising=False)  # pytest stdin isn't a tty either
    cy.do_ps(tmp_path, watch=True)
    assert called == ["passive"]


def test_watch_interactive_in_tmux_is_picker(cy, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cy, "_ps_watch_passive", lambda home: called.append("passive"))
    monkeypatch.setattr(cy, "_ps_picker", lambda home: called.append("picker"))
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    monkeypatch.setattr(cy.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    cy.do_ps(tmp_path, watch=True)
    assert called == ["picker"]


# --- startup log ------------------------------------------------------------


def test_startup_log_captured_for_spawned_window(cy, run_cli, tmux, dirs, monkeypatch):
    # A yolo-spawned window (YOLO_STARTUP_LOG marker + a tmux pane): the launch
    # snapshots the pane's history to <run-dir>/<container>/startup.log (0600)
    # just before exec'ing docker.
    home, work = dirs
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("YOLO_STARTUP_LOG", "1")
    argv = run_cli([], home=home, cwd=work)
    assert argv[:2] == ["docker", "run"]  # capture didn't derail the launch

    (capture,) = tmux.named("capture-pane")
    assert capture == ["capture-pane", "-p", "-e", "-S", "-", "-t", "%3"]
    log = home / ".claude-yolo-run" / "work" / "startup.log"
    assert log.read_text() == tmux.pane_dump
    assert log.stat().st_mode & 0o777 == 0o600


def test_startup_log_streams_post_exec_until_sentinel(cy, run_cli, tmux, dirs, monkeypatch):
    # The snapshot ends where yolo execvp's into docker; a pipe-pane started just
    # before appends the post-exec pane stream (docker chatter, the wrapper's
    # secrets/clones/.yolorc output) to the same log. Its reader exits at the
    # sentinel line the wrapper echoes right before exec'ing claude, so the TUI
    # never floods the log.
    home, work = dirs
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("YOLO_STARTUP_LOG", "1")
    argv = run_cli([], home=home, cwd=work)  # oauth-token default -> wrapped

    (pipe,) = tmux.named("pipe-pane")
    assert pipe[1:3] == ["-t", "%3"]
    reader = pipe[3]
    assert cy._STARTUP_END_LINE in reader
    assert str(home / ".claude-yolo-run" / "work" / "startup.log") in reader
    # ...and the wrapper really does emit that sentinel just before the exec.
    script = next(a for a in argv if 'exec "$@"' in a)
    assert f'echo {shlex.quote(cy._STARTUP_END_LINE)}; exec "$@"' in script


def test_startup_log_stream_skipped_for_unwrapped_launch(cy, run_cli, tmux, dirs, monkeypatch):
    # keychain auth with no secrets/clones/rc launches claude unwrapped: no
    # sentinel would ever print, so no pipe is started (nothing would stop it
    # before the TUI floods the log). The snapshot half still happens.
    home, work = dirs
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("YOLO_STARTUP_LOG", "1")
    run_cli(["--auth", "keychain"], home=home, cwd=work)
    assert tmux.named("pipe-pane") == []
    assert len(tmux.named("capture-pane")) == 1


def test_startup_log_skipped_without_marker(cy, run_cli, tmux, dirs, monkeypatch):
    # TMUX_PANE alone isn't enough: a hand-run yolo in someone's long-lived shell
    # pane must not capture their scrollback.
    home, work = dirs
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.delenv("YOLO_STARTUP_LOG", raising=False)
    run_cli([], home=home, cwd=work)
    assert tmux.named("capture-pane") == []
    assert tmux.named("pipe-pane") == []
    assert not (home / ".claude-yolo-run" / "work" / "startup.log").exists()


def test_startup_log_skipped_outside_tmux(cy, run_cli, tmux, dirs, monkeypatch):
    home, work = dirs
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("YOLO_STARTUP_LOG", "1")
    run_cli([], home=home, cwd=work)
    assert tmux.named("capture-pane") == []
    assert tmux.named("pipe-pane") == []


def test_startup_log_capture_failure_is_nonfatal(cy, run_cli, tmux, dirs, monkeypatch, capsys):
    # A capture-pane failure warns and the launch proceeds; no partial file.
    home, work = dirs
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("YOLO_STARTUP_LOG", "1")
    tmux.capture_rc = 1
    argv = run_cli([], home=home, cwd=work)
    assert argv[:2] == ["docker", "run"]
    assert not (home / ".claude-yolo-run" / "work" / "startup.log").exists()
    assert "couldn't capture the startup log" in capsys.readouterr().err


def test_spawn_session_window_sets_startup_marker(cy, tmux, dirs):
    # The dashboard's launch path marks its windows so the inner yolo knows the
    # pane history is exactly this launch and safe to snapshot.
    home, work = dirs
    tmux.has_session = True
    tmux.attached = True  # focus selects the window in the attached client; no exec
    cy._spawn_session_window(work, ["resume", "--no-tmux"], "repo-topic", "yolo")
    (new_window,) = tmux.named("new-window")
    assert window_command(new_window).startswith("YOLO_STARTUP_LOG=1 ")


def test_tmux_launch_window_has_no_startup_marker(cy, run_cli, tmux, dirs):
    # `yolo --tmux` from a shell puts only the `docker run` in the new window (the
    # startup output stayed in the invoking terminal), so no marker there.
    home, work = dirs
    run_cli(["--tmux"], home=home, cwd=work)
    (new_window,) = tmux.named("new-window")
    assert "YOLO_STARTUP_LOG" not in window_command(new_window)
