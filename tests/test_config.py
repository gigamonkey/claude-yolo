"""Tests for .yolo.json parsing, merging, and the `init` scaffold."""

import json

import pytest


def write(path, obj):
    path.write_text(json.dumps(obj))
    return path


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


@pytest.mark.parametrize(
    "obj",
    [
        {"ssh_agnet": True},  # typo / unknown key
        {"ssh-agent": "yes"},  # bool wants bool
        {"config-dir": 7},  # str wants str
        {"append-system-prompt": [1]},  # list must be of strings
        {"worktree": "x"},  # action keys are not config keys
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


def test_load_overlay_precedence_and_concat(cy, tmp_path):
    home = tmp_path / "home"
    proj = home / "work" / "repo"
    proj.mkdir(parents=True)
    write(
        home / ".yolo.json",
        {
            "ssh-agent": False,
            "auth": "bedrock",
            "append-system-prompt": ["home"],
        },
    )
    write(
        proj / ".yolo.json",
        {
            "ssh-agent": True,
            "append-system-prompt": ["proj"],
        },
    )
    merged = cy.load_yolo_config(proj, home)
    assert merged["ssh_agent"] is True  # nearest overrides home
    assert merged["auth"] == "bedrock"  # only in home
    assert merged["append_system_prompts"] == ["home", "proj"]  # concatenated


def test_load_uses_only_nearest_project_file(cy, tmp_path):
    home = tmp_path / "home"
    mid = home / "a"
    deep = mid / "b" / "c"
    deep.mkdir(parents=True)
    write(mid / ".yolo.json", {"aws-region": "from-mid"})
    write(deep / ".yolo.json", {"aws-region": "from-deep"})
    # walking up from deep stops at the first hit; the mid file is not consulted
    assert cy.load_yolo_config(deep, home) == {"aws_region": "from-deep"}


def test_load_dedups_when_home_is_nearest(cy, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    write(home / ".yolo.json", {"append-system-prompt": ["once"]})
    # cwd == home and no closer file: ~/.yolo.json must not be applied twice
    assert cy.load_yolo_config(home, home) == {"append_system_prompts": ["once"]}


def test_load_empty_when_no_files(cy, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    assert cy.load_yolo_config(work, home) == {}


# --- init scaffold ----------------------------------------------------------


def test_init_defaults_match_yolo_keys(cy):
    scaffold_keys = {k.replace("-", "_") for k in cy.YOLO_INIT_DEFAULTS}
    assert scaffold_keys == set(cy.YOLO_KEYS)


def test_write_default_yolo_scaffold_is_inert(cy, tmp_path):
    cy.write_default_yolo(tmp_path)
    path = tmp_path / ".yolo.json"
    assert json.loads(path.read_text()) == cy.YOLO_INIT_DEFAULTS
    # the unedited scaffold loads to exactly the built-in defaults (no surprises)
    merged = cy.load_yolo_config(tmp_path, tmp_path / "nohome")
    assert merged == {
        "auth": "keychain",
        "claude_json": True,
        "ssh_agent": True,
        "base": "HEAD",
        "append_system_prompts": [],
    }


def test_write_default_yolo_refuses_to_clobber(cy, tmp_path):
    (tmp_path / ".yolo.json").write_text("{}")
    with pytest.raises(SystemExit):
        cy.write_default_yolo(tmp_path)
