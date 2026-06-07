"""Tests for the worktree verbs: start / resume / shell / finish / list.

Unlike test_cli.py (which stubs everything), these exercise the real git
worktree machinery against a throwaway repo, and stub only `running_container_for`
(docker) plus the launch side effects via the `run_cli` fixture.
"""

import subprocess

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
    """Default: no container is running for any topic (override per-test)."""
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic: None)


def worktree_label(argv):
    for i, tok in enumerate(argv):
        if tok == "--label" and argv[i + 1].startswith("yolo.worktree="):
            return argv[i + 1].split("=", 1)[1]
    return None


def claude_command(cy, argv):
    return argv[argv.index(cy.DOCKER_IMAGE) + 1 :]


# --- start ------------------------------------------------------------------


def test_start_creates_worktree_branch_and_names_session(cy, run_cli, repo):
    r, home = repo
    argv = run_cli(["start", "auth-fix"], home=home, cwd=r)
    # worktree + branch created
    wt = home / ".claude-yolo" / "worktrees"
    created = list(wt.rglob("auth-fix"))
    assert created and created[0].is_dir()
    assert "auth-fix" in git(r, "branch", "--list", "auth-fix").stdout
    # labelled and named
    assert worktree_label(argv) == "auth-fix"
    assert claude_command(cy, argv)[:2] == ["--name", "auth-fix"]


def test_start_errors_if_topic_exists(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "dup"], home=home, cwd=r)
    with pytest.raises(SystemExit):
        run_cli(["start", "dup"], home=home, cwd=r)


def test_start_base_ref_is_used(cy, run_cli, repo):
    r, home = repo
    # second commit; branch a topic off the first commit via --base
    first = git(r, "rev-parse", "HEAD").stdout.strip()
    (r / "x").write_text("x")
    git(r, "add", ".")
    git(r, "commit", "-qm", "second")
    run_cli(["start", "topic", "--base", first], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    assert git(wt, "rev-parse", "HEAD").stdout.strip() == first


# --- resume -----------------------------------------------------------------


def test_resume_errors_without_worktree(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["resume", "ghost"], home=home, cwd=r)


def test_resume_defaults_to_continue(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["resume", "topic"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--continue" in cmd
    assert "--name" not in cmd


def test_resume_new_starts_named_fresh_session(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["resume", "topic", "--new"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert cmd[:2] == ["--name", "topic"]
    assert "--continue" not in cmd


def test_resume_with_session_id(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["resume", "topic", "-r", "SID"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--resume" in cmd and "SID" in cmd


# --- shell ------------------------------------------------------------------


def test_shell_execs_into_running_container(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic: "cid123456789")
    argv = run_cli(["shell", "topic"], home=home, cwd=r)
    assert argv == ["docker", "exec", "-it", "cid123456789", "/bin/bash"]


def test_shell_fresh_container_has_bash_entrypoint(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["shell", "topic"], home=home, cwd=r)  # no running container
    assert "--entrypoint" in argv
    assert argv[argv.index("--entrypoint") + 1] == "/bin/bash"
    assert worktree_label(argv) == "topic"


def test_shell_errors_without_worktree(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["shell", "ghost"], home=home, cwd=r)


# --- finish -----------------------------------------------------------------


def test_finish_removes_worktree_keeps_branch(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    assert wt.is_dir()
    run_cli(["finish", "topic"], home=home, cwd=r)
    assert not wt.exists()  # worktree gone
    assert "topic" in git(r, "branch", "--list", "topic").stdout  # branch kept


def test_finish_refuses_dirty_without_force(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "scratch.txt").write_text("uncommitted")
    with pytest.raises(SystemExit):
        run_cli(["finish", "topic"], home=home, cwd=r)
    assert wt.is_dir()  # still there
    run_cli(["finish", "topic", "--force"], home=home, cwd=r)
    assert not wt.exists()  # --force removes it


def test_finish_refuses_when_container_running(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic: "cid")
    with pytest.raises(SystemExit):
        run_cli(["finish", "topic"], home=home, cwd=r)


# --- list -------------------------------------------------------------------


def test_list_shows_worktrees(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    run_cli(["start", "beta"], home=home, cwd=r)
    capsys.readouterr()  # clear
    run_cli(["list"], home=home, cwd=r)
    lines = capsys.readouterr().out.splitlines()
    # header row, then one row per topic, each showing its worktree directory
    assert lines[0].split() == ["TOPIC", "BRANCH", "STATUS", "DIRECTORY"]
    body = "\n".join(lines[1:])
    assert "alpha" in body and "beta" in body
    assert "~/.claude-yolo/worktrees" in body  # the worktree directory column


def test_list_empty(cy, run_cli, repo, capsys):
    r, home = repo
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert "No worktrees" in capsys.readouterr().out


def _status_for(out, topic):
    """The STATUS column value for `topic` from `list` output."""
    for line in out.splitlines()[1:]:  # skip header
        cols = line.split()
        if cols and cols[0] == topic:
            return cols[2]  # TOPIC BRANCH STATUS DIRECTORY
    raise AssertionError(f"{topic} not in list output")


def test_list_marks_merged_branch(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "done"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("done"))
    # commit on the branch, then merge it into main
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    git(r, "merge", "--no-ff", "-m", "merge done", "done")
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "done") == "merged"


def test_list_unmerged_branch_is_dash(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "wip"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("wip"))
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "unmerged work")  # committed but NOT merged
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "wip") == "-"


def test_list_fresh_branch_not_marked_merged(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "fresh"], home=home, cwd=r)  # no commits, tip == main
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "fresh") == "-"


# --- dispatch guards --------------------------------------------------------


def test_verb_requires_topic(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["start"], home=home, cwd=r)


def test_new_only_with_resume(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["start", "topic", "--new"], home=home, cwd=r)
