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


def test_parse_finish_action_and_remote(cy, tmp_path):
    p = write(
        tmp_path / ".yolo.json",
        {"finish-action": "push", "finish-remote": "upstream"},
    )
    assert cy._parse_yolo_file(p) == {
        "finish_action": "push",
        "finish_remote": "upstream",
    }


def test_parse_rejects_invalid_finish_action(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"finish-action": "nonsense"})
    with pytest.raises(SystemExit):
        cy._parse_yolo_file(p)


def test_parse_accepts_underscored_keys(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"aws_profile": "prod"})
    assert cy._parse_yolo_file(p) == {"aws_profile": "prod"}


def test_parse_submodules_key(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"submodules": True})
    assert cy._parse_yolo_file(p) == {"submodules": True}


def test_parse_expands_user_in_path_keys(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    p = write(tmp_path / ".yolo.json", {"config-dir": "~/cfg"})
    assert cy._parse_yolo_file(p) == {"config_dir": "/home/someone/cfg"}


def test_parse_dockerfile_key_expands_user(cy, tmp_path, monkeypatch):
    # `dockerfile` is a path key: ~ is expanded at parse time, existence checked later.
    monkeypatch.setenv("HOME", "/home/someone")
    p = write(tmp_path / ".yolo.json", {"dockerfile": "~/Dockerfile.yolo"})
    assert cy._parse_yolo_file(p) == {"dockerfile": "/home/someone/Dockerfile.yolo"}


def test_parse_null_leaves_key_unset(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"config-dir": None, "auth": None})
    assert cy._parse_yolo_file(p) == {}


def test_parse_list_accepts_string_or_list(cy, tmp_path):
    one = write(tmp_path / "a.json", {"prompts": "x"})
    many = write(tmp_path / "b.json", {"prompts": ["x", "y"]})
    assert cy._parse_yolo_file(one) == {"prompts": ["x"]}
    assert cy._parse_yolo_file(many) == {"prompts": ["x", "y"]}


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
        {"prompts": [1]},  # list must be of strings
        {"mounts": [1]},  # ditto
        {"worktree": "x"},  # action keys are not config keys
        {"dangerously-allow-home": True},  # deliberately CLI-only, never a config key
    ],
)
def test_parse_rejects_bad_input(cy, tmp_path, obj):
    p = write(tmp_path / ".yolo.json", obj)
    with pytest.raises(SystemExit):
        cy._parse_yolo_file(p)


def test_parse_rejects_renamed_prompt_key_with_hint(cy, tmp_path):
    p = write(tmp_path / ".yolo.json", {"append-system-prompt": ["x"]})
    with pytest.raises(SystemExit) as exc:
        cy._parse_yolo_file(p)
    assert "renamed to 'prompts'" in str(exc.value)


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
            "prompts": ["home"],
            "mounts": ["/from-home"],
        },
    )
    write_projects(
        home,
        {
            str(proj): {
                "ssh-agent": True,
                "prompts": ["proj"],
                "mounts": ["/from-proj"],
            }
        },
    )
    merged, key = cy.load_yolo_config(proj, home)
    assert key == str(proj)
    assert merged["ssh_agent"] is True  # project entry overrides home
    assert merged["auth"] == "bedrock"  # only in home
    assert merged["prompts"] == ["home", "proj"]  # concatenated
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


def test_parse_mount_spec_accepts_a_file(cy, tmp_path):
    f = tmp_path / "token"
    f.write_text("x")
    assert cy._parse_mount_spec(f"{f}:ro") == (f, "ro")


def test_parse_mount_spec_missing_path_exits(cy, tmp_path):
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


def test_config_verb_persists_finish_action_and_remote(cy, run_cli, dirs):
    home, work = dirs
    run_cli(
        ["config", "--finish-action", "push", "--finish-remote", "upstream"],
        home=home,
        cwd=work,
    )
    assert read_projects(home) == {
        str(work): {"finish-action": "push", "finish-remote": "upstream"}
    }


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
            "prompts": ["EXTRA"],
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


def test_config_verb_persists_dockerfile(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    df = tmp_path / "Dockerfile.yolo"
    df.write_text("FROM ubuntu:24.04\n")
    run_cli(["config", "--dockerfile", str(df)], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"dockerfile": str(df)}}


def test_config_verb_validates_dockerfile_path(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["config", "--dockerfile", str(tmp_path / "nope")], home=home, cwd=work)
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


# --- `config --init` (empty registration) -------------------------------------


def test_config_init_writes_empty_entry(cy, run_cli, dirs, capsys):
    home, work = dirs
    argv = run_cli(["config", "--init"], home=home, cwd=work)
    assert argv is None  # terminal verb
    assert read_projects(home) == {str(work): {}}
    assert "Registered" in capsys.readouterr().out


def test_config_init_satisfies_require_project_entry(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"require-project-entry": True}))
    run_cli(["config", "--init"], home=home, cwd=work)
    assert run_cli([], home=home, cwd=work) is not None  # launches


def test_config_init_errors_if_entry_exists(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"auth": "bedrock"}})
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--init"], home=home, cwd=work)
    assert "already has" in str(exc.value)
    assert read_projects(home) == {str(work): {"auth": "bedrock"}}  # untouched


def test_config_init_rejects_config_flags(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--init", "--auth", "bedrock"], home=home, cwd=work)
    assert "no overrides" in str(exc.value)
    assert not (home / ".claude-yolo" / "projects.json").exists()


def test_config_init_warns_when_shadowing_ancestor_entry(cy, run_cli, dirs, capsys):
    home, work = dirs
    sub = work / "sub"
    sub.mkdir()
    write_projects(home, {str(work): {"auth": "bedrock"}})
    run_cli(["config", "--init"], home=home, cwd=sub)
    assert "shadows" in capsys.readouterr().err
    projects = read_projects(home)
    assert projects[str(sub)] == {}
    assert projects[str(work)] == {"auth": "bedrock"}  # ancestor entry kept


def test_init_flag_requires_config_verb(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["start", "--init"], home=home, cwd=work)
    assert "--init only applies" in str(exc.value)


# --- `config` list edits: --add-mount / --remove-mount / --add/--remove-prompt --


def test_config_add_mount_appends_to_existing_list(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    ref = tmp_path / "ref"
    ref.mkdir()
    write_projects(home, {str(work): {"mounts": ["/kept"], "auth": "bedrock"}})
    run_cli(["config", "--add-mount", str(ref)], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"mounts": ["/kept", str(ref)], "auth": "bedrock"}}


def test_config_add_mount_updates_mode_for_same_path(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    ref = tmp_path / "ref"
    ref.mkdir()
    write_projects(home, {str(work): {"mounts": [str(ref), "/kept"]}})
    run_cli(["config", "--add-mount", f"{ref}:rw"], home=home, cwd=work)
    assert read_projects(home)[str(work)]["mounts"] == ["/kept", f"{ref}:rw"]


def test_config_add_mount_validates_path(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["config", "--add-mount", str(work / "nope")], home=home, cwd=work)
    assert not (home / ".claude-yolo" / "projects.json").exists()  # typo not pinned


def test_config_remove_mount_removes_without_requiring_dir(cy, run_cli, dirs):
    home, work = dirs
    # /gone:rw doesn't exist on disk — removal must still work (that's the point)
    write_projects(home, {str(work): {"mounts": ["/gone:rw", "/kept"]}})
    run_cli(["config", "--remove-mount", "/gone"], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"mounts": ["/kept"]}}


def test_config_remove_mount_drops_emptied_key(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"mounts": "/only", "auth": "bedrock"}})
    run_cli(["config", "--remove-mount", "/only"], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"auth": "bedrock"}}


def test_config_remove_mount_errors_when_absent(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"mounts": ["/kept"]}})
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--remove-mount", "/nope"], home=home, cwd=work)
    assert "no such mount" in str(exc.value)
    assert read_projects(home) == {str(work): {"mounts": ["/kept"]}}  # untouched


def test_config_mount_conflicts_with_add_remove_mount(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    ref = tmp_path / "ref"
    ref.mkdir()
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--mount", str(ref), "--add-mount", str(ref)], home=home, cwd=work)
    assert "don't combine" in str(exc.value)


def test_config_add_and_remove_prompt(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"prompts": ["OLD", "KEPT"]}})
    run_cli(
        ["config", "--remove-prompt", "OLD", "--add-prompt", "NEW", "--add-prompt", "KEPT"],
        home=home,
        cwd=work,
    )
    # OLD removed, NEW appended, KEPT not duplicated
    assert read_projects(home) == {str(work): {"prompts": ["KEPT", "NEW"]}}


def test_config_remove_prompt_errors_when_absent(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"prompts": ["KEPT"]}})
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--remove-prompt", "NOPE"], home=home, cwd=work)
    assert "no such prompt" in str(exc.value)


# --- `config --unset` ---------------------------------------------------------


def test_config_unset_removes_key(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"auth": "bedrock", "mounts": ["/kept"]}})
    run_cli(["config", "--unset", "auth"], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"mounts": ["/kept"]}}


def test_config_unset_accepts_either_spelling(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"aws-profile": "prod"}})
    run_cli(["config", "--unset", "aws_profile"], home=home, cwd=work)
    assert read_projects(home) == {str(work): {}}


def test_config_unset_errors_when_not_set(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"auth": "bedrock"}})
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--unset", "mounts"], home=home, cwd=work)
    assert "not set" in str(exc.value)


def test_config_unset_conflicts_with_setting_same_key(cy, run_cli, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"auth": "bedrock"}})
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--unset", "auth", "--auth", "keychain"], home=home, cwd=work)
    assert "set and --unset" in str(exc.value)


def test_config_unset_repairs_unknown_key(cy, run_cli, dirs):
    home, work = dirs
    # an unknown key makes every launch fail; --unset must be able to remove it
    write_projects(home, {str(work): {"bogus-key": 1, "auth": "bedrock"}})
    run_cli(["config", "--unset", "bogus-key"], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"auth": "bedrock"}}


def test_config_migrates_renamed_prompt_key(cy, run_cli, dirs):
    home, work = dirs
    # the migration the rename error suggests: unset the pre-0.7 key in the same
    # call that re-adds its value under `prompts`
    write_projects(home, {str(work): {"append-system-prompt": ["OLD"]}})
    run_cli(
        ["config", "--unset", "append-system-prompt", "--add-prompt", "OLD"],
        home=home,
        cwd=work,
    )
    assert read_projects(home) == {str(work): {"prompts": ["OLD"]}}


# --- `config --global` (~/.yolo.json) ------------------------------------------


def test_config_global_sets_keys_in_home_yolo_json(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"ssh-agent": False}))
    run_cli(["config", "--global", "--auth", "bedrock"], home=home, cwd=work)
    assert json.loads((home / ".yolo.json").read_text()) == {
        "ssh-agent": False,
        "auth": "bedrock",
    }
    assert not (home / ".claude-yolo" / "projects.json").exists()  # project layer untouched


def test_config_global_creates_missing_file(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--global", "--no-ssh-agent"], home=home, cwd=work)
    assert json.loads((home / ".yolo.json").read_text()) == {"ssh-agent": False}


def test_config_global_list_edits_and_unset(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    ref = tmp_path / "ref"
    ref.mkdir()
    (home / ".yolo.json").write_text(
        json.dumps({"auth": "bedrock", "mounts": ["/gone"], "prompts": "P"})
    )
    run_cli(
        [
            "config",
            "--global",
            "--unset",
            "auth",
            "--remove-mount",
            "/gone",
            "--add-mount",
            str(ref),
            "--add-prompt",
            "Q",
        ],
        home=home,
        cwd=work,
    )
    assert json.loads((home / ".yolo.json").read_text()) == {
        "mounts": [str(ref)],
        "prompts": ["P", "Q"],
    }


def test_config_global_bare_is_read_only_show(cy, run_cli, dirs, capsys):
    home, work = dirs
    argv = run_cli(["config", "--global"], home=home, cwd=work)
    assert argv is None
    assert "no global config" in capsys.readouterr().out
    assert not (home / ".yolo.json").exists()

    (home / ".yolo.json").write_text(json.dumps({"auth": "bedrock"}))
    run_cli(["config", "--global"], home=home, cwd=work)
    out = capsys.readouterr().out
    assert ".yolo.json" in out and "bedrock" in out


def test_config_global_rejects_malformed_file(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text("not json")
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--global", "--auth", "bedrock"], home=home, cwd=work)
    assert "cannot read" in str(exc.value)
    assert (home / ".yolo.json").read_text() == "not json"  # never clobbered


def test_config_init_rejects_global(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--init", "--global"], home=home, cwd=work)
    assert "--global" in str(exc.value)


def test_config_init_rejects_list_edits(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--init", "--add-prompt", "P"], home=home, cwd=work)
    assert "no overrides" in str(exc.value)


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "--global"],
        ["start", "--unset", "auth"],
        ["start", "--add-mount", "/x"],
        ["start", "--remove-mount", "/x"],
        ["start", "--add-prompt", "P"],
        ["start", "--remove-prompt", "P"],
    ],
)
def test_config_only_flags_require_config_verb(cy, run_cli, dirs, argv):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(argv, home=home, cwd=work)
    assert "only applies to `config`" in str(exc.value)


@pytest.fixture
def dirs(tmp_path):
    """A fresh (home, work) pair of real directories."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work
