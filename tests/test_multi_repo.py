"""Tests for multi-repo projects.

Covers the `repos` config key (--repo / --add-repo / --remove-repo), saved
multi-repo projects (~/.claude-yolo/multirepos.json, `config --multi-repo` /
--dir, `start --multi-repo`), the multi-worktree start (creation, mounts,
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


def read_multirepos(home):
    return json.loads((home / ".claude-yolo" / "multirepos.json").read_text())


# --- config plumbing ---------------------------------------------------------


def test_config_add_and_remove_repo(cy, run_cli, repos):
    app, lib, proto, home = repos
    run_cli(["config", "--add-repo", str(lib)], home=home, cwd=app)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert projects[str(app)]["repos"] == [str(lib)]
    # adding the same path again is a no-op (matched by resolved path)
    run_cli(["config", "--add-repo", str(lib)], home=home, cwd=app)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert projects[str(app)]["repos"] == [str(lib)]
    run_cli(["config", "--remove-repo", str(lib)], home=home, cwd=app)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert "repos" not in projects[str(app)]


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
        json.dumps({str(app): {"repos": [str(lib)]}})
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
    run_cli(["config", "--multi-repo", "chat", "--add-repo", "../lib"], home=home, cwd=app)
    assert read_multirepos(home)["chat"]["repos"] == [str(lib)]


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


# --- saved multi-repo projects (multirepos.json) ------------------------------


def test_config_multirepo_creates_entry_with_inferred_dir(cy, run_cli, repos):
    app, lib, proto, home = repos
    run_cli(["config", "--multi-repo", "chat", "--add-repo", str(lib)], home=home, cwd=app)
    assert read_multirepos(home)["chat"] == {"dir": str(app), "repos": [str(lib)]}


def test_config_multirepo_dir_required_outside_a_repo(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(SystemExit) as e:
        run_cli(["config", "--multi-repo", "chat", "--add-repo", str(lib)], home=home, cwd=outside)
    assert "--dir" in str(e.value)
    run_cli(
        ["config", "--multi-repo", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=outside,
    )
    assert read_multirepos(home)["chat"]["dir"] == str(app)


def test_config_multirepo_dir_normalizes_to_repo_root_and_rejects_non_repo(
    cy, run_cli, repos, tmp_path
):
    app, lib, proto, home = repos
    sub = app / "src"
    sub.mkdir()
    run_cli(["config", "--multi-repo", "chat", "--dir", str(sub)], home=home, cwd=tmp_path)
    assert read_multirepos(home)["chat"]["dir"] == str(app)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SystemExit) as e:
        run_cli(["config", "--multi-repo", "bad", "--dir", str(plain)], home=home, cwd=tmp_path)
    assert "not a git repository" in str(e.value)


def test_start_multi_repo_launches_from_saved_dir_anywhere(
    cy, run_cli, repos, tmp_path, flag_values
):
    app, lib, proto, home = repos
    run_cli(
        ["config", "--multi-repo", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    argv = run_cli(["start", "feat", "--multi-repo", "chat"], home=home, cwd=elsewhere)
    assert wt_of(cy, home, app, "feat").is_dir()
    assert wt_of(cy, home, lib, "feat").is_dir()
    assert flag_values(argv, "-w") == [str(wt_of(cy, home, app, "feat"))]
    # the saved keys — plus the project's name — are stamped into the overlay:
    # the topic is self-describing
    assert read_overlay(cy, home, app, "feat") == {"name": "chat", "repos": [str(lib)]}


def test_multi_repo_sessions_are_named_after_the_project(cy, run_cli, repos, tmp_path):
    # Container/session names derive from the saved project's NAME, not the primary
    # repo's basename — the `wip` dashboard correlates sessions to tmux windows by
    # that name, and its `n` key names the spawned window NAME-TOPIC.
    app, lib, proto, home = repos
    run_cli(
        ["config", "--multi-repo", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    argv = run_cli(["start", "feat", "--multi-repo", "chat"], home=home, cwd=tmp_path)
    assert argv[argv.index("--name") + 1] == "chat-feat"  # docker --name
    img = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    cargs = argv[img + 1 :]  # what's passed to claude
    assert cargs[cargs.index("--name") + 1] == "chat:feat"  # claude session name
    # resume re-resolves the name from the overlay stamp alone: deleting the saved
    # entry doesn't rename (or otherwise change) the live topic
    (home / ".claude-yolo" / "multirepos.json").unlink()
    argv = run_cli(["resume", "feat"], home=home, cwd=app)
    assert argv[argv.index("--name") + 1] == "chat-feat"


def test_config_multirepo_rejects_name(cy, run_cli, repos, tmp_path):
    # The saved NAME *is* the project's name; a divergent stored `name` key could
    # never take effect (start injects NAME over it), so refuse to store one.
    app, lib, proto, home = repos
    with pytest.raises(SystemExit) as e:
        run_cli(
            ["config", "--multi-repo", "chat", "--dir", str(app), "--name", "other"],
            home=home,
            cwd=tmp_path,
        )
    assert "--name can't combine with --multi-repo" in str(e.value)


def test_saved_config_edit_never_changes_a_live_topic(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    run_cli(
        ["config", "--multi-repo", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    run_cli(["start", "feat", "--multi-repo", "chat"], home=home, cwd=app)
    # growing the saved config after start must not affect the live topic
    run_cli(["config", "--multi-repo", "chat", "--add-repo", str(proto)], home=home, cwd=app)
    run_cli(["resume", "feat"], home=home, cwd=app)
    assert not wt_of(cy, home, proto, "feat").exists()


def test_start_multi_repo_overlay_merges_saved_and_cli(cy, run_cli, repos, tmp_path):
    app, lib, proto, home = repos
    run_cli(
        ["config", "--multi-repo", "chat", "--dir", str(app), "--add-repo", str(lib)],
        home=home,
        cwd=tmp_path,
    )
    run_cli(
        ["start", "feat", "--multi-repo", "chat", "--repo", str(proto), "--ssh-agent"],
        home=home,
        cwd=app,
    )
    overlay = read_overlay(cy, home, app, "feat")
    assert overlay["repos"] == [str(lib), str(proto)]  # saved first, CLI appended
    assert overlay["ssh-agent"] is True
    assert wt_of(cy, home, proto, "feat").is_dir()


def test_multi_repo_flag_guards(cy, run_cli, repos):
    app, lib, proto, home = repos
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "--multi-repo", "chat"], home=home, cwd=app)
    assert "needs a topic" in str(e.value)
    with pytest.raises(SystemExit) as e:
        run_cli(["resume", "feat", "--multi-repo", "chat"], home=home, cwd=app)
    assert "only applies" in str(e.value)
    with pytest.raises(SystemExit) as e:
        run_cli(["start", "feat", "--multi-repo", "nope"], home=home, cwd=app)
    assert "no multi-repo project 'nope'" in str(e.value)


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


def test_wip_items_lists_saved_multirepo_rows(cy, repos, monkeypatch):
    app, lib, proto, home = repos
    (home / ".claude-yolo").mkdir(exist_ok=True)
    (home / ".claude-yolo" / "multirepos.json").write_text(
        json.dumps({"chat": {"dir": str(app), "repos": [str(lib), str(proto)]}})
    )
    monkeypatch.setattr(cy, "_wip_sessions", lambda home_: [])
    monkeypatch.setattr(cy, "_all_tmux_windows", lambda: {})
    monkeypatch.setattr(cy, "_worktree_rows", lambda *a, **k: [])
    monkeypatch.setattr(cy, "_wip_projects", lambda home_, sessions: [])
    items = cy._wip_items(home)["project"]
    row = next(it for it in items if it.kind == "multirepo")
    assert row.key == "multirepo:chat"
    assert row.cols[0] == "chat"
    assert "+2 repos" in row.cols[1]
    assert row.payload["name"] == "chat"
    assert items[-1].kind == "newsession"  # the `+` row stays last


def test_wip_new_worktree_on_multirepo_row_spawns_start_multi_repo(cy, monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(
        cy, "_spawn_session_window", lambda cwd, argv, name, sess: spawned.append((cwd, argv, name))
    )
    item = cy.WipItem(
        "multirepo", "multirepo:chat", ("chat", "~/app +1 repo"), {"name": "chat", "path": tmp_path}
    )
    msg = cy._wip_action("n", item, tmp_path, "yolo", FakeTerm([], lines=["feat"]))
    assert spawned == [
        (tmp_path, ["start", "feat", "--multi-repo", "chat", "--no-tmux"], "chat-feat")
    ]
    assert "multi-repo worktree" in msg


def test_wip_add_multirepo_creates_entry_and_opens_editor(cy, monkeypatch, repos):
    app, lib, proto, home = repos
    applied = []
    monkeypatch.setattr(
        cy, "_config_apply", lambda scope, flags: (applied.append((scope, flags)), (True, "ok"))[1]
    )
    opened = []
    monkeypatch.setattr(
        cy, "_config_editor_loop", lambda scope, term: (opened.append(scope), "edited")[1]
    )
    # `a` → picker (j to "multi-repo project", Enter), then name + primary path
    term = FakeTerm(["j", "\r"], lines=["chat", str(app)])
    msg = cy._wip_add_project(home, term)
    assert applied[0][0].config_args == ["config", "--multi-repo", "chat"]
    assert applied[0][1] == ["--dir", str(app)]
    assert opened and msg == "edited"


def test_wip_config_scope_reads_multirepos_entry(cy, repos):
    app, lib, proto, home = repos
    (home / ".claude-yolo").mkdir(exist_ok=True)
    (home / ".claude-yolo" / "multirepos.json").write_text(
        json.dumps({"chat": {"dir": str(app), "repos": [str(lib)]}})
    )
    scope = cy._config_scope("multirepo", {"name": "chat", "path": app}, home)
    assert scope.store == "multirepos.json"
    assert scope.read() == {"dir": str(app), "repos": [str(lib)]}
    assert scope.config_args == ["config", "--multi-repo", "chat"]


def test_wip_config_editor_dir_key_uses_dir_flag(cy, monkeypatch, repos):
    app, lib, proto, home = repos
    applied = []
    monkeypatch.setattr(
        cy, "_config_apply", lambda scope, flags: (applied.append(flags), (True, "ok"))[1]
    )
    scope = cy._config_scope("multirepo", {"name": "chat", "path": app}, home)
    cy._config_edit_key(scope, "dir", FakeTerm([], lines=[str(lib)]))
    assert applied == [["--dir", str(lib)]]
