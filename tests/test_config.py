"""Tests for config parsing/merging (~/.yolo.json + projects.json) and `yolo config`."""

import json

import pytest


def write(path, obj):
    path.write_text(json.dumps(obj))
    return path


def write_projects(home, mapping):
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    return write(d / "projects.json", mapping)


def read_projects(home):
    return json.loads((home / ".claude-yolo" / "projects.json").read_text())


# --- _parse_yolo_file -------------------------------------------------------


def test_parse_maps_keys_and_types(cy, tmp_path):
    p = write(
        tmp_path / ".yolo.json",
        {"config-dir": "/etc", "auth": "bedrock", "ssh-agent": False},
    )
    assert cy._parse_yolo_file(p) == {
        "config_dir": "/etc",
        "auth": "bedrock",
        "ssh_agent": False,
    }


def test_parse_rejects_invalid_auth(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"auth": "nonsense"})
    with pytest.raises(SystemExit):
        cy._parse_yolo_file(p)


def test_parse_accepts_underscored_keys(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"aws_profile": "prod"})
    assert cy._parse_yolo_file(p) == {"aws_profile": "prod"}


def test_parse_expands_user_in_path_keys(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    p = write(tmp_path / ".yolo.json", {"config-dir": "~/cfg"})
    assert cy._parse_yolo_file(p) == {"config_dir": "/home/someone/cfg"}


def test_parse_null_leaves_key_unset(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"config-dir": None, "auth": None})
    assert cy._parse_yolo_file(p) == {}


def test_parse_list_accepts_string_or_list(cy, tmp_path):
    one = write(tmp_path / "a.json", {"append-system-prompt": "x"})
    many = write(tmp_path / "b.json", {"append-system-prompt": ["x", "y"]})
    assert cy._parse_yolo_file(one) == {"append_system_prompts": ["x"]}
    assert cy._parse_yolo_file(many) == {"append_system_prompts": ["x", "y"]}


def test_parse_mounts_accepts_string_or_list(cy, tmp_path):
    one = write(tmp_path / "a.json", {"mounts": "/ref"})
    many = write(tmp_path / "b.json", {"mounts": ["/ref", "/other:rw"]})
    assert cy._parse_yolo_file(one) == {"mounts": ["/ref"]}
    assert cy._parse_yolo_file(many) == {"mounts": ["/ref", "/other:rw"]}


def test_parse_accepts_require_project_entry(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"require-project-entry": True})
    assert cy._parse_yolo_file(p) == {"require_project_entry": True}


@pytest.mark.parametrize(
    "obj",
    [
        {"ssh_agnet": True},  # typo / unknown key
        {"ssh-agent": "yes"},  # bool wants bool
        {"config-dir": 7},  # str wants str
        {"append-system-prompt": [1]},  # list must be of strings
        {"mounts": [1]},  # ditto
        {"worktree": "x"},  # action keys are not config keys
        {"dangerously-allow-home": True},  # deliberately CLI-only, never a config key
    ],
)
def test_parse_rejects_bad_input(cy, tmp_path, obj):
    p = write(tmp_path / ".yolo.json", obj)
    with pytest.raises(SystemExit):
        cy._parse_yolo_file(p)


def test_parse_rejects_non_object_and_bad_json(cy, tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json}")
    for p in (arr, bad):
        with pytest.raises(SystemExit):
            cy._parse_yolo_file(p)


# --- load_yolo_config -------------------------------------------------------


def test_load_merges_home_and_project_entry(cy, tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "work" / "repo"
    proj.mkdir(parents=True)
    home.mkdir()
    write(
        home / ".yolo.json",
        {
            "ssh-agent": False,
            "auth": "bedrock",
            "append-system-prompt": ["home"],
            "mounts": ["/from-home"],
        },
    )
    write_projects(
        home,
        {
            str(proj): {
                "ssh-agent": True,
                "append-system-prompt": ["proj"],
                "mounts": ["/from-proj"],
            }
        },
    )
    merged, key = cy.load_yolo_config(proj, home)
    assert key == str(proj)
    assert merged["ssh_agent"] is True  # project entry overrides home
    assert merged["auth"] == "bedrock"  # only in home
    assert merged["append_system_prompts"] == ["home", "proj"]  # concatenated
    assert merged["mounts"] == ["/from-home", "/from-proj"]  # concatenated


def test_load_entry_matches_from_subdirectory(cy, tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "repo"
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    home.mkdir()
    write_projects(home, {str(proj): {"aws-region": "eu-west-1"}})
    merged, key = cy.load_yolo_config(sub, home)
    assert key == str(proj)
    assert merged == {"aws_region": "eu-west-1"}


def test_load_longest_matching_key_wins(cy, tmp_path):
    home = tmp_path / "home"
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    home.mkdir()
    write_projects(
        home,
        {
            str(outer): {"aws-region": "from-outer", "auth": "bedrock"},
            str(inner): {"aws-region": "from-inner"},
        },
    )
    merged, key = cy.load_yolo_config(inner, home)
    # only the most specific entry applies; the outer one is not consulted at all
    assert key == str(inner)
    assert merged == {"aws_region": "from-inner"}


def test_load_empty_when_no_files(cy, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    assert cy.load_yolo_config(work, home) == ({}, None)


def test_load_in_directory_yolo_json_is_inert_and_warns(cy, tmp_path, capsys):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    write(proj / ".yolo.json", {"ssh-agent": False})
    merged, key = cy.load_yolo_config(proj, home)
    assert merged == {} and key is None  # the file contributed nothing
    assert "no longer read" in capsys.readouterr().err


def test_load_home_yolo_json_is_not_deprecation_warned(cy, tmp_path, capsys):
    home = tmp_path / "home"
    sub = home / "sub"
    sub.mkdir(parents=True)
    write(home / ".yolo.json", {"ssh-agent": False})
    merged, _ = cy.load_yolo_config(sub, home)
    # ~/.yolo.json is the global layer, found by the ancestor walk from sub —
    # it must be applied, not flagged as a leftover in-directory file.
    assert merged == {"ssh_agent": False}
    assert "no longer read" not in capsys.readouterr().err


def test_load_warns_about_dangling_keys(cy, tmp_path, capsys):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    gone = tmp_path / "renamed-away"  # never created
    write_projects(home, {str(gone): {"auth": "bedrock"}})
    merged, key = cy.load_yolo_config(work, home)
    assert merged == {} and key is None
    err = capsys.readouterr().err
    assert str(gone) in err and "no longer exists" in err
    # no entry matched the cwd either: the rename interpretation is suggested
    assert "used to be one of those" in err


def test_load_no_rename_hint_when_entry_matches(cy, tmp_path, capsys):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    gone = tmp_path / "renamed-away"
    write_projects(home, {str(gone): {}, str(work): {"auth": "bedrock"}})
    merged, key = cy.load_yolo_config(work, home)
    assert key == str(work) and merged == {"auth": "bedrock"}
    err = capsys.readouterr().err
    assert "no longer exists" in err  # the dangling key still warns
    assert "used to be one of those" not in err  # but this cwd is accounted for


def test_load_prints_provenance_line(cy, tmp_path, capsys):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()

    cy.load_yolo_config(work, home)
    assert "config: built-in defaults (no project entry)" in capsys.readouterr().err

    write(home / ".yolo.json", {})
    cy.load_yolo_config(work, home)
    assert "config: ~/.yolo.json (no project entry)" in capsys.readouterr().err

    write_projects(home, {str(work): {}})
    cy.load_yolo_config(work, home)
    assert f"config: ~/.yolo.json + projects.json[{work}]" in capsys.readouterr().err


def test_load_rejects_malformed_projects_file(cy, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    for content in ("[1]", "{not json}", json.dumps({"/p": "not an object"})):
        (home / ".claude-yolo").mkdir(exist_ok=True)
        (home / ".claude-yolo" / "projects.json").write_text(content)
        with pytest.raises(SystemExit):
            cy.load_yolo_config(work, home)


def test_load_rejects_unknown_key_in_project_entry(cy, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    write_projects(home, {str(work): {"bogus": 1}})
    with pytest.raises(SystemExit):
        cy.load_yolo_config(work, home)


# --- mount specs ------------------------------------------------------------


def test_parse_mount_spec_modes(cy, tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    assert cy._parse_mount_spec(str(d)) == (d, "ro")  # ro is the default
    assert cy._parse_mount_spec(f"{d}:ro") == (d, "ro")
    assert cy._parse_mount_spec(f"{d}:rw") == (d, "rw")


def test_parse_mount_spec_expands_user(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "ref"
    d.mkdir()
    assert cy._parse_mount_spec("~/ref:rw") == (d, "rw")


def test_parse_mount_spec_missing_dir_exits(cy, tmp_path):
    with pytest.raises(SystemExit):
        cy._parse_mount_spec(str(tmp_path / "nope"))


def test_resolve_mounts_dedupes_and_later_mode_wins(cy, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    # config layers come first, CLI last: the later :rw wins for the same path
    resolved = cy._resolve_mounts([str(a), str(b), f"{a}:rw"])
    assert resolved == [(a, "rw"), (b, "ro")]


# --- `config` verb ----------------------------------------------------------


def test_config_verb_writes_only_explicit_flags(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli(["config", "--auth", "bedrock", "--aws-profile", "prod"], home=home, cwd=work)
    assert argv is None  # terminal verb: no container launched
    # only the explicitly-passed flags are persisted — no defaulted keys
    assert read_projects(home) == {str(work): {"auth": "bedrock", "aws-profile": "prod"}}


def test_config_verb_persists_explicit_default_value(cy, run_cli, dirs):
    home, work = dirs
    # oauth-token is the built-in default, but explicitly passing it must still persist
    run_cli(["config", "--auth", "oauth-token"], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"auth": "oauth-token"}}


def test_config_verb_persists_bools_lists_and_mounts(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    ref = tmp_path / "ref"
    ref.mkdir()
    run_cli(
        ["config", "--no-ssh-agent", "-p", "EXTRA", "--mount", f"{ref}:rw"],
        home=home,
        cwd=work,
    )
    assert read_projects(home) == {
        str(work): {
            "ssh-agent": False,
            "append-system-prompt": ["EXTRA"],
            "mounts": [f"{ref}:rw"],
        }
    }


def test_config_verb_updates_existing_entry_per_key(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"auth": "bedrock", "mounts": ["/kept"]}})
    run_cli(["config", "--auth", "keychain"], home=home, cwd=work)
    # auth replaced, other keys untouched
    assert read_projects(home) == {str(work): {"auth": "keychain", "mounts": ["/kept"]}}


def test_config_verb_validates_mount_paths(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["config", "--mount", str(work / "nope")], home=home, cwd=work)
    assert not (home / ".claude-yolo" / "projects.json").exists()  # typo not pinned


def test_config_verb_bare_prints_without_writing(cy, run_cli, dirs, capsys):
    home, work = dirs
    argv = run_cli(["config"], home=home, cwd=work)
    assert argv is None
    out = capsys.readouterr().out
    assert "projects.json" in out and f"no entry for {work}" in out
    assert not (home / ".claude-yolo" / "projects.json").exists()  # read-only

    write_projects(home, {str(work): {"auth": "bedrock"}})
    run_cli(["config"], home=home, cwd=work)
    out = capsys.readouterr().out
    assert str(work) in out and "bedrock" in out


def test_config_verb_bare_flags_dangling_keys(cy, run_cli, dirs, capsys):
    home, work = dirs
    write_projects(home, {str(work / "gone"): {}})
    run_cli(["config"], home=home, cwd=work)
    assert "no longer exists" in capsys.readouterr().err


def test_config_verb_rejects_malformed_projects_file(cy, run_cli, dirs):
    home, work = dirs
    (home / ".claude-yolo").mkdir()
    (home / ".claude-yolo" / "projects.json").write_text("[]")
    with pytest.raises(SystemExit):
        run_cli(["config", "--auth", "keychain"], home=home, cwd=work)


@pytest.fixture
def dirs(tmp_path):
    """A fresh (home, work) pair of real directories."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work
