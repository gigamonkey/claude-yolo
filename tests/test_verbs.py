"""Tests for the worktree verbs: start / resume / shell / finish / list.

Unlike test_cli.py (which stubs everything), these exercise the real git
worktree machinery against a throwaway repo, and stub only `running_container_for`
(docker) plus the launch side effects via the `run_cli` fixture.
"""

import subprocess
import time

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
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: None)


def worktree_label(argv):
    for i, tok in enumerate(argv):
        if tok == "--label" and argv[i + 1].startswith("yolo.worktree="):
            return argv[i + 1].split("=", 1)[1]
    return None


def claude_command(cy, argv):
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    return argv[i + 1 :]


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
    cmd = claude_command(cy, argv)
    assert cmd[cmd.index("--name") + 1] == "auth-fix"


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
    assert cmd[cmd.index("--name") + 1] == "topic"
    assert "--continue" not in cmd


def test_resume_with_session_id(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["resume", "topic", "-r", "SID"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--resume" in cmd and "SID" in cmd


def test_resume_refuses_when_worktree_session_running(cy, run_cli, repo, monkeypatch):
    # Can't resume a worktree whose session is already running (non-tmux): refuse
    # up front rather than build + hit docker's name conflict.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    with pytest.raises(SystemExit) as exc:
        run_cli(["resume", "topic"], home=home, cwd=r)
    msg = str(exc.value)
    assert "already running" in msg and "topic" in msg


def _stop_capture(cy, monkeypatch):
    """Intercept `docker stop`/`docker inspect` (pass git etc. through); return the
    captured `docker stop` calls."""
    real_run = cy.subprocess.run
    stops = []

    def fake_run(cmd, **k):
        if cmd[:2] == ["docker", "stop"]:
            stops.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")  # no labels → state '-'
        return real_run(cmd, **k)

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    return stops


def test_stop_stops_worktree_session(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(
        cy, "running_container_for", lambda slug, topic=None, cwd=None: "cidabc123456"
    )
    stops = _stop_capture(cy, monkeypatch)
    run_cli(["stop", "topic"], home=home, cwd=r)
    assert stops == [["docker", "stop", "cidabc123456"]]


def test_stop_no_running_session_is_noop(cy, run_cli, repo, monkeypatch, capsys):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: None)
    stops = _stop_capture(cy, monkeypatch)
    run_cli(["stop", "topic"], home=home, cwd=r)
    assert stops == []  # nothing stopped
    assert "No running yolo session" in capsys.readouterr().out


def test_stop_refuses_working_session_without_force(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(
        cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid123456789"
    )
    monkeypatch.setattr(cy, "_read_session_state", lambda path, now: "working 3s")
    stops = _stop_capture(cy, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        run_cli(["stop", "topic"], home=home, cwd=r)
    assert "active" in str(exc.value)
    assert stops == []  # not stopped


def test_stop_force_stops_working_session(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(
        cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid123456789"
    )
    monkeypatch.setattr(cy, "_read_session_state", lambda path, now: "working 3s")
    stops = _stop_capture(cy, monkeypatch)
    run_cli(["stop", "topic", "--force"], home=home, cwd=r)
    assert stops == [["docker", "stop", "cid123456789"]]


# --- shell ------------------------------------------------------------------


def test_shell_execs_into_running_container(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(
        cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid123456789"
    )
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


def test_worktree_launch_exports_ps1_rewrite_env(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["shell", "topic"], home=home, cwd=r)  # fresh container
    envs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-e"]
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    assert f"YOLO_WT_DIR={wt}" in envs
    # one slug under the worktrees root, so the label collapses to the topic
    assert "YOLO_WT_LABEL=topic" in envs
    ps1 = next(e for e in envs if e.startswith("YOLO_PS1="))
    assert "${PWD/#$YOLO_WT_DIR/$YOLO_WT_LABEL}" in ps1


# --- finish -----------------------------------------------------------------


def test_finish_keeps_unmerged_branch(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    # a commit on the topic branch makes it diverge from base (HEAD) -> unmerged
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    run_cli(["finish", "topic"], home=home, cwd=r)
    assert not wt.exists()  # worktree gone
    assert "topic" in git(r, "branch", "--list", "topic").stdout  # branch kept


def test_finish_deletes_merged_branch(cy, run_cli, repo):
    r, home = repo
    # a fresh topic never diverged from base (HEAD), so it reads as merged
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    assert wt.is_dir()
    run_cli(["finish", "topic"], home=home, cwd=r)
    assert not wt.exists()  # worktree gone
    assert "topic" not in git(r, "branch", "--list", "topic").stdout  # branch deleted


def test_finish_removes_worktree_with_submodule(cy, run_cli, repo, tmp_path):
    """git refuses to `worktree remove` a tree with submodules; finish falls back."""
    r, home = repo
    # a tiny repo to embed as a submodule
    sub = _make_repo(tmp_path, "sub-origin")
    git(
        r,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(sub),
        "sub",
    )
    git(r, "commit", "-qm", "add submodule")
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    # populate the submodule in the worktree — git only refuses removal once it's
    # actually checked out (an empty gitlink dir removes fine)
    git(
        wt,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
    )
    assert (wt / "sub" / "README").exists()
    # sanity: plain `git worktree remove` would die on the submodule
    bare = subprocess.run(
        ["git", "-C", str(r), "worktree", "remove", str(wt)],
        capture_output=True,
        text=True,
    )
    assert bare.returncode != 0 and "submodule" in bare.stderr.lower()
    # finish must still clear it (via the rmtree + prune fallback)
    run_cli(["finish", "topic"], home=home, cwd=r)
    assert not wt.exists()
    assert "topic" not in git(r, "branch", "--list", "topic").stdout  # merged -> deleted
    # no stale admin entry left behind
    assert "topic" not in git(r, "worktree", "list").stdout


# --- submodule population (--submodules) ------------------------------------


def _add_submodule(repo_dir, tmp_path, home):
    """Embed a tiny file-protocol submodule at `sub` in `repo_dir` and commit it."""
    sub = _make_repo(tmp_path, "sub-origin")
    git(repo_dir, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "sub")
    git(repo_dir, "commit", "-qm", "add submodule")
    # `_init_submodules` runs git under HOME=home (set by run_cli), and the clone
    # subprocess submodule-update spawns reads protocol.file.allow from there, not
    # the superproject config. Allow file:// for the test (real submodules are
    # https/ssh, so this is test-only plumbing).
    (home / ".gitconfig").write_text('[protocol "file"]\n\tallow = always\n')


def test_start_populates_submodules_when_enabled(cy, run_cli, repo, tmp_path):
    r, home = repo
    _add_submodule(r, tmp_path, home)
    run_cli(["start", "topic", "--submodules"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    assert (wt / "sub" / "README").exists()  # populated host-side before launch


def test_start_skips_submodules_by_default(cy, run_cli, repo, tmp_path):
    r, home = repo
    _add_submodule(r, tmp_path, home)
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    assert not (wt / "sub" / "README").exists()  # gitlink dir left empty


def test_submodules_noop_without_gitmodules(cy, run_cli, repo):
    # A plain repo (no .gitmodules) with --submodules must launch fine, not error.
    r, home = repo
    argv = run_cli(["start", "topic", "--submodules"], home=home, cwd=r)
    assert argv is not None  # reached the docker-run assembly


# --- finish (continued) -----------------------------------------------------


def test_finish_keep_action_keeps_merged_branch(cy, run_cli, repo):
    r, home = repo
    # a fresh topic reads as merged; `keep` must leave it alone anyway
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    run_cli(["finish", "topic", "--finish-action", "keep"], home=home, cwd=r)
    assert not wt.exists()
    assert "topic" in git(r, "branch", "--list", "topic").stdout  # kept despite merged


def test_finish_merge_action_merges_and_deletes(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    run_cli(["finish", "topic", "--finish-action", "merge"], home=home, cwd=r)
    assert not wt.exists()
    assert "topic" not in git(r, "branch", "--list", "topic").stdout  # deleted
    assert (r / "work.txt").exists()  # merged into main's working tree


def test_finish_merge_conflict_keeps_branch(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    # both branches change README differently -> merge conflict
    (wt / "README").write_text("from topic\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "topic edit")
    (r / "README").write_text("from main\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main edit")
    run_cli(["finish", "topic", "--finish-action", "merge"], home=home, cwd=r)
    assert not wt.exists()
    assert "topic" in git(r, "branch", "--list", "topic").stdout  # kept on failure
    # the aborted merge left main clean
    assert git(r, "status", "--porcelain").stdout.strip() == ""


def test_finish_push_action_pushes_and_keeps(cy, run_cli, repo, tmp_path):
    r, home = repo
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(r, "remote", "add", "upstream", str(remote))
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    run_cli(
        ["finish", "topic", "--finish-action", "push", "--finish-remote", "upstream"],
        home=home,
        cwd=r,
    )
    assert not wt.exists()
    assert "topic" in git(r, "branch", "--list", "topic").stdout  # kept locally
    assert "refs/heads/topic" in git(r, "ls-remote", "upstream").stdout  # on the remote
    # -u set up tracking, so a later bare push/pull on the branch just works
    assert (
        git(r, "rev-parse", "--abbrev-ref", "topic@{upstream}").stdout.strip() == "upstream/topic"
    )


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


def _fake_docker_session(cy, monkeypatch, home, worktree):
    """Stub subprocess so a verb's `docker stop`/`docker inspect` don't hit docker.

    Shared by the finish and rebase tests. `docker stop` records the call and
    succeeds; `docker inspect` returns the container's yolo.config-dir / yolo.cwd
    labels so `_container_session_state` reads the state file `_write_session_state`
    writes (default config dir, keyed by the worktree path). Everything else (git)
    runs for real. Returns the list that captures the `docker stop` argv (empty for
    rebase, which never stops the container).
    """
    real_run = cy.subprocess.run
    stops = []

    def fake_run(cmd, **k):
        if cmd[:2] == ["docker", "stop"]:
            stops.append(cmd)
            return cy.subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "inspect"]:
            fmt = cmd[3]
            if "yolo.config-dir" in fmt:
                out = str(home / ".claude")
            elif "yolo.cwd" in fmt:
                out = str(worktree)
            else:
                out = ""
            return cy.subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        return real_run(cmd, **k)

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    return stops


def test_finish_stops_idle_session_and_finishes(cy, run_cli, repo, monkeypatch):
    # A running but idle (waiting) session is stopped as `yolo stop` would, then
    # finish proceeds — closing the session and removing the worktree in one step.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "waiting")
    stops = _fake_docker_session(cy, monkeypatch, home, wt)
    run_cli(["finish", "topic"], home=home, cwd=r)
    assert stops == [["docker", "stop", "cid"]]  # the idle session was stopped
    assert not wt.exists()  # worktree removed


def test_finish_refuses_when_session_working(cy, run_cli, repo, monkeypatch):
    # An actively working session is not cut off: finish refuses without --force.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "working")
    stops = _fake_docker_session(cy, monkeypatch, home, wt)
    with pytest.raises(SystemExit):
        run_cli(["finish", "topic"], home=home, cwd=r)
    assert stops == []  # nothing stopped
    assert wt.exists()  # worktree left intact


def test_finish_force_stops_active_session(cy, run_cli, repo, monkeypatch):
    # --force stops even an actively working session, then finishes.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "working")
    stops = _fake_docker_session(cy, monkeypatch, home, wt)
    run_cli(["finish", "topic", "--force"], home=home, cwd=r)
    assert stops == [["docker", "stop", "cid"]]
    assert not wt.exists()


# --- rebase -----------------------------------------------------------------


def test_rebase_replays_branch_onto_base(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    # a commit on the topic branch
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    topic_before = git(wt, "rev-parse", "HEAD").stdout.strip()
    # a commit lands on main after the worktree branched
    (r / "main.txt").write_text("mainwork")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main work")
    main_head = git(r, "rev-parse", "HEAD").stdout.strip()
    run_cli(["rebase", "topic"], home=home, cwd=r)
    # the topic now sits on top of main's new commit (parent == main HEAD) and
    # was rewritten (a new commit hash)
    assert git(wt, "rev-parse", "HEAD~1").stdout.strip() == main_head
    assert git(wt, "rev-parse", "HEAD").stdout.strip() != topic_before
    assert (wt / "main.txt").exists()  # main's work is now in the worktree


def test_rebase_requires_topic(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["rebase"], home=home, cwd=r)


def test_rebase_errors_without_worktree(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["rebase", "ghost"], home=home, cwd=r)


def test_rebase_refuses_dirty_worktree(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "scratch.txt").write_text("uncommitted")
    with pytest.raises(SystemExit):
        run_cli(["rebase", "topic"], home=home, cwd=r)


def _write_session_state(cy, home, worktree, activity):
    """Stamp a hook-style activity state file for `worktree` (default config dir)."""
    sd = home / ".claude" / cy._STATUS_DIR_NAME
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{cy._cwd_slug(worktree)}.state").write_text(f"{activity} {int(time.time())}")


def test_rebase_refuses_when_container_running_and_state_unknown(cy, run_cli, repo, monkeypatch):
    # A running container with no state file (e.g. a `yolo shell`, or a session
    # that hasn't taken a turn): state is "-", so refuse without --force.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _fake_docker_session(cy, monkeypatch, home, wt)  # labels present, but no state file
    with pytest.raises(SystemExit):
        run_cli(["rebase", "topic"], home=home, cwd=r)


def _rebase_setup_with_main_commit(cy, run_cli, repo):
    """start topic, commit on it, commit on main; return (r, home, worktree)."""
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    (r / "main.txt").write_text("mainwork")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main work")
    return r, home, wt


def test_rebase_proceeds_when_session_waiting(cy, run_cli, repo, monkeypatch):
    r, home, wt = _rebase_setup_with_main_commit(cy, run_cli, repo)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "waiting")
    _fake_docker_session(cy, monkeypatch, home, wt)
    run_cli(["rebase", "topic"], home=home, cwd=r)
    assert (wt / "main.txt").exists()  # rebased through an idle running container


def test_rebase_refuses_when_session_working(cy, run_cli, repo, monkeypatch):
    r, home, wt = _rebase_setup_with_main_commit(cy, run_cli, repo)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "working")
    _fake_docker_session(cy, monkeypatch, home, wt)
    with pytest.raises(SystemExit):
        run_cli(["rebase", "topic"], home=home, cwd=r)
    assert not (wt / "main.txt").exists()  # not rebased


def test_rebase_force_overrides_active_session(cy, run_cli, repo, monkeypatch):
    r, home, wt = _rebase_setup_with_main_commit(cy, run_cli, repo)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "working")
    _fake_docker_session(cy, monkeypatch, home, wt)
    run_cli(["rebase", "topic", "--force"], home=home, cwd=r)
    assert (wt / "main.txt").exists()  # --force rebased despite a working session


def test_rebase_reads_state_from_container_config_dir(cy, run_cli, repo, monkeypatch):
    # The session was started under an alternate --config-dir, so its state file
    # lives there, not in ~/.claude. rebase (invoked without --config-dir) must
    # still read the right state via the container's own yolo.config-dir label —
    # otherwise it'd see "-" and refuse an idle session.
    r, home, wt = _rebase_setup_with_main_commit(cy, run_cli, repo)
    altcfg = home / ".claude-work"
    sd = altcfg / cy._STATUS_DIR_NAME
    sd.mkdir(parents=True)
    (sd / f"{cy._cwd_slug(wt)}.state").write_text(f"waiting {int(time.time())}")
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")

    real_run = cy.subprocess.run

    def fake_run(cmd, **k):
        if cmd[:2] == ["docker", "inspect"]:
            fmt = cmd[3]
            out = str(altcfg) if "yolo.config-dir" in fmt else str(wt) if "yolo.cwd" in fmt else ""
            return cy.subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        return real_run(cmd, **k)

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    run_cli(["rebase", "topic"], home=home, cwd=r)
    assert (wt / "main.txt").exists()  # idle session found via the container's label


def test_rebase_honours_base(cy, run_cli, repo):
    r, home = repo
    # branch topic off the first commit, then add a second commit on main
    first = git(r, "rev-parse", "HEAD").stdout.strip()
    (r / "second.txt").write_text("two")
    git(r, "add", ".")
    git(r, "commit", "-qm", "second")
    run_cli(["start", "topic", "--base", first], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    # rebase onto the first commit (an explicit --base): a no-op replay, the
    # branch stays based on `first`, not main's second commit
    run_cli(["rebase", "topic", "--base", first], home=home, cwd=r)
    assert git(wt, "rev-parse", "HEAD~1").stdout.strip() == first
    assert not (wt / "second.txt").exists()


# --- list -------------------------------------------------------------------


def test_list_shows_worktrees(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    run_cli(["start", "beta"], home=home, cwd=r)
    capsys.readouterr()  # clear
    run_cli(["list"], home=home, cwd=r)
    lines = capsys.readouterr().out.splitlines()
    # header row, then one row per topic, each showing its worktree directory
    assert lines[0].split() == ["TOPIC", "STATUS", "DIRECTORY"]
    body = "\n".join(lines[1:])
    assert "alpha" in body and "beta" in body
    assert "~/.claude-yolo/worktrees" in body  # the worktree directory column


def test_list_no_branch_column_when_topic_matches(cy, run_cli, repo, capsys):
    """The TOPIC cell is just the topic when the checked-out branch matches it."""
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert "branch:" not in capsys.readouterr().out


def test_list_shows_branch_when_diverged(cy, run_cli, repo, capsys):
    """A worktree on a *different* branch than its topic surfaces it inline."""
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("alpha"))
    git(wt, "checkout", "-qb", "other")  # someone switched branches in the container
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    out = capsys.readouterr().out
    assert "alpha (branch: other)" in out


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
            return cols[1]  # TOPIC STATUS DIRECTORY
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


def test_list_unmerged_branch(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "wip"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("wip"))
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "unmerged work")  # committed but NOT merged
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "wip") == "unmerged"


def test_list_fast_forward_merge_reads_merged(cy, run_cli, repo, capsys):
    """A fast-forward merge leaves tip == base; it must still read `merged`."""
    r, home = repo
    run_cli(["start", "ff"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("ff"))
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    git(r, "merge", "ff")  # fast-forward: main had not moved, so main tip == ff tip
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "ff") == "merged"


def test_list_fresh_branch_reads_merged(cy, run_cli, repo, capsys):
    # A never-diverged branch (tip == base) is "contained" — git branch --merged
    # reports it merged too, and we match that.
    r, home = repo
    run_cli(["start", "fresh"], home=home, cwd=r)  # no commits, tip == base
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "fresh") == "merged"


def test_list_merged_uses_base_target(cy, run_cli, repo, capsys):
    """`merged` is judged against --base, not a hardcoded main."""
    r, home = repo
    # an integration branch that is NOT main
    git(r, "branch", "release")
    run_cli(["start", "feat", "--base", "release"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("feat"))
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    # merge feat into release in the main checkout, then return to main
    git(r, "checkout", "-q", "release")
    git(r, "merge", "--no-ff", "-m", "merge feat", "feat")
    git(r, "checkout", "-q", "main")
    capsys.readouterr()
    # not merged into main (HEAD), but merged into release
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "feat") == "unmerged"  # vs main
    capsys.readouterr()
    run_cli(["list", "--base", "release"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "feat") == "merged"  # vs release


def _make_repo(tmp_path, name):
    """A second throwaway git repo sharing the test's fake HOME."""
    r = tmp_path / name
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Tester")
    (r / "README").write_text("hi\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r


def test_list_all_spans_repos(cy, run_cli, repo, tmp_path, capsys):
    """`list --all` shows worktrees across every repo, with a REPO column."""
    r, home = repo
    other = _make_repo(tmp_path, "other-repo")
    run_cli(["start", "alpha"], home=home, cwd=r)
    run_cli(["start", "beta"], home=home, cwd=other)
    capsys.readouterr()
    run_cli(["list", "--all"], home=home, cwd=r)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["REPO", "TOPIC", "STATUS", "DIRECTORY"]
    body = "\n".join(lines[1:])
    # both repos' worktrees appear, even though we ran from `repo`
    assert "alpha" in body and "beta" in body
    assert "repo" in body and "other-repo" in body


def test_list_all_empty(cy, run_cli, repo, capsys):
    r, home = repo
    capsys.readouterr()
    run_cli(["list", "--all"], home=home, cwd=r)
    assert "No worktrees" in capsys.readouterr().out


def test_list_all_merged_judged_per_repo(cy, run_cli, repo, tmp_path, capsys):
    """A branch merged in its own repo reads `merged` under --all, run from elsewhere."""
    r, home = repo
    other = _make_repo(tmp_path, "other-repo")
    run_cli(["start", "done"], home=home, cwd=other)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("done"))
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    git(other, "merge", "--no-ff", "-m", "merge done", "done")
    capsys.readouterr()
    # run from `repo`, but `done` lives in `other-repo`
    run_cli(["list", "--all"], home=home, cwd=r)
    out = capsys.readouterr().out
    # STATUS is the 3rd column under --all (REPO TOPIC STATUS DIRECTORY)
    status = next(line.split()[2] for line in out.splitlines()[1:] if line.split()[1] == "done")
    assert status == "merged"


def test_all_only_applies_to_list(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["ps", "--all"], home=home, cwd=r)


# --- current-directory mode (no TOPIC) --------------------------------------


def test_start_no_topic_runs_in_cwd(cy, run_cli, repo):
    r, home = repo
    argv = run_cli(["start"], home=home, cwd=r)
    # No worktree was created; the container runs in (and is named for) the cwd. The
    # launch stamps the recent-projects registry (so `wip` can list it) but never
    # touches the deliberate config ledgers projects.json / worktrees.json.
    assert (home / ".claude-yolo" / "recent-projects.json").exists()
    assert not (home / ".claude-yolo" / "projects.json").exists()
    assert not (home / ".claude-yolo" / "worktrees.json").exists()
    assert worktree_label(argv) is None
    cmd = claude_command(cy, argv)
    assert "--name" not in cmd  # a plain cwd session is unnamed
    assert "--continue" not in cmd and "--resume" not in cmd  # fresh


def test_bare_is_equivalent_to_start(cy, run_cli, repo):
    r, home = repo
    bare = run_cli([], home=home, cwd=r)
    started = run_cli(["start"], home=home, cwd=r)
    assert claude_command(cy, bare) == claude_command(cy, started)


def test_resume_no_topic_continues_cwd(cy, run_cli, repo):
    r, home = repo
    argv = run_cli(["resume"], home=home, cwd=r)
    assert worktree_label(argv) is None
    assert "--continue" in claude_command(cy, argv)


def test_resume_no_topic_with_session_id(cy, run_cli, repo):
    r, home = repo
    argv = run_cli(["resume", "-r", "SID"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--resume" in cmd and "SID" in cmd


def test_shell_no_topic_execs_into_running_cwd_container(cy, run_cli, repo, monkeypatch):
    r, home = repo
    monkeypatch.setattr(
        cy, "running_container_for", lambda slug, topic=None, cwd=None: "cidcwd123456"
    )
    argv = run_cli(["shell"], home=home, cwd=r)
    assert argv == ["docker", "exec", "-it", "cidcwd123456", "/bin/bash"]


def test_shell_no_topic_fresh_container(cy, run_cli, repo):
    r, home = repo
    argv = run_cli(["shell"], home=home, cwd=r)  # no running container
    assert worktree_label(argv) is None
    assert argv[argv.index("--entrypoint") + 1] == "/bin/bash"


# --- config verb in a real repo ----------------------------------------------


def test_config_verb_keys_entry_by_repo_root(cy, run_cli, repo):
    import json

    r, home = repo
    sub = r / "sub"
    sub.mkdir()
    # run from a subdirectory: the entry must be keyed by the repo root, so
    # subdirectory runs and worktree sessions all share it
    run_cli(["config", "--no-ssh-agent"], home=home, cwd=sub)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert projects == {str(r): {"ssh-agent": False}}


def test_project_entry_applies_to_worktree_session(cy, run_cli, repo):
    import json

    r, home = repo
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "projects.json").write_text(json.dumps({str(r): {"ssh-agent": False}}))
    argv = run_cli(["start", "topic"], home=home, cwd=r)
    # config matched on the real cwd (the repo), before worktree retargeting
    envs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-e"]
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in envs


# --- dispatch guards --------------------------------------------------------


def test_finish_requires_topic(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["finish"], home=home, cwd=r)


def test_new_only_with_resume(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["start", "topic", "--new"], home=home, cwd=r)


def test_new_requires_topic(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["resume", "--new"], home=home, cwd=r)


def test_resume_flag_only_with_resume_verb(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["-r", "SID"], home=home, cwd=r)


# --- dir --------------------------------------------------------------------


def test_dir_prints_worktree_root(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("alpha"))
    capsys.readouterr()  # clear
    argv = run_cli(["dir", "alpha"], home=home, cwd=r)
    assert argv is None  # terminal verb: no container launched
    out = capsys.readouterr()
    assert out.out.strip() == str(wt)  # only the path, on stdout


def test_dir_no_topic_prints_cwd(cy, run_cli, repo, capsys):
    r, home = repo
    capsys.readouterr()
    argv = run_cli(["dir"], home=home, cwd=r)
    assert argv is None
    assert capsys.readouterr().out.strip() == str(r)


def test_dir_unknown_topic_errors(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["dir", "nope"], home=home, cwd=r)
