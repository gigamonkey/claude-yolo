"""Tests for the --yolorc axis: an rc file sourced inside the container at startup.

Covers path resolution (relative vs absolute vs ~), the launch wiring (read-only
mount at the fixed container path + YOLO_RC env), the claude-launch source wrapper
vs the .bashrc path used by `yolo shell`, the missing-file guard, and the `yolorc`
config key (parse + the `config` verb persist/validate).
"""

import json

import pytest


def write_projects(home, mapping):
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "projects.json").write_text(json.dumps(mapping))


def read_projects(home):
    return json.loads((home / ".claude-yolo" / "projects.json").read_text())


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def command_after_image(cy, argv):
    """The args after the image tag — what the container actually runs."""
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    return argv[i + 1 :]


def entrypoint(argv):
    return argv[argv.index("--entrypoint") + 1] if "--entrypoint" in argv else None


# --- path resolution --------------------------------------------------------


def test_resolve_yolorc_relative_vs_absolute(cy, tmp_path):
    base = tmp_path / "session"
    assert cy._resolve_yolorc(".yolorc", base) == base / ".yolorc"
    assert cy._resolve_yolorc("sub/rc.sh", base) == base / "sub" / "rc.sh"
    absrc = tmp_path / "abs" / "rc"
    assert cy._resolve_yolorc(str(absrc), base) == absrc


def test_resolve_yolorc_expands_user(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cy._resolve_yolorc("~/rc", tmp_path / "ignored") == tmp_path / "rc"


def test_parse_yolorc_key_expands_user(cy, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    p = tmp_path / ".yolo.json"
    p.write_text(json.dumps({"yolorc": "~/.yolorc"}))
    assert cy._parse_yolo_file(p) == {"yolorc": "/home/someone/.yolorc"}


# --- launch wiring ----------------------------------------------------------


def test_relative_yolorc_mounts_ro_and_wraps_claude(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (work / ".yolorc").write_text("export FOO=bar\n")
    argv = run_cli(["--yolorc", ".yolorc"], home=home, cwd=work)

    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert f"{work}/.yolorc:{cy._YOLORC_CONTAINER_PATH}:ro" in mounts
    assert f"YOLO_RC={cy._YOLORC_CONTAINER_PATH}" in envs

    # The claude launch is wrapped: drop into bash, source the rc, exec claude.
    assert entrypoint(argv) == "/bin/bash"
    cmd = command_after_image(cy, argv)
    assert cmd[0] == "-c"
    assert "YOLO_RC" in cmd[1] and "exec" in cmd[1]
    assert cmd[2:5] == ["yolo-rc", "claude", "--dangerously-skip-permissions"]
    # ...and the real claude args are reconstructed positionally after it.
    assert "--settings" in cmd[5:]


def test_absolute_yolorc_used_as_is(cy, run_cli, flag_values, dirs, tmp_path):
    home, work = dirs
    rc = tmp_path / "rc.sh"
    rc.write_text("echo hi\n")
    argv = run_cli(["--yolorc", str(rc)], home=home, cwd=work)
    assert f"{rc}:{cy._YOLORC_CONTAINER_PATH}:ro" in flag_values(argv, "-v")


def test_missing_yolorc_exits(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["--yolorc", str(tmp_path / "nope")], home=home, cwd=work)
    assert "yolorc" in str(exc.value)


def test_no_yolorc_leaves_launch_unwrapped(cy, run_cli, flag_values, dirs):
    # Use --auth keychain so there are no env values to load: oauth-token (the
    # default) now wraps the launch to source the token via /run/secrets, so the
    # "unwrapped" baseline only exists when nothing needs the loader.
    home, work = dirs
    argv = run_cli(["--auth", "keychain"], home=home, cwd=work)
    assert entrypoint(argv) is None  # the image's claude ENTRYPOINT
    assert all(not e.startswith("YOLO_RC=") for e in flag_values(argv, "-e"))
    assert all(cy._YOLORC_CONTAINER_PATH not in v for v in flag_values(argv, "-v"))
    assert command_after_image(cy, argv)[0] != "-c"  # not wrapped


def test_shell_sources_via_bashrc_not_wrapper(cy, run_cli, flag_values, dirs, monkeypatch):
    home, work = dirs
    (work / ".yolorc").write_text("echo hi\n")
    # No running container -> fresh bash entrypoint; the rc rides .bashrc + the env.
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: None)
    argv = run_cli(["shell", "--yolorc", ".yolorc"], home=home, cwd=work)
    assert entrypoint(argv) == "/bin/bash"
    assert command_after_image(cy, argv) == []  # shell is not command-wrapped
    assert f"YOLO_RC={cy._YOLORC_CONTAINER_PATH}" in flag_values(argv, "-e")
    assert f"{work}/.yolorc:{cy._YOLORC_CONTAINER_PATH}:ro" in flag_values(argv, "-v")


def test_default_dockerfile_sources_yolorc_in_bashrc(cy):
    # The baked .bashrc must source $YOLO_RC (guarded by the sentinel) so a
    # `yolo shell` gets the same per-session setup a claude launch does.
    assert "YOLO_RC" in cy.DEFAULT_DOCKERFILE
    assert "YOLO_RC_SOURCED" in cy.DEFAULT_DOCKERFILE


# --- config key -------------------------------------------------------------


def test_config_verb_persists_yolorc(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    rc = tmp_path / "rc.sh"
    rc.write_text("echo hi\n")
    run_cli(["config", "--yolorc", str(rc)], home=home, cwd=work)
    assert read_projects(home) == {str(work): {"yolorc": str(rc)}}


def test_config_verb_validates_yolorc_path(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["config", "--yolorc", str(tmp_path / "nope")], home=home, cwd=work)
    assert not (home / ".claude-yolo" / "projects.json").exists()  # typo not pinned


def test_yolorc_config_key_drives_launch(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (work / ".yolorc").write_text("echo hi\n")
    write_projects(home, {str(work): {"yolorc": ".yolorc"}})
    argv = run_cli([], home=home, cwd=work)
    assert f"YOLO_RC={cy._YOLORC_CONTAINER_PATH}" in flag_values(argv, "-e")
    assert entrypoint(argv) == "/bin/bash"  # wrapped from the config key alone
