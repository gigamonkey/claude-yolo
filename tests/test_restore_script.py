"""Tests for scripts/restore-orphaned-worktrees.py: re-registering worktrees whose
admin record (`.git/worktrees/<id>`) the repo has lost — the orphaned state a
`git worktree prune` run from a container that couldn't see them leaves behind.

Runs the script as a subprocess against real throwaway repos, with HOME pointed
at a tmp dir so the yolo worktree-dir scan is isolated.
"""

import os
import pathlib
import re
import shutil
import subprocess

import pytest

SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "restore-orphaned-worktrees.py"
)


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def run(repo, home, *args):
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        cwd=str(repo),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A main repo with one commit, its yolo worktree dir, and an isolated HOME."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "Tester")
    (r / "f").write_text("base\n")
    git(r, "add", "f")
    git(r, "commit", "-qm", "init")
    home = tmp_path / "home"
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(r))
    wts = home / ".claude-yolo" / "worktrees" / slug
    wts.mkdir(parents=True)
    return r, home, wts


def orphan(r, wt):
    """Delete the repo's admin record for `wt`, leaving the dir — the pruned state."""
    rec = r / ".git" / "worktrees" / wt.name
    assert rec.is_dir()
    shutil.rmtree(rec)
    assert git(wt, "status", check=False).returncode != 0  # orphaned: git can't use it


def test_dry_run_plans_but_changes_nothing(repo):
    r, home, wts = repo
    git(r, "worktree", "add", "-q", str(wts / "topic"), "-b", "topic")
    orphan(r, wts / "topic")
    before = (wts / "topic" / ".git").read_text()
    out = run(r, home, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "would: write" in out.stdout and "would restore 1 worktree." in out.stdout
    assert not (r / ".git" / "worktrees" / "topic").exists()
    assert (wts / "topic" / ".git").read_text() == before
    assert git(wt := wts / "topic", "status", check=False).returncode != 0 and wt


def test_restores_pruned_worktree_keeping_uncommitted_work(repo):
    r, home, wts = repo
    wt = wts / "topic"
    git(r, "worktree", "add", "-q", str(wt), "-b", "topic")
    (wt / "f").write_text("edited\n")
    (wt / "new").write_text("untracked\n")
    orphan(r, wt)
    out = run(r, home)
    assert out.returncode == 0, out.stderr
    assert "restored 1 worktree." in out.stdout
    # registered again, on its branch, with the working files intact
    assert f"{wt} " in git(r, "worktree", "list").stdout
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "topic"
    assert sorted(git(wt, "status", "--short").stdout.split("\n")[:-1]) == [" M f", "?? new"]
    assert (wt / "f").read_text() == "edited\n"
    # idempotent: a healthy worktree is left alone
    again = run(r, home)
    assert again.returncode == 0 and "restored 0 worktrees." in again.stdout


def test_recreates_a_deleted_branch_from_base(repo):
    r, home, wts = repo
    wt = wts / "topic"
    git(r, "worktree", "add", "-q", str(wt), "-b", "topic")
    orphan(r, wt)
    git(r, "branch", "-D", "topic")
    git(r, "commit", "-q", "--allow-empty", "-m", "later")
    out = run(r, home, "--base", "HEAD~1")
    assert out.returncode == 0, out.stderr
    assert "creating it from HEAD~1" in out.stdout
    assert git(wt, "rev-parse", "topic").stdout == git(r, "rev-parse", "HEAD~1").stdout


def test_skips_when_branch_is_checked_out_elsewhere(repo):
    r, home, wts = repo
    wt = wts / "topic"
    git(r, "worktree", "add", "-q", str(wt), "-b", "topic")
    orphan(r, wt)
    git(r, "worktree", "add", "-q", str(wts.parent / "elsewhere"), "topic")
    out = run(r, home)
    assert out.returncode == 1
    assert "already checked out" in out.stderr
    assert not (r / ".git" / "worktrees" / "topic").exists()


def test_moved_repo_uses_repair_when_record_is_intact_and_needs_adopt(repo, tmp_path):
    r, home, wts = repo
    intact, pruned = tmp_path / "intact", tmp_path / "pruned"
    git(r, "worktree", "add", "-q", str(intact), "-b", "intact")
    git(r, "worktree", "add", "-q", str(pruned), "-b", "pruned")
    shutil.rmtree(r / ".git" / "worktrees" / "pruned")
    moved = tmp_path / "moved"
    r.rename(moved)
    # the pointers name the old repo path: not ours without --adopt
    out = run(moved, home, str(intact), str(pruned), "--dry-run")
    assert out.returncode == 1 and "pass --adopt" in out.stderr
    out = run(moved, home, "--adopt", str(intact), str(pruned))
    assert out.returncode == 0, out.stderr
    assert "git worktree repair" in out.stdout  # intact record: git's own tool
    assert "re-register" in out.stdout  # pruned record: rebuilt by hand
    listed = git(moved, "worktree", "list").stdout
    assert f"{intact} " in listed and f"{pruned} " in listed
    assert git(intact, "status").returncode == 0 and git(pruned, "status").returncode == 0


def test_two_orphans_wanting_one_id_get_distinct_records(repo, tmp_path):
    # Two dirs whose pointers name the same admin id (a copied worktree): the
    # second gets git's `<id>1` suffix, even in a dry run where nothing is written.
    r, home, wts = repo
    a = wts / "a"
    git(r, "worktree", "add", "-q", str(a), "-b", "a")
    shutil.copytree(a, wts / "b")
    orphan(r, a)
    out = run(r, home, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "worktrees/a on 'a'" in out.stdout and "worktrees/a1 on 'b'" in out.stdout
    out = run(r, home)
    assert out.returncode == 0, out.stderr
    assert (r / ".git" / "worktrees" / "a").is_dir() and (r / ".git" / "worktrees" / "a1").is_dir()


def test_refuses_to_run_from_a_linked_worktree(repo):
    r, home, wts = repo
    wt = wts / "topic"
    git(r, "worktree", "add", "-q", str(wt), "-b", "topic")
    out = run(wt, home)
    assert out.returncode != 0 and "main checkout" in out.stderr
