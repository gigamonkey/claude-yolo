"""Local-plugin injection: the --plugin-dir / `plugin-dirs` axis.

A plugin dir is bind-mounted read-only at its identical host path and passed to
claude as --plugin-dir, so its bundled skills load for yolo sessions only (never
for a plain host Claude session, which never gets the flag). Launch-side tests
drive `cy.main()` through `run_cli` and assert on the assembled `docker run`
argv; the rest exercise spec parsing and the `config` verb.
"""

import json

import pytest


@pytest.fixture
def dirs(tmp_path):
    """A fresh (home, work) pair plus a real plugin directory."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    plugin = tmp_path / "yolo-plugin"
    for d in (home, work, plugin):
        d.mkdir()
    return home, work, plugin


def claude_args(cy, argv):
    """The args after the image name — i.e. what's passed to `claude`."""
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    return argv[i + 1 :]


def entry(home, work):
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    return projects[str(work)]


# --- spec parsing -----------------------------------------------------------


def test_parse_plugin_dir_spec_resolves(cy, tmp_path):
    p = tmp_path / "plug"
    p.mkdir()
    assert cy._parse_plugin_dir_spec(str(p)) == p.resolve()


def test_parse_plugin_dir_spec_missing_exits(cy, tmp_path):
    with pytest.raises(SystemExit, match="plugin-dir"):
        cy._parse_plugin_dir_spec(str(tmp_path / "nope"))


def test_resolve_plugin_dirs_dedupes(cy, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    # exact-path dup collapses; order preserved
    assert cy._resolve_plugin_dirs([str(a), str(b), str(a)]) == [a.resolve(), b.resolve()]


# --- launch assembly --------------------------------------------------------


def test_no_plugin_dir_by_default(cy, run_cli, dirs):
    home, work, _ = dirs
    argv = run_cli([], home=home, cwd=work)
    assert "--plugin-dir" not in claude_args(cy, argv)


def test_plugin_dir_mounts_ro_and_passes_flag(cy, run_cli, flag_values, dirs):
    home, work, plugin = dirs
    argv = run_cli(["--plugin-dir", str(plugin)], home=home, cwd=work)
    resolved = str(plugin.resolve())
    # bind-mounted read-only at its identical host path...
    assert f"{resolved}:{resolved}:ro" in flag_values(argv, "-v")
    # ...and handed to claude as --plugin-dir
    assert flag_values(claude_args(cy, argv), "--plugin-dir") == [resolved]


def test_plugin_dir_not_announced_as_add_dir(cy, run_cli, flag_values, dirs):
    home, work, plugin = dirs
    argv = run_cli(["--plugin-dir", str(plugin)], home=home, cwd=work)
    # a plugin dir is not a working directory — it must not become an --add-dir
    assert str(plugin.resolve()) not in flag_values(claude_args(cy, argv), "--add-dir")


def test_missing_plugin_dir_exits(cy, run_cli, dirs):
    home, work, _ = dirs
    with pytest.raises(SystemExit, match="plugin-dir"):
        run_cli(["--plugin-dir", str(home / "nope")], home=home, cwd=work)


def test_missing_plugin_dir_does_not_break_terminal_verbs(cy, run_cli, dirs, capsys):
    home, work, _ = dirs
    (home / ".yolo.json").write_text(json.dumps({"plugin-dirs": str(home / "nope")}))
    run_cli(["tokens"], home=home, cwd=work)  # resolved only on launch paths
    assert "No tokens recorded" in capsys.readouterr().out


def test_plugin_dirs_concatenate_across_layers(cy, run_cli, flag_values, dirs, tmp_path):
    home, work, plugin = dirs
    other = tmp_path / "other-plugin"
    other.mkdir()
    (home / ".yolo.json").write_text(json.dumps({"plugin-dirs": str(plugin)}))
    argv = run_cli(["--plugin-dir", str(other)], home=home, cwd=work)
    assert flag_values(claude_args(cy, argv), "--plugin-dir") == [
        str(plugin.resolve()),
        str(other.resolve()),
    ]


# --- config verb ------------------------------------------------------------


def test_config_persists_plugin_dirs(cy, run_cli, dirs):
    home, work, plugin = dirs
    run_cli(["config", "--plugin-dir", str(plugin)], home=home, cwd=work)
    assert entry(home, work) == {"plugin-dirs": [str(plugin)]}


def test_config_rejects_missing_plugin_dir(cy, run_cli, dirs):
    home, work, _ = dirs
    with pytest.raises(SystemExit, match="plugin-dir"):
        run_cli(["config", "--plugin-dir", str(home / "nope")], home=home, cwd=work)


def test_config_add_plugin_dir_appends_and_is_idempotent(cy, run_cli, dirs, tmp_path):
    home, work, plugin = dirs
    other = tmp_path / "p2"
    other.mkdir()
    run_cli(["config", "--plugin-dir", str(plugin)], home=home, cwd=work)
    run_cli(["config", "--add-plugin-dir", str(other)], home=home, cwd=work)
    assert entry(home, work) == {"plugin-dirs": [str(plugin), str(other)]}
    run_cli(["config", "--add-plugin-dir", str(other)], home=home, cwd=work)  # dup -> no-op
    assert entry(home, work) == {"plugin-dirs": [str(plugin), str(other)]}


def test_config_remove_plugin_dir(cy, run_cli, dirs, tmp_path):
    home, work, plugin = dirs
    other = tmp_path / "p2"
    other.mkdir()
    run_cli(
        ["config", "--plugin-dir", str(plugin), "--plugin-dir", str(other)], home=home, cwd=work
    )
    run_cli(["config", "--remove-plugin-dir", str(other)], home=home, cwd=work)
    assert entry(home, work) == {"plugin-dirs": [str(plugin)]}


def test_config_remove_missing_plugin_dir_errors(cy, run_cli, dirs):
    home, work, plugin = dirs
    run_cli(["config", "--plugin-dir", str(plugin)], home=home, cwd=work)
    with pytest.raises(SystemExit, match="no such plugin dir"):
        run_cli(["config", "--remove-plugin-dir", str(work)], home=home, cwd=work)


def test_config_plugin_dir_conflicts_with_add(cy, run_cli, dirs):
    home, work, plugin = dirs
    with pytest.raises(SystemExit, match="replaces the whole"):
        run_cli(
            ["config", "--plugin-dir", str(plugin), "--add-plugin-dir", str(plugin)],
            home=home,
            cwd=work,
        )


def test_add_plugin_dir_only_in_config(cy, run_cli, dirs):
    home, work, plugin = dirs
    with pytest.raises(SystemExit, match="only applies to `config`"):
        run_cli(["--add-plugin-dir", str(plugin)], home=home, cwd=work)
