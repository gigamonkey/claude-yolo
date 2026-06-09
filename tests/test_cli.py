"""Integration tests for `main`: verb dispatch and docker-run arg assembly.

These drive `cy.main()` through the `run_cli` fixture (host side effects stubbed)
and assert on the argv that would have been exec'd into `docker run`.
"""

import json

import pytest


@pytest.fixture
def dirs(tmp_path):
    """A fresh (home, work) pair of real directories."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def container_name(argv):
    return argv[argv.index("--name") + 1]


def claude_args(cy, argv):
    """The args after the image name — i.e. what's passed to `claude`."""
    return argv[argv.index(cy.DOCKER_IMAGE) + 1 :]


# --- default run ------------------------------------------------------------


def test_default_run_assembles_expected_mounts(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")

    assert container_name(argv) == "work"
    assert f"{home}/.claude:/home/claude/.claude" in mounts
    assert f"{home}/.claude.json:/home/claude/.claude.json" in mounts
    assert "/tmp/creds.json:/home/claude/.claude/.credentials.json" in mounts
    assert "SSH_AUTH_SOCK=/run/ssh-agent" in envs
    # no CLAUDE_CONFIG_DIR in any mode (mount is always the default location)
    assert not any(e.startswith("CLAUDE_CONFIG_DIR=") for e in envs)


def test_no_ssh_agent_drops_socket_mounts(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--no-ssh-agent"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in envs
    assert not any("ssh-auth.sock" in m for m in mounts)
    assert not any("known_hosts" in m for m in mounts)


def test_no_claude_json_drops_that_mount(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--no-claude-json"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    assert not any(".claude.json:" in m for m in mounts)
    # the config dir itself is still mounted
    assert f"{home}/.claude:/home/claude/.claude" in mounts


# --- config dir -------------------------------------------------------------


def test_config_dir_mounts_dir_and_suffixes_name(cy, run_cli, flag_values, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    cfg = tmp_path / "altcfg"
    for d in (home, work, cfg):
        d.mkdir()
    argv = run_cli(["--config-dir", str(cfg)], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    assert f"{cfg}:/home/claude/.claude" in mounts
    assert container_name(argv) == "work-altcfg"


def test_config_dir_must_exist(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["--config-dir", str(work / "nope")], home=home, cwd=work)


# --- bedrock ----------------------------------------------------------------


def test_bedrock_sets_env_and_skips_keychain(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--auth", "bedrock", "--aws-profile", "prod"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "CLAUDE_CODE_USE_BEDROCK=1" in envs
    assert "AWS_PROFILE=prod" in envs
    assert "AWS_REGION=us-east-1" in envs  # default region
    assert f"{home}/.aws:/home/claude/.aws:ro" in mounts
    assert not any(".credentials.json" in m for m in mounts)  # no keychain creds
    assert container_name(argv) == "work-prod"


def test_bedrock_composes_with_config_dir(cy, run_cli, flag_values, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    cfg = tmp_path / "cfg"
    for d in (home, work, cfg):
        d.mkdir()
    argv = run_cli(["--auth", "bedrock", "--config-dir", str(cfg)], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert f"{cfg}:/home/claude/.claude" in mounts  # config dir honored
    assert "CLAUDE_CODE_USE_BEDROCK=1" in envs  # and bedrock honored
    assert container_name(argv) == "work-cfg-bedrock"


def test_aws_flag_without_bedrock_warns(cy, run_cli, flag_values, dirs, capsys):
    home, work = dirs
    argv = run_cli(["--aws-profile", "prod"], home=home, cwd=work)
    assert "ignored without --auth bedrock" in capsys.readouterr().err
    # still a normal keychain run
    assert any(".credentials.json" in m for m in flag_values(argv, "-v"))


# --- auth: oauth-token ------------------------------------------------------


def test_oauth_token_forwards_env_and_skips_keychain(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--auth", "oauth-token"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in envs
    # no rotating keychain creds mounted in this mode
    assert not any(".credentials.json" in m for m in mounts)
    # the config dir / claude.json are still mounted (auth is orthogonal to them)
    assert f"{home}/.claude:/home/claude/.claude" in mounts
    assert f"{home}/.claude.json:/home/claude/.claude.json" in mounts


def test_oauth_token_composes_with_config_dir(cy, run_cli, flag_values, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    cfg = tmp_path / "cfg"
    for d in (home, work, cfg):
        d.mkdir()
    argv = run_cli(["--auth", "oauth-token", "--config-dir", str(cfg)], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert f"{cfg}:/home/claude/.claude" in mounts
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in envs
    assert not any(".credentials.json" in m for m in mounts)


def test_oauth_token_via_yolo_json(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (work / ".yolo.json").write_text(json.dumps({"auth": "oauth-token"}))
    argv = run_cli([], home=home, cwd=work)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in flag_values(argv, "-e")
    assert not any(".credentials.json" in m for m in flag_values(argv, "-v"))


def test_invalid_auth_choice_rejected(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["--auth", "nonsense"], home=home, cwd=work)


def test_generate_oauth_token_requires_a_tty(cy, monkeypatch):
    # Non-interactive (no tty) auto-generate must bail with guidance, not hang on
    # the browser flow. Force isatty False so the result is independent of how the
    # test runner wires stdin.
    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cy.generate_oauth_token(None)
    assert "interactive terminal" in str(exc.value)


def test_oauth_service_is_keyed_to_config_dir(cy, tmp_path):
    import hashlib

    cfg = tmp_path / "altcfg"
    cfg.mkdir()
    default = cy._oauth_service(None)
    scoped = cy._oauth_service(str(cfg))
    assert default == cy.OAUTH_KC_SERVICE
    assert scoped != default
    # same hash scheme Claude itself uses for the per-dir keychain entry
    h = hashlib.sha256(str(cfg.resolve()).encode()).hexdigest()[:8]
    assert scoped == f"{cy.OAUTH_KC_SERVICE}-{h}"


# --- .yolo.json integration -------------------------------------------------


def test_yolo_provides_defaults_and_cli_overrides(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (work / ".yolo.json").write_text(json.dumps({"ssh-agent": False}))

    no_flag = run_cli([], home=home, cwd=work)
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in flag_values(no_flag, "-e")

    override = run_cli(["--ssh-agent"], home=home, cwd=work)
    assert "SSH_AUTH_SOCK=/run/ssh-agent" in flag_values(override, "-e")


def test_cli_auth_overrides_yolo(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (work / ".yolo.json").write_text(json.dumps({"auth": "bedrock"}))
    argv = run_cli(["--auth", "keychain"], home=home, cwd=work)
    envs = flag_values(argv, "-e")
    assert "CLAUDE_CODE_USE_BEDROCK=1" not in envs
    assert any(".credentials.json" in m for m in flag_values(argv, "-v"))


def test_append_prompt_concatenates_builtin_yolo_and_cli(cy, run_cli, dirs):
    home, work = dirs
    (work / ".yolo.json").write_text(json.dumps({"append-system-prompt": ["FROM_YOLO"]}))
    argv = run_cli(["-p", "FROM_CLI"], home=home, cwd=work)
    cargs = claude_args(cy, argv)
    joined = cargs[cargs.index("--append-system-prompt") + 1]
    assert "ephemeral Ubuntu container" in joined  # built-in prompt
    assert "FROM_YOLO" in joined
    assert "FROM_CLI" in joined


def test_docker_passthrough_after_double_dash(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli(["--", "--network", "host"], home=home, cwd=work)
    # passthrough args land before the image, after the assembled docker args
    img = argv.index(cy.DOCKER_IMAGE)
    assert argv[img - 2 : img] == ["--network", "host"]


# --- PS1 (yolo shell prompt) ------------------------------------------------


def test_default_run_sets_yolo_ps1(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    ps1 = [e for e in flag_values(argv, "-e") if e.startswith("YOLO_PS1=")]
    assert ps1, "every launch should export YOLO_PS1 for in-container bash"
    assert "yolo" in ps1[0]
    assert r"\w" in ps1[0]  # cwd mode shows the plain working directory
    # no worktree, so no rewrite helpers
    assert not any(e.startswith("YOLO_WT_") for e in flag_values(argv, "-e"))


def test_worktree_ps1_label_strips_root_and_shared_slug_prefix(cy, tmp_path):
    root = tmp_path / ".claude-yolo" / "worktrees"
    a = root / "-Users-peter-hacks-claude-yolo" / "fix-auth"
    b = root / "-Users-peter-hacks-otherrepo" / "topic"
    for d in (a, b):
        d.mkdir(parents=True)
    # shared prefix "-Users-peter-hacks-" is dropped along with the worktrees root
    assert cy._worktree_ps1_label(a) == "claude-yolo/fix-auth"
    assert cy._worktree_ps1_label(b) == "otherrepo/topic"


def test_worktree_ps1_label_single_slug_is_just_the_topic(cy, tmp_path):
    # With one slug the shared prefix is the whole slug; only the topic remains.
    only = tmp_path / "worktrees" / "-Users-peter-hacks-claude-yolo" / "fix-auth"
    only.mkdir(parents=True)
    assert cy._worktree_ps1_label(only) == "fix-auth"


# --- verbs ------------------------------------------------------------------


def test_init_verb_writes_file_and_does_not_exec(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli(["init"], home=home, cwd=work)
    assert argv is None  # execvp never reached
    assert (work / ".yolo.json").is_file()


def test_unknown_verb_exits(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["bogus"], home=home, cwd=work)


def test_version_flag_prints_and_exits(cy, run_cli, dirs, capsys):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["--version"], home=home, cwd=work)
    assert exc.value.code == 0
    assert capsys.readouterr().out.split()[-1] == cy._version()


# --- worktree mounts (start TOPIC) ------------------------------------------


def test_start_worktree_mounts_shared_git_and_names_session(
    cy, run_cli, monkeypatch, flag_values, tmp_path
):
    home = tmp_path / "home"
    work = tmp_path / "work"
    wt = tmp_path / "wt"
    main_root = tmp_path / "repo"
    git = main_root / ".git"
    ghost = tmp_path / "ghost"  # the worktree path start checks — must not exist yet
    for d in (home, work, wt, git):
        d.mkdir(parents=True)
    monkeypatch.setattr(cy, "_worktree_dir", lambda topic, h: (ghost, main_root, "slug"))
    monkeypatch.setattr(cy, "_branch_exists", lambda name: False)
    monkeypatch.setattr(cy, "setup_worktree", lambda name, h, base="HEAD": (wt, git, main_root))

    argv = run_cli(["start", "feat"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    assert f"{wt}:{wt}" in mounts  # worktree cwd mounted
    assert f"{git}:{git}" in mounts  # shared .git mounted
    assert container_name(argv) == "repo-feat"
    cargs = claude_args(cy, argv)
    assert cargs[:2] == ["--name", "feat"]  # session named
