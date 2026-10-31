"""Tests for the `doctor` verb: the host-side consistency audit, its `--fix`,
and its `--clean`.

Each check is exercised against a planted inconsistency under an isolated HOME
(and, for the worktree checks, a real throwaway git repo). The environment probe
is stubbed out — it shells out to docker/git/tmux — so the docker-backed checks
(run dir, images) read as skipped; `_doctor_images` is covered directly with a
canned `subprocess.run`.
"""

import datetime
import json
import os
import shutil
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
def no_environment_probe(cy, monkeypatch):
    """Skip the docker/git/tmux probe (it shells out); docker reads as unreachable."""
    monkeypatch.setattr(cy, "_doctor_environment", lambda rep: False)
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: None)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def doctor(run_cli, home, cwd, capsys, *flags):
    """Run `yolo doctor [flags]`; return (exit code, stdout)."""
    capsys.readouterr()
    code = 0
    try:
        run_cli(["doctor", *flags], home=home, cwd=cwd)
    except SystemExit as e:
        code = e.code
    return code, capsys.readouterr().out


# --- clean state ---------------------------------------------------------------


def test_clean_state_reports_no_problems_and_exits_zero(cy, run_cli, repo, capsys):
    r, home = repo
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "PROBLEM" not in out
    # nothing to report: just the summary, pointing at --verbose
    assert out.strip() == "0 problems, 0 notes (`--verbose` lists every check)."


def test_verbose_lists_every_section_with_its_ok_line(cy, run_cli, repo, capsys):
    r, home = repo
    code, out = doctor(run_cli, home, r, capsys, "--verbose")
    assert code == 0
    for header in ("projects (", "worktree overlays (", "worktrees (", "tokens (", "secrets ("):
        assert header in out
    assert "  ok       none" in out
    assert "0 problems, 0 notes." in out


def test_default_report_shows_only_sections_with_findings(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(home / ".claude-yolo" / "projects.json", {"ghost": {"dir": "/no/such/dir"}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert out.startswith("projects (")
    assert "project 'ghost': dir no longer exists" in out
    for header in ("worktree overlays (", "worktrees (", "tokens (", "secrets ("):
        assert header not in out
    assert "  ok " not in out
    assert "`--verbose`" not in out


def test_fix_flag_only_applies_to_doctor(cy, run_cli, repo):
    r, home = repo
    with pytest.raises(SystemExit) as exc:
        run_cli(["list", "--fix"], home=home, cwd=r)
    assert "--fix only applies to `doctor`" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        run_cli(["list", "--clean"], home=home, cwd=r)
    assert "--clean only applies to `doctor`" in str(exc.value)


# --- --clean: config entries naming paths that no longer exist -------------------------


def test_clean_drops_dangling_config_entries_fix_does_not(cy, run_cli, repo, capsys):
    """Every dangling path-valued entry is a `--clean` (never a `--fix`): the
    spec goes, the rest of the list stays, an emptied key is dropped, a bad
    dockerfile is unset; the global layer's bare-string list and its illegal
    `repos` are handled too. A referenced-but-unstored secret is never cleaned."""
    r, home = repo
    pfile = home / ".claude-yolo" / "projects.json"
    write_json(
        pfile,
        {
            "repo": {
                "dir": str(r),
                "mounts": [f"{r}:ro", "/no/such/mount:ro"],
                "plugin-dirs": ["/no/such/plugin"],
                "repos": [str(r.parent)],  # exists but isn't a git repo
                "secrets": ["API_KEY", "bad spec!"],
                "dockerfile": "/no/such/Dockerfile.yolo",
            }
        },
    )
    write_json(home / ".yolo.json", {"repos": [str(r)], "mounts": "/no/such/global"})
    before = pfile.read_text()
    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert code == 1
    assert pfile.read_text() == before  # --fix touches none of it
    assert out.count("--clean would:") == 7
    assert "can be cleaned up: re-run with `yolo doctor --clean`" in out

    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 1  # the unstored secret remains a problem
    assert out.count("  cleaned ") == 7
    entry = json.loads(pfile.read_text())["repo"]
    assert entry["mounts"] == [f"{r}:ro"]
    assert entry["secrets"] == ["API_KEY"]
    for key in ("plugin-dirs", "repos", "dockerfile"):
        assert key not in entry
    assert json.loads((home / ".yolo.json").read_text()) == {}
    assert "secret API_KEY is referenced but not stored" in out
    assert "--clean would" not in out

    code, out = doctor(run_cli, home, r, capsys)
    assert "mount source" not in out and "plugin dir" not in out and "dockerfile" not in out


def test_clean_edits_the_overlay_layer_too(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    wt_file = home / ".claude-yolo" / "worktrees.json"
    overlays = json.loads(wt_file.read_text())
    (key,) = overlays
    overlays[key] = {"mounts": ["/no/such:rw"], "ssh-agent": True}
    wt_file.write_text(json.dumps(overlays))
    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 0
    assert json.loads(wt_file.read_text())[key] == {"ssh-agent": True}


# --- projects.json ------------------------------------------------------------------


def test_dangling_project_dir_is_a_problem_with_hints(cy, run_cli, repo, capsys):
    """The launch-time 'dir no longer exists' warning, as a doctor finding."""
    r, home = repo
    write_json(
        home / ".claude-yolo" / "projects.json",
        {"ghost": {"dir": str(r.parent / "moved-away")}, "repo": {"dir": str(r)}},
    )
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "project 'ghost': dir no longer exists" in out
    assert "yolo config --project ghost --dir ." in out
    assert "yolo config --project ghost --delete" in out
    assert "'repo'" not in out.split("projects (")[1].split("\n\n")[0]  # the good one is silent


def test_fix_never_deletes_a_project_entry(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(home / ".claude-yolo" / "projects.json", {"ghost": {"dir": "/no/such/dir"}})
    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert code == 1
    assert "ghost" in json.loads((home / ".claude-yolo" / "projects.json").read_text())


def test_project_config_keys_missing_targets(cy, run_cli, repo, capsys):
    """A missing mount source, plugin dir, repo, and an unstored secret each flag."""
    r, home = repo
    write_json(
        home / ".claude-yolo" / "projects.json",
        {
            "repo": {
                "dir": str(r),
                "mounts": ["/no/such/mount:ro"],
                "plugin-dirs": ["/no/such/plugin"],
                "repos": [str(r.parent)],  # exists but isn't a git repo
                "secrets": ["API_KEY"],
            }
        },
    )
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "mount source does not exist: /no/such/mount" in out
    assert "--remove-mount /no/such/mount:ro" in out
    assert "plugin dir does not exist: /no/such/plugin" in out
    assert "repo is not a git repository" in out
    assert "secret API_KEY is referenced but not stored" in out
    assert "--project-scope" in out


def test_referenced_secret_resolves_at_global_or_project_scope(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(
        home / ".claude-yolo" / "projects.json",
        {"repo": {"dir": str(r), "secrets": ["API_KEY", "DB_URL"]}},
    )
    os.environ["HOME"] = str(home)  # the store helpers key off Path.home()
    svc = cy._secret_service("API_KEY", "global")
    cy._store_secret_value(svc, "x")
    cy._write_secret_entry(svc, "global", "API_KEY", None)
    psvc = cy._secret_service("DB_URL", "project", str(r))
    cy._store_secret_value(psvc, "y")
    cy._write_secret_entry(psvc, "project", "DB_URL", str(r))
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0, out
    assert "referenced but not stored" not in out


def test_projects_sharing_a_dir_is_a_note(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(
        home / ".claude-yolo" / "projects.json",
        {"aa": {"dir": str(r)}, "bb": {"dir": str(r)}},
    )
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0, out
    assert "2 projects share" in out and "aa, bb" in out and "--project NAME" in out


def test_malformed_projects_json_is_reported_not_fatal(cy, run_cli, repo, capsys):
    r, home = repo
    (home / ".claude-yolo").mkdir()
    (home / ".claude-yolo" / "projects.json").write_text("{not json")
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "cannot read projects config" in out
    code, out = doctor(run_cli, home, r, capsys, "-v")
    assert "tokens (" in out  # the later sections still ran


def test_invalid_project_name(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(home / ".claude-yolo" / "projects.json", {"bad name!": {"dir": str(r)}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "not a valid name" in out and "--name NEW" in out


# --- ~/.yolo.json ---------------------------------------------------------------------


def test_global_config_checks(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(
        home / ".yolo.json",
        {"config-dir": "/no/such/cfg", "repos": [str(r)], "dockerfile": "/no/such/Dockerfile"},
    )
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "config-dir is not a directory: /no/such/cfg" in out
    assert "`repos` can't be set globally" in out
    assert "dockerfile: not a file: /no/such/Dockerfile" in out


def test_global_config_unknown_key(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(home / ".yolo.json", {"bogus": 1})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "unknown config option 'bogus'" in out


# --- worktrees.json overlays ---------------------------------------------------------


def test_stale_overlay_is_fixed(cy, run_cli, repo, capsys):
    r, home = repo
    gone = home / ".claude-yolo" / "worktrees" / "some-slug" / "gone"
    write_json(home / ".claude-yolo" / "worktrees.json", {str(gone): {"ports": ["3000"]}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "entry for a worktree that no longer exists" in out
    assert "--fix would: drop the entry" in out

    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert code == 0
    assert "fixed" in out and "1 fixed" in out
    assert json.loads((home / ".claude-yolo" / "worktrees.json").read_text()) == {}


def test_dangling_project_pointer_is_fixed_keeping_the_overlay(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    wt_file = home / ".claude-yolo" / "worktrees.json"
    overlays = json.loads(wt_file.read_text())
    (key,) = overlays
    overlays[key] = {"project": "nope", "ports": ["3000"]}
    wt_file.write_text(json.dumps(overlays))

    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "topic 'alpha' points at project 'nope', which no longer exists" in out
    assert "--unset project" in out

    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert code == 0
    assert json.loads(wt_file.read_text()) == {key: {"ports": ["3000"]}}


def test_overlay_config_keys_checked_against_the_worktree(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    wt_file = home / ".claude-yolo" / "worktrees.json"
    overlays = json.loads(wt_file.read_text())
    (key,) = overlays
    overlays[key] = {"dockerfile": "Dockerfile.yolo", "mounts": ["/no/such:rw"]}
    wt_file.write_text(json.dumps(overlays))
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "topic 'alpha': dockerfile: not a file: Dockerfile.yolo" in out
    assert "topic 'alpha': mount source does not exist: /no/such" in out
    assert f"cd {r} && yolo config alpha --remove-mount" in out


# --- ~/.claude-yolo/worktrees/ ----------------------------------------------------------


def test_orphaned_worktree_is_a_problem(cy, run_cli, repo, tmp_path, capsys):
    r, home = repo
    run_cli(["start", "beta"], home=home, cwd=r)
    r.rename(tmp_path / "repo-moved")  # orphan beta's worktree
    code, out = doctor(run_cli, home, tmp_path, capsys)
    assert code == 1
    assert "orphaned worktree" in out and "/beta" in out
    assert "(repo)" in out  # the repo name recovered from the .git pointer
    assert "git worktree repair" in out


def test_worktree_removed_by_hand_is_pruned_by_fix(cy, run_cli, repo, capsys):
    """rm -rf of a worktree leaves git's admin entry and the overlay behind."""
    r, home = repo
    run_cli(["start", "beta"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("beta"))
    shutil.rmtree(wt)
    assert str(wt) in git(r, "worktree", "list").stdout

    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "git still lists a worktree whose directory is gone" in out
    assert "entry for a worktree that no longer exists" in out  # the overlay too

    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert code == 0
    assert "3 fixed" in out  # the overlay, the prune, and the now-empty slug dir
    assert str(wt) not in git(r, "worktree", "list").stdout
    assert not wt.parent.exists()
    assert json.loads((home / ".claude-yolo" / "worktrees.json").read_text()) == {}


def test_empty_slug_dir_is_a_fixable_note(cy, run_cli, repo, capsys):
    r, home = repo
    empty = home / ".claude-yolo" / "worktrees" / "old-slug"
    empty.mkdir(parents=True)
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "empty repo dir: old-slug/" in out and "--fix would: remove it" in out
    doctor(run_cli, home, r, capsys, "--fix")
    assert not empty.exists()


def test_worktree_under_wrong_slug(cy, run_cli, repo, tmp_path, capsys):
    """A repo moved + `git worktree repair` leaves the worktree filed under the
    old path's slug, where `yolo resume` from the new path won't look."""
    r, home = repo
    run_cli(["start", "beta"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("beta"))
    moved = tmp_path / "repo-moved"
    r.rename(moved)
    git(moved, "worktree", "repair", str(wt))
    code, out = doctor(run_cli, home, tmp_path, capsys)
    assert code == 1
    assert "belongs to" in out and "whose worktrees live under" in out
    assert cy._repo_root_of(moved)[2] in out  # the slug it should live under
    assert "--clean would" not in out  # docker unreachable: can't rule out a session


def test_clean_refiles_worktree_under_the_right_slug(cy, run_cli, repo, tmp_path):
    """With docker answering and no session in it, --clean moves the worktree
    under the repo's current slug, repairs the link, and re-keys its overlay."""
    r, home = repo
    run_cli(["start", "beta"], home=home, cwd=r)
    wt = next((home / ".claude-yolo" / "worktrees").rglob("beta"))
    wt_file = home / ".claude-yolo" / "worktrees.json"
    write_json(wt_file, {str(wt.resolve()): {"ssh-agent": True}})
    moved = tmp_path / "repo-moved"
    r.rename(moved)
    git(moved, "worktree", "repair", str(wt))
    rep = cy._DoctorReport()
    cy._doctor_worktree_dirs(rep, home, tmp_path, {}, True)
    (item,) = [it for it in rep.items if it.level == "problem"]
    assert item.clean and item.fix is not None
    assert rep.apply_fixes(clean=True) == []
    dest = home / ".claude-yolo" / "worktrees" / cy._repo_root_of(moved)[2] / "beta"
    assert dest.is_dir() and not wt.exists()
    assert str(dest) in git(moved, "worktree", "list").stdout
    assert git(dest, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "beta"
    assert json.loads(wt_file.read_text()) == {str(dest.resolve()): {"ssh-agent": True}}


def test_clean_deletes_stray_files_in_the_worktrees_dir(cy, run_cli, repo, capsys):
    r, home = repo
    run_cli(["start", "alpha"], home=home, cwd=r)
    root = home / ".claude-yolo" / "worktrees"
    top = root / "notes.txt"
    top.write_text("x")
    inner = next(root.iterdir() if False else (d for d in root.iterdir() if d.is_dir())) / "stray"
    inner.write_text("y")
    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert top.exists() and inner.exists()
    assert out.count("--clean would: delete it") == 2
    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 0
    assert not top.exists() and not inner.exists()


# --- recent-projects.json ----------------------------------------------------------------


def test_vanished_recent_project_is_a_fixable_note(cy, run_cli, repo, capsys):
    r, home = repo
    path = home / ".claude-yolo" / "recent-projects.json"
    write_json(path, {"/no/such/dir": {"last_opened": "2026-01-01T00:00:00+00:00"}, str(r): {}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "/no/such/dir: directory no longer exists" in out
    doctor(run_cli, home, r, capsys, "--fix")
    assert json.loads(path.read_text()) == {str(r): {}}


# --- tokens.json vs the store ----------------------------------------------------------------


def _minted(days_ago):
    return (datetime.datetime.now().astimezone() - datetime.timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )


def test_stale_token_registry_row_is_fixed(cy, run_cli, repo, capsys):
    r, home = repo
    path = home / ".claude-yolo" / "tokens.json"
    write_json(path, {cy.OAUTH_KC_SERVICE: {"config_dir": None, "minted": _minted(10)}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "registry entry but no token in the credential store" in out
    assert "yolo setup-token" in out
    doctor(run_cli, home, r, capsys, "--fix")
    assert json.loads(path.read_text()) == {}


def test_expired_and_expiring_tokens(cy, run_cli, repo, tmp_path, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    cfg = tmp_path / "altcfg"
    cfg.mkdir()
    alt = cy._oauth_service(str(cfg))
    cy._cred_set(cy.OAUTH_KC_SERVICE, "sk-ant-oat-old")
    cy._cred_set(alt, "sk-ant-oat-soon")
    write_json(
        home / ".claude-yolo" / "tokens.json",
        {
            cy.OAUTH_KC_SERVICE: {"config_dir": None, "minted": _minted(400)},
            alt: {"config_dir": str(cfg), "minted": _minted(cy.TOKEN_LIFETIME_DAYS - 3)},
        },
    )
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "~/.claude: token past its estimated expiry" in out
    assert "token expires in about" in out and f"--config-dir {cfg}" in out


def test_token_without_registry_row_is_a_note(cy, run_cli, repo, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    cy._cred_set(cy.OAUTH_KC_SERVICE, "sk-ant-oat-legacy")  # store only, no tokens.json
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "a token is cached but has no registry entry" in out


def test_token_for_vanished_config_dir_is_a_note(cy, run_cli, repo, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    svc = cy._oauth_service("/no/such/cfg")
    cy._cred_set(svc, "sk-ant-oat-x")
    write_json(
        home / ".claude-yolo" / "tokens.json",
        {svc: {"config_dir": "/no/such/cfg", "minted": _minted(1)}},
    )
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "config dir no longer exists but its token is still cached" in out
    assert "yolo forget-token --config-dir /no/such/cfg" in out
    assert "--clean would: forget it" in out


def test_clean_forgets_tokens_for_vanished_dirs_and_past_expiry(cy, run_cli, repo, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    gone = cy._oauth_service("/no/such/cfg")
    cy._cred_set(gone, "sk-ant-oat-gone")
    cy._cred_set(cy.OAUTH_KC_SERVICE, "sk-ant-oat-old")
    write_json(
        home / ".claude-yolo" / "tokens.json",
        {
            gone: {"config_dir": "/no/such/cfg", "minted": _minted(1)},
            cy.OAUTH_KC_SERVICE: {"config_dir": None, "minted": _minted(400)},
        },
    )
    doctor(run_cli, home, r, capsys, "--fix")
    assert cy._keychain_has(gone) and cy._keychain_has(cy.OAUTH_KC_SERVICE)
    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 0
    assert out.count("  cleaned ") == 2
    assert not cy._keychain_has(gone) and not cy._keychain_has(cy.OAUTH_KC_SERVICE)
    assert json.loads((home / ".claude-yolo" / "tokens.json").read_text()) == {}


# --- secrets.json vs the store -------------------------------------------------------------------


def test_stale_secret_registry_row_is_fixed(cy, run_cli, repo, capsys):
    r, home = repo
    path = home / ".claude-yolo" / "secrets.json"
    svc = cy._secret_service("API_KEY", "global")
    write_json(path, {svc: {"scope": "global", "name": "API_KEY", "project_key": None}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert "API_KEY (global): registry entry but no value" in out
    doctor(run_cli, home, r, capsys, "--fix")
    assert json.loads(path.read_text()) == {}


def test_project_secret_for_vanished_dir_is_a_note(cy, run_cli, repo, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    svc = cy._secret_service("DB", "project", "/no/such/proj")
    cy._store_secret_value(svc, "v")
    cy._write_secret_entry(svc, "project", "DB", "/no/such/proj")
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "DB: project-scope secret for /no/such/proj, which no longer exists" in out
    assert "--clean would: remove it" in out


def test_clean_removes_project_secret_for_vanished_dir(cy, run_cli, repo, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    svc = cy._secret_service("DB", "project", "/no/such/proj")
    cy._store_secret_value(svc, "v")
    cy._write_secret_entry(svc, "project", "DB", "/no/such/proj")
    keep = cy._secret_service("DB", "global")
    cy._store_secret_value(keep, "g")
    cy._write_secret_entry(keep, "global", "DB", None)
    doctor(run_cli, home, r, capsys, "--fix")
    assert cy._keychain_has(svc)
    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 0 and "  cleaned " in out
    assert not cy._keychain_has(svc) and svc not in cy._read_secrets_file()
    assert cy._keychain_has(keep) and keep in cy._read_secrets_file()  # the global one stays


# --- the file credential store --------------------------------------------------------------------


def test_cred_file_permissions_and_unknown_files(cy, run_cli, repo, capsys):
    r, home = repo
    os.environ["HOME"] = str(home)
    cy._cred_set(cy.OAUTH_KC_SERVICE, "tok")
    cy._write_token_entry(None)
    known = cy._cred_file_path(cy.OAUTH_KC_SERVICE)
    known.chmod(0o644)
    stray = known.parent / "deadbeef.cred"
    stray.write_text("?")
    stray.chmod(0o600)
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert f"{known.name} is mode 644; should be 600" in out
    assert "deadbeef.cred: in neither tokens.json nor secrets.json" in out
    doctor(run_cli, home, r, capsys, "--fix")
    assert known.stat().st_mode & 0o777 == 0o600
    assert stray.exists()  # never deleted automatically


# --- docker images (direct, with a canned docker) -----------------------------------------------


def test_stale_images_noted(cy, monkeypatch):
    uid = os.getuid()
    current = cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)
    custom_text = "FROM ${YOLO_BASE}\nRUN true\n"
    custom = cy._custom_image_tag(custom_text, uid)

    def fake_run(cmd, **kw):
        if cmd[:3] == ["docker", "image", "ls"]:
            out = "\n".join(
                f"{t}\t1.2GB\t3 days ago" for t in (current, custom, "deadbeef", "inuse123")
            )
        elif cmd[:2] == ["docker", "ps"]:
            out = f"{cy.DOCKER_IMAGE_REPO}:inuse123\n"
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    rep = cy._DoctorReport()
    cy._doctor_images(rep, True, {"dockerfiles": [custom_text]})
    assert len(rep.items) == 1
    item = rep.items[0]
    assert item.level == "note"
    assert item.text.startswith("1 image no configured Dockerfile builds")
    assert f"{cy.DOCKER_IMAGE_REPO}:deadbeef" in item.text and "1.2GB" in item.text
    for tag in (current, custom, "inuse123"):
        assert f"{cy.DOCKER_IMAGE_REPO}:{tag}" not in item.text
    assert item.hint.startswith(f"`docker rmi {cy.DOCKER_IMAGE_REPO}:deadbeef`")
    assert item.fix is not None


def test_stale_images_one_note_and_clean_removes_them(cy, monkeypatch):
    """Several stale images collapse to one note with one combined `docker rmi`,
    and --clean (not --fix) runs exactly that command; current and in-use tags
    are untouched."""
    uid = os.getuid()
    current = cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)
    calls = []

    def fake_run(cmd, **kw):
        if cmd[:3] == ["docker", "image", "ls"]:
            out = "\n".join(
                f"{t}\t{s}\t{a} ago"
                for t, s, a in (
                    (current, "2.1GB", "2 days"),
                    ("aaaa1111", "2.1GB", "3 weeks"),
                    ("bbbb2222", "900MB", "2 months"),
                    ("inuse123", "2.1GB", "4 months"),
                )
            )
        elif cmd[:2] == ["docker", "ps"]:
            out = f"{cy.DOCKER_IMAGE_REPO}:inuse123\n"
        elif cmd[:2] == ["docker", "rmi"]:
            calls.append(cmd)
            out = ""
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    rep = cy._DoctorReport()
    cy._doctor_images(rep, True, {"dockerfiles": []})
    assert len(rep.items) == 1
    item = rep.items[0]
    a, b = f"{cy.DOCKER_IMAGE_REPO}:aaaa1111", f"{cy.DOCKER_IMAGE_REPO}:bbbb2222"
    assert item.text.startswith("2 images no configured Dockerfile builds")
    assert item.hint.startswith(f"`docker rmi {a} {b}`")
    rendered = "\n".join(rep.render(fixing=False))
    assert "--clean would: remove them" in rendered
    assert rep.apply_fixes() == [] and calls == []  # --fix alone leaves images be
    assert rep.items[0].level == "note"
    assert "--clean would: remove them" in "\n".join(rep.render(fixing=True))
    assert rep.apply_fixes(clean=True) == []
    assert calls == [["docker", "rmi", a, b]]
    assert rep.items[0].level == "cleaned"
    assert "  cleaned  2 images" in "\n".join(rep.render(fixing=True, cleaning=True))


def test_no_stale_images_no_note(cy, monkeypatch):
    uid = os.getuid()
    current = cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)

    def fake_run(cmd, **kw):
        if cmd[:3] == ["docker", "image", "ls"]:
            out = f"{current}\t2.1GB\t2 days ago\n"
        elif cmd[:2] == ["docker", "ps"]:
            out = ""
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    rep = cy._DoctorReport()
    cy._doctor_images(rep, True, {"dockerfiles": []})
    assert rep.items == []
    assert rep.render(fixing=False) == []
    assert "every one current" in "\n".join(rep.render(fixing=False, verbose=True))


def test_custom_image_tag_matches_build_image_scheme(cy):
    """The doctor's expected-tag computation is the one `_build_image` uses."""
    uid = os.getuid()
    plain = "FROM ubuntu\n"
    assert cy._custom_image_tag(plain, uid) == cy._image_tag(plain, uid)
    layered = "FROM ${YOLO_BASE}\n"
    base_tag = cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)
    assert cy._custom_image_tag(layered, uid) == cy._image_tag(layered + base_tag, uid)


# --- leftovers ------------------------------------------------------------------------------------


def test_in_directory_yolo_json_is_a_problem(cy, run_cli, repo, capsys):
    r, home = repo
    (r / ".yolo.json").write_text("{}")
    (r / "sub").mkdir()
    code, out = doctor(run_cli, home, r / "sub", capsys)  # found walking up from a subdir
    assert code == 1
    assert "an in-directory .yolo.json is no longer read" in out


def test_macos_credentials_file_is_cleaned(cy, run_cli, repo, capsys, monkeypatch):
    r, home = repo
    monkeypatch.setattr(cy, "_is_macos", lambda: True)
    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text("{}")
    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert code == 1 and creds.exists()
    assert "--clean would: delete it" in out
    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 0 and not creds.exists()


def test_migration_backup_is_a_fixable_note(cy, run_cli, repo, capsys):
    r, home = repo
    bak = home / ".claude-yolo" / "projects.json.bak"
    write_json(bak, {})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "backup left by the projects.json migration" in out
    doctor(run_cli, home, r, capsys, "--fix")
    assert not bak.exists()


def test_docker_unreachable_skips_docker_checks(cy, run_cli, repo, capsys):
    """The skip is informational (the environment section carries the problem),
    so it shows only under --verbose and doesn't count as a note."""
    r, home = repo
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 0
    assert "skipped" not in out
    assert "0 notes" in out
    code, out = doctor(run_cli, home, r, capsys, "-v")
    assert code == 0
    assert "  ok       skipped: docker isn't answering" in out


def test_render_wraps_to_width_without_splitting_words(cy):
    rep = cy._DoctorReport()
    rep.start("sec", "")
    long_hint = "run `yolo config --project ghost --dir .` from its new location, or delete it"
    rep.problem("sec", "project 'ghost': dir no longer exists: ~/work/ghost", long_hint)
    rep.note(
        "sec",
        "2 images stale:\n             claude-yolo:aaaa1111   2.1GB  built 3 weeks ago",
        "`docker rmi claude-yolo:aaaa1111 claude-yolo:bbbb2222 claude-yolo:cccc3333` reclaims",
        fix=lambda: None,
        fix_label="remove them",
    )
    unwrapped = rep.render(fixing=False)
    assert any(len(line) > 60 for line in unwrapped)
    wrapped = rep.render(fixing=False, width=60)
    assert all(len(line) <= 60 for line in wrapped)
    assert "\n".join(wrapped).replace("\n", " ").split() == "\n".join(unwrapped).split()
    # continuation lines sit under the text column; the image row keeps its own
    assert wrapped[1].startswith("  PROBLEM  ") and wrapped[2].startswith("           ")
    assert any(line.startswith("             claude-yolo:aaaa1111") for line in wrapped)
    # a word longer than the width is never split
    narrow = rep.render(fixing=False, width=20)
    assert any("claude-yolo:aaaa1111" in line for line in narrow)
    # None keeps the raw lines, and a pipe is what do_doctor passes then
    assert rep.render(fixing=False, width=None) == unwrapped


def test_summary_counts_fixable_items(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(home / ".claude-yolo" / "recent-projects.json", {"/gone": {}})
    write_json(home / ".claude-yolo" / "projects.json", {"ghost": {"dir": "/gone"}})
    code, out = doctor(run_cli, home, r, capsys)
    assert code == 1
    assert out.rstrip().endswith(
        "1 problem, 1 note; 1 can be fixed automatically: re-run with `yolo doctor --fix`."
    )


def test_summary_counts_cleanable_items(cy, run_cli, repo, capsys):
    r, home = repo
    write_json(home / ".claude-yolo" / "recent-projects.json", {"/gone": {}})
    write_json(home / ".yolo.json", {"mounts": ["/no/such"]})
    code, out = doctor(run_cli, home, r, capsys)
    assert out.rstrip().endswith(
        "1 problem, 1 note; 1 can be fixed automatically: re-run with `yolo doctor --fix`, "
        "1 more with `--clean`."
    )
    code, out = doctor(run_cli, home, r, capsys, "--fix")
    assert out.rstrip().endswith(
        "1 problem, 0 notes, 1 fixed; 1 can be cleaned up: re-run with `yolo doctor --clean`."
    )
    code, out = doctor(run_cli, home, r, capsys, "--clean")
    assert code == 0
    assert out.rstrip().endswith("0 problems, 0 notes, 1 cleaned.")
