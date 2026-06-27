"""The `clones` / `--clone` axis: clone a git repo into the container at startup.

`--clone URL DIR` (two args) accumulates as `{url, dir}` objects (shared with the
config-file form); at session start the claude launch wrapper runs `bash
/etc/yolo/clone.sh <url> <resolved-dir>` for each. Launch tests drive `run_cli` and
assert on the wrapper script in the assembled argv.
"""

import json

import pytest


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "proj"
    home.mkdir()
    work.mkdir()
    return home, work


def wrapper_script(cy, argv):
    """The bash `-c` script the claude launch wrapper runs (after the image)."""
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    cargs = argv[i + 1 :]
    return cargs[cargs.index("-c") + 1]


def entry(home, work):
    return json.loads((home / ".claude-yolo" / "projects.json").read_text())[str(work)]


# --- resolution -------------------------------------------------------------


def test_resolve_clones_relative_absolute_home_and_dedup(cy, tmp_path):
    cwd = tmp_path / "a" / "b"
    specs = [
        {"url": "u1", "dir": "../foo"},  # sibling of cwd
        {"url": "u2", "dir": "/work/x"},  # absolute, as-is
        {"url": "u3", "dir": "~/lib"},  # container home, NOT host
        {"url": "first", "dir": "../dup"},
        {"url": "wins", "dir": "../dup"},  # same dest -> later (higher layer) wins
    ]
    got = {dest: url for url, dest, _ in cy._resolve_clones(specs, cwd)}
    assert got[str(tmp_path / "a" / "foo")] == "u1"
    assert got["/work/x"] == "u2"
    assert got["/home/claude/lib"] == "u3"
    assert got[str(tmp_path / "a" / "dup")] == "wins"


def test_resolve_clones_carries_depth(cy, tmp_path):
    out = cy._resolve_clones(
        [
            {"url": "u1", "dir": "/a", "depth": 1},
            {"url": "u2", "dir": "/b"},  # CLI form / no depth
        ],
        tmp_path,
    )
    assert ("u1", "/a", 1) in out
    assert ("u2", "/b", None) in out


# --- config-file parsing ----------------------------------------------------


def test_parse_clones_object_and_list(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".yolo.json").write_text(
        json.dumps({"clones": {"url": "https://x/r", "dir": "../r"}})  # single object → list
    )
    cfg, _ = cy.load_yolo_config(tmp_path, tmp_path)
    assert cfg["clones"] == [{"url": "https://x/r", "dir": "../r"}]


def test_parse_clones_rejects_bad_shape(cy, tmp_path):
    with pytest.raises(SystemExit, match="url, dir"):
        cy._parse_yolo_dict({"clones": [{"url": "u"}]}, "test")  # missing dir
    with pytest.raises(SystemExit, match="url, dir"):
        cy._parse_yolo_dict({"clones": ["u dir"]}, "test")  # strings, not objects


def test_parse_clones_depth(cy):
    # depth is optional; a positive int is kept, anything else is rejected
    assert cy._parse_yolo_dict({"clones": [{"url": "u", "dir": "d", "depth": 1}]}, "t") == {
        "clones": [{"url": "u", "dir": "d", "depth": 1}]
    }
    for bad in (0, -1, "1", 1.5, True):
        with pytest.raises(SystemExit, match="depth"):
            cy._parse_yolo_dict({"clones": [{"url": "u", "dir": "d", "depth": bad}]}, "t")


# --- launch assembly --------------------------------------------------------


def test_no_clone_by_default(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert "clone.sh" not in wrapper_script(cy, argv)


def test_clone_in_wrapper_with_resolved_dir(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli(["--clone", "https://github.com/me/lib", "../lib"], home=home, cwd=work)
    assert "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "/bin/bash"
    sibling = str(work.parent / "lib")
    assert f"bash /etc/yolo/clone.sh https://github.com/me/lib {sibling}" in wrapper_script(
        cy, argv
    )


def test_clones_concatenate_across_layers(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"clones": [{"url": "https://x/g", "dir": "/g"}]}))
    argv = run_cli(["--clone", "https://x/c", "/c"], home=home, cwd=work)
    script = wrapper_script(cy, argv)
    assert "bash /etc/yolo/clone.sh https://x/g /g" in script  # config layer
    assert "bash /etc/yolo/clone.sh https://x/c /c" in script  # CLI layer


def test_clone_depth_in_wrapper(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(
        json.dumps({"clones": [{"url": "https://x/lib", "dir": "/lib", "depth": 1}]})
    )
    argv = run_cli([], home=home, cwd=work)
    # depth becomes the optional 3rd arg to clone.sh (-> git clone --depth)
    assert "bash /etc/yolo/clone.sh https://x/lib /lib 1" in wrapper_script(cy, argv)


# --- config verb ------------------------------------------------------------


def test_config_persists_clones_as_objects(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--clone", "https://github.com/me/lib", "../lib"], home=home, cwd=work)
    assert entry(home, work) == {"clones": [{"url": "https://github.com/me/lib", "dir": "../lib"}]}


def test_config_clone_repeatable_replaces_whole_list(cy, run_cli, dirs):
    home, work = dirs
    run_cli(
        ["config", "--clone", "https://x/a", "../a", "--clone", "https://x/b", "../b"],
        home=home,
        cwd=work,
    )
    assert entry(home, work) == {
        "clones": [
            {"url": "https://x/a", "dir": "../a"},
            {"url": "https://x/b", "dir": "../b"},
        ]
    }
    run_cli(["config", "--unset", "clones"], home=home, cwd=work)
    assert "clones" not in entry(home, work)


def test_config_add_remove_clone(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--add-clone", "https://x/lib", "../lib"], home=home, cwd=work)
    run_cli(["config", "--add-clone", "https://x/api", "../api", "1"], home=home, cwd=work)
    assert entry(home, work)["clones"] == [
        {"url": "https://x/lib", "dir": "../lib"},
        {"url": "https://x/api", "dir": "../api", "depth": 1},  # optional depth kept
    ]
    # Adding a same-dir clone replaces it (so url/depth can be changed).
    run_cli(["config", "--add-clone", "https://x/lib2", "../lib"], home=home, cwd=work)
    assert entry(home, work)["clones"] == [
        {"url": "https://x/api", "dir": "../api", "depth": 1},
        {"url": "https://x/lib2", "dir": "../lib"},
    ]
    run_cli(["config", "--remove-clone", "../api"], home=home, cwd=work)
    assert entry(home, work)["clones"] == [{"url": "https://x/lib2", "dir": "../lib"}]


def test_config_remove_clone_missing_errors(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--add-clone", "https://x/lib", "../lib"], home=home, cwd=work)
    with pytest.raises(SystemExit, match="no such clone"):
        run_cli(["config", "--remove-clone", "../nope"], home=home, cwd=work)


def test_config_add_clone_conflict_guard(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="don't combine"):
        run_cli(
            ["config", "--clone", "https://x/a", "../a", "--add-clone", "https://x/b", "../b"],
            home=home,
            cwd=work,
        )


def test_add_clone_bad_depth_rejected(cy, run_cli, dirs, capsys):
    home, work = dirs
    # argparse's parser.error raises SystemExit(2); the message goes to stderr.
    with pytest.raises(SystemExit):
        run_cli(["config", "--add-clone", "https://x/a", "../a", "notanint"], home=home, cwd=work)
    assert "positive integer" in capsys.readouterr().err


def test_add_clone_only_for_config(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="only applies to .config"):
        run_cli(["start", "--add-clone", "https://x/a", "../a"], home=home, cwd=work)
