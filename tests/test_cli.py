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
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    return argv[i + 1 :]


def assert_token_via_run_secrets(home, argv, flag_values, token="sk-ant-oat-TESTTOKEN"):
    """Assert the OAuth token rides the /run/secrets file transport, not the argv.

    oauth-token mode no longer passes `-e CLAUDE_CODE_OAUTH_TOKEN=…` (that would
    leak it into the docker-run argv / `docker inspect` / tmux pane command). The
    token is staged as a chmod-600 run-dir file and mounted at /run/secrets, where
    the baked loader exports it. Returns nothing; raises on mismatch.
    """
    mounts = flag_values(argv, "-v")
    assert any(m.endswith(":/run/secrets:rw") for m in mounts), mounts
    # the value never appears anywhere on the argv
    assert not any("CLAUDE_CODE_OAUTH_TOKEN" in a for a in argv), argv
    assert token not in " ".join(argv)
    staged = (
        home / ".claude-yolo-run" / container_name(argv) / "secrets" / "CLAUDE_CODE_OAUTH_TOKEN"
    )
    assert staged.read_text() == token


# --- default run ------------------------------------------------------------


def test_default_run_assembles_expected_mounts(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")

    assert container_name(argv) == "work"
    assert f"{home}/.claude:/home/claude/.claude" in mounts
    assert f"{home}/.claude.json:/home/claude/.claude.json" in mounts
    # the default auth mode is oauth-token: the token rides /run/secrets (not -e,
    # so it stays off the argv / docker inspect), no mounted keychain-credentials
    # snapshot (that mode is unsafe for concurrent sessions)
    assert_token_via_run_secrets(home, argv, flag_values)
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
    # without the agent, the GitHub HTTPS->SSH rewrite is NOT applied, so plain
    # HTTPS clones of public repos still work instead of failing on an SSH URL
    assert "GIT_CONFIG_COUNT=1" not in envs


def test_ssh_agent_opt_in_adds_socket_mounts(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--ssh-agent"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    envs = flag_values(argv, "-e")
    assert "SSH_AUTH_SOCK=/run/ssh-agent" in envs
    assert any("ssh-auth.sock" in m for m in mounts)
    # the GitHub HTTPS->SSH rewrite rides along with the agent, applied as run-time
    # git config via GIT_CONFIG_* (not baked into the image)
    assert "GIT_CONFIG_COUNT=1" in envs
    assert "GIT_CONFIG_KEY_0=url.git@github.com:.insteadOf" in envs
    assert "GIT_CONFIG_VALUE_0=https://github.com/" in envs


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
    assert_token_via_run_secrets(home, argv, flag_values)


# --- auth: oauth-token ------------------------------------------------------


def test_oauth_token_forwards_env_and_skips_keychain(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--auth", "oauth-token"], home=home, cwd=work)
    mounts = flag_values(argv, "-v")
    assert_token_via_run_secrets(home, argv, flag_values)
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


def test_stale_host_credentials_file_warns(cy, run_cli, flag_values, dirs, capsys, monkeypatch):
    # a .credentials.json on the host should never exist on macOS; warn (and still
    # mask it so the run works) so the user knows to delete it
    monkeypatch.setattr(cy, "_is_macos", lambda: True)
    home, work = dirs
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    argv = run_cli([], home=home, cwd=work)
    err = capsys.readouterr().err
    assert ".credentials.json exists on the host" in err
    assert cred_overlays(flag_values(argv, "-v")) == [MASK_CREDFILE]


def test_no_stale_warning_on_linux_where_file_is_the_store(cy, run_cli, dirs, capsys, monkeypatch):
    # on a Linux host that file IS Claude Code's legitimate credential store, so its
    # presence is expected — no warning
    monkeypatch.setattr(cy, "_is_macos", lambda: False)
    home, work = dirs
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    run_cli([], home=home, cwd=work)
    assert ".credentials.json exists on the host" not in capsys.readouterr().err


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
    assert f"{cfg}:/home/claude/.claude" in mounts
    assert_token_via_run_secrets(home, argv, flag_values)
    assert cred_overlays(mounts) == [MASK_CREDFILE]


def test_oauth_token_via_yolo_json(cy, run_cli, flag_values, dirs):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"auth": "oauth-token"}))
    argv = run_cli([], home=home, cwd=work)
    assert_token_via_run_secrets(home, argv, flag_values)
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
    import pty
    import struct
    import termios

    stored = {}
    seen = {}

    def fake_spawn(argv, master_read):
        assert argv == ["claude", "setup-token"]
        master, slave = pty.openpty()
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
    monkeypatch.setattr(pty, "spawn", fake_spawn)
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
    assert_token_via_run_secrets(home, argv, flag_values)


def test_in_directory_yolo_json_is_ignored(cy, run_cli, flag_values, dirs, capsys):
    home, work = dirs
    (work / ".yolo.json").write_text(json.dumps({"auth": "keychain"}))
    argv = run_cli([], home=home, cwd=work)
    # the in-directory file no longer configures anything: still a default
    # oauth-token run, not the keychain run the file asks for
    assert_token_via_run_secrets(home, argv, flag_values)
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


def test_mount_file_is_bind_mounted_but_not_added_as_dir(cy, run_cli, flag_values, tmp_path):
    # A file (e.g. a token file) can be mounted; it's bind-mounted like a dir but
    # NOT forwarded to claude as --add-dir (which is directory-only).
    home = tmp_path / "home"
    work = tmp_path / "work"
    for d in (home, work):
        d.mkdir()
    token = tmp_path / "token"
    token.write_text("sekret\n")
    argv = run_cli(["--mount", f"{token}:ro"], home=home, cwd=work)
    assert f"{token}:{token}:ro" in flag_values(argv, "-v")
    assert str(token) not in flag_values(claude_args(cy, argv), "--add-dir")


def test_mount_missing_path_exits(cy, run_cli, dirs):
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
    img = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    assert argv[img - 2 : img] == ["--network", "host"]


# --- YOLO_SESSION marker ----------------------------------------------------


def test_every_launch_sets_yolo_session_marker(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert "YOLO_SESSION=1" in flag_values(argv, "-e")


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


def _pyproject_version_str(cy):
    import pathlib
    import re

    text = (pathlib.Path(cy.__file__).resolve().parent / "pyproject.toml").read_text()
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)


def test_pyproject_version_reads_adjacent_file(cy):
    # Tests load yolo.py from the checkout, so pyproject.toml is adjacent (the
    # editable/standalone case) — this is exactly what _pyproject_version detects.
    assert cy._pyproject_version() == _pyproject_version_str(cy)


def test_base_version_prefers_pyproject_over_metadata(cy, monkeypatch):
    """Editable install: a frozen metadata snapshot must not shadow live pyproject."""
    import importlib.metadata as im

    monkeypatch.setattr(im, "version", lambda name: "0.0.0-stale")
    assert cy._base_version() == _pyproject_version_str(cy)


def test_base_version_falls_back_to_metadata_for_a_wheel(cy, monkeypatch):
    """Wheel install: no adjacent pyproject -> recorded package metadata."""
    import importlib.metadata as im

    monkeypatch.setattr(cy, "_pyproject_version", lambda: None)
    monkeypatch.setattr(im, "version", lambda name: "9.9.9")
    assert cy._base_version() == "9.9.9"


def test_base_version_unknown_with_neither(cy, monkeypatch):
    import importlib.metadata as im

    monkeypatch.setattr(cy, "_pyproject_version", lambda: None)

    def _missing(name):
        raise im.PackageNotFoundError(name)

    monkeypatch.setattr(im, "version", _missing)
    assert cy._base_version() == "unknown"


def _fake_git(cy, monkeypatch, *, head, dirty, tag_commit):
    """Stub cy._git so _git_suffix runs against a scripted repo state.

    head=None models "not a git repo" (a wheel). Otherwise HEAD is `head`; the
    v{base} tag resolves to `tag_commit` (None = tag absent); `dirty` toggles the
    working-tree state.
    """

    def fake(*args):
        if head is None:
            return None
        if args[:2] == ("rev-parse", "--short=7"):
            return head
        if args[0] == "status":
            return " M yolo.py" if dirty else ""
        if "--verify" in args:  # the v{base}^{commit} tag lookup
            return tag_commit
        if args == ("rev-parse", "HEAD"):
            return head
        return None

    monkeypatch.setattr(cy, "_git", fake)


def test_git_suffix_wheel_is_bare(cy, monkeypatch):
    _fake_git(cy, monkeypatch, head=None, dirty=False, tag_commit=None)
    assert cy._git_suffix("0.11.0") == ""


def test_git_suffix_editable_on_clean_release(cy, monkeypatch):
    # HEAD == the v0.11.0 tag, clean tree, but it's a live checkout (a wheel would
    # have returned bare above) -> +editable, not bare.
    _fake_git(cy, monkeypatch, head="abc1234", dirty=False, tag_commit="abc1234")
    assert cy._git_suffix("0.11.0") == "+editable"


def test_git_suffix_dirty_on_release(cy, monkeypatch):
    _fake_git(cy, monkeypatch, head="abc1234", dirty=True, tag_commit="abc1234")
    assert cy._git_suffix("0.11.0") == "+dirty"


def test_git_suffix_past_release_uses_sha(cy, monkeypatch):
    # HEAD diverges from the v0.11.0 tag (commit past the release / tag absent).
    _fake_git(cy, monkeypatch, head="def5678", dirty=False, tag_commit="abc1234")
    assert cy._git_suffix("0.11.0") == "+gdef5678"
    _fake_git(cy, monkeypatch, head="def5678", dirty=True, tag_commit=None)
    assert cy._git_suffix("0.11.0") == "+gdef5678.dirty"


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
    assert cargs[cargs.index("--name") + 1] == "feat"  # session named


# --- custom Dockerfile (--dockerfile) + content-addressed tag ----------------


def image_tag(argv):
    """The claude-yolo:<hash8> tag in a docker-run argv."""
    return next(a for a in argv if a.startswith("claude-yolo:"))


def test_default_run_uses_default_dockerfile_tag(cy, run_cli, dirs):
    import os

    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert image_tag(argv) == cy._image_tag(cy.DEFAULT_DOCKERFILE, os.getuid())


def test_custom_dockerfile_changes_the_image_tag(cy, run_cli, dirs, tmp_path):
    import os

    home, work = dirs
    df = tmp_path / "Dockerfile.custom"
    df.write_text("FROM ubuntu:24.04\nARG HOST_UID\nRUN echo marker\n")
    argv = run_cli(["--dockerfile", str(df)], home=home, cwd=work)
    tag = image_tag(argv)
    assert tag == cy._image_tag(df.read_text(), os.getuid())
    # ...and it is distinct from the built-in default's tag.
    assert tag != cy._image_tag(cy.DEFAULT_DOCKERFILE, os.getuid())


def test_missing_dockerfile_path_exits(cy, run_cli, dirs, tmp_path):
    home, work = dirs
    missing = tmp_path / "nope" / "Dockerfile"
    with pytest.raises(SystemExit) as exc:
        run_cli(["--dockerfile", str(missing)], home=home, cwd=work)
    assert "dockerfile" in str(exc.value)


def test_warns_on_unconfigured_dockerfile_yolo(cy, run_cli, dirs, capsys):
    # A Dockerfile.yolo in the session dir with no `dockerfile` config is a
    # silent no-op (the feature is opt-in) — nudge instead.
    home, work = dirs
    (work / "Dockerfile.yolo").write_text("FROM ubuntu:24.04\nARG HOST_UID\n")
    run_cli([], home=home, cwd=work)
    err = capsys.readouterr().err
    assert "Dockerfile.yolo" in err and "no `dockerfile` config" in err


def test_no_dockerfile_yolo_warning_when_configured(cy, run_cli, dirs, capsys):
    # When the file IS wired up (relative path resolves against the cwd), it's
    # used, not ignored — so no warning.
    home, work = dirs
    (work / "Dockerfile.yolo").write_text("FROM ubuntu:24.04\nARG HOST_UID\n")
    run_cli(["--dockerfile", "Dockerfile.yolo"], home=home, cwd=work)
    assert "no `dockerfile` config" not in capsys.readouterr().err


def test_no_dockerfile_yolo_warning_when_absent(cy, run_cli, dirs, capsys):
    home, work = dirs
    run_cli([], home=home, cwd=work)
    assert "Dockerfile.yolo" not in capsys.readouterr().err


def test_resume_refuses_when_cwd_session_running(cy, run_cli, dirs, monkeypatch):
    # A live session for this dir → can't launch a second container with the same
    # name; non-tmux refuses up front, before the (now-pointless) image build.
    home, work = dirs
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: "abc123")
    built = []
    monkeypatch.setattr(
        cy, "_build_image", lambda parsed, cwd: built.append(cwd) or "claude-yolo:x"
    )
    with pytest.raises(SystemExit) as exc:
        run_cli(["resume"], home=home, cwd=work)
    assert "already running" in str(exc.value)
    assert built == []  # refused before building


def test_stop_stops_cwd_session(cy, run_cli, dirs, monkeypatch):
    # `yolo stop` (no topic) stops the container running in this directory.
    home, work = dirs
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: "abc123def456")
    real_run = cy.subprocess.run
    stops = []

    def fake_run(cmd, **k):
        if cmd[:2] == ["docker", "stop"]:
            stops.append(cmd)
            return cy.subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["docker", "inspect"]:
            return cy.subprocess.CompletedProcess(cmd, 0, "", "")  # no labels → state '-'
        return real_run(cmd, **k)

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    assert run_cli(["stop"], home=home, cwd=work) is None  # terminal verb
    assert stops == [["docker", "stop", "abc123def456"]]


def test_docker_command_hidden_unless_verbose(cy, run_cli, dirs, capsys):
    home, work = dirs
    run_cli([], home=home, cwd=work)
    assert "docker run" not in capsys.readouterr().out  # hidden by default
    run_cli(["--verbose"], home=home, cwd=work)
    assert "docker run" in capsys.readouterr().out
    run_cli(["-v"], home=home, cwd=work)
    assert "docker run" in capsys.readouterr().out  # short form too


def test_build_docker_image_passes_uid_build_arg(cy, monkeypatch, tmp_path):
    # build_docker_image is stubbed in run_cli, so exercise the real builder
    # directly: it must tag with the content-addressed tag and pass the host UID
    # as the HOST_UID build ARG.
    calls = {}
    monkeypatch.setattr(cy.subprocess, "run", lambda cmd, **k: calls.setdefault("cmd", cmd))
    cy.build_docker_image("FROM scratch\n", "claude-yolo:abc12345", 4242)
    cmd = calls["cmd"]
    assert cmd[:3] == ["docker", "build", "-t"]
    assert "claude-yolo:abc12345" in cmd
    assert "--build-arg" in cmd and "HOST_UID=4242" in cmd


def test_build_context_contains_only_the_dockerfile(cy, monkeypatch):
    # The empty build context is what stops a custom Dockerfile's COPY/ADD from
    # reaching host files — capture the context dir (last build arg) and assert it
    # holds nothing but the Dockerfile.
    import pathlib

    seen = {}

    def fake_run(cmd, **k):
        build_dir = pathlib.Path(cmd[-1])
        seen["contents"] = sorted(p.name for p in build_dir.iterdir())

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    cy.build_docker_image("FROM scratch\n", "claude-yolo:abc12345", 4242)
    assert seen["contents"] == ["Dockerfile"]


# --- custom Dockerfile layering on the default (FROM ${YOLO_BASE}) -----------


def _parsed(cy, dockerfile=None):
    """A minimal namespace with the attributes _build_image reads."""
    import types

    return types.SimpleNamespace(dockerfile=dockerfile, rebuild_image=False)


def _record_builds(cy, monkeypatch):
    """Stub build_docker_image + _verify_image_user, returning the recorded builds."""
    builds = []

    def fake_build(text, tag, uid, *, build_args=None, no_cache=False):
        builds.append({"text": text, "tag": tag, "uid": uid, "build_args": build_args or {}})

    monkeypatch.setattr(cy, "build_docker_image", fake_build)
    monkeypatch.setattr(cy, "_verify_image_user", lambda tag: None)
    return builds


def test_build_image_default_builds_once(cy, monkeypatch, tmp_path):
    import os

    builds = _record_builds(cy, monkeypatch)
    tag = cy._build_image(_parsed(cy), tmp_path)
    assert len(builds) == 1
    assert builds[0]["text"] == cy.DEFAULT_DOCKERFILE
    assert tag == cy._image_tag(cy.DEFAULT_DOCKERFILE, os.getuid())


def test_build_image_fully_custom_builds_once_no_base(cy, monkeypatch, tmp_path):
    import os

    builds = _record_builds(cy, monkeypatch)
    df = tmp_path / "Dockerfile"
    df.write_text("FROM ubuntu:24.04\nARG HOST_UID\nRUN echo hi\n")
    tag = cy._build_image(_parsed(cy, str(df)), tmp_path)
    # No YOLO_BASE reference → single build, no base, no YOLO_BASE build arg.
    assert len(builds) == 1
    assert "YOLO_BASE" not in builds[0]["build_args"]
    assert tag == cy._image_tag(df.read_text(), os.getuid())


def test_build_image_layers_on_base_when_yolo_base_referenced(cy, monkeypatch, tmp_path):
    import os

    builds = _record_builds(cy, monkeypatch)
    df = tmp_path / "Dockerfile"
    text = "ARG YOLO_BASE\nFROM ${YOLO_BASE}\nRUN sudo apt-get install -y foo\n"
    df.write_text(text)
    tag = cy._build_image(_parsed(cy, str(df)), tmp_path)

    uid = os.getuid()
    base_tag = cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)
    # Two builds: the default as the base first, then the custom image.
    assert len(builds) == 2
    assert builds[0]["text"] == cy.DEFAULT_DOCKERFILE and builds[0]["tag"] == base_tag
    assert builds[1]["text"] == text
    assert builds[1]["build_args"].get("YOLO_BASE") == base_tag
    # Final tag folds in the base tag so a base change yields a distinct image.
    assert tag == cy._image_tag(text + base_tag, uid)


def test_build_image_relative_dockerfile_resolves_against_cwd(cy, monkeypatch, tmp_path):
    # A relative --dockerfile path is read from the session cwd (a worktree dir),
    # not the process cwd — so a worktree can carry its own Dockerfile.
    builds = _record_builds(cy, monkeypatch)
    (tmp_path / "Dockerfile.yolo").write_text("FROM ubuntu:24.04\nARG HOST_UID\n")
    cy._build_image(_parsed(cy, "Dockerfile.yolo"), tmp_path)
    assert builds[0]["text"] == "FROM ubuntu:24.04\nARG HOST_UID\n"


def test_build_image_absolute_dockerfile_ignores_cwd(cy, monkeypatch, tmp_path):
    # An absolute path is used as-is regardless of the session cwd.
    builds = _record_builds(cy, monkeypatch)
    df = tmp_path / "abs" / "Dockerfile"
    df.parent.mkdir()
    df.write_text("FROM ubuntu:24.04\nARG HOST_UID\nRUN echo abs\n")
    cy._build_image(_parsed(cy, str(df)), tmp_path / "unrelated")
    assert builds[0]["text"] == df.read_text()


def test_build_image_verifies_user_for_custom(cy, monkeypatch, tmp_path):
    # A custom build must be USER-verified; the default must not be.
    seen = []
    monkeypatch.setattr(cy, "build_docker_image", lambda *a, **k: None)
    monkeypatch.setattr(cy, "_verify_image_user", lambda tag: seen.append(tag))

    cy._build_image(_parsed(cy), tmp_path)  # default
    assert seen == []

    df = tmp_path / "Dockerfile"
    df.write_text("FROM ubuntu:24.04\nARG HOST_UID\n")
    cy._build_image(_parsed(cy, str(df)), tmp_path)  # custom
    assert len(seen) == 1


def test_verify_image_user_accepts_claude_rejects_root(cy, monkeypatch):
    import subprocess

    def inspect(user):
        return lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=user, stderr="")

    monkeypatch.setattr(cy.subprocess, "run", inspect("claude\n"))
    cy._verify_image_user("claude-yolo:tag")  # ok, no raise

    monkeypatch.setattr(cy.subprocess, "run", inspect("\n"))  # USER unset → root
    with pytest.raises(SystemExit) as exc:
        cy._verify_image_user("claude-yolo:tag")
    assert "USER claude" in str(exc.value)


def test_custom_dockerfile_with_yolo_base_tag_in_launch_argv(cy, run_cli, dirs, tmp_path):
    import os

    home, work = dirs
    df = tmp_path / "Dockerfile"
    text = "ARG YOLO_BASE\nFROM ${YOLO_BASE}\nRUN sudo apt-get install -y foo\n"
    df.write_text(text)
    argv = run_cli(["--dockerfile", str(df)], home=home, cwd=work)
    uid = os.getuid()
    base_tag = cy._image_tag(cy.DEFAULT_DOCKERFILE, uid)
    assert image_tag(argv) == cy._image_tag(text + base_tag, uid)


# --- `yolo dockerfile` (dump the default) -----------------------------------


def test_dockerfile_verb_prints_default(cy, run_cli, dirs, capsys):
    home, work = dirs
    argv = run_cli(["dockerfile"], home=home, cwd=work)
    assert argv is None  # terminal verb: no container launched
    assert capsys.readouterr().out == cy.DEFAULT_DOCKERFILE


def test_dockerfile_verb_rejects_a_topic(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["dockerfile", "extra"], home=home, cwd=work)


def test_dockerfile_verb_custom_prints_template(cy, run_cli, dirs, capsys):
    home, work = dirs
    argv = run_cli(["dockerfile", "--custom"], home=home, cwd=work)
    assert argv is None  # terminal verb: no container launched
    out = capsys.readouterr().out
    assert out == cy.CUSTOM_DOCKERFILE
    # The template must layer on the default (FROM ${YOLO_BASE}) and end as `claude`,
    # the two invariants _build_image / _verify_image_user enforce.
    assert "FROM ${YOLO_BASE}" in out
    assert out.rstrip().endswith("USER claude")


def test_custom_flag_rejected_outside_dockerfile(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["--custom"], home=home, cwd=work)
