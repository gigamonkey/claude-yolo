"""Session-activity tracking: Stop/UserPromptSubmit hooks + the `ps` STATE column.

Launch-side tests drive `cy.main()` through `run_cli` and assert on the assembled
`docker run` argv (the injected `--settings` hooks, the `yolo.config-dir` label,
the status-file reset). The state-rendering and hook-merge helpers are tested
directly.
"""

import json

import pytest


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def claude_args(cy, argv):
    return argv[argv.index(cy.DOCKER_IMAGE) + 1 :]


def settings_obj(cy, argv):
    cargs = claude_args(cy, argv)
    return json.loads(cargs[cargs.index("--settings") + 1])


def labels(cy, argv, flag_values):
    return flag_values(argv, "--label")


# --- the injected hooks -----------------------------------------------------


def test_launch_injects_stop_and_userpromptsubmit_hooks(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    settings = settings_obj(cy, argv)
    assert settings["sandbox"] == {"enabled": False}  # the old override still there

    hooks = settings["hooks"]
    # both events present, each a matcher-group with an inner "hooks" array
    stop_cmd = hooks["Stop"][0]["hooks"][0]["command"]
    work_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
    slug = cy._cwd_slug(work)
    target = f"/home/claude/.claude/.yolo-status/{slug}.state"
    assert stop_cmd == f"printf 'waiting %s' \"$(date +%s)\" > {target}"
    assert work_cmd == f"printf 'working %s' \"$(date +%s)\" > {target}"


def test_launch_stamps_config_dir_label_default(cy, run_cli, dirs, flag_values):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert f"yolo.config-dir={home}/.claude" in labels(cy, argv, flag_values)


def test_launch_stamps_config_dir_label_alternate(cy, run_cli, dirs, flag_values, tmp_path):
    home, work = dirs
    alt = tmp_path / "alt-config"
    alt.mkdir()
    argv = run_cli(["--config-dir", str(alt)], home=home, cwd=work)
    assert f"yolo.config-dir={alt}" in labels(cy, argv, flag_values)


def test_launch_resets_stale_status_file(cy, run_cli, dirs):
    home, work = dirs
    status_dir = home / ".claude" / cy._STATUS_DIR_NAME
    status_dir.mkdir(parents=True)
    stale = status_dir / f"{cy._cwd_slug(work)}.state"
    stale.write_text("waiting 100")
    run_cli([], home=home, cwd=work)
    assert not stale.exists()  # cleared so ps doesn't show a prior session's time


def test_shell_does_not_inject_hooks_or_reset(cy, run_cli, dirs):
    # the bash shell entrypoint runs no claude, so no hooks and no status reset
    home, work = dirs
    status_dir = home / ".claude" / cy._STATUS_DIR_NAME
    status_dir.mkdir(parents=True)
    stale = status_dir / f"{cy._cwd_slug(work)}.state"
    stale.write_text("waiting 100")
    argv = run_cli(["shell"], home=home, cwd=work)
    assert cy.DOCKER_IMAGE in argv and "--settings" not in claude_args(cy, argv)
    assert stale.exists()  # untouched


# --- merging the user's own hooks -------------------------------------------


def test_user_hooks_are_preserved_alongside_yolos(cy, run_cli, dirs):
    home, work = dirs
    user_hook = {"type": "command", "command": "echo hi"}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [user_hook]}]}})
    )
    argv = run_cli([], home=home, cwd=work)
    hooks = settings_obj(cy, argv)["hooks"]
    # the user's PostToolUse hook survives the --settings override...
    assert hooks["PostToolUse"][0]["hooks"][0]["command"] == "echo hi"
    # ...and yolo's own are still there
    assert hooks["Stop"] and hooks["UserPromptSubmit"]


def test_user_hooks_on_same_event_concatenate(cy, run_cli, dirs):
    home, work = dirs
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}})
    )
    argv = run_cli([], home=home, cwd=work)
    stop_groups = settings_obj(cy, argv)["hooks"]["Stop"]
    cmds = [h["command"] for g in stop_groups for h in g["hooks"]]
    assert "mine" in cmds  # user's Stop hook kept
    assert any("waiting %s" in c for c in cmds)  # yolo's appended


def test_read_settings_hooks_merges_settings_and_local(cy, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "a"}]}]}})
    )
    (cfg / "settings.local.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "b"}]}]}})
    )
    merged = cy._read_settings_hooks(str(cfg), tmp_path)
    cmds = [h["command"] for g in merged["Stop"] for h in g["hooks"]]
    assert cmds == ["a", "b"]


def test_read_settings_hooks_tolerates_missing_and_malformed(cy, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    assert cy._read_settings_hooks(str(cfg), tmp_path) == {}  # no files
    (cfg / "settings.json").write_text("{not json")
    assert cy._read_settings_hooks(str(cfg), tmp_path) == {}  # malformed -> ignored


# --- state rendering --------------------------------------------------------


@pytest.mark.parametrize(
    "secs,expected",
    [(0, "0s"), (45, "45s"), (60, "1m"), (599, "9m"), (3600, "1h"), (90000, "1d")],
)
def test_humanize_secs(cy, secs, expected):
    assert cy._humanize_secs(secs) == expected


def test_read_session_state_waiting(cy, tmp_path):
    f = tmp_path / "s.state"
    f.write_text("waiting 1000")
    assert cy._read_session_state(f, 1000 + 180) == "waiting 3m"


def test_read_session_state_working(cy, tmp_path):
    f = tmp_path / "s.state"
    f.write_text("working 1000")
    assert cy._read_session_state(f, 1000 + 5) == "working"


def test_read_session_state_clamps_future_timestamp(cy, tmp_path):
    f = tmp_path / "s.state"
    f.write_text("waiting 2000")  # in the "future" relative to now
    assert cy._read_session_state(f, 1000) == "waiting 0s"


def test_read_session_state_missing_or_garbage(cy, tmp_path):
    assert cy._read_session_state(tmp_path / "nope.state", 1000) == "-"
    bad = tmp_path / "bad.state"
    bad.write_text("garbage")
    assert cy._read_session_state(bad, 1000) == "-"
    bad.write_text("waiting notanumber")
    assert cy._read_session_state(bad, 1000) == "-"
