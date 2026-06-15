"""Tests for per-worktree overlay config (~/.claude-yolo/worktrees.json).

Like test_verbs.py these exercise the real git worktree machinery against a
throwaway repo, stubbing only `running_container_for` plus the launch side
effects via `run_cli`. They cover: `start` populating the overlay from explicit
CLI flags, `resume`/`shell` consuming it (with project<overlay<CLI precedence
and concat-key accumulation), `yolo config TOPIC` show/edit, and `finish`
removing it.
"""

import json
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
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: None)


def read_worktrees(home):
    return json.loads((home / ".claude-yolo" / "worktrees.json").read_text())


def overlay_for(cy, home, repo_path, topic):
    """The worktrees.json entry for a topic, by its resolved worktree path key."""
    worktree, _, _ = cy._worktree_dir(topic, home)
    return read_worktrees(home)[cy._worktree_overlay_key(worktree)]


# --- start populates --------------------------------------------------------


def test_start_populates_overlay_from_explicit_flags(cy, run_cli, repo, tmp_path):
    r, home = repo
    ref = tmp_path / "ref"
    ref.mkdir()
    run_cli(
        ["start", "fix-auth", "--mount", str(ref), "--port", "8000", "--ssh-agent"],
        home=home,
        cwd=r,
    )
    assert overlay_for(cy, home, r, "fix-auth") == {
        "mounts": [str(ref)],
        "ports": ["8000"],
        "ssh-agent": True,
    }


def test_start_writes_empty_overlay_when_no_flags(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "bare"], home=home, cwd=r)
    assert overlay_for(cy, home, r, "bare") == {}


# --- resume/shell consume ---------------------------------------------------


def test_resume_consumes_overlay(cy, run_cli, repo, flag_values, tmp_path):
    r, home = repo
    ref = tmp_path / "ref"
    ref.mkdir()
    run_cli(["start", "fix-auth", "--mount", str(ref), "--port", "8000"], home=home, cwd=r)
    argv = run_cli(["resume", "fix-auth"], home=home, cwd=r)
    # the overlay's port + mount reach the relaunch without retyping
    assert any(p.endswith(":8000") for p in flag_values(argv, "-p"))
    assert any(str(ref.resolve()) in m for m in flag_values(argv, "-v"))


def test_cli_flag_overrides_overlay_on_resume(cy, run_cli, repo, flag_values):
    r, home = repo
    run_cli(["start", "wt", "--ssh-agent"], home=home, cwd=r)  # overlay: ssh-agent true
    argv = run_cli(["resume", "wt", "--no-ssh-agent"], home=home, cwd=r)  # CLI wins
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in flag_values(argv, "-e")


def test_concat_keys_accumulate_project_overlay_cli(cy, run_cli, repo, flag_values):
    r, home = repo
    # project entry forwards 7000; overlay (from start) forwards 8000; CLI adds 9000
    proj = home / ".claude-yolo" / "projects.json"
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.write_text(json.dumps({str(r): {"ports": ["7000"]}}))
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    argv = run_cli(["resume", "wt", "--port", "9000"], home=home, cwd=r)
    published = flag_values(argv, "-p")
    for cport in ("7000", "8000", "9000"):
        assert any(p.endswith(f":{cport}") for p in published)


# --- resume updates the overlay ---------------------------------------------


def test_resume_flags_update_overlay(cy, run_cli, repo, tmp_path):
    r, home = repo
    ref = tmp_path / "ref"
    ref.mkdir()
    run_cli(["start", "fix-auth", "--port", "8000"], home=home, cwd=r)
    # resume restarts the container, so its config flags persist (lists accumulate,
    # scalars override)
    run_cli(
        ["resume", "fix-auth", "--port", "9000", "--mount", str(ref), "--ssh-agent"],
        home=home,
        cwd=r,
    )
    assert overlay_for(cy, home, r, "fix-auth") == {
        "ports": ["8000", "9000"],
        "mounts": [str(ref)],
        "ssh-agent": True,
    }


def test_resume_added_flags_persist_to_next_resume(cy, run_cli, repo, flag_values):
    r, home = repo
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    run_cli(["resume", "wt", "--port", "9000"], home=home, cwd=r)  # add 9000
    argv = run_cli(["resume", "wt"], home=home, cwd=r)  # plain resume, no flags
    published = flag_values(argv, "-p")
    assert any(p.endswith(":8000") for p in published)
    assert any(p.endswith(":9000") for p in published)


def test_resume_repeated_spec_does_not_duplicate(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    run_cli(["resume", "wt", "--port", "8000"], home=home, cwd=r)  # same port again
    assert overlay_for(cy, home, r, "wt")["ports"] == ["8000"]


def test_resume_scalar_flag_overrides_overlay_persistently(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "wt", "--ssh-agent"], home=home, cwd=r)  # overlay ssh-agent true
    run_cli(["resume", "wt", "--no-ssh-agent"], home=home, cwd=r)
    assert overlay_for(cy, home, r, "wt")["ssh-agent"] is False


def test_resume_without_flags_leaves_overlay_untouched(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    run_cli(["resume", "wt"], home=home, cwd=r)
    assert overlay_for(cy, home, r, "wt") == {"ports": ["8000"]}


def test_shell_flags_do_not_persist_to_overlay(cy, run_cli, repo):
    # shell is excluded — a fresh shell applies the flag for the run but must not
    # silently rewrite the persisted config (and a shell into a running container
    # couldn't change mounts at all).
    r, home = repo
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    run_cli(["shell", "wt", "--port", "9000"], home=home, cwd=r)
    assert overlay_for(cy, home, r, "wt") == {"ports": ["8000"]}


def test_resume_provenance_names_worktree_overlay(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "fix-auth", "--port", "8000"], home=home, cwd=r)
    capsys.readouterr()  # drop the start output
    run_cli(["resume", "fix-auth"], home=home, cwd=r)
    assert "worktrees.json[fix-auth]" in capsys.readouterr().err


# --- yolo config TOPIC ------------------------------------------------------


def test_config_topic_shows_overlay(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "fix-auth", "--port", "8000"], home=home, cwd=r)
    capsys.readouterr()
    run_cli(["config", "fix-auth"], home=home, cwd=r)
    out = capsys.readouterr().out
    assert "fix-auth" in out and "8000" in out


def test_config_topic_edits_overlay(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "fix-auth", "--port", "8000"], home=home, cwd=r)
    # --add-port appends, --port replaces, --unset drops
    run_cli(["config", "fix-auth", "--add-port", "9000"], home=home, cwd=r)
    assert overlay_for(cy, home, r, "fix-auth")["ports"] == ["8000", "9000"]
    run_cli(["config", "fix-auth", "--port", "5000"], home=home, cwd=r)
    assert overlay_for(cy, home, r, "fix-auth")["ports"] == ["5000"]
    run_cli(["config", "fix-auth", "--unset", "ports"], home=home, cwd=r)
    assert "ports" not in overlay_for(cy, home, r, "fix-auth")


def test_config_topic_show_missing_worktree_is_not_an_error(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["config", "ghost"], home=home, cwd=r)
    assert "no overlay for 'ghost'" in capsys.readouterr().out


def test_config_topic_edit_missing_worktree_errors(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["config", "ghost", "--port", "8000"], home=home, cwd=r)


def test_config_topic_rejects_global_and_init(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit):
        run_cli(["config", "wt", "--global", "--port", "8000"], home=home, cwd=r)
    with pytest.raises(SystemExit):
        run_cli(["config", "wt", "--init"], home=home, cwd=r)


# --- finish removes ---------------------------------------------------------


def test_finish_removes_overlay(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "fix-auth", "--port", "8000"], home=home, cwd=r)
    worktree, _, _ = cy._worktree_dir("fix-auth", home)
    key = cy._worktree_overlay_key(worktree)
    assert key in read_worktrees(home)
    run_cli(["finish", "fix-auth"], home=home, cwd=r)
    assert key not in read_worktrees(home)


def test_finish_without_overlay_does_not_choke(cy, run_cli, repo, monkeypatch):
    r, home = repo
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    # wipe the overlay file so finish must tolerate a missing entry
    (home / ".claude-yolo" / "worktrees.json").unlink()
    run_cli(["finish", "wt"], home=home, cwd=r)  # no exception


# --- malformed file ---------------------------------------------------------


def test_malformed_worktrees_file_errors_on_launch(cy, run_cli, repo):
    r, home = repo
    run_cli(["start", "wt", "--port", "8000"], home=home, cwd=r)
    (home / ".claude-yolo" / "worktrees.json").write_text("{ not json")
    with pytest.raises(SystemExit):
        run_cli(["resume", "wt"], home=home, cwd=r)
