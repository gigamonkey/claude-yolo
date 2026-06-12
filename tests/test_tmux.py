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
                lines = (f"{w[0]}\t{self._session(w)}\t{w[1]}\n" for w in self.windows)
            else:
                lines = (f"{w[0]}\t{w[1]}\n" for w in self.windows)
            return cp(0, out="".join(lines))
        if verb == "display-message":
            return cp(0, out=self.session_name + "\n")
        return cp(0)  # select-window / switch-client

    def _session(self, window):
        return window[2] if len(window) == 3 else self.session_name

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

    # fresh server: session created detached, window 0 = the ps --watch dashboard
    (new_session,) = tmux.named("new-session")
    assert new_session[1:5] == ["-d", "-s", "yolo", "-n"]
    assert new_session[5] == cy.TMUX_DASHBOARD_WINDOW
    assert "ps --watch" in window_command(new_session)

    # the claude window runs the same docker run the default mode would exec,
    # shell-quoted (the --settings JSON must survive the round trip) and wrapped
    # to hold a *failed* window open
    (new_window,) = tmux.named("new-window")
    assert new_window[new_window.index("-n") + 1] == "work"
    cmd = window_command(new_window)
    assert "; ec=$?" in cmd
    run_cmd = unwrapped(cmd)
    assert run_cmd[:2] == ["docker", "run"]
    assert '{"sandbox":{"enabled":false}}' in run_cmd
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in run_cmd

    # outside tmux, the invoking terminal becomes the client, on the new window
    assert argv == ["tmux", "select-window", "-t", "@10", ";", "attach-session", "-t", "=yolo"]


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
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: "deadbeef1234")
    tmux.has_session = True
    tmux.windows.append(("@3", "work"))
    argv = run_cli(["resume", "--tmux"], home=home, cwd=work)
    # no duplicate docker run spawned; the existing window is focused instead
    assert tmux.named("new-window") == []
    assert argv == ["tmux", "select-window", "-t", "@3", ";", "attach-session", "-t", "=yolo"]


def test_tmux_spawns_when_container_runs_but_no_window_matches(
    cy, run_cli, tmux, dirs, monkeypatch
):
    # container started outside tmux mode: nothing to reuse, spawn and let
    # docker report the name conflict inside the (kept-open) window
    home, work = dirs
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: "deadbeef1234")
    tmux.has_session = True
    run_cli(["resume", "--tmux"], home=home, cwd=work)
    assert len(tmux.named("new-window")) == 1


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
    assert projects[str(work)] == {"tmux": True, "tmux-session": "hacking"}


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
    out = (
        f"myrepo-fix\tfix\t{home}/.claude-yolo/worktrees/-x-myrepo/fix\t2 hours\n"
        f"work\t\t{home}/hacks/work\t5 minutes\n"
    )
    calls = fake_docker_ps(monkeypatch, cy, out)
    cy.do_ps(home, watch=False)
    printed = capsys.readouterr().out

    (ps_call,) = calls
    assert ps_call[:2] == ["docker", "ps"]
    assert "label=yolo.cwd" in ps_call  # the filter that finds yolo's containers

    lines = printed.splitlines()
    assert lines[0].split() == ["NAME", "TOPIC", "DIRECTORY", "UP"]
    assert "myrepo-fix" in lines[1] and "fix" in lines[1]
    # cwd-mode row: "-" for no topic, home shortened to ~
    assert "work" in lines[2] and " - " in lines[2] and "~/hacks/work" in lines[2]


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
        ["alpha", "-", "~/hacks/alpha", "2 hours"],
        ["beta-fix", "fix", "~/wt/fix", "5 minutes"],
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
        ("refresh", lambda: ps_rows.insert(0, ["zeta", "-", "~/z", "1 second"])),
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
