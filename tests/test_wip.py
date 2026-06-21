"""Tests for the `wip` dashboard (the tmux-resident management dashboard).

The data layer (`_wip_sessions`/`_order_sessions`/`_wip_items`) is tested against
a real throwaway git repo with stubbed docker; the interactive loop (`_wip_loop`)
is driven by a scripted FakeTerm with `_wip_items`/`_draw_wip` and the action cores
stubbed, mirroring how test_tmux drives the ps picker. The seeded-dashboard window
itself is covered in test_tmux (the `wip --_dashboard` command).
"""

import json
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


class FakeTerm:
    """A scripted picker terminal: canned keys, prompt lines, and confirmations."""

    def __init__(self, keys, *, lines=None, confirms=None):
        self._keys = list(keys)
        self._lines = list(lines or [])
        self._confirms = list(confirms or [])

    def wait_key(self, timeout):
        return self._keys.pop(0) if self._keys else "q"  # default-quit ends the loop

    def prompt_line(self, prompt):
        return self._lines.pop(0) if self._lines else ""

    prompt_path = prompt_line  # same scripted-line source; completion is the real term's job

    def confirm(self, prompt):
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
    cols = over.pop("cols", ("repo-topic", "topic", "waiting 5m", "-", "1m"))
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
        over.pop("cols", ("/p",)),
        {
            "path": over.pop("path", "/p"),
            "registered": over.pop("registered", True),
            "window": over.pop("window", None),
        },
    )


def run_loop(cy, monkeypatch, sections, keys, *, lines=None, confirms=None):
    """Drive _wip_loop with fixed sections + scripted term; return the draw frames.

    Each frame is (selected_key, footer) as _draw_wip would have rendered it.
    """
    monkeypatch.setattr(cy, "_wip_items", lambda home: sections)
    frames = []
    monkeypatch.setattr(cy, "_draw_wip", lambda secs, sel, foot: frames.append((sel, foot)))
    term = FakeTerm(keys, lines=lines, confirms=confirms)
    # home=None → per-worktree _worktree_config returns built-in defaults, so the
    # loop runs without touching real config.
    cy._wip_loop(None, "yolo", term)
    return frames


# --- data layer -------------------------------------------------------------


def test_order_sessions_unknown_then_waiting_then_working(cy):
    def mk(name, state, age, created_at=""):
        return cy.WipSession("c", name, "", "/c", "", "", "1m", state, age, created_at)

    ordered = cy._order_sessions(
        [
            mk("w-short", "working", 5),
            mk("idle-short", "waiting", 5),
            mk("unknown-new", None, 0, "2026-06-20 10:00:00 +0000 UTC"),
            mk("unknown-old", None, 0, "2026-06-20 09:00:00 +0000 UTC"),
            mk("idle-long", "waiting", 99),
            mk("w-long", "working", 50),
        ]
    )
    names = [s.name for s in ordered]
    # unknown (oldest-created first), then waiting (longest first), then working
    assert names == [
        "unknown-old",
        "unknown-new",
        "idle-long",
        "idle-short",
        "w-long",
        "w-short",
    ]


def test_draw_wip_renders_one_sessions_table_grouped(cy, capsys):
    # One combined SESSIONS table (no separate WAITING/WORKING/OTHER headers), with
    # the rows in the order given (unknown → waiting → working from _order_sessions).
    sections = {
        "session": [
            session_item(
                cy, key="session:s1", payload={"state": None}, cols=("s1", "-", "1m", "-", "-")
            ),
            session_item(
                cy,
                key="session:w1",
                payload={"state": "waiting"},
                cols=("w1", "-", "1m", "-", "waiting 9m"),
            ),
            session_item(
                cy,
                key="session:k1",
                payload={"state": "working"},
                cols=("k1", "-", "1m", "-", "working 2m"),
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
    # SESSION/STATE color (green waiting, yellow working) instead.
    sections = {
        "session": [
            session_item(
                cy,
                key="session:w1",
                payload={"state": "waiting"},
                cols=("w1", "-", "1m", "-", "waiting 9m"),
            ),
            session_item(
                cy,
                key="session:k1",
                payload={"state": "working"},
                cols=("k1", "-", "1m", "-", "working 1m"),
            ),
        ],
        "worktree": [],
        "project": [],
    }
    cy._draw_wip(sections, None, "")
    lines = capsys.readouterr().out.splitlines()
    w1 = next(i for i, ln in enumerate(lines) if "w1" in ln)
    k1 = next(i for i, ln in enumerate(lines) if "k1" in ln)
    assert k1 == w1 + 1  # adjacent rows, no blank line between groups
    assert f"\x1b[{cy._GREEN}m" in lines[w1]  # waiting row tinted green
    assert f"\x1b[{cy._YELLOW}m" in lines[k1]  # working row tinted yellow


def test_draw_wip_sessions_none_when_empty(cy, capsys):
    sections = {"session": [], "worktree": [], "project": []}
    cy._draw_wip(sections, None, "")
    out = capsys.readouterr().out
    assert "SESSIONS" in out and "(none)" in out


def test_draw_wip_projects_omits_column_header(cy, capsys):
    # The single-column PROJECTS section drops the redundant "PROJECT" header row,
    # but still lists the projects under the title.
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, key="project:/work/a", cols=("/work/a",))],
    }
    cy._draw_wip(sections, None, "")
    lines = cy._SGR_RE.sub("", capsys.readouterr().out).splitlines()
    assert "PROJECTS" in lines  # the section title
    assert "  PROJECT" not in lines  # ...but not a separate column-header row
    assert any("/work/a" in ln for ln in lines)  # the project still listed


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
            cy.WipSession("cidA", "repo-alpha", "alpha", str(wt_alpha), "", "", "1m", "waiting", 9)
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


def test_wip_projects_flags_active(cy, tmp_path):
    home = tmp_path / "home"
    (home / ".claude-yolo").mkdir(parents=True)
    (home / ".claude-yolo" / "projects.json").write_text('{"/work/a": {}, "/work/b": {}}')
    sessions = [cy.WipSession("c", "n", "", "/work/a/sub", "", "", "1m", "waiting", 1)]
    projects = cy._wip_projects(home, sessions)
    by_path = {str(p["path"]): p["active"] for p in projects}
    assert by_path == {"/work/a": True, "/work/b": False}
    assert all(p["registered"] for p in projects)


def _sess(cy, name, cwd):
    return cy.WipSession("c", name, "", cwd, "", "", "1m", "waiting", 1)


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
    frames = run_loop(cy, monkeypatch, sections, ["down", "down", "q"])
    assert [sel for sel, _ in frames] == [
        "session:repo-topic",
        "worktree:repo:old",
        "project:/p",
    ]


def test_loop_refresh_preserves_selection_by_key(cy, monkeypatch):
    sections = {"session": [session_item(cy)], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["down", None, "q"])
    # after moving to the worktree, a refresh (None) keeps it selected
    assert frames[-1][0] == "worktree:repo:old"


# --- loop: actions ----------------------------------------------------------


def test_enter_session_switches_window(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(cy, "_focus_tmux_window", lambda s, w: calls.append((s, w)))
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["\r", "q"])
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
    frames = run_loop(cy, monkeypatch, sections, ["b", "q"])
    assert "opened http://x/None" in frames[-1][1]


def test_browse_session_prompts_for_multiple_ports(cy, monkeypatch):
    monkeypatch.setattr(cy, "_forwarded_ports", lambda cid: [8000, 3000])
    seen = []
    monkeypatch.setattr(cy, "browse_session", lambda cid, select=None: seen.append(select) or "ok")
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["b", "q"], lines=["3000"])
    assert seen == [3000]


def test_stop_confirms_then_calls_core(cy, monkeypatch):
    stopped = []
    monkeypatch.setattr(
        cy, "stop_session", lambda cid, where, home, *, force: stopped.append((cid, force)) or "ok"
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    # waiting session -> force False
    run_loop(cy, monkeypatch, sections, ["s", "q"], confirms=[True])
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
    run_loop(cy, monkeypatch, sections, ["s", "q"], confirms=[True])
    assert stopped == [True]


def test_stop_cancelled_does_nothing(cy, monkeypatch):
    stopped = []
    monkeypatch.setattr(cy, "stop_session", lambda *a, **k: stopped.append(1) or "ok")
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["s", "q"], confirms=[False])
    assert stopped == []
    assert "cancelled" in frames[-1][1]


def test_finish_worktree_confirms_then_calls_core(cy, monkeypatch):
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


def test_finish_waiting_session_allowed(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cy,
        "finish_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append(topic) or "ok",
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["f", "q"], confirms=[True])
    assert calls == ["topic"]  # the idle session's worktree gets finished


def test_rebase_worktree_calls_core(cy, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cy,
        "rebase_worktree",
        lambda wt, mr, slug, topic, home, base, **k: calls.append((topic, k)) or "rebased",
    )
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["r", "q"])
    assert calls == [("old", {"capture": True})]
    assert frames[-1][1] == "rebased"


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
    assert argv == ["diff", "old", "--base", "HEAD", "--stat"]
    assert name == "diff-old"
    assert frames[-1][1] == "diffing 'old'…"


def test_d_on_worktree_session_spawns_diff_window(cy, monkeypatch):
    # `d` also works on a worktree-backed session row (read-only, so any state).
    spawned = []
    monkeypatch.setattr(
        cy,
        "_spawn_session_window",
        lambda repo, argv, name, sess: spawned.append((repo, argv, name)),
    )
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}  # a worktree session
    run_loop(cy, monkeypatch, sections, ["d", "q"])
    ((repo, argv, name),) = spawned
    assert (
        repo == "/repo"
        and argv == ["diff", "topic", "--base", "HEAD", "--stat"]
        and name == "diff-topic"
    )


def test_d_on_cwd_session_is_noop(cy, monkeypatch):
    # A plain cwd session (no topic / main repo) isn't a worktree, so `d` does nothing.
    spawned = []
    monkeypatch.setattr(cy, "_spawn_session_window", lambda *a: spawned.append(a))
    sess = session_item(cy, payload={"topic": "", "main_root": None})
    sections = {"session": [sess], "worktree": [], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["d", "q"])
    assert spawned == []
    assert "diff applies to worktrees" in frames[-1][1]


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
    assert cmd == ["git", "diff", "BASESHA...HEAD", "--", "c.py"]  # the selected file
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
    cy._draw_diff_stat("topic", "HEAD", DIFF_STAT, 3, 1)  # 3 files, b.py selected
    out = capsys.readouterr().out
    assert "\x1b[7m b.py" in out  # selected file line is a reverse-video bar
    assert "\x1b[90m 3 files changed" in out  # the summary line is dim
    assert " a.py" in out and " c.py" in out  # other files shown plainly


def test_tmux_window_command_env_prefixes_assignments(cy):
    # `env` prepends `KEY=val` to the command (the diff windows pass LESS=R so the
    # pager doesn't auto-quit a one-screen diff); the failure-hold is unchanged.
    cmd = cy._tmux_window_command(["git", "diff", "A...B"], env={"LESS": "R"})
    assert cmd.startswith("LESS=R git diff A...B;")
    assert "-ne 0" in cmd  # still keeps a *failed* window open
    assert "LESS=" not in cy._tmux_window_command(["git", "diff"])  # no env → no prefix


def test_action_yolo_error_lands_in_footer(cy, monkeypatch):
    def boom(*a, **k):
        raise cy.YoloError("nope")

    monkeypatch.setattr(cy, "rebase_worktree", boom)
    sections = {"session": [], "worktree": [worktree_item(cy)], "project": []}
    frames = run_loop(cy, monkeypatch, sections, ["r", "q"])
    assert frames[-1][1] == "nope"  # the loop survived and showed the error


def test_add_project_prompts_and_registers(cy, monkeypatch, tmp_path):
    registered = []
    monkeypatch.setattr(cy, "register_project", lambda home, key: registered.append(key) or "ok")
    d = tmp_path / "newproj"
    d.mkdir()
    sections = {"session": [session_item(cy)], "worktree": [], "project": []}
    run_loop(cy, monkeypatch, sections, ["a", "q"], lines=[str(d)])
    assert registered == [str(d.resolve())]


def test_add_project_on_recent_registers_selection(cy, monkeypatch):
    # `a` on a selected recent-only project registers *that* one directly (no prompt).
    registered = []
    monkeypatch.setattr(cy, "register_project", lambda home, key: registered.append(key) or "ok")
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
    monkeypatch.setattr(cy, "register_project", lambda home, key: registered.append(key) or "ok")
    d = tmp_path / "other"
    d.mkdir()
    sections = {
        "session": [],
        "worktree": [],
        "project": [project_item(cy, path="/work/reg", registered=True)],
    }
    run_loop(cy, monkeypatch, sections, ["a", "q"], lines=[str(d)])
    assert registered == [str(d.resolve())]


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
    assert cy._worktree_config(home, r, r / "wt") == ("HEAD", "delete-if-merged", "origin")
    (home / ".yolo.json").write_text('{"base": "main", "finish-action": "push"}')
    base, action, remote = cy._worktree_config(home, r, r / "wt")
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
    assert cy._worktree_config(None, None, None) == ("HEAD", "delete-if-merged", "origin")


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
    assert projects[-1].kind == "newsession" and projects[-1].cols == ("+",)


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
