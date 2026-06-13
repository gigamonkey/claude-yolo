"""Integration tests for `main`: verb dispatch and docker-run arg assembly.

These drive `cy.main()` through the `run_cli` fixture (host side effects stubbed)
and assert on the argv that would have been exec'd into `docker run`.
"""

import json

import pytest
from conftest import MASK_CREDFILE


def cred_overlays(mounts):
    """Source paths bind-mounted at the container's .credentials.json.

    Lets a test tell the throwaway oauth-token/bedrock mask (MASK_CREDFILE) apart
    from the keychain snapshot (creds_path) — both land at the same container path.
    """
    suffix = ":/home/claude/.claude/.credentials.json"
    return [m[: -len(suffix)] for m in mounts if m.endswith(suffix)]


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
    # the default auth mode is oauth-token: a forwarded env token, no mounted
    # keychain-credentials snapshot (that mode is unsafe for concurrent sessions)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in envs
    # the only .credentials.json overlay is the throwaway mask, never the keychain
    # snapshot — so a stale host creds file can't shadow the env token
    assert cred_overlays(mounts) == [MASK_CREDFILE]
    # ssh-agent is off by default: no socket forwarded into the container
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in envs
    assert not any("ssh-auth.sock" in m for m in mounts)
    # no CLAUDE_CONFIG_DIR in any mode (mount is always the default location)
    assert not any(e.startswith("CLAUDE_CONFIG_DIR=") for e in envs)


def test_no_ssh_agent_drops_socket_mounts(cy, run_cli, flag_values, dirs):
    # --no-ssh-agent is now also the default; assert it's a no-op-equivalent
    home, work = dirs
    argv = run_cli(["--no-ssh-agent"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in envs
    assert not any("ssh-auth.sock" in m for m in mounts)
    assert not any("known_hosts" in m for m in mounts)


def test_ssh_agent_opt_in_adds_socket_mounts(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--ssh-agent"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "SSH_AUTH_SOCK=/run/ssh-agent" in envs
    assert any("ssh-auth.sock" in m for m in mounts)


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
    assert cred_overlays(mounts) == [MASK_CREDFILE]  # throwaway mask, no keychain creds
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
    # still a normal (default oauth-token) run
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in flag_values(argv, "-e")


# --- auth: oauth-token ------------------------------------------------------


def test_oauth_token_forwards_env_and_skips_keychain(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--auth", "oauth-token"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in envs
    # no rotating keychain creds mounted in this mode — just the throwaway mask
    assert cred_overlays(mounts) == [MASK_CREDFILE]
    # the config dir / claude.json are still mounted (auth is orthogonal to them)
    assert f"{home}/.claude:/home/claude/.claude" in mounts
    assert f"{home}/.claude.json:/home/claude/.claude.json" in mounts


def test_mask_overlays_at_real_config_dir_path(cy, run_cli, flag_values, tmp_path):
    # the mask must land at the *mounted* config dir's .credentials.json, so it
    # shadows a stale host file under an alternate --config-dir too
    home = tmp_path / "home"
    work = tmp_path / "work"
    cfg = tmp_path / "cfg"
    for d in (home, work, cfg):
        d.mkdir()
    argv = run_cli(["--config-dir", str(cfg)], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    assert f"{MASK_CREDFILE}:/home/claude/.claude/.credentials.json" in mounts


def test_stale_host_credentials_file_warns(cy, run_cli, flag_values, dirs, capsys):
    # a .credentials.json on the host should never exist on macOS; warn (and still
    # mask it so the run works) so the user knows to delete it
    home, work = dirs
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    argv = run_cli([], home=home, cwd=work)
    err = capsys.readouterr().err
    assert ".credentials.json exists on the host" in err
    assert cred_overlays(flag_values(argv, "-v")) == [MASK_CREDFILE]


def test_no_warning_without_stale_host_credentials(cy, run_cli, dirs, capsys):
    home, work = dirs
    run_cli([], home=home, cwd=work)
    assert ".credentials.json exists on the host" not in capsys.readouterr().err


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
    assert cred_overlays(mounts) == [MASK_CREDFILE]


def test_oauth_token_via_yolo_json(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"auth": "oauth-token"}))
    argv = run_cli([], home=home, cwd=work)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in flag_values(argv, "-e")
    assert cred_overlays(flag_values(argv, "-v")) == [MASK_CREDFILE]


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


FULL_TOKEN = "sk-ant-oat01-" + "A" * 95  # realistic ~108-char setup-token output


def test_scrape_token_extracts_from_noisy_output(cy):
    raw = (
        b"\x1b[1mClaude Code\x1b[0m\r\nYour token:\r\n\r\n"
        + FULL_TOKEN.encode()
        + b"\r\n\r\nStore it somewhere safe.\r\n"
    )
    assert cy._scrape_token(raw) == FULL_TOKEN


def test_scrape_token_rejects_hard_wrapped_token(cy):
    # Regression: with a narrow pty, `claude setup-token` hard-wraps the token at
    # the terminal width; the scrape must fail (-> manual-paste fallback) rather
    # than silently return the first line as a truncated token.
    wrapped = FULL_TOKEN[:79].encode() + b"\r\n" + FULL_TOKEN[79:].encode()
    raw = b"Your token:\r\n\r\n" + wrapped + b"\r\n\r\nDone.\r\n"
    assert cy._scrape_token(raw) == ""


def test_generate_oauth_token_widens_the_pty(cy, monkeypatch):
    # The pty must be resized wide enough that the token never hard-wraps (the
    # truncation bug behind the scrape guard above). Drive generate_oauth_token
    # with a fake pty.spawn that feeds output through the real master/slave pair,
    # then check the window size the child would have seen.
    import fcntl
    import os
    import struct
    import termios

    stored = {}
    seen = {}

    def fake_spawn(argv, master_read):
        assert argv == ["claude", "setup-token"]
        master, slave = cy.pty.openpty()
        try:
            os.write(slave, b"Your token:\n" + FULL_TOKEN.encode() + b"\n")
            master_read(master)
            rows, cols, *_ = struct.unpack(
                "HHHH", fcntl.ioctl(slave, termios.TIOCGWINSZ, b"\0" * 8)
            )
            seen["size"] = (rows, cols)
        finally:
            os.close(master)
            os.close(slave)
        return 0

    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cy.shutil, "which", lambda cmd: "/usr/local/bin/claude")
    monkeypatch.setattr(cy.pty, "spawn", fake_spawn)
    monkeypatch.setattr(cy, "_store_oauth_token", lambda tok, cfg: stored.__setitem__("tok", tok))

    assert cy.generate_oauth_token(None) == FULL_TOKEN
    assert stored["tok"] == FULL_TOKEN
    assert seen["size"][1] >= 200  # wide enough that a ~108-char token can't wrap


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


# --- config integration (~/.yolo.json + projects.json) -----------------------


def write_projects(home, mapping):
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "projects.json").write_text(json.dumps(mapping))


def test_yolo_provides_defaults_and_cli_overrides(cy, run_cli, flag_values, dirs):
    home, work = dirs
    # config opts ssh-agent on (it's off by built-in default); the CLI overrides back
    (home / ".yolo.json").write_text(json.dumps({"ssh-agent": True}))

    no_flag = run_cli([], home=home, cwd=work)
    assert "SSH_AUTH_SOCK=/run/ssh-agent" in flag_values(no_flag, "-e")

    override = run_cli(["--no-ssh-agent"], home=home, cwd=work)
    assert "SSH_AUTH_SOCK=/run/ssh-agent" not in flag_values(override, "-e")


def test_cli_auth_overrides_yolo(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"auth": "bedrock"}))
    argv = run_cli(["--auth", "keychain"], home=home, cwd=work)
    envs = flag_values(argv, "-e")
    assert "CLAUDE_CODE_USE_BEDROCK=1" not in envs
    # keychain mounts the real extracted creds at .credentials.json, not the mask
    assert cred_overlays(flag_values(argv, "-v")) == ["/tmp/creds.json"]


def test_project_entry_provides_defaults(cy, run_cli, flag_values, dirs):
    home, work = dirs
    write_projects(home, {str(work): {"auth": "oauth-token"}})
    argv = run_cli([], home=home, cwd=work)
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in flag_values(argv, "-e")


def test_in_directory_yolo_json_is_ignored(cy, run_cli, flag_values, dirs, capsys):
    home, work = dirs
    (work / ".yolo.json").write_text(json.dumps({"auth": "keychain"}))
    argv = run_cli([], home=home, cwd=work)
    # the in-directory file no longer configures anything: still a default
    # oauth-token run, not the keychain run the file asks for
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-TESTTOKEN" in flag_values(argv, "-e")
    assert cred_overlays(flag_values(argv, "-v")) == [MASK_CREDFILE]
    assert "no longer read" in capsys.readouterr().err


def test_append_prompt_concatenates_builtin_entry_and_cli(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"prompts": ["FROM_HOME"]}))
    write_projects(home, {str(work): {"prompts": ["FROM_PROJECT"]}})
    argv = run_cli(["-p", "FROM_CLI"], home=home, cwd=work)
    cargs = claude_args(cy, argv)
    joined = cargs[cargs.index("--append-system-prompt") + 1]
    assert "ephemeral Ubuntu container" in joined  # built-in prompt
    assert "FROM_HOME" in joined
    assert "FROM_PROJECT" in joined
    assert "FROM_CLI" in joined


# --- extra mounts (--mount / `mounts`) ----------------------------------------


def test_mount_flag_mounts_ro_and_forwards_add_dir(cy, run_cli, flag_values, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    ref = tmp_path / "ref"
    for d in (home, work, ref):
        d.mkdir()
    argv = run_cli(["--mount", str(ref)], home=home, cwd=work)
    assert f"{ref}:{ref}:ro" in flag_values(argv, "-v")  # ro is the default
    cargs = claude_args(cy, argv)
    assert cargs[cargs.index("--add-dir") + 1] == str(ref)


def test_mount_rw_suffix(cy, run_cli, flag_values, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    ref = tmp_path / "ref"
    for d in (home, work, ref):
        d.mkdir()
    argv = run_cli(["--mount", f"{ref}:rw"], home=home, cwd=work)
    assert f"{ref}:{ref}:rw" in flag_values(argv, "-v")


def test_mount_missing_dir_exits(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["--mount", str(work / "nope")], home=home, cwd=work)


def test_mounts_concatenate_across_layers_and_cli_mode_wins(cy, run_cli, flag_values, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (home, work, a, b):
        d.mkdir()
    (home / ".yolo.json").write_text(json.dumps({"mounts": [str(a)]}))
    write_projects(home, {str(work): {"mounts": [str(b)]}})
    argv = run_cli(["--mount", f"{a}:rw"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    assert f"{a}:{a}:rw" in mounts  # CLI spec wins the ro/rw conflict for a
    assert f"{a}:{a}:ro" not in mounts
    assert f"{b}:{b}:ro" in mounts  # project-entry mount also present


# --- guardrails ---------------------------------------------------------------


def test_refuses_to_launch_at_home(cy, run_cli, dirs):
    home, _ = dirs
    with pytest.raises(SystemExit):
        run_cli([], home=home, cwd=home)


def test_refuses_to_launch_above_home(cy, run_cli, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(SystemExit):
        run_cli([], home=home, cwd=tmp_path)


def test_dangerously_allow_home_overrides(cy, run_cli, flag_values, dirs):
    home, _ = dirs
    argv = run_cli(["--dangerously-allow-home"], home=home, cwd=home)
    assert f"{home}:{home}" in flag_values(argv, "-v")  # launched, home mounted


def test_home_guard_exempts_terminal_verbs(cy, run_cli, dirs, capsys):
    home, _ = dirs
    assert run_cli(["config"], home=home, cwd=home) is None  # no SystemExit


def test_require_project_entry_blocks_without_entry(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"require-project-entry": True}))
    with pytest.raises(SystemExit):
        run_cli([], home=home, cwd=work)


def test_require_project_entry_satisfied_by_entry(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"require-project-entry": True}))
    write_projects(home, {str(work): {}})
    assert run_cli([], home=home, cwd=work) is not None  # launched


def test_require_project_entry_cli_override(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"require-project-entry": True}))
    assert run_cli(["--no-require-project-entry"], home=home, cwd=work) is not None


def test_require_project_entry_does_not_block_config_verb(cy, run_cli, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"require-project-entry": True}))
    assert run_cli(["config"], home=home, cwd=work) is None  # prints, no error


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


def test_config_verb_is_terminal(cy, run_cli, dirs):
    home, work = dirs
    argv = run_cli(["config", "--no-ssh-agent"], home=home, cwd=work)
    assert argv is None  # execvp never reached
    assert (home / ".claude-yolo" / "projects.json").is_file()


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
