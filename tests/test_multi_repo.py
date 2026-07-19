"""Tests for multi-repo projects.

Covers the `repos` config key (--repo / --add-repo / --remove-repo), saved
named projects (~/.claude-yolo/projects.json, `config --project` / --dir,
`start --project`), the multi-worktree start (creation, mounts,
--add-dir, prompt, overlay stamp, guards, rollback), resume recreating missing
extra worktrees, the worktree verbs operating across the repo set, and the
`wip` dashboard's multi-repo rows/actions.

Like test_worktree_config.py these exercise the real git worktree machinery
against throwaway repos, stubbing only the launch side effects via `run_cli`.
"""

import json
import pathlib
import shutil
import subprocess

import pytest


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "Tester")
    (path / "README").write_text(f"{path.name}\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def repos(tmp_path):
    """Three sibling git repos (app = primary, lib, proto) plus a fake HOME."""
    app = make_repo(tmp_path / "app")
    lib = make_repo(tmp_path / "lib")
    proto = make_repo(tmp_path / "proto")
    home = tmp_path / "home"
    home.mkdir()
    return app, lib, proto, home


@pytest.fixture(autouse=True)
def no_docker_ps(cy, monkeypatch):
    monkeypatch.setattr(cy, "running_container_for", lambda slug, topic=None, cwd=None: None)


def wt_of(cy, home, repo, topic):
    """The worktree path yolo uses for `topic` in `repo` (keyed by its slug)."""
    slug = cy._repo_root_of(repo)[2]
    return home / ".claude-yolo" / "worktrees" / slug / topic


def branch_exists(repo, name):
    return (
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]
        ).returncode
        == 0
    )


def read_overlay(cy, home, repo, topic):
    worktrees = json.loads((home / ".claude-yolo" / "worktrees.json").read_text())
    key = cy._worktree_overlay_key(wt_of(cy, home, repo, topic))
    return worktrees[key]


def read_project(home, name):
    return json.loads((home / ".claude-yolo" / "projects.json").read_text())[name]


def entry_by_dir(home, d):
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    e = next(v for v in projects.values() if v.get("dir") == str(d))
    return {k: v for k, v in e.items() if k != "dir"}


# --- config plumbing ---------------------------------------------------------


def test_config_add_and_remove_repo(cy, run_cli, repos):
    app, lib, proto, home = repos
    run_cli(["config", "--add-repo", str(lib)], home=home, cwd=app)
    assert entry_by_dir(home, app)["repos"] == [str(lib)]
    # adding the same path again is a no-op (matched by resolved path)
    run_cli(["config", "--add-repo", str(lib)], home=home, cwd=app)
    assert entry_by_dir(home, app)["repos"] == [str(lib)]
    run_cli(["config", "--remove-repo", str(lib)], home=home, cwd=app)
    assert "repos" not in entry_by_dir(home, app)


def test_config_add_repo_rejects_non_repo_path(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SystemExit) as e:
        run_cli(["config", "--add-repo", str(plain)], home=home, cwd=app)
    assert "not a git repository" in str(e.value)
    with pytest.raises(SystemExit) as e:
        run_cli(["config", "--add-repo", str(tmp_path / "nope")], home=home, cwd=app)
    assert "no such directory" in str(e.value)


def test_repos_from_project_entry_reaches_the_launch(cy, run_cli, repos, flag_values):
    # `repos` on the project entry: every worktree start of that project is
    # multi-repo (the always-on form).
    app, lib, proto, home = repos
    (home / ".claude-yolo").mkdir()
    (home / ".claude-yolo" / "projects.json").write_text(
        json.dumps({"app": {"dir": str(app), "repos": [str(lib)]}})
    )
    argv = run_cli(["start", "t1"], home=home, cwd=app)
    lib_wt = wt_of(cy, home, lib, "t1")
    assert lib_wt.is_dir()
    assert f"{lib_wt}:{lib_wt}" in flag_values(argv, "-v")


# --- start: creation, mounts, prompt, overlay --------------------------------


def test_start_creates_worktrees_and_branches_in_every_repo(cy, run_cli, repos):
    app, lib, proto, home = repos
    run_cli(["start", "feat", "--repo", str(lib), "--repo", str(proto)], home=home, cwd=app)
    for r in (app, lib, proto):
        wt = wt_of(cy, home, r, "feat")
        assert wt.is_dir()
        assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feat"


def test_start_mounts_each_extra_worktree_and_its_git(cy, run_cli, repos, flag_values):
    app, lib, proto, home = repos
    argv = run_cli(["start", "feat", "--repo", str(lib)], home=home, cwd=app)
    mounts = flag_values(argv, "-v")
    lib_wt = wt_of(cy, home, lib, "feat")
    lib_git = cy._repo_root_of(lib)[0]
    assert f"{lib_wt}:{lib_wt}" in mounts
    assert f"{lib_git}:{lib_git}" in mounts
    # the working dir stays the primary's worktree
    assert flag_values(argv, "-w") == [str(wt_of(cy, home, app, "feat"))]


def test_start_announces_extras_as_add_dirs_and_in_prompt(cy, run_cli, repos, flag_values):
    app, lib, proto, home = repos
    argv = run_cli(["start", "feat", "--repo", str(lib)], home=home, cwd=app)
    assert str(wt_of(cy, home, lib, "feat")) in flag_values(argv, "--add-dir")
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "spans multiple repositories" in prompt
    assert str(wt_of(cy, home, lib, "feat")) in prompt
    labels = flag_values(argv, "--label")
    assert any(lbl.startswith("yolo.extra-repos=") for lbl in labels)


def test_start_stamps_repo_flags_into_overlay(cy, run_cli, repos):
    app, lib, proto, home = repos
    run_cli(["start", "feat", "--repo", str(lib)], home=home, cwd=app)
    assert read_overlay(cy, home, app, "feat") == {"repos": [str(lib)]}


def test_relative_repo_specs_are_stored_absolute(cy, run_cli, repos):
    # A relative --repo/--add-repo is resolved (against the invocation cwd) at
    # *storage* time: config entries and overlays are read back from arbitrary
    # cwds, so a stored `../lib` would later point somewhere else entirely.
    app, lib, proto, home = repos
    run_cli(["start", "feat", "--repo", "../lib"], home=home, cwd=app)
    assert read_overlay(cy, home, app, "feat") == {"repos": [str(lib)]}
    run_cli(["config", "feat", "--add-repo", "../proto"], home=home, cwd=app)
    assert read_overlay(cy, home, app, "feat")["repos"] == [str(lib), str(proto)]
    run_cli(["config", "--project", "chat", "--add-repo", "../lib"], home=home, cwd=app)
    assert read_project(home, "chat")["repos"] == [str(lib)]


def test_single_repo_start_unaffected(cy, run_cli, repos, flag_values):
    # No repos configured: exactly one worktree, no extra-repos label, no
    # multi-repo prompt line.
    app, lib, proto, home = repos
    argv = run_cli(["start", "solo"], home=home, cwd=app)
    assert not wt_of(cy, home, lib, "solo").exists()
    assert not any(lbl.startswith("yolo.extra-repos=") for lbl in flag_values(argv, "--label"))
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "spans multiple repositories" not in prompt


# --- start: guards and rollback ----------------------------------------------


def test_start_errors_on_bad_repo_path_before_creating_anything(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "feat", "--repo", str(tmp_path / "nope")], home=home, cwd=app)
    assert "no such directory" in str(e.value)
    assert not wt_of(cy, home, app, "feat").exists()
    assert not branch_exists(app, "feat")


def test_start_aborts_whole_set_when_branch_exists_in_one_extra(cy, run_cli, repos):
    app, lib, proto, home = repos
    git(lib, "branch", "clash")
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "clash", "--repo", str(lib), "--repo", str(proto)], home=home, cwd=app)
    assert "already exists in" in str(e.value)
    for r in (app, lib, proto):
        assert not wt_of(cy, home, r, "clash").exists()
    assert not branch_exists(app, "clash")
    assert not branch_exists(proto, "clash")


def test_start_rolls_back_on_midway_creation_failure(cy, run_cli, repos, monkeypatch):
    app, lib, proto, home = repos
    real = cy.setup_worktree

    def failing(name, home_, base="HEAD", repo=None):
        if repo is not None and pathlib.Path(repo).name == "proto":
            raise cy.YoloError("boom")
        return real(name, home_, base, repo)

    monkeypatch.setattr(cy, "setup_worktree", failing)
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "feat", "--repo", str(lib), "--repo", str(proto)], home=home, cwd=app)
    assert "rolled back" in str(e.value)
    for r in (app, lib, proto):
        assert not wt_of(cy, home, r, "feat").exists()
        assert not branch_exists(r, "feat")


def test_cwd_session_ignores_repos_with_a_note(cy, run_cli, repos, flag_values, capsys):
    app, lib, proto, home = repos
    argv = run_cli(["start", "--repo", str(lib)], home=home, cwd=app)
    assert "ignored for current-directory sessions" in capsys.readouterr().err
    assert not any(str(lib) in m for m in flag_values(argv, "-v"))


# --- resume / config edits mid-topic -----------------------------------------


def test_resume_remounts_the_set_from_the_overlay(cy, run_cli, repos, flag_values):
    app, lib, proto, home = repos
    run_cli(["start", "feat", "--repo", str(lib)], home=home, cwd=app)
    argv = run_cli(["resume", "feat"], home=home, cwd=app)
    lib_wt = wt_of(cy, home, lib, "feat")
    assert f"{lib_wt}:{lib_wt}" in flag_values(argv, "-v")


def test_resume_creates_worktree_for_repo_added_mid_topic(cy, run_cli, repos, flag_values):
    app, lib, proto, home = repos
    run_cli(["start", "feat", "--repo", str(lib)], home=home, cwd=app)
    assert not wt_of(cy, home, proto, "feat").exists()
    run_cli(["config", "feat", "--add-repo", str(proto)], home=home, cwd=app)
    argv = run_cli(["resume", "feat"], home=home, cwd=app)
    proto_wt = wt_of(cy, home, proto, "feat")
    assert proto_wt.is_dir()
    assert f"{proto_wt}:{proto_wt}" in flag_values(argv, "-v")


# --- named projects (`config --project`, `start --project`) -------------------


def test_config_project_creates_entry_with_inferred_dir(cy, run_cli, repos):
    app, lib, proto, home = repos
    run_cli(["config", "--project", "chat", "--add-repo", str(lib)], home=home, cwd=app)
    assert read_project(home, "chat") == {"dir": str(app), "repos": [str(lib)]}


def test_config_project_dir_required_outside_a_repo(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(SystemExit) as e:
        run_cli(["config", "--project", "chat", "--add-repo", str(lib)], home=home, cwd=outside)
    assert "--dir" in str(e.value)
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=outside,
    )
    assert read_project(home, "chat")["dir"] == str(app)


def test_config_project_dir_normalizes_to_repo_root_and_allows_plain_dirs(
    cy, run_cli, repos, tmp_path
):
    app, lib, proto, home = repos
    sub = app / "src"
    sub.mkdir()
    run_cli(["config", "--project", "chat", "--dir", str(sub)], home=home, cwd=tmp_path)
    assert read_project(home, "chat")["dir"] == str(app)
    # a plain (non-git) directory is a fine primary — cwd-session projects need no git
    plain = tmp_path / "plain"
    plain.mkdir()
    run_cli(["config", "--project", "notes", "--dir", str(plain)], home=home, cwd=tmp_path)
    assert read_project(home, "notes")["dir"] == str(plain.resolve())
    with pytest.raises(SystemExit) as e:
        run_cli(
            ["config", "--project", "bad", "--dir", str(tmp_path / "nope")], home=home, cwd=tmp_path
        )
    assert "no such directory" in str(e.value)


def test_start_multi_repo_launches_from_saved_dir_anywhere(
    cy, run_cli, repos, tmp_path, flag_values
):
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    argv = run_cli(["start", "feat", "--project", "chat"], home=home, cwd=elsewhere)
    assert wt_of(cy, home, app, "feat").is_dir()
    assert wt_of(cy, home, lib, "feat").is_dir()
    assert flag_values(argv, "-w") == [str(wt_of(cy, home, app, "feat"))]
    # the overlay records only the project *pointer* (plus explicit CLI flags) —
    # the entry stays a live layer, never copied
    assert read_overlay(cy, home, app, "feat") == {"project": "chat"}


def test_project_sessions_are_named_after_the_project(cy, run_cli, repos, tmp_path):
    # Container/session names derive from the project's NAME, not the primary
    # repo's basename — the `wip` dashboard correlates sessions to tmux windows by
    # that name, and its `n` key names the spawned window NAME-TOPIC.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    argv = run_cli(["start", "feat", "--project", "chat"], home=home, cwd=tmp_path)
    assert argv[argv.index("--name") + 1] == "chat-feat"  # docker --name
    img = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    cargs = argv[img + 1 :]  # what's passed to claude
    assert cargs[cargs.index("--name") + 1] == "chat:feat"  # claude session name
    # resume resolves the project via the overlay's pointer, no --project needed
    argv = run_cli(["resume", "feat"], home=home, cwd=app)
    assert argv[argv.index("--name") + 1] == "chat-feat"


def test_start_project_without_topic_opens_cwd_session_in_dir(cy, run_cli, repos, tmp_path):
    # A project's dir is a fine cwd-session target: `start --project NAME` with no
    # TOPIC launches there from anywhere, named after the project.
    app, lib, proto, home = repos
    run_cli(["config", "--project", "chat", "--dir", str(app)], home=home, cwd=tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    argv = run_cli(["start", "--project", "chat"], home=home, cwd=elsewhere)
    assert argv[argv.index("-w") + 1] == str(app)
    assert argv[argv.index("--name") + 1] == "chat"


def test_config_project_name_renames_and_repoints_topics(cy, run_cli, repos, tmp_path):
    # --name renames the entry; worktree overlays pointing at it are rewritten.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    run_cli(["start", "feat", "--project", "chat"], home=home, cwd=app)
    run_cli(["config", "--project", "chat", "--name", "comms"], home=home, cwd=tmp_path)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert "chat" not in projects and projects["comms"]["dir"] == str(app)
    assert read_overlay(cy, home, app, "feat") == {"project": "comms"}
    # the topic relaunches under the new name
    argv = run_cli(["resume", "feat"], home=home, cwd=app)
    assert argv[argv.index("--name") + 1] == "comms-feat"


def test_config_project_delete_guards_live_topics(cy, run_cli, repos, tmp_path, capsys):
    app, lib, proto, home = repos
    run_cli(["config", "--project", "chat", "--dir", str(app)], home=home, cwd=tmp_path)
    run_cli(["start", "feat", "--project", "chat"], home=home, cwd=app)
    with pytest.raises(SystemExit) as e:
        run_cli(["config", "--project", "chat", "--delete"], home=home, cwd=tmp_path)
    assert "live worktrees" in str(e.value) and "feat" in str(e.value)
    run_cli(["config", "--project", "chat", "--delete", "--force"], home=home, cwd=tmp_path)
    assert json.loads((home / ".claude-yolo" / "projects.json").read_text()) == {}
    # the orphaned topic degrades to dir matching + its overlay: resume still works,
    # with a warning about the dangling pointer, named after the dir again
    argv = run_cli(["resume", "feat"], home=home, cwd=app)
    assert "no longer exists" in capsys.readouterr().err
    assert argv[argv.index("--name") + 1] == "app-feat"


def test_project_config_edits_reach_live_topics(cy, run_cli, repos, tmp_path):
    # The entry is a live layer: growing its `repos` after start reaches the
    # topic at its next launch (the worktree is created then), and the guard
    # symmetry — this is exactly how a dir-matched project entry behaves.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    run_cli(["start", "feat", "--project", "chat"], home=home, cwd=app)
    assert not wt_of(cy, home, proto, "feat").exists()
    run_cli(["config", "--project", "chat", "--add-repo", str(proto)], home=home, cwd=app)
    run_cli(["resume", "feat"], home=home, cwd=app)
    assert wt_of(cy, home, proto, "feat").is_dir()


def test_start_project_overlay_keeps_cli_flags_only(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    run_cli(
        ["start", "feat", "--project", "chat", "--repo", str(proto), "--ssh-agent"],
        home=home,
        cwd=app,
    )
    overlay = read_overlay(cy, home, app, "feat")
    assert overlay["repos"] == [str(proto)]  # CLI extras only; the entry's lib is live
    assert overlay["ssh-agent"] is True
    assert overlay["project"] == "chat"
    assert wt_of(cy, home, proto, "feat").is_dir()
    assert wt_of(cy, home, lib, "feat").is_dir()  # from the live entry


def test_project_flag_guards(cy, run_cli, repos):
    app, lib, proto, home = repos
    with pytest.raises(SystemExit) as e:
        run_cli(["list", "--project", "chat"], home=home, cwd=app)
    assert "only applies" in str(e.value)
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "feat", "--project", "nope"], home=home, cwd=app)
    assert "no project 'nope'" in str(e.value)


def test_cwd_ambiguity_requires_project_flag(cy, run_cli, repos, tmp_path):
    # Two projects over the same dir: a bare start can't pick — the error names
    # them — and --project disambiguates.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    run_cli(
        ["config", "--project", "web", "--dir", str(app), "--add-repo", str(proto)],
        home=home,
        cwd=tmp_path,
    )
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "feat"], home=home, cwd=app)
    assert "chat, web" in str(e.value) and "--project" in str(e.value)
    run_cli(["start", "feat", "--project", "web"], home=home, cwd=app)
    assert wt_of(cy, home, proto, "feat").is_dir()
    assert not wt_of(cy, home, lib, "feat").exists()


def test_topic_verbs_resolve_shared_dir_by_pointer_or_flag(cy, run_cli, repos, tmp_path):
    # Two projects over the same dir: the topic verbs must still resolve — via the
    # topic's stamped `project` pointer on a bare invocation, or via an explicit
    # --project (what the wip dashboard's `d` passes) — instead of erroring as
    # ambiguous.
    app, lib, proto, home = repos
    run_cli(["config", "--project", "chat", "--dir", str(app)], home=home, cwd=tmp_path)
    run_cli(["config", "--project", "web", "--dir", str(app)], home=home, cwd=tmp_path)
    run_cli(["start", "feat", "--project", "web"], home=home, cwd=app)
    # bare `diff`: the overlay's pointer picks the entry — no ambiguity error
    run_cli(["diff", "feat"], home=home, cwd=app)
    # explicit --project also works, and retargets to the project's dir
    run_cli(["diff", "feat", "--project", "web"], home=home, cwd=tmp_path)
    run_cli(["rebase", "feat", "--project", "web"], home=home, cwd=tmp_path)
    run_cli(["finish", "feat", "--project", "web"], home=home, cwd=tmp_path)
    assert not wt_of(cy, home, app, "feat").exists()


# --- verbs across the set ------------------------------------------------------


def start_pair(cy, run_cli, repos, topic="feat"):
    app, lib, proto, home = repos
    run_cli(["start", topic, "--repo", str(lib)], home=home, cwd=app)
    return wt_of(cy, home, app, topic), wt_of(cy, home, lib, topic)


def test_finish_removes_all_worktrees_and_merged_branches(cy, run_cli, repos, capsys):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    run_cli(["finish", "feat"], home=home, cwd=app)
    out = capsys.readouterr().out
    assert "[app]" in out and "[lib]" in out
    assert not awt.exists() and not lwt.exists()
    # never diverged → reads as merged → deleted (delete-if-merged), in both repos
    assert not branch_exists(app, "feat")
    assert not branch_exists(lib, "feat")


def test_finish_blocks_on_dirty_extra_worktree(cy, run_cli, repos):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    (lwt / "junk.txt").write_text("dirty\n")
    with pytest.raises(SystemExit) as e:
        run_cli(["finish", "feat"], home=home, cwd=app)
    assert "uncommitted changes" in str(e.value) and "lib" in str(e.value)
    assert awt.is_dir() and lwt.is_dir()  # nothing was removed
    run_cli(["finish", "feat", "--force"], home=home, cwd=app)
    assert not awt.exists() and not lwt.exists()


def test_finish_skips_vanished_extra_repo(cy, run_cli, repos, capsys):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    shutil.rmtree(lib)
    run_cli(["finish", "feat"], home=home, cwd=app)
    err = capsys.readouterr().err
    assert "skipping repo" in err
    assert not awt.exists()  # the primary still finished


def test_rebase_iterates_all_repos(cy, run_cli, repos):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    for repo, fname in ((app, "a-main.txt"), (lib, "l-main.txt")):
        (repo / fname).write_text("new on main\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "advance main")
    for wt, fname in ((awt, "a-wt.txt"), (lwt, "l-wt.txt")):
        (wt / fname).write_text("on branch\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
    run_cli(["rebase", "feat"], home=home, cwd=app)
    # each worktree was replayed onto its own repo's advanced main
    assert (awt / "a-main.txt").exists()
    assert (lwt / "l-main.txt").exists()


def test_merge_lands_each_branch_in_its_own_checkout(cy, run_cli, repos, capsys):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    for wt, fname in ((awt, "a-wt.txt"), (lwt, "l-wt.txt")):
        (wt / fname).write_text("on branch\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
    run_cli(["merge", "feat"], home=home, cwd=app)
    assert (app / "a-wt.txt").exists()
    assert (lib / "l-wt.txt").exists()
    assert awt.is_dir() and lwt.is_dir()  # merge keeps worktrees + branches


def test_diff_concatenates_per_repo_with_headers(cy, run_cli, repos, capfd):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    (awt / "README").write_text("app change\n")
    (lwt / "README").write_text("lib change\n")
    capfd.readouterr()
    run_cli(["diff", "feat"], home=home, cwd=app)
    out = capfd.readouterr().out
    assert "== app ==" in out and "== lib ==" in out
    assert "app change" in out and "lib change" in out


def test_merge_this_repo_merges_only_the_cwd_repo(cy, run_cli, repos):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    for wt, fname in ((awt, "a-wt.txt"), (lwt, "l-wt.txt")):
        (wt / fname).write_text("on branch\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
    run_cli(["merge", "feat", "--this-repo"], home=home, cwd=app)
    assert (app / "a-wt.txt").exists()
    assert not (lib / "l-wt.txt").exists()  # the other repo of the set untouched
    assert awt.is_dir() and lwt.is_dir()


def test_rebase_this_repo_rebases_only_the_cwd_repo(cy, run_cli, repos):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    for repo, fname in ((app, "a-main.txt"), (lib, "l-main.txt")):
        (repo / fname).write_text("new on main\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "advance main")
    for wt, fname in ((awt, "a-wt.txt"), (lwt, "l-wt.txt")):
        (wt / fname).write_text("on branch\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
    run_cli(["rebase", "feat", "--this-repo"], home=home, cwd=app)
    assert (awt / "a-main.txt").exists()
    assert not (lwt / "l-main.txt").exists()  # the other repo's branch not replayed


def test_diff_this_repo_skips_extras_and_headers(cy, run_cli, repos, capfd):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    (awt / "README").write_text("app change\n")
    (lwt / "README").write_text("lib change\n")
    capfd.readouterr()
    run_cli(["diff", "feat", "--this-repo"], home=home, cwd=app)
    out = capfd.readouterr().out
    assert "app change" in out
    assert "lib change" not in out
    assert "== app ==" not in out  # single-repo output: no headers


# --- list across the directory's projects --------------------------------------


def _list_body(capsys, run_cli, home, cwd):
    capsys.readouterr()  # clear
    run_cli(["list"], home=home, cwd=cwd)
    return capsys.readouterr().out


def test_list_spans_the_projects_repo_set(cy, run_cli, repos, capsys):
    """A plain `yolo list` in a multi-repo project shows every repo's worktrees,
    with a REPO column, not just the primary's."""
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=app,
    )
    run_cli(["start", "feat", "--project", "chat"], home=home, cwd=app)
    out = _list_body(capsys, run_cli, home, app)
    lines = out.splitlines()
    assert lines[0].split() == ["REPO", "TOPIC", "STATUS", "COMMITS", "DIRECTORY"]
    body = "\n".join(lines[1:])
    # the same topic appears once per repo, each under its own repo/slug
    assert "app" in body and "lib" in body
    app_slug = cy._repo_root_of(app)[2]
    lib_slug = cy._repo_root_of(lib)[2]
    assert f"{app_slug}/feat" in body and f"{lib_slug}/feat" in body


def test_list_single_repo_directory_keeps_lean_table(cy, run_cli, repos, capsys):
    """No extra repos configured: the output is the plain no-REPO table it always
    was (the across-projects widening only kicks in when there's a second repo)."""
    app, lib, proto, home = repos
    run_cli(["start", "solo"], home=home, cwd=app)
    lines = _list_body(capsys, run_cli, home, app).splitlines()
    assert lines[0].split() == ["TOPIC", "STATUS", "COMMITS", "DIRECTORY"]


def test_list_unions_repos_across_projects_sharing_a_dir(cy, run_cli, repos, capsys):
    """Two projects rooted at the same dir, each naming a different extra repo:
    `list` unions the whole directory's work even though a launch there would be
    ambiguous — it must not error the way `start` does."""
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=app,
    )
    run_cli(
        ["config", "--project", "web", "--dir", str(app), "--add-repo", str(proto)],
        home=home,
        cwd=app,
    )
    run_cli(["start", "chatfeat", "--project", "chat"], home=home, cwd=app)
    run_cli(["start", "webfeat", "--project", "web"], home=home, cwd=app)
    body = _list_body(capsys, run_cli, home, app)
    # both projects' extra-repo worktrees show, plus the primary's own
    assert "lib" in body and "proto" in body
    assert "chatfeat" in body and "webfeat" in body


def test_list_judges_each_repo_against_its_own_base(cy, run_cli, repos, capsys):
    """A branch merged in its own repo reads `merged` even when the other repo's
    same-topic branch is not — each worktree judged in its own main repo."""
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)  # topic `feat` in app + lib
    # advance and merge only lib's branch into lib's main
    (lwt / "x").write_text("x")
    git(lwt, "add", ".")
    git(lwt, "commit", "-qm", "work")
    git(lib, "merge", "--no-ff", "-m", "merge feat", "feat")
    # leave app's branch committed but unmerged
    (awt / "y").write_text("y")
    git(awt, "add", ".")
    git(awt, "commit", "-qm", "work")
    out = _list_body(capsys, run_cli, home, app)
    rows = {
        cols[0]: cols[2]  # REPO -> STATUS
        for line in out.splitlines()[1:]
        if (cols := line.split()) and cols[1] == "feat"
    }
    assert rows["lib"] == "merged"
    assert rows["app"] == "unmerged"


# --- wip dashboard -------------------------------------------------------------


class FakeTerm:
    """A scripted picker terminal (test_wip.py's shape): keys, lines, confirms."""

    def __init__(self, keys, *, lines=None, confirms=None):
        self._keys = list(keys)
        self._lines = list(lines or [])
        self._confirms = list(confirms or [])

    def wait_key(self, timeout):
        return self._keys.pop(0) if self._keys else "q"

    def prompt_line(self, prompt):
        return self._lines.pop(0) if self._lines else ""

    prompt_path = prompt_line

    def confirm(self, prompt):
        return self._confirms.pop(0) if self._confirms else False


def test_wip_items_lists_named_project_rows(cy, repos, monkeypatch):
    app, lib, proto, home = repos
    (home / ".claude-yolo").mkdir(exist_ok=True)
    (home / ".claude-yolo" / "projects.json").write_text(
        json.dumps({"chat": {"dir": str(app), "repos": [str(lib), str(proto)]}})
    )
    monkeypatch.setattr(cy, "_wip_sessions", lambda home_: [])
    monkeypatch.setattr(cy, "_all_tmux_windows", lambda: {})
    monkeypatch.setattr(cy, "_worktree_rows", lambda *a, **k: [])
    items = cy._wip_items(home)["project"]
    row = next(it for it in items if it.kind == "project")
    assert row.key == "project:chat"
    assert row.cols[0] == "chat"
    assert "+2 repos" in row.cols[1]
    assert row.payload["name"] == "chat"
    assert row.payload["registered"] is True
    assert items[-1].kind == "newsession"  # the `+` row stays last


def test_wip_new_worktree_on_project_row_spawns_start_project(cy, monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda cwd, argv, name, sess: spawned.append((cwd, argv, name))
    )
    item = cy.WipItem(
        "project",
        "project:chat",
        ("chat", "~/app +1 repo"),
        {"name": "chat", "path": tmp_path, "registered": True, "window": None},
    )
    msg = cy._wip_action("n", item, tmp_path, "yolo", FakeTerm([], lines=["feat"]))
    assert spawned == [(tmp_path, ["start", "feat", "--project", "chat", "--no-tmux"], "chat-feat")]
    assert "starting worktree 'feat'" in msg


def test_wip_config_scope_targets_named_project(cy, repos):
    app, lib, proto, home = repos
    (home / ".claude-yolo").mkdir(exist_ok=True)
    (home / ".claude-yolo" / "projects.json").write_text(
        json.dumps({"chat": {"dir": str(app), "repos": [str(lib)]}})
    )
    scope = cy._config_scope("project", {"name": "chat", "path": app, "registered": True}, home)
    assert scope.store == "projects.json"
    assert scope.read() == {"dir": str(app), "repos": [str(lib)]}
    assert scope.config_args == ["config", "--project", "chat"]


def test_wip_extra_worktree_row_routes_to_primary(cy, run_cli, repos, monkeypatch):
    # The extra repo's worktree row belongs to the topic's ONE session (the
    # primary's): Enter/N resolve the primary and spawn there, named after the
    # project — never a second container named for the secondary repo.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=app,
    )
    run_cli(["start", "feat", "--project", "chat"], home=home, cwd=app)
    lib_wt = wt_of(cy, home, lib, "feat")
    p = {
        "worktree": str(lib_wt),
        "main_root": str(lib),
        "slug": lib_wt.parent.name,
        "topic": "feat",
    }
    cwd, window, label, extra = cy._wip_spawn_target("worktree", p, home)
    assert pathlib.Path(cwd).resolve() == app.resolve()  # the primary repo
    assert window == "chat-feat"  # the project's session name, not lib-feat
    # ...while the primary's own row is unaffected
    app_wt = wt_of(cy, home, app, "feat")
    p = {
        "worktree": str(app_wt),
        "main_root": str(app),
        "slug": app_wt.parent.name,
        "topic": "feat",
    }
    cwd, window, _, _ = cy._wip_spawn_target("worktree", p, home)
    assert pathlib.Path(cwd) == app and window == "chat-feat"


def test_wip_finish_on_extra_worktree_row_finishes_the_whole_set(cy, run_cli, repos):
    # `f` on an extra repo's worktree row routes to the topic's primary, so it
    # finishes every repo of the set — finishing just the extra would half-
    # dismantle the topic (and the live project entry would recreate or trip
    # over it at the next resume). r/m/d stay per-repo.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--project", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=app,
    )
    run_cli(["start", "feat", "--project", "chat"], home=home, cwd=app)
    lib_wt = wt_of(cy, home, lib, "feat")
    payload = {
        "worktree": str(lib_wt),
        "main_root": str(lib),
        "slug": lib_wt.parent.name,
        "topic": "feat",
    }
    msg = cy._wip_finish("worktree", payload, home, FakeTerm([], confirms=[True]))
    assert not lib_wt.exists()
    assert not wt_of(cy, home, app, "feat").exists()  # the primary went too
    assert "[app]" in msg and "[lib]" in msg


def test_wip_merge_on_primary_row_merges_only_that_repo(cy, run_cli, repos):
    # `m` is per-repo on EVERY row, the primary's included: the dashboard calls
    # the core with single_repo=True, so the whole-set fan-out of `yolo merge
    # TOPIC` never happens from a row.
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    for wt, fname in ((awt, "a-wt.txt"), (lwt, "l-wt.txt")):
        (wt / fname).write_text("on branch\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
    payload = {"worktree": awt, "main_root": app, "slug": awt.parent.name, "topic": "feat"}
    msg = cy._wip_merge("worktree", payload, home, FakeTerm([]))
    assert "Merged 'feat'" in msg
    assert (app / "a-wt.txt").exists()
    assert not (lib / "l-wt.txt").exists()  # the extra repo untouched


def test_wip_rebase_on_primary_row_rebases_only_that_repo(cy, run_cli, repos):
    app, lib, proto, home = repos
    awt, lwt = start_pair(cy, run_cli, repos)
    for repo, fname in ((app, "a-main.txt"), (lib, "l-main.txt")):
        (repo / fname).write_text("new on main\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "advance main")
    for wt, fname in ((awt, "a-wt.txt"), (lwt, "l-wt.txt")):
        (wt / fname).write_text("on branch\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
    payload = {"worktree": awt, "main_root": app, "slug": awt.parent.name, "topic": "feat"}
    msg = cy._wip_rebase("worktree", payload, home, FakeTerm([]))
    assert "Rebased 'feat'" in msg
    assert (awt / "a-main.txt").exists()
    assert not (lwt / "l-main.txt").exists()  # the extra repo's branch not replayed


def test_wip_extra_worktree_row_gets_primary_session_window(cy):
    # A running multi-repo session advertises its extras via the
    # yolo.extra-repos label; the extra's worktree row picks up that session's
    # window so Enter switches instead of reporting "no tmux window".
    s = cy.WipSession(
        "cid", "chat-feat", "feat", "/wt/app/feat", "", "", "1m", "waiting", 5, "", "lib-1234abcd"
    )
    windows = {"chat-feat": ("@7", "yolo")}
    assert cy._extra_session_window("lib-1234abcd", "feat", [s], windows) == "@7"
    assert cy._extra_session_window("other-slug", "feat", [s], windows) is None
    assert cy._extra_session_window("lib-1234abcd", "other", [s], windows) is None


def test_project_names_must_be_container_safe(cy, run_cli, repos, tmp_path):
    # The name IS the container/window name, so it's validated up front instead
    # of silently coerced at launch (which would desync the wip correlation).
    app, lib, proto, home = repos
    with pytest.raises(SystemExit) as e:
        run_cli(
            ["config", "--project", "bhs-cs + courses", "--dir", str(app)],
            home=home,
            cwd=tmp_path,
        )
    assert "letters, digits" in str(e.value)
    # an invalid *existing* name (hand-edited/migrated) is still addressable —
    # renaming it to a valid one is the fix
    (home / ".claude-yolo").mkdir(exist_ok=True)
    (home / ".claude-yolo" / "projects.json").write_text(
        json.dumps({"bhs-cs + courses": {"dir": str(app)}})
    )
    run_cli(
        ["config", "--project", "bhs-cs + courses", "--name", "bhs-cs"], home=home, cwd=tmp_path
    )
    assert "bhs-cs" in json.loads((home / ".claude-yolo" / "projects.json").read_text())


def test_wip_config_editor_dir_key_uses_dir_flag(cy, monkeypatch, repos):
    app, lib, proto, home = repos
    applied = []
    monkeypatch.setattr(
        cy, "_config_apply", lambda scope, flags: (applied.append(flags), (True, "ok"))[1]
    )
    scope = cy._config_scope("project", {"name": "chat", "path": app, "registered": True}, home)
    cy._config_edit_key(scope, "dir", FakeTerm([], lines=[str(lib)]))
    assert applied == [["--dir", str(lib)]]
