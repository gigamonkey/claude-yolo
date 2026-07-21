"""Tests for the worktree verbs: start / resume / shell / finish / list.

Unlike test_cli.py (which stubs everything), these exercise the real git
worktree machinery against a throwaway repo, and stub only `running_container_for`
(docker) plus the launch side effects via the `run_cli` fixture.
"""

import re
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


def seed_session(cy, home, session_dir):
    """Plant a fake Claude transcript so a plain `resume` finds a session to continue.

    Mirrors ~/.claude/projects/<slug>/*.jsonl, which `_has_resumable_session`
    checks before issuing `claude --continue`. Without one, `resume` falls back to
    a fresh session.
    """
    proj = home / ".claude" / "projects" / cy._cwd_slug(session_dir)
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "sess.jsonl").write_text("{}\n")


# --- start ------------------------------------------------------------------


def test_start_creates_worktree_branch_and_names_session(cy, run_cli, repo, flag_values):
    r, home = repo
    argv = run_cli(["start", "auth-fix"], home=home, cwd=r)
    assert "YOLO_SESSION=worktree" in flag_values(argv, "-e")  # value names the kind
    # worktree + branch created
    wt = home / ".claude-yolo" / "worktrees"
    created = list(wt.rglob("auth-fix"))
    assert created and created[0].is_dir()
    # YOLO_WORKDIR names the worktree dir, not the launch cwd
    assert f"YOLO_WORKDIR={created[0]}" in flag_values(argv, "-e")
    assert "auth-fix" in git(r, "branch", "--list", "auth-fix").stdout
    # labelled and named `<repo>:<topic>`
    assert worktree_label(argv) == "auth-fix"
    cmd = claude_command(cy, argv)
    assert cmd[cmd.index("--name") + 1] == f"{r.name}:auth-fix"
    # a worktree is an isolated copy, so the cwd-mode live-checkout caution is absent
    assert "live checkout" not in cmd[cmd.index("--append-system-prompt") + 1]
    # ...and the build-dir redirect is cwd-only, so a worktree launch skips it
    assert not any(e.startswith("UV_PROJECT_ENVIRONMENT=") for e in flag_values(argv, "-e"))


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
    # A topic with no worktree, no branch, and no transcript is a typo, not a
    # finished topic — resume still refuses rather than reviving it.
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["resume", "ghost"], home=home, cwd=r)


def test_resume_revives_finished_topic_with_transcript(cy, run_cli, repo, capsys):
    # A finished topic leaves its Claude transcript behind (keyed by the
    # deterministic worktree path): resume recreates the worktree and issues
    # `--continue`, reconnecting the old session.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    seed_session(cy, home, wt)
    run_cli(["finish", "topic"], home=home, cwd=r)  # undiverged → branch deleted too
    assert not wt.exists()
    argv = run_cli(["resume", "topic"], home=home, cwd=r)
    assert wt.is_dir()  # recreated
    assert "--continue" in claude_command(cy, argv)  # the old session, not a fresh one
    assert "Recreating the 'topic' worktree" in capsys.readouterr().err


def test_resume_revive_reattaches_surviving_branch(cy, run_cli, repo):
    # finish keeps an unmerged branch; a later resume of the finished topic
    # reattaches the recreated worktree to that branch (same tip), rather than
    # erroring or creating a fresh branch off base.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "w.txt").write_text("work\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    tip = git(wt, "rev-parse", "HEAD").stdout.strip()
    run_cli(["finish", "topic"], home=home, cwd=r)  # unmerged → branch kept
    assert not wt.exists()
    argv = run_cli(["resume", "topic"], home=home, cwd=r)
    assert git(wt, "rev-parse", "HEAD").stdout.strip() == tip
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "topic"
    # no transcript was seeded, so the revived worktree gets a fresh session
    assert "--continue" not in claude_command(cy, argv)


def test_resume_defaults_to_continue(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    seed_session(cy, home, wt)
    argv = run_cli(["resume", "topic"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--continue" in cmd
    # the display name rides along on every continue, so the label above the
    # prompt tracks the current project name (a rename reaches old sessions)
    assert cmd[cmd.index("--name") + 1] == f"{r.name}:topic"


def test_resume_without_session_falls_back_to_fresh(cy, run_cli, repo, capsys):
    # No transcript for the worktree (claude --continue would error inside the
    # container): resume starts a fresh *named* session instead and says so.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["resume", "topic"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--continue" not in cmd
    assert cmd[cmd.index("--name") + 1] == f"{r.name}:topic"
    assert "No previous Claude session" in capsys.readouterr().err


def test_resume_new_starts_named_fresh_session(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    argv = run_cli(["resume", "topic", "--new"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert cmd[cmd.index("--name") + 1] == f"{r.name}:topic"
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
    # the guard finds the live session by label and refuses (non-tmux)
    monkeypatch.setattr(cy, "_running_container_name", lambda *a, **k: "repo-topic")
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


def test_stop_refuses_agenting_session_without_force(cy, run_cli, repo, monkeypatch):
    # agenting = turn over but background agents still running; the session will
    # act again on its own, so stop treats it like working.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    monkeypatch.setattr(
        cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid123456789"
    )
    monkeypatch.setattr(cy, "_read_session_state", lambda path, now: "agenting 3s")
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
    with pytest.raises(SystemExit):  # merge failure aborts finish
        run_cli(["finish", "topic", "--finish-action", "merge"], home=home, cwd=r)
    assert wt.exists()  # worktree kept — the merge failed, so nothing was removed
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


def test_finish_refuses_when_session_agenting(cy, run_cli, repo, monkeypatch):
    # A session waiting on its own background agents is active work too.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "agenting")
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


def test_rebase_refuses_when_session_agenting(cy, run_cli, repo, monkeypatch):
    # agenting = background agents still running; the session will act again, so
    # rebase refuses just as it would for working.
    r, home, wt = _rebase_setup_with_main_commit(cy, run_cli, repo)
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: "cid")
    _write_session_state(cy, home, wt, "agenting")
    _fake_docker_session(cy, monkeypatch, home, wt)
    with pytest.raises(SystemExit) as exc:
        run_cli(["rebase", "topic"], home=home, cwd=r)
    assert "active" in str(exc.value)  # refused as active, not as unconfirmable
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


# --- merge ------------------------------------------------------------------


def test_merge_merges_branch_into_main_keeping_worktree(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    topic_head = git(wt, "rev-parse", "HEAD").stdout.strip()
    run_cli(["merge", "topic"], home=home, cwd=r)
    # main now contains the branch's work (fast-forward: main HEAD == topic HEAD)…
    assert git(r, "rev-parse", "HEAD").stdout.strip() == topic_head
    assert (r / "work.txt").exists()
    # …and the worktree and branch are still there.
    assert wt.is_dir()
    assert git(r, "rev-parse", "--verify", "topic").returncode == 0


def test_merge_creates_merge_commit_when_diverged(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "work.txt").write_text("done")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "work")
    # main diverges too, so the merge can't fast-forward
    (r / "main.txt").write_text("mainwork")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main work")
    run_cli(["merge", "topic"], home=home, cwd=r)
    # a real merge commit with two parents; both sides' files present
    parents = git(r, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3  # commit + 2 parents
    assert (r / "work.txt").exists() and (r / "main.txt").exists()
    assert wt.is_dir()  # worktree kept


def test_merge_requires_topic(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["merge"], home=home, cwd=r)


def test_merge_errors_without_worktree(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["merge", "ghost"], home=home, cwd=r)


def test_merge_conflict_aborts_and_keeps_branch(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    # both branches edit the same file differently → conflict on merge
    (wt / "README").write_text("topic side\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "topic edit")
    (r / "README").write_text("main side\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main edit")
    with pytest.raises(SystemExit):
        run_cli(["merge", "topic"], home=home, cwd=r)
    # the merge was aborted (no half-merged state) and the branch survives
    no_merge = subprocess.run(
        ["git", "-C", str(r), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        capture_output=True,
        text=True,
    )
    assert no_merge.returncode != 0  # MERGE_HEAD gone → the merge was aborted
    assert git(r, "rev-parse", "--verify", "topic").returncode == 0
    assert wt.is_dir()


def test_merge_refuses_base_not_checked_out(cy, run_cli, repo):
    r, home = repo
    # a base that resolves but isn't the checked-out branch: merge can't land there
    other = git(r, "rev-parse", "HEAD").stdout.strip()
    git(r, "branch", "otherbase", other)
    (r / "main.txt").write_text("mainwork")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main moves on")  # HEAD now != otherbase
    run_cli(["start", "topic"], home=home, cwd=r)
    with pytest.raises(SystemExit):
        run_cli(["merge", "topic", "--base", "otherbase"], home=home, cwd=r)


# --- diff -------------------------------------------------------------------


def test_diff_requires_topic(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["diff"], home=home, cwd=r)


def test_diff_errors_without_worktree(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["diff", "ghost"], home=home, cwd=r)


def test_diff_shows_branch_changes_three_dot(cy, run_cli, repo, capfd):
    # `diff` is a three-dot `base...HEAD`: it shows what the branch *adds* since it
    # diverged, not changes the base made on its own.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "branch.txt").write_text("b\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "branch work")
    # advance the base (main) with its own file after the worktree branched
    (r / "main.txt").write_text("m\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main work")
    capfd.readouterr()  # clear
    run_cli(["diff", "topic"], home=home, cwd=r)  # base defaults to HEAD (main's tip)
    out = capfd.readouterr().out
    assert "branch.txt" in out and "+b" in out  # the branch's change is shown
    assert "main.txt" not in out  # base-only changes are not (three-dot)


def test_diff_includes_uncommitted_changes(cy, run_cli, repo, capfd):
    # `diff` diffs against the working tree, so uncommitted (dirty) changes to
    # tracked files show up — both committed and not.
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "committed.txt").write_text("c\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "committed work")
    (wt / "README").write_text("dirty edit\n")  # tracked, uncommitted
    capfd.readouterr()
    run_cli(["diff", "topic"], home=home, cwd=r)
    out = capfd.readouterr().out
    assert "committed.txt" in out and "+c" in out  # the committed change
    assert "README" in out and "+dirty edit" in out  # the uncommitted change


def test_diff_stat_shows_uncommitted_changes(cy, run_cli, repo, capfd, monkeypatch):
    # The --stat view (what the wip `d` action drives) reflects dirty changes too.
    monkeypatch.delenv("TMUX", raising=False)
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)  # no commits, only a dirty edit
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "README").write_text("dirty edit\n")
    capfd.readouterr()
    run_cli(["diff", "topic", "--stat"], home=home, cwd=r)
    out = capfd.readouterr().out
    assert "README" in out and "1 file changed" in out  # dirty file in the stat


def test_diff_stat_prints_stat_without_tmux(cy, run_cli, repo, capfd, monkeypatch):
    # --stat wants tmux for the interactive picker; without it (a test/CLI run) it
    # just prints `git diff --stat` and returns.
    monkeypatch.delenv("TMUX", raising=False)
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "branch.txt").write_text("b\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "branch work")
    capfd.readouterr()
    run_cli(["diff", "topic", "--stat"], home=home, cwd=r)
    out = capfd.readouterr().out
    assert "branch.txt" in out and "1 file changed" in out  # the git --stat summary


def test_diff_stat_empty_says_no_changes(cy, run_cli, repo, capfd):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)  # branched, no commits → no diff
    capfd.readouterr()
    run_cli(["diff", "topic", "--stat"], home=home, cwd=r)
    assert "No changes" in capfd.readouterr().out


def test_stat_only_applies_to_diff(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["list", "--stat"], home=home, cwd=r)


# --- list -------------------------------------------------------------------


def test_list_shows_worktrees(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    run_cli(["start", "beta"], home=home, cwd=r)
    capsys.readouterr()  # clear
    run_cli(["list"], home=home, cwd=r)
    lines = capsys.readouterr().out.splitlines()
    # header row, then one row per topic, each showing its worktree directory
    assert lines[0].split() == ["TOPIC", "STATUS", "COMMITS", "DIRECTORY"]
    body = "\n".join(lines[1:])
    assert "alpha" in body and "beta" in body
    # DIRECTORY is <slug>/<topic> — the shared ~/.claude-yolo/worktrees/ prefix dropped
    assert "~/.claude-yolo/worktrees" not in body
    slug = cy._repo_paths()[2]
    assert f"{slug}/alpha" in body and f"{slug}/beta" in body


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


def test_list_flags_rebase_conflicts(cy, run_cli, repo, capsys):
    # A rebase left mid-conflict shows STATUS 'conflicts' (detected from the
    # worktree's git dir, regardless of who started it), and the TOPIC stays the
    # topic name rather than the detached-HEAD the rebase leaves it on.
    r, home = repo
    run_cli(["start", "wip"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("wip"))
    (wt / "README").write_text("branch\n")  # conflicting change vs main's advance
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "branch edit")
    (r / "README").write_text("main\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "main edit")
    with pytest.raises(SystemExit):  # single-repo conflict raises
        run_cli(["rebase", "wip"], home=home, cwd=r)
    assert cy._rebase_in_progress(wt)
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    assert _status_for(capsys.readouterr().out, "wip") == "conflicts"


def test_list_shows_commits(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "topic"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("topic"))
    (wt / "x").write_text("x")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "ahead")  # one ahead of base, none behind
    capsys.readouterr()
    run_cli(["list"], home=home, cwd=r)
    # TOPIC STATUS COMMITS DIRECTORY -> COMMITS reads "↓behind ↑ahead"
    line = next(ln for ln in capsys.readouterr().out.splitlines()[1:] if ln.split()[0] == "topic")
    assert "↓0 ↑1" in line


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
    assert lines[0].split() == ["REPO", "TOPIC", "STATUS", "COMMITS", "DIRECTORY"]
    body = "\n".join(lines[1:])
    # both repos' worktrees appear, even though we ran from `repo`
    assert "alpha" in body and "beta" in body
    assert "repo" in body and "other-repo" in body


def test_list_all_orphaned_worktree(cy, run_cli, repo, tmp_path, capsys):
    # A worktree whose main repo was moved/deleted is orphaned (git can't resolve
    # it): STATUS reads `orphaned`, COMMITS `-`, and the REPO name is recovered from
    # the worktree's .git pointer (not the slugified path). A footer hint points at
    # `git worktree repair`.
    r, home = repo
    other = _make_repo(tmp_path, "other-repo")
    run_cli(["start", "beta"], home=home, cwd=other)
    other.rename(tmp_path / "other-repo-moved")  # orphan beta's worktree
    capsys.readouterr()
    run_cli(["list", "--all"], home=home, cwd=r)
    out = capsys.readouterr()
    # cols: REPO TOPIC STATUS COMMITS DIRECTORY
    beta = next(
        cols for line in out.out.splitlines()[1:] if (cols := line.split())[1:2] == ["beta"]
    )
    assert beta[0] == "other-repo"  # recovered repo name, not the long slug
    assert beta[0].count("-") == 1
    assert beta[2] == "orphaned" and beta[3] == "-"  # STATUS / COMMITS
    assert "orphaned" in out.err and "git worktree repair" in out.err  # footer hint


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
    assert cmd[cmd.index("--name") + 1] == r.name  # cwd session named after the dir
    assert "--continue" not in cmd and "--resume" not in cmd  # fresh


def test_name_config_renames_sessions(cy, run_cli, repo):
    # The `name` config key (yolo config --name) overrides the directory/repo
    # basename in every session name: cwd container, worktree container, and the
    # claude session label. This is what makes a project renameable — and what a
    # saved multi-repo project's NAME rides in on.
    r, home = repo
    run_cli(["config", "--name", "myproj"], home=home, cwd=r)
    argv = run_cli(["start"], home=home, cwd=r)
    assert argv[argv.index("--name") + 1] == "myproj"
    cmd = claude_command(cy, argv)
    assert cmd[cmd.index("--name") + 1] == "myproj"
    argv = run_cli(["start", "feat"], home=home, cwd=r)
    assert argv[argv.index("--name") + 1] == "myproj-feat"
    cmd = claude_command(cy, argv)
    assert cmd[cmd.index("--name") + 1] == "myproj:feat"


def test_docker_safe_name(cy):
    """Directory basenames coerced into valid docker --name / --hostname strings."""
    f = cy._docker_safe_name
    # already-valid names pass through untouched (existing containers keep their name)
    assert f("repo") == "repo"
    assert f("my-project_1") == "my-project_1"
    assert f("my.project") == "my.project"  # a mid-name dot is fine for docker
    # leading dot/underscore (hidden dirs, dunder dirs) are the reported break
    assert f(".dotfiles") == "dotfiles"
    assert f("._foo-") == "foo"  # leading & trailing junk stripped
    assert f("__pycache__") == "pycache"  # leading & trailing underscores stripped
    # stray characters become hyphens
    assert f("my project!") == "my-project"
    # nothing usable, or under docker's 2-char minimum, falls back
    assert f(".") == "workspace"
    assert f("...") == "workspace"
    assert f("a") == "workspace"
    assert f("", fallback="x") == "x"


def _dot_repo(tmp_path, name):
    """A git repo whose basename is `name`, plus an isolated fake HOME."""
    r = tmp_path / name
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


def test_name_available(cy, monkeypatch):
    """A name is free only if neither namespace holds it: no docker container of that
    name, and (under tmux) no window of that name — a window can outlive its --rm
    container, so a stale one must still block a same-named newcomer."""
    monkeypatch.setattr(cy, "_running_container_names", lambda **k: {"taken"})
    monkeypatch.setattr(cy, "_find_tmux_window", lambda s, n: "@1" if n == "win" else None)
    assert cy._name_available("free", None) is True
    assert cy._name_available("taken", None) is False  # a docker container holds it
    assert cy._name_available("win", None) is True  # non-tmux: windows don't count
    assert cy._name_available("win", "yolo") is False  # tmux: a stale window blocks it
    assert cy._name_available("free", "yolo") is True


def test_cwd_leading_dot_launches_with_clean_name(cy, run_cli, tmp_path):
    """A hidden directory (basename starting with '.') can't be a docker --name or
    --hostname raw — docker rejects it. yolo coerces the leading dot away so cwd mode
    still launches, and with nothing else holding the name the session keeps the
    clean, un-uglified `dotfiles` (the win of the live-conflict scheme)."""
    r, home = _dot_repo(tmp_path, ".dotfiles")
    argv = run_cli(["start"], home=home, cwd=r)
    # the first --name / --hostname on the argv are docker-run's (the container),
    # distinct from the later `claude --name` session flag after the image.
    name = argv[argv.index("--name") + 1]
    host = argv[argv.index("--hostname") + 1]
    assert name == "dotfiles" and host == "dotfiles"  # no conflict → no ugly suffix
    assert name[0].isalnum() and host[0].isalnum()  # docker's leading-char rule


def test_cwd_falls_back_to_hashed_name_on_live_conflict(cy, run_cli, tmp_path, monkeypatch):
    """When the friendly name is already held by another live session, the newcomer
    falls back to a per-cwd hashed name so the two coexist — but only then."""
    hidden, home = _dot_repo(tmp_path, ".foo")
    # simulate a plain `foo` session already running (holds the `foo` name in docker)
    monkeypatch.setattr(cy, "_running_container_names", lambda **k: {"foo"})
    argv = run_cli(["start"], home=home, cwd=hidden)
    name = argv[argv.index("--name") + 1]
    assert re.fullmatch(r"foo-[0-9a-f]{8}", name), name  # uglified to dodge the clash
    assert argv[argv.index("--hostname") + 1] == "foo"  # hostname needn't be unique


def test_bare_is_equivalent_to_start(cy, run_cli, repo):
    r, home = repo
    bare = run_cli([], home=home, cwd=r)
    started = run_cli(["start"], home=home, cwd=r)
    assert claude_command(cy, bare) == claude_command(cy, started)


def test_resume_no_topic_continues_cwd(cy, run_cli, repo):
    r, home = repo
    seed_session(cy, home, r)
    argv = run_cli(["resume"], home=home, cwd=r)
    assert worktree_label(argv) is None
    assert "--continue" in claude_command(cy, argv)


def test_resume_no_topic_without_session_falls_back_to_fresh(cy, run_cli, repo):
    r, home = repo  # no transcript for the cwd
    argv = run_cli(["resume"], home=home, cwd=r)
    cmd = claude_command(cy, argv)
    assert "--continue" not in cmd
    assert cmd[cmd.index("--name") + 1] == r.name  # fresh fallback names it after the dir


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
    assert projects == {r.name: {"dir": str(r), "ssh-agent": False}}


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


def test_dir_explicit_project_root(cy, run_cli, repo, tmp_path, capsys):
    # `yolo dir TOPIC DIR` from an unrelated cwd matches `yolo dir TOPIC` run in DIR
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("alpha"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    capsys.readouterr()  # clear
    argv = run_cli(["dir", "alpha", str(r)], home=home, cwd=elsewhere)
    assert argv is None
    assert capsys.readouterr().out.strip() == str(wt)


def test_dir_explicit_root_not_a_directory(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit) as e:
        run_cli(["dir", "alpha", str(r / "nope")], home=home, cwd=r)
    assert "not a directory" in str(e.value)


def test_dir_explicit_root_not_a_repo(cy, run_cli, repo, tmp_path):
    r, home = repo
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SystemExit) as e:
        run_cli(["dir", "alpha", str(plain)], home=home, cwd=r)
    assert "not inside a git repository" in str(e.value)


def test_dir_rejects_second_extra_arg(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit) as e:
        run_cli(["dir", "alpha", str(r), "extra"], home=home, cwd=r)
    assert "unexpected argument" in str(e.value)
