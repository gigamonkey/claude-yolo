"""Tests for the `wip` dashboard (the tmux-resident management dashboard).

The data layer (`_wip_sessions`/`_order_sessions`/`_wip_items`) is tested against
a real throwaway git repo with stubbed docker; the interactive loop (`_wip_loop`)
is driven by a scripted FakeTerm with `_wip_items`/`_draw_wip` and the action cores
stubbed, mirroring how test_tmux drives the ps picker. The seeded-dashboard window
itself is covered in test_tmux (the `wip --_dashboard` command).
"""

import contextlib
import json
import os
import pathlib
import subprocess
import types

import pytest


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit, plus an isolated fake HOME. Returns (repo, home)."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Tester")
    (r / "README").write_text("hi\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    home = tmp_path / "home"
    home.mkdir()
    return r, home


@pytest.fixture(autouse=True)
def no_docker_ps(cy, monkeypatch):
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: None)


class KeysExhausted(Exception):
    """FakeTerm ran out of scripted keys (run_loop catches this to end _wip_loop,
    since `q` no longer quits the dashboard while sessions are running)."""


class FakeTerm:
    """A scripted picker terminal: canned keys, prompt lines, and confirmations."""

    def __init__(self, keys, *, lines=None, confirms=None):
        self._keys = list(keys)
        self._lines = list(lines or [])
        self._confirms = list(confirms or [])
        self.confirm_prompts = []

    def wait_key(self, timeout):
        if not self._keys:
            raise KeysExhausted()
        return self._keys.pop(0)

    def prompt_line(self, prompt):
        return self._lines.pop(0) if self._lines else ""

    prompt_path = prompt_line  # same scripted-line source; completion is the real term's job

    def confirm(self, prompt):
        self.confirm_prompts.append(prompt)
        return self._confirms.pop(0) if self._confirms else False


def session_item(cy, **over):
    p = {
        "cid": "cid0",
        "name": "repo-topic",
        "topic": "topic",
        "state": "waiting",
        "window": "@5",
        "worktree": "/wt/topic",
        "slug": "repo",
        "main_root": "/repo",
    }
    p.update(over.pop("payload", {}))
    cols = over.pop("cols", ("repo-topic", "topic", "waiting 5m", "1m"))
    return cy.WipItem("session", over.pop("key", "session:repo-topic"), cols, p)


def worktree_item(cy, **over):
    p = {"worktree": "/wt/old", "main_root": "/repo", "slug": "repo", "topic": "old"}
    p.update(over.pop("payload", {}))
    return cy.WipItem(
        "worktree",
        over.pop("key", "worktree:repo:old"),
        over.pop("cols", ("repo", "old", "merged", "↓2 ↑0", "~/old")),
        p,
    )


def project_item(cy, **over):
    return cy.WipItem(
        "project",
        over.pop("key", "project:/p"),
        over.pop("cols", ("p", "/p")),  # REPO, DIRECTORY
        {
            "name": over.pop("name", None),
            "path": over.pop("path", "/p"),
            "registered": over.pop("registered", True),
            "window": over.pop("window", None),
        },
    )


def run_loop(cy, monkeypatch, sections, keys, *, lines=None, confirms=None, term=None):
    """Drive _wip_loop with fixed sections + scripted term; return the draw frames.

    Each frame is (selected_key, footer) as _draw_wip would have rendered it.
    Pass a prebuilt `term` to inspect it afterwards (e.g. confirm_prompts).
    """
    monkeypatch.setattr(cy, "_wip_items", lambda home: sections)
    frames = []
    monkeypatch.setattr(cy, "_draw_wip", lambda secs, sel, foot: frames.append((sel, foot)))
    term = term or FakeTerm(keys, lines=lines, confirms=confirms)
    # home=None → per-worktree _worktree_config returns built-in defaults, so the
    # loop runs without touching real config. The loop ends either on `q` (only
    # allowed with no sessions) or by exhausting the scripted keys.
    with contextlib.suppress(KeysExhausted):
        cy._wip_loop(None, "yolo", term)
    return frames


# --- data layer -------------------------------------------------------------


def test_order_sessions_unknown_then_waiting_then_agenting_then_working(cy):
    def mk(name, state, age, created_at=""):
        return cy.WipSession("c", name, "", "/c", "", "1m", state, age, created_at)

    ordered = cy._order_sessions(
        [
            mk("w-short", "working", 5),
            mk("idle-short", "waiting", 5),
            mk("agent-short", "agenting", 3),
            mk("unknown-new", None, 0, "2026-06-20 10:00:00 +0000 UTC"),
            mk("unknown-old", None, 0, "2026-06-20 09:00:00 +0000 UTC"),
            mk("idle-long", "waiting", 99),
            mk("agent-long", "agenting", 70),
            mk("w-long", "working", 50),
        ]
    )
    names = [s.name for s in ordered]
    # unknown (oldest-created first), then waiting, agenting, and working
    # (each longest first)
    assert names == [
        "unknown-old",
        "unknown-new",
        "idle-long",
        "idle-short",
        "agent-long",
        "agent-short",
        "w-long",
        "w-short",
    ]


def test_draw_wip_renders_one_sessions_table_grouped(cy, capsys):
    # One combined SESSIONS table (no separate WAITING/WORKING/OTHER headers), with
    # the rows in the order given (unknown → waiting → working from _order_sessions).
    sections = {
        "session": [
            session_item(
                cy, key="session:s1", payload={"state": None}, cols=("s1", "-", "1m", "-")
            ),
            session_item(
                cy,
                key="session:w1",
                payload={"state": "waiting"},
                cols=("w1", "-", "1m", "waiting 9m"),
            ),
            session_item(
                cy,
                key="session:k1",
                payload={"state": "working"},
                cols=("k1", "-", "1m", "working 2m"),
            ),
        ],
        "worktree": [],
        "project": [],
    }
    cy._draw_wip(sections, "session:s1", "")
    out = capsys.readouterr().out
    assert "SESSIONS" in out
    assert "WAITING SESSIONS" not in out and "WORKING SESSIONS" not in out
    assert out.index("s1") < out.index("w1") < out.index("k1")


def test_draw_wip_sessions_no_blank_lines_grouped_by_color(cy, capsys):
    # No blank lines between status groups anymore — they're distinguished by the
    # SESSION/STATE color (green waiting, cyan agenting, yellow working) instead.
    sections = {
        "session": [
            session_item(
                cy,
                key="session:w1",
                payload={"state": "waiting"},
                cols=("w1", "-", "1m", "waiting 9m"),
            ),
            session_item(
                cy,
                key="session:a1",
                payload={"state": "agenting"},
                cols=("a1", "-", "1m", "agenting 3m"),
            ),
            session_item(
                cy,
                key="session:k1",
                payload={"state": "working"},
                cols=("k1", "-", "1m", "working 1m"),
            ),
        ],
        "worktree": [],
        "project": [],
    }
    cy._draw_wip(sections, None, "")
    lines = capsys.readouterr().out.splitlines()
    w1 = next(i for i, ln in enumerate(lines) if "w1" in ln)
    a1 = next(i for i, ln in enumerate(lines) if "a1" in ln)
    k1 = next(i for i, ln in enumerate(lines) if "k1" in ln)
    assert (a1, k1) == (w1 + 1, w1 + 2)  # adjacent rows, no blank line between groups
    assert f"\x1b[{cy._GREEN}m" in lines[w1]  # waiting row tinted green
    assert f"\x1b[{cy._CYAN}m" in lines[a1]  # agenting row tinted cyan
    assert f"\x1b[{cy._YELLOW}m" in lines[k1]  # working row tinted yellow


def test_draw_wip_sessions_none_when_empty(cy, capsys):
    sections = {"session": [], "worktree": [], "project": []}
    cy._draw_wip(sections, None, "")
    out = capsys.readouterr().out
    assert "SESSIONS" in out and "(none)" in out


def test_draw_wip_projects_is_repo_directory_table(cy, capsys):
    # PROJECTS is a REPO / DIRECTORY table (like WORKTREES, minus the extra columns).
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, key="project:/work/a", cols=("a", "~/work/a"))],
    }
    cy._draw_wip(sections, None, "")
    out = cy._SGR_RE.sub("", capsys.readouterr().out)
    header = next(ln for ln in out.splitlines() if "REPO" in ln and "DIRECTORY" in ln)
    assert header.index("REPO") < header.index("DIRECTORY")  # two-column header
    assert "a" in out and "~/work/a" in out  # the repo basename and its directory


def test_wip_items_lists_all_worktrees_flagging_running(cy, run_cli, repo, monkeypatch):
    # Two worktrees; one has a running session. Both appear in the worktrees section
    # (the running one also shows as a session), with `running` set accordingly.
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    run_cli(["start", "beta"], home=home, cwd=r)
    wt_alpha = next((home / ".claude-yolo" / "worktrees").rglob("alpha"))

    monkeypatch.setattr(cy, "_all_tmux_windows", lambda: {})
    monkeypatch.setattr(
        cy,
        "_wip_sessions",
        lambda h: [
            cy.WipSession("cidA", "repo-alpha", "alpha", str(wt_alpha), "", "1m", "waiting", 9)
        ],
    )

    sections = cy._wip_items(home)
    assert [it.payload["topic"] for it in sections["session"]] == ["alpha"]
    # every worktree is listed, including the running one (it also shows as a
    # session); the running one is flagged so its row's f/r refuse and Enter switches
    wt_by_topic = {it.payload["topic"]: it.payload for it in sections["worktree"]}
    assert set(wt_by_topic) == {"alpha", "beta"}
    assert wt_by_topic["alpha"]["running"] and not wt_by_topic["beta"]["running"]
    # the running session resolved its worktree + main repo for finish/rebase
    sess = sections["session"][0]
    assert sess.payload["worktree"] == wt_alpha
    assert sess.payload["main_root"] is not None


def test_worktree_rows_report_commits(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))

    def commits():  # displayed "N behind; M ahead" (GitHub's order)
        rows = cy._worktree_rows(home, "HEAD", all_repos=True)
        return next(w for w in rows if w.topic == "topic").commits

    assert commits() == "↓0 ↑0"  # just branched off HEAD
    (wt / "f").write_text("x\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "ahead")
    assert commits() == "↓0 ↑1"  # one commit ahead of base, none behind
    # advance the base (main's HEAD) so the worktree is now also one behind
    (r / "g").write_text("y\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base moves")
    assert commits() == "↓1 ↑1"


def test_draw_wip_renders_commits_column(cy, capsys):
    sections = {
        "session": [],
        "worktree": [worktree_item(cy, cols=("repo", "old", "unmerged", "↓3 ↑1", "~/old"))],
        "project": [],
    }
    cy._draw_wip(sections, None, "")
    out = capsys.readouterr().out
    # values are colorized so the cell isn't one contiguous string; check the parts
    assert "COMMITS" in out and "↓3" in out and "↑1" in out
    assert f"\x1b[{cy._RED}m↓3" in out  # nonzero behind in red
    assert f"\x1b[{cy._GREEN}m↑1" in out  # nonzero ahead in green


def test_color_status_orphaned_is_red_even_when_running(cy):
    assert cy._color_status("orphaned") == f"\x1b[{cy._RED}morphaned\x1b[0m"
    # orphaned beats running (a running, orphaned worktree is still a problem)
    assert f"\x1b[{cy._RED}m" in cy._color_status("running, orphaned")


def test_wip_projects_flags_active(cy, tmp_path):
    home = tmp_path / "home"
    (home / ".claude-yolo").mkdir(parents=True)
    (home / ".claude-yolo" / "projects.json").write_text('{"/work/a": {}, "/work/b": {}}')
    sessions = [cy.WipSession("c", "n", "", "/work/a/sub", "", "1m", "waiting", 1)]
    projects = cy._wip_projects(home, sessions)
    by_path = {str(p["path"]): p["active"] for p in projects}
    assert by_path == {"/work/a": True, "/work/b": False}
    assert all(p["registered"] for p in projects)


def _sess(cy, name, cwd):
    return cy.WipSession("c", name, "", cwd, "", "1m", "waiting", 1)


def test_session_window_for_prefers_exact_path(cy):
    sessions = [
        _sess(cy, "proj-sub", "/work/proj/sub"),
        _sess(cy, "proj", "/work/proj"),
    ]
    windows = {"proj": ("@7", "yolo"), "proj-sub": ("@9", "yolo")}
    assert cy._session_window_for(pathlib.Path("/work/proj"), sessions, windows) == "@7"


def test_session_window_for_falls_back_to_subdir(cy):
    # No exact-path session, but one running in a subdir still counts.
    sessions = [_sess(cy, "proj-sub", "/work/proj/sub")]
    windows = {"proj-sub": ("@9", "yolo")}
    assert cy._session_window_for(pathlib.Path("/work/proj"), sessions, windows) == "@9"


def test_session_window_for_none_without_window(cy):
    # Running but started outside tmux (no window) → nothing to focus.
    sessions = [_sess(cy, "proj", "/work/proj")]
    assert cy._session_window_for(pathlib.Path("/work/proj"), sessions, {}) is None
    # And no session there at all → None.
    assert (
        cy._session_window_for(pathlib.Path("/work/other"), sessions, {"proj": ("@7", "y")}) is None
    )


def test_wip_projects_unions_recent_registry(cy, tmp_path):
    # A recently-opened project (in recent-projects.json) shows up alongside the
    # registered ones, flagged unregistered — but only if its directory still exists.
    home = tmp_path / "home"
    (home / ".claude-yolo").mkdir(parents=True)
    (home / ".claude-yolo" / "projects.json").write_text(f'{{"{tmp_path}/reg": {{}}}}')
    (tmp_path / "reg").mkdir()
    seen = tmp_path / "seen"
    seen.mkdir()
    cy._record_recent_project(home, str(seen))
    cy._record_recent_project(home, str(tmp_path / "gone"))  # dir doesn't exist
    cy._record_recent_project(home, str(tmp_path / "reg"))  # also registered
    projects = cy._wip_projects(home, [])
    by_path = {str(p["path"]): p["registered"] for p in projects}
    assert by_path == {str(tmp_path / "reg"): True, str(seen): False}  # gone/dup dropped


# --- loop: navigation + refresh ---------------------------------------------


def test_loop_navigation_moves_across_sections(cy, monkeypatch):
    sections = {
        "session": [session_item(cy)],
        "worktree": [worktree_item(cy)],
        "project": [project_item(cy)],
    }
    frames = run_loop(cy, monkeypatch, sections, ["down", "down"])
    assert [sel for sel, _ in frames] == [
        "session:repo-topic",
        "worktree:repo:old",
        "project:/p",
    ]


def test_loop_refresh_preserves_selection_by_key(cy, monkeypatch):
    sections = {"session": [session_item(cy)], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["down", None])
    # after moving to the worktree, a refresh (None) keeps it selected
    assert frames[-1][0] == "worktree:repo:old"


def test_q_quits_when_no_sessions(cy, monkeypatch):
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["q", "down"])
    assert len(frames) == 1  # q returned right away; the 'down' was never consumed


def test_q_blocked_while_sessions_running(cy, monkeypatch):
    # Quitting would close the dashboard's tmux window under running sessions, so
    # q and Esc are refused (with a footer explaining why) until none are left.
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["q", "\x1b"])
    assert [footer for _, footer in frames[1:]] == [
        "1 session still running — stop them before quitting.",
        "1 session still running — stop them before quitting.",
    ]


def test_draw_wip_quit_hint_only_without_sessions(cy, capsys):
    empty = {"session": [], "worktree": [], "project": []}
    cy._draw_wip(empty, None, "")
    assert "q quit" in capsys.readouterr().out
    busy = {"session": [session_item(cy)], "worktree": [], "project": []}
    cy._draw_wip(busy, "session:repo-topic", "")
    assert "q quit" not in capsys.readouterr().out


# --- loop: actions ----------------------------------------------------------


def test_enter_session_switches_window(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda s, w: calls.append((s, w)))
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["\r"])
    assert calls == [("yolo", "@5")]
    assert "switched to repo-topic" in frames[-1][1]


def test_enter_worktree_spawns_resume_window(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append((repo, argv))
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    run_loop(cy, monkeypatch, sections, ["\r", "q"])
    ((repo, argv),) = spawned
    assert repo == "/repo"
    assert argv == ["resume", "old", "--no-tmux"]


def test_enter_active_worktree_focuses_window(cy, monkeypatch):
    # A worktree with a live session window: Enter jumps to it (like an active
    # project), rather than spawning a resume the already-running guard would reject.
    focused = []
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda sess, win: focused.append((sess, win)))
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    sections = {
        "session": [],
        "worktree": [worktree_item(cy, payload={"running": True, "window": "@8"})],
        "project": [],
    }
    run_loop(cy, monkeypatch, sections, ["\r", "q"])
    assert focused == [("yolo", "@8")]
    assert spawned == []


def test_finish_rebase_on_running_worktree_defer_to_core(cy, monkeypatch):
    # The dashboard no longer refuses f/r on a running worktree row — it calls the
    # cores, which own the session guard (finish stops an idle session; rebase
    # guards a working one). Both cores are invoked, not short-circuited.
    called = []
    monkeypatch.setattr(cy, "finish_worktree", lambda *a, **k: called.append("finish") or "f")
    monkeypatch.setattr(cy, "rebase_worktree", lambda *a, **k: called.append("rebase") or "r")
    sections = {
        "session": [],
        "worktree": [worktree_item(cy, payload={"running": True, "window": "@8"})],
        "project": [],
    }
    run_loop(cy, monkeypatch, sections, ["f", "r", "q"], confirms=[True])
    assert called == ["finish", "rebase"]


def test_enter_project_spawns_resume_window(cy, monkeypatch):
    # Enter on a project resumes the dir's session (falling back to a fresh one
    # when there's nothing to continue — handled inside the spawned `yolo resume`).
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append((repo, argv))
    )
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/work/proj")]}
    run_loop(cy, monkeypatch, sections, ["\r", "q"])
    ((repo, argv),) = spawned
    assert repo == "/work/proj"
    assert argv == ["resume", "--no-tmux"]


def test_enter_active_project_focuses_window(cy, monkeypatch):
    # A project with a live session window: Enter jumps to it (like a session row),
    # rather than spawning a `resume` the already-running guard would reject.
    focused = []
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda sess, win: focused.append((sess, win)))
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, path="/work/proj", window="@7")],
    }
    run_loop(cy, monkeypatch, sections, ["\r", "q"])
    assert focused == [("yolo", "@7")]
    assert spawned == []


def test_wip_spawn_names_use_project_name(cy, tmp_path):
    # A project's name is what its sessions run under (containers become
    # `myproj` / `myproj-TOPIC`), so the dashboard must name the windows it
    # spawns the same way — the session↔window match is by exact name — and a
    # registered row spawns with `--project NAME` so the inner yolo agrees.
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    (home / ".claude-yolo").mkdir(parents=True)
    proj.mkdir()
    (home / ".claude-yolo" / "projects.json").write_text(json.dumps({"myproj": {"dir": str(proj)}}))
    assert cy._wip_spawn_target("project", {"name": "myproj", "path": str(proj)}, home) == (
        str(proj),
        "myproj",
        "myproj",
        ["--project", "myproj"],
    )
    # an unregistered (recent-dir) row: basename, no --project
    assert cy._wip_spawn_target("project", {"path": str(proj)}, home) == (
        str(proj),
        "proj",
        "proj",
        [],
    )
    # a worktree row resolves the project by dir containment for its window name
    wt = tmp_path / "wt" / "feat"
    p = {"main_root": str(proj), "worktree": str(wt), "topic": "feat"}
    assert cy._wip_spawn_target("worktree", p, home)[1] == "myproj-feat"
    # home=None (the loop's test/standalone path) falls back to the basename
    assert cy._wip_spawn_target("worktree", p, None)[1] == "proj-feat"


def test_N_new_session_on_worktree_and_project(cy, monkeypatch):
    # `N` starts a *fresh* session: `resume TOPIC --new` for a worktree, `start` for
    # a project (vs Enter, which resumes the most recent).
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    sections = {
        "session": [],
        "worktree": [worktree_item(cy)],
        "project": [project_item(cy, path="/work/proj")],
    }
    # N on the worktree (first row), then move down to the project and N again
    run_loop(cy, monkeypatch, sections, ["N", "j", "N", "q"])
    assert spawned == [
        ("/repo", ["resume", "old", "--new", "--no-tmux"], "repo-old"),
        ("/work/proj", ["start", "--no-tmux"], "proj"),
    ]


def test_R_resume_pick_on_worktree_and_project(cy, monkeypatch):
    # `R` opens claude's session picker (`resume -r`) so you can pick an older session.
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append((repo, argv))
    )
    sections = {
        "session": [],
        "worktree": [worktree_item(cy)],
        "project": [project_item(cy, path="/work/proj")],
    }
    run_loop(cy, monkeypatch, sections, ["R", "j", "R", "q"])
    assert spawned == [
        ("/repo", ["resume", "old", "-r", "--no-tmux"]),
        ("/work/proj", ["resume", "-r", "--no-tmux"]),
    ]


def test_N_R_refuse_when_session_running(cy, monkeypatch):
    # A row with a live window: N/R refuse (one session per dir) and never spawn.
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda *a: None)
    sections = {
        "session": [],
        "worktree": [worktree_item(cy, payload={"running": True, "window": "@8"})],
        "project": [],
    }
    frames = run_loop(cy, monkeypatch, sections, ["N", "R", "q"])
    assert spawned == []
    assert "already running" in frames[-1][1]


def test_N_on_session_row_is_noop(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda *a: None)
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["N"])
    assert spawned == [] and "applies to worktrees and projects" in frames[-1][1]


def test_n_on_project_prompts_topic_and_starts_worktree(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/work/proj")]}
    run_loop(cy, monkeypatch, sections, ["n", "q"], lines=["fix-auth"])
    ((repo, argv, name),) = spawned
    assert repo == "/work/proj"
    assert argv == ["start", "fix-auth", "--no-tmux"]
    assert name == "proj-fix-auth"


def test_n_on_project_cancels_on_empty_topic(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append(argv)
    )
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/work/proj")]}
    frames = run_loop(cy, monkeypatch, sections, ["n", "q"], lines=[""])
    assert spawned == []
    assert frames[-1][1] == "cancelled."


def test_n_with_history_confirms_then_resumes(cy, monkeypatch):
    # Typing a previously-used topic under `n` asks before resuming — the name
    # may be a deliberate revive or an accidental collision — dating the prompt
    # with the topic's last activity. Confirming spawns `resume`, not `start`:
    # a finished topic revives with its old Claude session, and a live
    # worktree/branch resumes instead of tripping `start`'s already-exists guard.
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append(argv)
    )
    two_days = cy.time.time() - 2 * 86400
    monkeypatch.setattr(
        cy, "_topic_history", lambda home, path, topic: two_days if topic == "old-feat" else None
    )
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/work/proj")]}
    term = FakeTerm(["n", "q"], lines=["old-feat"], confirms=[True])
    frames = run_loop(cy, monkeypatch, sections, [], term=term)
    assert spawned == [["resume", "old-feat", "--no-tmux"]]
    assert "resuming worktree 'old-feat'" in frames[-1][1]
    assert term.confirm_prompts == ["'old-feat' already exists (last active 2d ago) — resume it?"]


def test_n_with_history_declined_cancels(cy, monkeypatch):
    # Declining the resume prompt spawns nothing — `start` over the existing
    # name would only trip its already-exists guard.
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    monkeypatch.setattr(cy, "_topic_history", lambda home, path, topic: cy.time.time() - 60)
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/work/proj")]}
    frames = run_loop(cy, monkeypatch, sections, ["n", "q"], lines=["old-feat"], confirms=[False])
    assert spawned == []
    assert frames[-1][1] == "cancelled."


def test_R_on_project_offers_finished_topics(cy, monkeypatch):
    # With finished topics on record, `R` on a project row opens a picker:
    # "(this directory)" first, then the topics newest-first; picking a topic
    # spawns `resume <topic>`, which revives it (worktree + old Claude session).
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    monkeypatch.setattr(cy, "_finished_topics", lambda home, path: ["feat-a", "feat-b"])
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/work/proj")]}
    run_loop(cy, monkeypatch, sections, ["R", "j", "\r", "q"])  # j past "(this directory)"
    ((repo, argv, name),) = spawned
    assert repo == "/work/proj"
    assert argv == ["resume", "feat-a", "--no-tmux"]
    assert name == "proj-feat-a"


def test_R_finished_topic_picker_this_directory_keeps_plain_picker(cy, monkeypatch):
    # Enter on "(this directory)" falls through to the plain `resume -r` spawn.
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append(argv)
    )
    monkeypatch.setattr(cy, "_finished_topics", lambda home, path: ["feat"])
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/p")]}
    run_loop(cy, monkeypatch, sections, ["R", "\r", "q"])
    assert spawned == [["resume", "-r", "--no-tmux"]]


def test_R_finished_topic_picker_cancel(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    monkeypatch.setattr(cy, "_finished_topics", lambda home, path: ["feat"])
    sections = {"session": [], "worktree": [], "project": [project_item(cy, path="/p")]}
    frames = run_loop(cy, monkeypatch, sections, ["R", "q", "q"])
    assert spawned == []
    assert frames[-1][1] == "cancelled."


def test_R_running_project_still_offers_finished_topics(cy, monkeypatch):
    # A live session in the project dir blocks the dir's own picker but not a
    # finished topic (its session is a separate container): "(this directory)"
    # is withheld and the topics remain pickable.
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda repo, argv, name, sess: spawned.append(argv)
    )
    monkeypatch.setattr(cy, "_finished_topics", lambda home, path: ["feat"])
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, path="/p", window="@3")],
    }
    run_loop(cy, monkeypatch, sections, ["R", "\r", "q"])  # the first option IS the topic
    assert spawned == [["resume", "feat", "--no-tmux"]]


def test_pick_one_scrolls_long_lists(cy, monkeypatch, capsys):
    # A 10-row terminal leaves a 6-row body; 20 options scroll with the
    # selection (the diff-stat viewport scheme) and the title carries a
    # position cue while the list overflows.
    monkeypatch.setattr(
        cy.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 10))
    )
    opts = [f"t{i:02}" for i in range(20)]
    term = FakeTerm(["j"] * 7 + ["\r"])
    assert cy._pick_one(term, "resume:", opts) == "t07"
    frames = capsys.readouterr().out.split("\x1b[H\x1b[2J")
    # first frame: viewport at the top — t00..t05 visible, t06 beyond the body
    assert "t00" in frames[1] and "t05" in frames[1] and "t06" not in frames[1]
    # last frame: the selection crossed the bottom edge → the viewport followed
    assert "\x1b[7m› t07" in frames[-1] and "t02" in frames[-1] and "t01" not in frames[-1]
    assert "8/20" in frames[-1]


def test_pick_one_short_list_has_no_position_cue(cy, monkeypatch, capsys):
    monkeypatch.setattr(
        cy.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 24))
    )
    assert cy._pick_one(FakeTerm(["\r"]), "resume:", ["a", "b"]) == "a"
    assert "1/2" not in capsys.readouterr().out


def test_finished_topics_from_transcript_buckets(cy, repo):
    # Enumerated from ~/.claude/projects/: buckets under the repo's worktree-base
    # slug whose worktree is gone, newest transcript first; live topics, empty
    # buckets, other repos' buckets, and the home=None loop default are excluded.
    r, home = repo
    slug = cy._repo_root_of(r)[2]
    base = home / ".claude-yolo" / "worktrees" / slug
    (base / "live").mkdir(parents=True)

    def bucket(topic, mtime):
        d = home / ".claude" / "projects" / cy._cwd_slug(base / topic)
        d.mkdir(parents=True)
        f = d / "s.jsonl"
        f.write_text("{}\n")
        os.utime(f, (mtime, mtime))

    bucket("live", 100)  # worktree still exists → excluded
    bucket("done-old", 200)
    bucket("done-new", 300)
    (home / ".claude" / "projects" / cy._cwd_slug(base / "empty")).mkdir()  # no *.jsonl
    (home / ".claude" / "projects" / "unrelated-bucket").mkdir()  # another repo's
    assert cy._finished_topics(home, r) == ["done-new", "done-old"]
    assert cy._finished_topics(None, r) == []


def test_topic_history_worktree_branch_or_transcript(cy, repo):
    # Returns the topic's last-activity timestamp (or None when fresh): the
    # newest of transcript mtime, branch tip commit time, and worktree mtime.
    r, home = repo
    slug = cy._repo_root_of(r)[2]
    base = home / ".claude-yolo" / "worktrees" / slug
    assert cy._topic_history(home, r, "nope") is None
    d = home / ".claude" / "projects" / cy._cwd_slug(base / "done")
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text("{}\n")
    os.utime(d / "s.jsonl", (500, 500))
    assert cy._topic_history(home, r, "done") == 500  # a finished topic's transcript
    git(r, "branch", "kept")
    tip = int(git(r, "log", "-1", "--format=%ct", "kept").stdout)
    assert cy._topic_history(home, r, "kept") == tip  # a surviving branch
    (base / "live").mkdir(parents=True)
    assert cy._topic_history(home, r, "live")  # a live worktree (dir mtime)
    assert cy._topic_history(None, r, "done") is None  # home=None (loop default)


def _newsession_item(cy):
    return cy.WipItem("newsession", "newsession:+", ("+",), {})


def test_enter_new_session_prompts_dir_and_starts(cy, monkeypatch, tmp_path):
    # Enter on the `+` row prompts for a directory and starts a fresh session there.
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    d = tmp_path / "somedir"
    d.mkdir()
    sections = {"session": [], "worktree": [], "project": [_newsession_item(cy)]}
    run_loop(cy, monkeypatch, sections, ["\r", "q"], lines=[str(d)])
    ((repo, argv, name),) = spawned
    assert repo == d and argv == ["start", "--no-tmux"] and name == "somedir"


def test_enter_new_session_cancels_on_empty(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    sections = {"session": [], "worktree": [], "project": [_newsession_item(cy)]}
    frames = run_loop(cy, monkeypatch, sections, ["\r", "q"], lines=[""])
    assert spawned == []
    assert frames[-1][1] == "cancelled."


def test_enter_new_session_rejects_non_dir(cy, monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    sections = {"session": [], "worktree": [], "project": [_newsession_item(cy)]}
    frames = run_loop(cy, monkeypatch, sections, ["\r", "q"], lines=[str(tmp_path / "nope")])
    assert spawned == []
    assert "not a directory" in frames[-1][1]


def test_browse_session_one_port(cy, monkeypatch):
    monkeypatch.setattr(cy, "_forwarded_ports", lambda cid: [8000])
    monkeypatch.setattr(cy, "browse_session", lambda cid, select=None: f"http://x/{select}")
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["b"])
    assert "opened http://x/None" in frames[-1][1]


def test_browse_session_prompts_for_multiple_ports(cy, monkeypatch):
    monkeypatch.setattr(cy, "_forwarded_ports", lambda cid: [(None, 8000), (None, 3000)])
    seen = []
    monkeypatch.setattr(cy, "browse_session", lambda cid, select=None: seen.append(select) or "ok")
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["b"], lines=["3000"])
    assert seen == [3000]


def test_stop_idle_session_no_confirm(cy, monkeypatch):
    # A waiting session stops without a confirm (nothing unrecoverable is lost:
    # the transcript persists and `resume` reconnects). No confirms are scripted,
    # so if one were asked FakeTerm would decline it and the stop wouldn't run.
    stopped = []
    monkeypatch.setattr(
        cy, "stop_session", lambda cid, where, home, *, force: stopped.append((cid, force)) or "ok"
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    # waiting session -> force False
    run_loop(cy, monkeypatch, sections, ["s"])
    assert stopped == [("cid0", False)]


def test_stop_working_session_uses_force(cy, monkeypatch):
    stopped = []
    monkeypatch.setattr(
        cy, "stop_session", lambda cid, where, home, *, force: stopped.append(force) or "ok"
    )
    sections = {
        "session": [session_item(cy, payload={"state": "working"})],
        "worktree": [],
        "project": [],
    }
    run_loop(cy, monkeypatch, sections, ["s"], confirms=[True])
    assert stopped == [True]


def test_stop_agenting_session_uses_force(cy, monkeypatch):
    # agenting (waiting on its own background agents) is active work: confirm + force
    stopped = []
    monkeypatch.setattr(
        cy, "stop_session", lambda cid, where, home, *, force: stopped.append(force) or "ok"
    )
    sections = {
        "session": [session_item(cy, payload={"state": "agenting"})],
        "worktree": [],
        "project": [],
    }
    run_loop(cy, monkeypatch, sections, ["s"], confirms=[True])
    assert stopped == [True]


def test_stop_active_session_cancelled_does_nothing(cy, monkeypatch):
    # Declining the active-session confirm (the only one `s` still asks) is a no-op.
    stopped = []
    monkeypatch.setattr(cy, "stop_session", lambda *a, **k: stopped.append(1) or "ok")
    sections = {
        "session": [session_item(cy, payload={"state": "working"})],
        "worktree": [],
        "project": [],
    }
    frames = run_loop(cy, monkeypatch, sections, ["s"], confirms=[False])
    assert stopped == []
    assert "cancelled" in frames[-1][1]


def test_S_opens_shell_in_session(cy, monkeypatch):
    # `S` on a session row docker-exec's a bash shell into its container, in a new
    # `<name>-shell` tmux window.
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_window", lambda cwd, cmd, name, sess, **k: spawned.append((cmd, name))
    )
    sections = {
        "session": [session_item(cy, payload={"cid": "cid9", "name": "repo-topic"})],
        "worktree": [],
        "project": [],
    }
    frames = run_loop(cy, monkeypatch, sections, ["S"])
    ((cmd, name),) = spawned
    assert cmd == ["docker", "exec", "-it", "cid9", "/bin/bash"]
    assert name == "repo-topic-shell"
    assert "opening a shell in repo-topic" in frames[-1][1]


def test_S_on_worktree_is_noop(cy, monkeypatch):
    # `S` (shell) is a session-only action — a worktree row does nothing.
    spawned = []
    monkeypatch.setattr(cy, "_spawn_window", lambda *a, **k: spawned.append(a))
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    run_loop(cy, monkeypatch, sections, ["S", "q"])
    assert spawned == []


def test_finish_worktree_confirms_then_calls_core(cy, monkeypatch):
    # run_loop drives with home=None, so `_finish_all_merged` is conservatively
    # False (never auto-skips without real config) → the confirm path runs.
    calls = []
    monkeypatch.setattr(
        cy,
        "finish_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "done",
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["f", "q"], confirms=[True])
    assert calls and calls[0][0] == "old"
    assert calls[0][1]["action"] == "delete-if-merged"
    assert frames[-1][1] == "done"


def test_finish_skips_confirm_when_all_merged(cy, monkeypatch):
    # A fully-merged topic finishes with no confirm: delete-if-merged disposes
    # the merged branch cleanly, and removing the worktree strands no committed
    # work (a finished topic revives by name). No confirms scripted → had one
    # been asked, FakeTerm would decline it and finish wouldn't run.
    monkeypatch.setattr(cy, "_finish_all_merged", lambda *a: True)
    calls = []
    monkeypatch.setattr(
        cy,
        "finish_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append(topic) or "done",
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["f", "q"])
    assert calls == ["old"]
    assert frames[-1][1] == "done"


def test_finish_confirms_when_unmerged(cy, monkeypatch):
    # An unmerged topic (commits not yet on its base) still confirms; declining
    # is a no-op.
    monkeypatch.setattr(cy, "_finish_all_merged", lambda *a: False)
    calls = []
    monkeypatch.setattr(cy, "finish_worktree", lambda *a, **k: calls.append(1) or "x")
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["f", "q"], confirms=[False])
    assert calls == []
    assert frames[-1][1] == "cancelled."


def test_finish_all_merged_reflects_branch_state(cy, run_cli, repo):
    # The real merge check behind the skip: a fresh topic branched off HEAD with
    # no commits reads as merged (skippable); a commit on the branch diverges it
    # (confirm required); home=None never auto-skips.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    slug = cy._repo_root_of(r)[2]
    assert cy._finish_all_merged(home, str(wt), str(r), slug, "topic", "HEAD")
    (wt / "x").write_text("x\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    assert not cy._finish_all_merged(home, str(wt), str(r), slug, "topic", "HEAD")
    assert not cy._finish_all_merged(None, str(wt), str(r), slug, "topic", "HEAD")


def test_finish_waiting_session_allowed(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cy,
        "finish_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append(topic) or "ok",
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["f"], confirms=[True])
    assert calls == ["topic"]  # the idle session's worktree gets finished


def test_discard_worktree_confirms_then_calls_core(cy, monkeypatch):
    # `x` always confirms — even a fully-merged topic (no _finish_all_merged
    # skip) — then runs the finish core with force=True and action="discard".
    monkeypatch.setattr(cy, "_finish_all_merged", lambda *a: True)
    calls = []
    monkeypatch.setattr(
        cy,
        "finish_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "gone",
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["x", "q"], confirms=[True])
    assert calls and calls[0][0] == "old"
    assert calls[0][1]["force"] is True
    assert calls[0][1]["action"] == "discard"
    assert frames[-1][1] == "gone"


def test_discard_declined_is_noop(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(cy, "finish_worktree", lambda *a, **k: calls.append(1) or "x")
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["x", "q"], confirms=[False])
    assert calls == []
    assert frames[-1][1] == "cancelled."


def test_discard_on_session_row_is_noop(cy, monkeypatch):
    # `x` is worktree-only — a session row (even an idle one, which `f` accepts)
    # just explains in the footer.
    calls = []
    monkeypatch.setattr(cy, "finish_worktree", lambda *a, **k: calls.append(1) or "x")
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["x"])
    assert calls == []
    assert frames[-1][1] == "discard applies to worktrees."


def test_discard_deletes_dirty_worktree_and_unmerged_branch(cy, run_cli, repo):
    # The real thing end-to-end: a topic with an unmerged commit *and* an
    # uncommitted file — everything `f` would refuse or preserve — is removed
    # wholesale, branch included, and its overlay entry goes with it.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("committed\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    (wt / "dirty.txt").write_text("uncommitted\n")
    slug = cy._repo_root_of(r)[2]
    payload = {"worktree": wt, "main_root": r, "slug": slug, "topic": "topic"}
    msg = cy._wip_discard("worktree", payload, home, FakeTerm([], confirms=[True]))
    assert "Removed worktree" in msg and "Deleted branch 'topic'" in msg
    assert not wt.exists()
    branches = git(r, "branch", "--list", "topic").stdout.strip()
    assert branches == ""


def test_rebase_worktree_calls_core(cy, monkeypatch):
    # `r` on a worktree row is per-repo (single_repo=True), like `m`/`d`.
    calls = []
    monkeypatch.setattr(
        cy,
        "rebase_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "rebased",
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["r", "q"])
    assert calls == [("old", {"capture": True, "single_repo": True})]
    assert frames[-1][1] == "rebased"


def test_rebase_on_session_row_rebases_whole_set(cy, monkeypatch):
    # `r` on an idle session row is the whole-topic rebase (single_repo=False):
    # the session is the whole topic, so it rebases every repo of the set.
    calls = []
    monkeypatch.setattr(
        cy,
        "rebase_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "rebased",
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}  # waiting
    run_loop(cy, monkeypatch, sections, ["r"])
    assert calls == [("topic", {"capture": True, "single_repo": False})]


def test_rebase_on_working_session_row_is_noop(cy, monkeypatch):
    # `r` still won't touch an actively-working session row (the idle guard),
    # unlike `m`/`d` which don't mutate the working tree.
    calls = []
    monkeypatch.setattr(cy, "rebase_worktree", lambda *a, **k: calls.append(1) or "x")
    working = session_item(cy, payload={"state": "working"})
    sections = {"session": [working], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["r"])
    assert calls == []
    assert "rebase applies to worktrees and idle sessions" in frames[-1][1]


def test_merge_worktree_calls_core_without_confirm(cy, monkeypatch):
    # No confirm on `m`: the core aborts on conflict and a landed merge is
    # reflog-revertable. No confirms are scripted, so if one were asked FakeTerm
    # would decline it and the merge wouldn't run.
    calls = []
    monkeypatch.setattr(
        cy,
        "merge_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "merged",
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["m", "q"])
    assert calls == [("old", {"capture": True, "single_repo": True})]
    assert frames[-1][1] == "merged"


def test_merge_on_session_row_merges_whole_set(cy, monkeypatch):
    # `m` on a session row is the whole-topic merge (the session's one container
    # spans every repo of the set), so it calls the core with single_repo=False —
    # unlike a worktree row, which is one repo. No idle guard (unlike f/r): the
    # merge only reads the branch's committed tip, so a `working` session is fine.
    calls = []
    monkeypatch.setattr(
        cy,
        "merge_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "ok",
    )
    working = session_item(cy, payload={"state": "working"})
    sections = {"session": [working], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["m"])
    assert calls == [("topic", {"capture": True, "single_repo": False})]


def test_d_on_worktree_spawns_diff_window(cy, monkeypatch):
    # `d` on a worktree row spawns `yolo diff <topic> --base <base>` in a new window;
    # base comes from the worktree's own config (home=None → built-in HEAD here).
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["d", "q"])
    ((repo, argv, name),) = spawned
    assert repo == "/repo"
    assert argv == ["diff", "old", "--base", "HEAD", "--stat", "--this-repo"]
    assert name == "diff-old"
    assert frames[-1][1] == "diffing 'old'…"


def test_d_on_session_row_diffs_whole_set(cy, monkeypatch):
    # `d` on a session row is the whole-topic diff — it omits `--this-repo`, so
    # `yolo diff` walks every repo of the set (each under a `== repo ==` header).
    # Read-only, so it works on any session state.
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}  # a worktree session
    run_loop(cy, monkeypatch, sections, ["d"])
    ((repo, argv, name),) = spawned
    assert (
        repo == "/repo"
        and argv == ["diff", "topic", "--base", "HEAD", "--stat"]  # no --this-repo
        and name == "diff-topic"
    )


def test_d_passes_matched_project_to_spawned_diff(cy, monkeypatch):
    # When the worktree's config resolves a project entry, `d` passes it as
    # --project so the spawned yolo picks the same entry even when several
    # projects share the directory (a bare `yolo diff` there would error).
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append(argv),
    )
    monkeypatch.setattr(
        cy, "_worktree_config", lambda home, root, wt: ("main", "delete-if-merged", "origin", "web")
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    run_loop(cy, monkeypatch, sections, ["d", "q"])
    assert spawned == [
        ["diff", "old", "--project", "web", "--base", "main", "--stat", "--this-repo"]
    ]


def test_d_on_cwd_session_is_noop(cy, monkeypatch):
    # A plain cwd session (no topic / main repo) isn't a worktree, so `d` does nothing.
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    sess = session_item(cy, payload={"topic": "", "main_root": None})
    sections = {"session": [sess], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["d"])
    assert spawned == []
    assert "diff applies to worktrees" in frames[-1][1]


# --- c (config) -------------------------------------------------------------


def _stub_config_run(cy, monkeypatch, *, returncode=0, stderr=""):
    """Stub `yolo config` subprocess; return the list it records (cmd, cwd) into."""
    calls = []
    monkeypatch.setattr(cy, "_self_invocation", lambda: "yolo")
    monkeypatch.setattr(
        cy.subprocess,
        "run",
        lambda cmd, **k: (
            calls.append((cmd, k.get("cwd")))
            or types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)
        ),
    )
    return calls


# --- the config editor (`c`) ------------------------------------------------

# The editor reads/writes real config files under `home`, so these drive
# `_wip_config`/the editor sub-loops directly with a real tmp home (the run_loop
# fixture uses home=None for the per-worktree defaults the dashboard needs). Writes
# still go through the `yolo config` subprocess, stubbed by _stub_config_run.

WT_PAYLOAD = {"worktree": "/wt/old", "main_root": "/repo", "slug": "repo", "topic": "old"}


def _seed_worktree_entry(home, entry, key="/wt/old"):
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "worktrees.json").write_text(json.dumps({key: entry}))


def test_c_on_session_is_noop(cy, monkeypatch, tmp_path):
    # `c` is a worktree/project action; a session row gets the explanatory message.
    calls = _stub_config_run(cy, monkeypatch)
    msg = cy._wip_config("session", session_item(cy).payload, tmp_path, FakeTerm([]))
    assert calls == [] and "config applies to" in msg


def test_c_raw_flags_escape_hatch_worktree(cy, monkeypatch, tmp_path):
    calls = _stub_config_run(cy, monkeypatch)
    term = FakeTerm(["e", "q"], lines=["--mount /x --port 8000"])
    msg = cy._wip_config("worktree", WT_PAYLOAD, tmp_path, term)
    ((cmd, cwd),) = calls
    assert cmd == ["yolo", "config", "old", "--mount", "/x", "--port", "8000"]
    assert cwd == "/repo"  # the worktree's main repo
    assert "edited config for worktree old" in msg


def test_c_raw_flags_project_scope(cy, monkeypatch, tmp_path):
    calls = _stub_config_run(cy, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    term = FakeTerm(["e", "q"], lines=["--auth bedrock"])
    cy._wip_config("project", {"path": str(proj)}, tmp_path, term)
    ((cmd, cwd),) = calls
    assert cmd == ["yolo", "config", "--auth", "bedrock"]  # no TOPIC → the project entry
    assert cwd == str(proj)


def test_c_edit_scalar_unsets_via_picker(cy, monkeypatch, tmp_path, capsys):
    # An existing scalar key: 'x' on it unsets immediately (no confirm), and the
    # footer echoes the removed value so an accidental unset is restorable.
    calls = _stub_config_run(cy, monkeypatch)
    _seed_worktree_entry(tmp_path, {"auth": "bedrock"})
    term = FakeTerm(["x", "q"])
    cy._wip_config("worktree", WT_PAYLOAD, tmp_path, term)
    ((cmd, _),) = calls
    assert cmd == ["yolo", "config", "old", "--unset", "auth"]
    assert "(was bedrock)" in capsys.readouterr().out  # shown in the next frame


def test_c_add_key_menu_is_sorted(cy, monkeypatch, tmp_path):
    # `a` offers the not-yet-set keys alphabetically, not in YOLO_KEYS order.
    _seed_worktree_entry(tmp_path, {"auth": "bedrock"})
    seen = []
    monkeypatch.setattr(cy, "_pick_one", lambda term, title, options: seen.append(options))
    cy._wip_config("worktree", WT_PAYLOAD, tmp_path, FakeTerm(["a", "q"]))
    (options,) = seen
    assert options == sorted(options)
    assert "auth" not in options  # already set, so not offered


def test_c_add_mount_element_with_mode(cy, monkeypatch, tmp_path):
    # The list-element view: add a mount — Tab-completed path, then a ro/rw pick.
    calls = _stub_config_run(cy, monkeypatch)
    scope = cy._config_scope("worktree", WT_PAYLOAD, tmp_path)
    # a → prompt_path (/data) → _pick_one mode (Enter picks "ro") → back, q to exit
    term = FakeTerm(["a", "\r", "q"], lines=["/data"])
    cy._config_list_loop(scope, "mounts", term)
    ((cmd, _),) = calls
    assert cmd == ["yolo", "config", "old", "--add-mount", "/data:ro"]


def test_c_remove_list_element(cy, monkeypatch, tmp_path):
    calls = _stub_config_run(cy, monkeypatch)
    _seed_worktree_entry(tmp_path, {"mounts": ["/x:ro", "/y:rw"]})
    scope = cy._config_scope("worktree", WT_PAYLOAD, tmp_path)
    cy._config_list_loop(scope, "mounts", FakeTerm(["x", "q"]))  # x removes the selected (first)
    ((cmd, _),) = calls
    assert cmd == ["yolo", "config", "old", "--remove-mount", "/x:ro"]


def test_c_clones_add_with_depth_via_edit_key(cy, monkeypatch, tmp_path):
    # Enter on the `clones` key routes to the dict-valued clones loop (not the
    # scalar prompt); add prompts url + dir + optional depth → --add-clone.
    calls = _stub_config_run(cy, monkeypatch)
    scope = cy._config_scope("worktree", WT_PAYLOAD, tmp_path)
    term = FakeTerm(["a", "q"], lines=["https://x/lib", "../lib", "2"])
    cy._config_edit_key(scope, "clones", term)
    ((cmd, _),) = calls
    assert cmd == ["yolo", "config", "old", "--add-clone", "https://x/lib", "../lib", "2"]


def test_c_clones_add_no_depth(cy, monkeypatch, tmp_path):
    # A blank depth line → no 3rd arg (a full clone).
    calls = _stub_config_run(cy, monkeypatch)
    scope = cy._config_scope("worktree", WT_PAYLOAD, tmp_path)
    term = FakeTerm(["a", "q"], lines=["https://x/lib", "../lib", ""])
    cy._config_clones_loop(scope, term)
    ((cmd, _),) = calls
    assert cmd == ["yolo", "config", "old", "--add-clone", "https://x/lib", "../lib"]


def test_c_clones_remove(cy, monkeypatch, tmp_path):
    calls = _stub_config_run(cy, monkeypatch)
    _seed_worktree_entry(tmp_path, {"clones": [{"url": "https://x/lib", "dir": "../lib"}]})
    scope = cy._config_scope("worktree", WT_PAYLOAD, tmp_path)
    cy._config_clones_loop(scope, FakeTerm(["x", "q"]))  # x removes the selected (first) by dir
    ((cmd, _),) = calls
    assert cmd == ["yolo", "config", "old", "--remove-clone", "../lib"]


def test_c_failure_surfaces_in_editor(cy, monkeypatch, tmp_path, capsys):
    _stub_config_run(cy, monkeypatch, returncode=2, stderr="not a directory: /nope")
    term = FakeTerm(["e", "q"], lines=["--mount /nope"])
    cy._wip_config("worktree", WT_PAYLOAD, tmp_path, term)
    assert "not a directory: /nope" in capsys.readouterr().out  # shown in the editor frame


def test_c_shows_current_values_and_inherited(cy, monkeypatch, tmp_path, capsys):
    _seed_worktree_entry(tmp_path, {"auth": "bedrock", "mounts": ["/x:ro"]})
    (tmp_path / ".yolo.json").write_text(json.dumps({"base": "main"}))  # a lower layer
    cy._wip_config("worktree", WT_PAYLOAD, tmp_path, FakeTerm(["q"]))
    out = capsys.readouterr().out
    assert "auth" in out and "bedrock" in out  # editable: the overlay's own keys
    assert "mounts" in out and "/x:ro" in out
    assert "inherited" in out and "base" in out and "main" in out  # read-only lower layer


def test_c_rename_project_entry(cy, monkeypatch, tmp_path, capsys):
    # `r` on a registered project entry renames it via `config --project OLD
    # --name NEW`, then rebinds the scope so later edits target the new name.
    calls = _stub_config_run(cy, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    scope = cy._config_scope("project", {"path": str(proj), "name": "old"}, tmp_path)
    assert scope.renameable
    msg = cy._config_editor_loop(scope, FakeTerm(["r", "q"], lines=["fresh"]))
    ((cmd, cwd),) = calls
    assert cmd == ["yolo", "config", "--project", "old", "--name", "fresh"]
    assert cwd == str(proj)
    # the scope rebinds, so a subsequent edit would hit `--project fresh`, not `old`
    assert scope.name == "fresh"
    assert scope.config_args == ["config", "--project", "fresh"]
    out = capsys.readouterr().out
    assert "r rename" in out  # the hint shows for a renameable scope
    assert "renamed project 'old' to 'fresh'." in out  # the confirmation frame
    assert "project fresh" in msg  # final footer reflects the new label


def test_c_rename_cancel_on_blank_or_same(cy, monkeypatch, tmp_path):
    calls = _stub_config_run(cy, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    scope = cy._config_scope("project", {"path": str(proj), "name": "old"}, tmp_path)
    cy._config_editor_loop(scope, FakeTerm(["r", "r", "q"], lines=["", "old"]))
    assert calls == []  # neither a blank nor an unchanged name runs config
    assert scope.name == "old"


def test_c_rename_failure_keeps_old_name(cy, monkeypatch, tmp_path, capsys):
    _stub_config_run(
        cy, monkeypatch, returncode=2, stderr="a project named 'taken' already exists."
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    scope = cy._config_scope("project", {"path": str(proj), "name": "old"}, tmp_path)
    cy._config_editor_loop(scope, FakeTerm(["r", "q"], lines=["taken"]))
    assert scope.name == "old"  # no rebind on a failed rename
    assert "already exists" in capsys.readouterr().out


def test_c_rename_not_offered_on_non_project_scopes(cy, monkeypatch, tmp_path, capsys):
    # Worktree overlays have no name; an unregistered recent dir has no entry — `r`
    # is inert on both, and the hint is absent.
    calls = _stub_config_run(cy, monkeypatch)
    wt = cy._config_scope("worktree", WT_PAYLOAD, tmp_path)
    assert not wt.renameable
    cy._config_editor_loop(wt, FakeTerm(["r", "q"]))  # `r` falls through, no subprocess
    proj = tmp_path / "p"
    proj.mkdir()
    unreg = cy._config_scope("project", {"path": str(proj)}, tmp_path)  # no name
    assert not unreg.renameable
    assert calls == []
    assert "r rename" not in capsys.readouterr().out


# --- config-editor units ----------------------------------------------------


def test_config_value_flags_bool_and_scalar(cy):
    assert cy._config_value_flags("ssh-agent", "true") == ["--ssh-agent"]
    assert cy._config_value_flags("ssh-agent", "false") == ["--no-ssh-agent"]
    assert cy._config_value_flags("auth", "bedrock") == ["--auth", "bedrock"]


def test_prompt_config_value_routes_by_kind(cy):
    # bool/choice go through the j/k+Enter picker; path/str through the line prompts
    assert cy._prompt_config_value(FakeTerm(["\r"]), "ssh-agent") == "true"  # first option
    assert cy._prompt_config_value(FakeTerm(["j", "\r"]), "auth") == "oauth-token"  # 2nd of AUTH
    assert cy._prompt_config_value(FakeTerm([], lines=["/df"]), "dockerfile") == "/df"
    assert cy._prompt_config_value(FakeTerm([], lines=["x86"]), "aws-profile") == "x86"


def test_pick_one_navigates_and_cancels(cy):
    assert cy._pick_one(FakeTerm(["j", "\r"]), "t", ["a", "b", "c"]) == "b"
    assert cy._pick_one(FakeTerm(["q"]), "t", ["a", "b"]) is None
    assert cy._pick_one(FakeTerm([]), "t", []) is None  # empty options → None


# --- diff-stat picker -------------------------------------------------------

DIFF_STAT = [" a.py | 1 +", " b.py | 2 ++", " c.py | 3 +++", " 3 files changed, 6 insertions(+)"]


def test_diff_stat_loop_navigates_and_opens_selected_file(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_window", lambda cwd, cmd, name, sess, **k: spawned.append((cwd, cmd, name, k))
    )
    monkeypatch.setattr(cy, "_draw_diff_stat", lambda *a: None)
    files = ["a.py", "b.py", "c.py"]
    term = FakeTerm(["down", "down", " ", "q"])  # to c.py, Space opens it, q quits
    cy._diff_stat_loop(files, DIFF_STAT, "/wt", "BASESHA", "yolo", "topic", "HEAD", term)
    ((cwd, cmd, name, kw),) = spawned
    assert cwd == "/wt"
    # two-dot against the merge-base origin, so the working tree (dirty changes) is
    # the right-hand side
    assert cmd == ["git", "diff", "BASESHA", "--", "c.py"]  # the selected file
    assert name == "diff-c.py"
    assert kw == {"env": {"LESS": "R"}}  # pager stays open until q (no auto-quit)


def test_diff_stat_loop_enter_opens_and_q_quits_without_spawn(cy, monkeypatch):
    spawned = []
    monkeypatch.setattr(cy, "_spawn_window", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(cy, "_draw_diff_stat", lambda *a: None)
    # Enter opens the first file; a bare quit opens nothing.
    cy._diff_stat_loop(["a.py"], DIFF_STAT, "/wt", "S", "yolo", "t", "HEAD", FakeTerm(["\r", "q"]))
    assert len(spawned) == 1 and spawned[0][1][-1] == "a.py"
    spawned.clear()
    cy._diff_stat_loop(["a.py"], DIFF_STAT, "/wt", "S", "yolo", "t", "HEAD", FakeTerm(["q"]))
    assert spawned == []


def test_draw_diff_stat_highlights_file_and_dims_summary(cy, capsys):
    cy._draw_diff_stat("topic", "HEAD", DIFF_STAT, 3, 1, 0, 20)  # 3 files, b.py selected
    out = capsys.readouterr().out
    assert "\x1b[7m b.py" in out  # selected file line is a reverse-video bar
    assert "\x1b[90m 3 files changed" in out  # the summary line is dim
    assert " a.py" in out and " c.py" in out  # other files shown plainly
    assert "2/3" not in out  # everything fits → no position cue in the title


def test_draw_diff_stat_windows_to_viewport(cy, capsys):
    # Only stat_lines[top:top+body] render; the title gains a selected/nfiles cue.
    cy._draw_diff_stat("topic", "HEAD", DIFF_STAT, 3, 2, 1, 2)  # viewport = b.py, c.py
    out = capsys.readouterr().out
    assert " a.py" not in out and "3 files changed" not in out  # outside the viewport
    assert " b.py" in out and "\x1b[7m c.py" in out
    assert "3/3" in out  # position cue when the stat overflows


def test_diff_stat_loop_scrolls_viewport_with_selection(cy, monkeypatch):
    frames = []
    monkeypatch.setattr(cy, "_draw_diff_stat", lambda *a: frames.append((a[4], a[5], a[6])))
    monkeypatch.setattr(
        cy.shutil, "get_terminal_size", lambda fallback=None: os.terminal_size((80, 6))
    )
    files = [f"f{i}.py" for i in range(5)]
    stat = [f" f{i}.py | 1 +" for i in range(5)] + [" 5 files changed, 5 insertions(+)"]
    term = FakeTerm(["j", "j", "j", "k", "q"])
    cy._diff_stat_loop(files, stat, "/wt", "S", "yolo", "t", "HEAD", term)
    # 6 rows - 4 chrome = 2-line viewport: (selected, top, body) per frame.
    # top follows the selection down past the bottom edge, then holds on the way up.
    assert frames == [(0, 0, 2), (1, 0, 2), (2, 1, 2), (3, 2, 2), (2, 2, 2)]


def test_tmux_window_command_env_prefixes_assignments(cy):
    # `env` prepends `KEY=val` to the command (the diff windows pass LESS=R so the
    # pager doesn't auto-quit a one-screen diff); the failure-hold is unchanged.
    cmd = cy._tmux_window_command(["git", "diff", "A...B"], env={"LESS": "R"})
    assert cmd.startswith("LESS=R git diff A...B;")
    assert "-ne 0" in cmd  # still keeps a *failed* window open
    assert "LESS=" not in cy._tmux_window_command(["git", "diff"])  # no env → no prefix


def test_tmux_window_command_closes_on_intentional_stop(cy):
    # A real failure holds the window open, but an intentional stop must not —
    # `docker stop` → exit 143 (SIGTERM), Ctrl-C → 130 (SIGINT). Otherwise every
    # stopped session leaves a stale window that a later resume duplicates.
    cmd = cy._tmux_window_command(["docker", "run", "x"])
    assert "[ $ec -ne 0 ]" in cmd  # genuine launch failure (name conflict, …) holds
    assert "-ne 130" in cmd and "-ne 143" in cmd  # SIGINT / SIGTERM close the window


def test_action_yolo_error_lands_in_footer(cy, monkeypatch):
    def boom(*a, **k):
        raise cy.YoloError("nope")

    monkeypatch.setattr(cy, "rebase_worktree", boom)
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["r", "q"])
    assert frames[-1][1] == "nope"  # the loop survived and showed the error


def test_add_project_prompts_and_registers(cy, monkeypatch, tmp_path):
    # `a` prompts for a path and a name (defaulting to the dir basename).
    registered = []
    monkeypatch.setattr(
        cy, "register_project", lambda home, key, name=None: registered.append((key, name)) or "ok"
    )
    d = tmp_path / "newproj"
    d.mkdir()
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["a"], lines=[str(d), ""])
    assert registered == [(str(d.resolve()), "newproj")]


def test_add_project_on_recent_registers_selection(cy, monkeypatch):
    # `a` on a selected recent-only project registers *that* one directly (no prompt).
    registered = []
    monkeypatch.setattr(
        cy, "register_project", lambda home, key, name=None: registered.append(key) or "ok"
    )
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, path="/work/seen", registered=False)],
    }
    run_loop(cy, monkeypatch, sections, ["a", "q"])
    assert registered == ["/work/seen"]


def test_add_project_on_registered_still_prompts(cy, monkeypatch, tmp_path):
    # `a` on an already-registered project falls back to the prompt (add another).
    registered = []
    monkeypatch.setattr(
        cy, "register_project", lambda home, key, name=None: registered.append((key, name)) or "ok"
    )
    d = tmp_path / "other"
    d.mkdir()
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, path="/work/reg", registered=True)],
    }
    run_loop(cy, monkeypatch, sections, ["a", "q"], lines=[str(d), "mylabel"])
    assert registered == [(str(d.resolve()), "mylabel")]


# --- rebuild-image (the `B` key) --------------------------------------------


def test_draw_wip_advertises_rebuild_image_key(cy, capsys):
    empty = {"session": [], "worktree": [], "project": []}
    cy._draw_wip(empty, None, "")
    assert "B rebuild-image" in capsys.readouterr().out


def test_B_spawns_rebuild_window(cy, monkeypatch):
    # `B` (global — works with no selected row) spawns `yolo wip --rebuild-image`
    # in a dedicated window, no confirm (the build burns only time, in a killable
    # window). No confirms are scripted, so a prompt would decline and not spawn.
    monkeypatch.setattr(cy, "_self_invocation", lambda: "yolo")
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_window", lambda cwd, cmd, name, sess, **k: spawned.append((cmd, name))
    )
    sections = {"session": [], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["B", "q"])
    ((cmd, name),) = spawned
    assert cmd == ["yolo", "wip", "--rebuild-image"]
    assert name == "yolo-rebuild-image"
    assert "rebuilding the default image" in frames[-1][1]


def test_do_rebuild_image_builds_default_no_cache(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cy, "build_docker_image", lambda text, tag, uid, **k: calls.append((text, tag, uid, k))
    )
    cy.do_rebuild_image()
    ((text, tag, uid, kw),) = calls
    assert text == cy.DEFAULT_DOCKERFILE
    assert tag == cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)  # content-addressed, unchanged
    assert kw == {"no_cache": True}


def test_wip_rebuild_image_then_opens_dashboard(cy, run_cli, monkeypatch, tmp_path):
    # `yolo wip --rebuild-image` rebuilds the default image and then chains into
    # the dashboard bootstrap (do_wip) — a hand-typed run lands in the dashboard,
    # and the `B`-spawned window refocuses it when the build finishes.
    monkeypatch.setattr(cy.shutil, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None)
    called = []
    monkeypatch.setattr(cy, "do_rebuild_image", lambda: called.append("rebuild"))
    monkeypatch.setattr(
        cy, "do_wip", lambda home, *, dashboard, tmux_session: called.append(("wip", dashboard))
    )
    home = tmp_path / "home"
    home.mkdir()
    run_cli(["wip", "--rebuild-image"], home=home, cwd=tmp_path)
    assert called == ["rebuild", ("wip", False)]


def test_wip_rebuild_image_skips_dashboard_without_tmux(cy, run_cli, monkeypatch, tmp_path):
    # On a tmux-less box the rebuild still stands alone: build, then return —
    # never do_wip (whose bootstrap would sys.exit asking for tmux after a
    # perfectly good build).
    monkeypatch.setattr(cy.shutil, "which", lambda n: None)
    called = []
    monkeypatch.setattr(cy, "do_rebuild_image", lambda: called.append("rebuild"))
    monkeypatch.setattr(cy, "do_wip", lambda *a, **k: called.append("wip"))
    home = tmp_path / "home"
    home.mkdir()
    run_cli(["wip", "--rebuild-image"], home=home, cwd=tmp_path)
    assert called == ["rebuild"]


# --- bootstrap / dashboard role ---------------------------------------------


def test_do_wip_bootstrap_focuses_dashboard(cy, tmp_path, monkeypatch):
    from test_tmux import FakeTmux  # reuse the in-memory tmux server

    fake = FakeTmux()
    monkeypatch.setattr(cy, "_tmux", fake)
    monkeypatch.setattr(cy.shutil, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None)
    focused = []
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda s, w: focused.append((s, w)))
    cy.do_wip(tmp_path, dashboard=False, tmux_session="yolo")
    assert fake.named("new-session")  # session was created (seeded the dashboard)
    assert focused and focused[0][0] == "yolo"  # and we focused its window


def test_do_wip_respawns_missing_dashboard_window(cy, tmp_path, monkeypatch):
    # A live tmux session whose dashboard window is gone (crashed, or killed by
    # hand): a user-typed `yolo wip` respawns it instead of dead-ending.
    from test_tmux import FakeTmux

    fake = FakeTmux()
    fake.has_session = True
    fake.windows = [("@1", "repo-topic")]  # a session window, but no yolo-wip
    monkeypatch.setattr(cy, "_tmux", fake)
    monkeypatch.setattr(cy.shutil, "which", lambda n: "/usr/bin/tmux" if n == "tmux" else None)
    monkeypatch.setattr(cy, "_self_invocation", lambda: "yolo")
    focused = []
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda s, w: focused.append((s, w)))
    cy.do_wip(tmp_path, dashboard=False, tmux_session="yolo")
    assert not fake.named("new-session")  # existing session reused
    respawned = [w for w in fake.windows if w[1] == cy.TMUX_DASHBOARD_WINDOW]
    assert len(respawned) == 1
    assert focused == [("yolo", respawned[0][0])]


def test_do_wip_without_tmux_exits(cy, tmp_path, monkeypatch):
    monkeypatch.setattr(cy.shutil, "which", lambda n: None)
    with pytest.raises(SystemExit, match="needs tmux"):
        cy.do_wip(tmp_path, dashboard=False, tmux_session="yolo")


def test_do_wip_dashboard_without_tty_falls_back_to_passive(cy, tmp_path, monkeypatch):
    monkeypatch.setattr(cy.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    called = []
    monkeypatch.setattr(cy, "_ps_watch_passive", lambda home: called.append(home))
    cy.do_wip(tmp_path, dashboard=True, tmux_session="yolo")
    assert called == [tmp_path]


def test_worktree_config_reads_global_base(cy, repo):
    # The dashboard re-resolves each worktree's base/finish-action/finish-remote
    # from config; a freshly-edited global ~/.yolo.json takes effect (no restart).
    r, home = repo
    assert cy._worktree_config(home, r, r / "wt") == ("HEAD", "delete-if-merged", "origin", None)
    (home / ".yolo.json").write_text('{"base": "main", "finish-action": "push"}')
    base, action, remote, _ = cy._worktree_config(home, r, r / "wt")
    assert base == "main" and action == "push" and remote == "origin"


def test_worktree_config_uses_the_worktrees_own_repo_entry(cy, repo):
    # The base comes from the worktree's *own* repo entry (keyed by main_root),
    # overriding global — so each cross-repo worktree uses its own configured base.
    r, home = repo
    (home / ".yolo.json").write_text('{"base": "main"}')
    (home / ".claude-yolo").mkdir(parents=True, exist_ok=True)
    (home / ".claude-yolo" / "projects.json").write_text(json.dumps({str(r): {"base": "develop"}}))
    assert cy._worktree_config(home, r, r / "wt")[0] == "develop"


def test_worktree_config_none_is_defaults(cy):
    assert cy._worktree_config(None, None, None) == ("HEAD", "delete-if-merged", "origin", None)


def test_complete_path_single_match_fills_full_path(cy, tmp_path):
    (tmp_path / "alpha").mkdir()
    assert cy._complete_path(f"{tmp_path}/al") == (f"{tmp_path / 'alpha'}/", [])


def test_complete_path_common_prefix_and_options(cy, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alps").mkdir()
    (tmp_path / "afile").write_text("x")  # a plain file is not offered
    new, options = cy._complete_path(f"{tmp_path}/al")
    assert new == f"{tmp_path}/alp"  # extended to the longest common prefix
    assert options == ["alpha/", "alps/"]  # basenames of the dir candidates


def test_complete_path_expands_tilde(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "proj").mkdir()
    assert cy._complete_path("~/p") == (f"{tmp_path / 'proj'}/", [])  # ~ like a shell


def test_complete_path_no_match_is_unchanged(cy, tmp_path):
    assert cy._complete_path(f"{tmp_path}/zzz") == (f"{tmp_path}/zzz", [])


def test_wip_items_appends_new_session_row(cy, repo, monkeypatch):
    # The PROJECTS section always ends with a `+` row (open a session in any dir).
    r, home = repo
    monkeypatch.setattr(cy, "_wip_sessions", lambda h: [])
    monkeypatch.setattr(cy, "_all_tmux_windows", lambda: {})
    projects = cy._wip_items(home)["project"]
    assert projects[-1].kind == "newsession" and projects[-1].cols == ("+", "")


def test_color_project_row_blue_repo_grey_dir_uniform(cy):
    # Every project row is colored the same — REPO blue, DIRECTORY grey (no
    # active/recent distinction); registered vs recent look identical.
    reg = cy._color_project_row(project_item(cy, cols=("r", "~/r"), registered=True))
    rec = cy._color_project_row(project_item(cy, cols=("r", "~/r"), registered=False))
    assert reg == rec
    assert reg == (f"\x1b[{cy._BLUE}mr\x1b[0m", f"\x1b[{cy._GREY}m~/r\x1b[0m")


def test_wip_items_project_cols_are_repo_and_dir(cy, repo, monkeypatch):
    # A project row is (REPO basename, ~-relative DIRECTORY) — the WORKTREES format.
    r, home = repo
    proj = home / "myproj"
    proj.mkdir()
    (home / ".claude-yolo").mkdir(parents=True, exist_ok=True)
    (home / ".claude-yolo" / "projects.json").write_text(json.dumps({str(proj): {}}))
    monkeypatch.setattr(cy, "_wip_sessions", lambda h: [])
    monkeypatch.setattr(cy, "_all_tmux_windows", lambda: {})
    item = next(it for it in cy._wip_items(home)["project"] if it.kind == "project")
    assert item.cols == ("myproj", "~/myproj")


def test_dashboard_project_launch_uses_per_project_config(cy, run_cli, repo, tmp_path):
    # Closing the loop on the dashboard's launch path: Enter on a project spawns
    # `yolo resume --no-tmux` and `n` spawns `yolo start <topic> --no-tmux`, both
    # with the window cwd set to the project dir (_spawn_session_window). Run those
    # exact argv from the project dir and assert that dir's projects.json config (a
    # mount) reaches the docker run argv — i.e. per-project, resolved by the fresh
    # inner yolo, not the dashboard's own / a global default.
    r, home = repo
    ref = tmp_path / "refdocs"
    ref.mkdir()
    (home / ".claude-yolo").mkdir(parents=True, exist_ok=True)
    (home / ".claude-yolo" / "projects.json").write_text(
        json.dumps({str(r): {"mounts": [str(ref)]}})
    )
    for tail in (["resume", "--no-tmux"], ["start", "wt", "--no-tmux"]):
        argv = run_cli(tail, home=home, cwd=r)
        mounts = [argv[i + 1] for i, t in enumerate(argv) if t == "-v"]
        assert f"{ref}:{ref}:ro" in mounts, tail  # the project's mount reached docker run
        cargs = argv[
            next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":")) :
        ]
        assert cargs[cargs.index("--add-dir") + 1] == str(ref)  # …and forwarded to claude


# --- startup log (`l`) ------------------------------------------------------


def test_l_opens_startup_log_window(cy, monkeypatch, tmp_path):
    # `l` on a session row opens the captured startup log in a `less -R` window.
    log = tmp_path / "repo-topic" / "startup.log"
    log.parent.mkdir()
    log.write_text("build output\n")
    monkeypatch.setattr(cy, "_run_dir", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_window", lambda cwd, cmd, name, sess, **k: spawned.append((cmd, name))
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["l"])
    ((cmd, name),) = spawned
    assert cmd == ["less", "-R", str(log)]
    assert name == "log-repo-topic"
    assert "showing startup log for repo-topic" in frames[-1][1]


def test_l_without_log_reports_in_footer(cy, monkeypatch, tmp_path):
    # No startup.log (session predates the feature, or wasn't yolo-spawned):
    # footer message, no window.
    monkeypatch.setattr(cy, "_run_dir", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(cy, "_spawn_window", lambda *a, **k: spawned.append(a))
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["l"])
    assert spawned == []
    assert "no startup log for repo-topic" in frames[-1][1]


def test_l_on_worktree_is_noop(cy, monkeypatch, tmp_path):
    # `l` is a session-only action — a worktree row does nothing.
    monkeypatch.setattr(cy, "_run_dir", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(cy, "_spawn_window", lambda *a, **k: spawned.append(a))
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    run_loop(cy, monkeypatch, sections, ["l", "q"])
    assert spawned == []
