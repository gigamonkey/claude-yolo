"""Tests for the keychain-backed secrets feature and the per-session run-dir GC.

The keychain (`security`) and `pbpaste` are never touched: the wrapping helpers
(_keychain_has / _keychain_delete / _store_secret_value / _read_secret_value) and
the clipboard/stdin/prompt input sources are stubbed per test, like test_tokens.py.
"""

import json
import os

import pytest


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def read_registry(home):
    return json.loads((home / ".claude-yolo" / "secrets.json").read_text())


def write_registry(home, mapping):
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "secrets.json").write_text(json.dumps(mapping))


def mount_values(argv):
    """All `-v` mount specs in a docker-run argv."""
    return [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]


# --- spec parsing -------------------------------------------------------------


def test_parse_bare_name_is_env_target(cy):
    assert cy._parse_secret_spec("GH_TOKEN") == ("GH_TOKEN", "env", "GH_TOKEN", False)


def test_parse_env_rename(cy):
    assert cy._parse_secret_spec("DB_PASSWORD:PGPASSWORD") == (
        "DB_PASSWORD",
        "env",
        "PGPASSWORD",
        False,
    )


def test_parse_file_target_abs(cy):
    assert cy._parse_secret_spec("X:/etc/secret") == ("X", "file", "/etc/secret", False)


def test_parse_file_target_tilde_expands_to_container_home_not_host(cy, monkeypatch):
    # ~ must resolve to the *container* home, never the host $HOME.
    monkeypatch.setenv("HOME", "/Users/somebody")
    name, kind, target, eph = cy._parse_secret_spec("DEPLOY_KEY:~/.ssh/id_ed25519")
    assert (kind, target) == ("file", "/home/claude/.ssh/id_ed25519")
    assert "/Users/somebody" not in target
    # bare ~ -> the container home itself
    assert cy._parse_secret_spec("K:~")[2] == "/home/claude"


def test_parse_ephemeral_marker(cy):
    assert cy._parse_secret_spec("GH_TOKEN!") == ("GH_TOKEN", "env", "GH_TOKEN", True)
    assert cy._parse_secret_spec("A:B!") == ("A", "env", "B", True)


def test_parse_file_target_cannot_be_ephemeral(cy):
    with pytest.raises(SystemExit) as exc:
        cy._parse_secret_spec("K:/path!")
    assert "ephemeral" in str(exc.value)


def test_parse_invalid_name_and_env_target(cy):
    with pytest.raises(SystemExit):
        cy._parse_secret_spec("9bad")
    with pytest.raises(SystemExit):
        cy._parse_secret_spec("OK:9bad")


def test_resolve_dedups_and_collision_higher_layer_wins(cy):
    # exact dup collapses; B:X and C:X collide on env target X -> later (C) wins.
    resolved = cy._resolve_secret_specs(["A", "A", "B:X", "C:X"])
    assert resolved == [("A", "env", "A", False), ("C", "env", "X", False)]


# --- keychain service naming --------------------------------------------------


def test_service_names(cy):
    assert cy._secret_service("GH", "global") == "claude-yolo-secret-GH"
    proj = cy._secret_service("GH", "project", "/repo")
    assert proj.startswith("claude-yolo-secret-") and proj.endswith("-GH")
    # project hash is stable and one-way
    assert cy._secret_service("GH", "project", "/repo") == proj
    assert cy._secret_service("GH", "project", "/other") != proj


# --- registry -----------------------------------------------------------------


def test_write_and_remove_secret_entry(cy, monkeypatch, dirs):
    home, _ = dirs
    monkeypatch.setenv("HOME", str(home))
    cy._write_secret_entry("claude-yolo-secret-GH", "global", "GH", None)
    entry = read_registry(home)["claude-yolo-secret-GH"]
    assert entry["scope"] == "global"
    assert entry["name"] == "GH"
    assert entry["created"] and entry["modified"]

    # re-upsert preserves the original created timestamp
    created = entry["created"]
    cy._write_secret_entry("claude-yolo-secret-GH", "global", "GH", None)
    assert read_registry(home)["claude-yolo-secret-GH"]["created"] == created

    assert cy._remove_secret_entry("claude-yolo-secret-GH") is not None
    assert read_registry(home) == {}
    assert cy._remove_secret_entry("claude-yolo-secret-GH") is None


def test_malformed_registry_exits_naming_file(cy, monkeypatch, dirs):
    home, _ = dirs
    monkeypatch.setenv("HOME", str(home))
    path = home / ".claude-yolo" / "secrets.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    with pytest.raises(SystemExit) as exc:
        cy._read_secrets_file()
    assert "secrets.json" in str(exc.value)


def test_read_secret_value_strips_single_trailing_newline(cy, monkeypatch):
    class R:
        returncode = 0
        stdout = "the-value\n"

    monkeypatch.setattr(cy.subprocess, "run", lambda *a, **k: R())
    assert cy._read_secret_value("svc") == "the-value"


# --- `secret set` -------------------------------------------------------------


def test_secret_set_clipboard(cy, monkeypatch, dirs, capsys):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    stored = {}
    monkeypatch.setattr(cy, "_store_secret_value", lambda svc, val: stored.update(svc=svc, val=val))

    class R:
        returncode = 0
        stdout = "pasted-token\n"

    monkeypatch.setattr(cy.subprocess, "run", lambda *a, **k: R())
    cy.do_secret_set("GH_TOKEN", project=False, clipboard=True, cwd=work)
    assert stored == {"svc": "claude-yolo-secret-GH_TOKEN", "val": "pasted-token"}
    assert read_registry(home)["claude-yolo-secret-GH_TOKEN"]["name"] == "GH_TOKEN"
    assert "global" in capsys.readouterr().out


def test_secret_set_stdin_when_piped(cy, monkeypatch, dirs):
    import io

    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    stored = {}
    monkeypatch.setattr(cy, "_store_secret_value", lambda svc, val: stored.update(svc=svc, val=val))
    monkeypatch.setattr(cy.sys, "stdin", io.StringIO("from-stdin\n"))
    cy.do_secret_set("TOK", project=False, clipboard=False, cwd=work)
    assert stored["val"] == "from-stdin"


def test_secret_set_interactive_prompt(cy, monkeypatch, dirs):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    stored = {}
    monkeypatch.setattr(cy, "_store_secret_value", lambda svc, val: stored.update(val=val))
    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cy.getpass, "getpass", lambda prompt: "typed-secret")
    cy.do_secret_set("TOK", project=False, clipboard=False, cwd=work)
    assert stored["val"] == "typed-secret"


def test_secret_set_rejects_invalid_name(cy, monkeypatch, dirs):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(SystemExit) as exc:
        cy.do_secret_set("9bad", project=False, clipboard=False, cwd=work)
    assert "shell identifier" in str(exc.value)


def test_secret_set_rejects_empty_value(cy, monkeypatch, dirs):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cy.getpass, "getpass", lambda prompt: "")
    with pytest.raises(SystemExit) as exc:
        cy.do_secret_set("TOK", project=False, clipboard=False, cwd=work)
    assert "empty" in str(exc.value)


def test_secret_set_project_scope_uses_project_key(cy, monkeypatch, dirs):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cy, "_project_key", lambda cwd: "/the/repo")
    stored = {}
    monkeypatch.setattr(cy, "_store_secret_value", lambda svc, val: stored.update(svc=svc))
    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cy.getpass, "getpass", lambda prompt: "v")
    cy.do_secret_set("TOK", project=True, clipboard=False, cwd=work)
    assert stored["svc"] == cy._secret_service("TOK", "project", "/the/repo")
    entry = read_registry(home)[stored["svc"]]
    assert entry["scope"] == "project" and entry["project_key"] == "/the/repo"


# --- `secret rm` --------------------------------------------------------------


def test_secret_rm_deletes_keychain_and_registry(cy, monkeypatch, dirs, capsys):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    write_registry(
        home,
        {"claude-yolo-secret-GH": {"scope": "global", "name": "GH", "project_key": None}},
    )
    deleted = []
    monkeypatch.setattr(cy, "_keychain_delete", lambda s: (deleted.append(s), True)[1])
    cy.do_secret_rm("GH", project=False, cwd=work)
    assert deleted == ["claude-yolo-secret-GH"]
    assert read_registry(home) == {}
    assert "Removed secret" in capsys.readouterr().out


def test_secret_rm_nothing_to_remove(cy, monkeypatch, dirs):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cy, "_keychain_delete", lambda s: False)
    with pytest.raises(SystemExit) as exc:
        cy.do_secret_rm("GH", project=False, cwd=work)
    assert "no global-scope secret" in str(exc.value)


# --- `secret list` ------------------------------------------------------------


def test_secret_list_filters_to_global_and_current_project(cy, monkeypatch, dirs, capsys):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cy, "_project_key", lambda cwd: "/this/repo")
    monkeypatch.setattr(cy, "_keychain_has", lambda s: True)
    write_registry(
        home,
        {
            "claude-yolo-secret-G": {
                "scope": "global",
                "name": "G",
                "project_key": None,
                "created": "2026-06-01T00:00:00+00:00",
            },
            "claude-yolo-secret-h1-P": {
                "scope": "project",
                "name": "P",
                "project_key": "/this/repo",
                "created": "2026-06-02T00:00:00+00:00",
            },
            "claude-yolo-secret-h2-Q": {
                "scope": "project",
                "name": "Q",
                "project_key": "/other/repo",
                "created": "2026-06-03T00:00:00+00:00",
            },
        },
    )
    cy.do_secret_list(work, all_projects=False)
    out = capsys.readouterr().out
    assert "G" in out and "P" in out
    assert "Q" not in out  # other project's secret hidden without --all

    cy.do_secret_list(work, all_projects=True)
    assert "Q" in capsys.readouterr().out


def test_secret_list_marks_stale(cy, monkeypatch, dirs, capsys):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cy, "_keychain_has", lambda s: False)
    write_registry(
        home,
        {"claude-yolo-secret-G": {"scope": "global", "name": "G", "project_key": None}},
    )
    cy.do_secret_list(work, all_projects=False)
    assert "stale (not in keychain)" in capsys.readouterr().out


def test_secret_list_empty(cy, monkeypatch, dirs, capsys):
    home, work = dirs
    monkeypatch.setenv("HOME", str(home))
    cy.do_secret_list(work, all_projects=False)
    assert "No secrets recorded" in capsys.readouterr().out


# --- launch wiring ------------------------------------------------------------


def _stub_secret_values(cy, monkeypatch, values):
    """Make every configured secret resolve to `values[name]` (project-key ignored)."""
    monkeypatch.setattr(cy, "_resolve_secret_value", lambda name, pk: values.get(name))


def test_launch_env_secret_mounts_run_secrets_rw_and_sources_loader(cy, run_cli, monkeypatch, dirs):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {"GH_TOKEN": "ghs_xxx"})
    argv = run_cli(["--secret", "GH_TOKEN"], home=home, cwd=work)

    # the env loader dir is mounted rw at /run/secrets
    mounts = mount_values(argv)
    assert any(m.endswith(":/run/secrets:rw") for m in mounts), mounts
    # the staged file (named for the env var) exists chmod 600, no trailing newline
    secrets_dir = home / ".claude-yolo-run" / "work" / "secrets"
    f = secrets_dir / "GH_TOKEN"
    assert f.read_text() == "ghs_xxx"
    assert oct(f.stat().st_mode)[-3:] == "600"
    # claude is launched via the bash wrapper that sources the loader
    assert "--entrypoint" in argv
    joined = " ".join(argv)
    assert "/etc/yolo/load-secrets.sh" in joined
    # the value never appears in the docker argv
    assert "ghs_xxx" not in joined


def test_launch_env_rename_and_ephemeral(cy, run_cli, monkeypatch, dirs):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {"DB": "pw"})
    run_cli(["--secret", "DB:PGPASSWORD!"], home=home, cwd=work)
    secrets_dir = home / ".claude-yolo-run" / "work" / "secrets"
    assert (secrets_dir / "PGPASSWORD").read_text() == "pw"
    # ephemeral marker sits beside it for the loader to act on
    assert (secrets_dir / "PGPASSWORD.ephemeral").exists()
    assert not (secrets_dir / "DB").exists()


def test_launch_file_secret_mounts_ro_at_path(cy, run_cli, monkeypatch, dirs):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {"DEPLOY_KEY": "PRIVATEKEY"})
    # --auth keychain so the oauth token doesn't add its own /run/secrets mount —
    # this isolates the file-target path (no env-target secret here).
    argv = run_cli(
        ["--auth", "keychain", "--secret", "DEPLOY_KEY:~/.ssh/id_ed25519"], home=home, cwd=work
    )
    mounts = mount_values(argv)
    assert any(m.endswith(":/home/claude/.ssh/id_ed25519:ro") for m in mounts), mounts
    # no /run/secrets mount, since there's no env-target secret
    assert not any("/run/secrets" in m for m in mounts)
    assert "PRIVATEKEY" not in " ".join(argv)


def test_launch_missing_secret_exits(cy, run_cli, monkeypatch, dirs):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {})  # nothing in the keychain
    with pytest.raises(SystemExit) as exc:
        run_cli(["--secret", "NOPE"], home=home, cwd=work)
    assert "yolo secret set NOPE" in str(exc.value)


def test_launch_warns_on_host_visible_file_target(cy, run_cli, monkeypatch, dirs, capsys):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {"K": "v"})
    run_cli(["--secret", "K:/home/claude/.claude/secret"], home=home, cwd=work)
    assert "host-visible bind mount" in capsys.readouterr().err


def test_launch_no_secrets_no_run_secrets_mount(cy, run_cli, dirs):
    # --auth keychain: with no user secrets and no oauth token, nothing rides
    # /run/secrets (the default oauth-token mode mounts it for the token).
    home, work = dirs
    argv = run_cli(["--auth", "keychain"], home=home, cwd=work)
    assert not any("/run/secrets" in m for m in mount_values(argv))


# --- the Anthropic OAuth token rides the same transport ----------------------


def test_oauth_token_staged_via_run_secrets_not_argv(cy, run_cli, dirs):
    # Default (oauth-token) mode delivers CLAUDE_CODE_OAUTH_TOKEN through the
    # /run/secrets file transport instead of -e, so it's off the docker-run argv
    # (and thus docker inspect / host ps / tmux pane command).
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert not any("CLAUDE_CODE_OAUTH_TOKEN" in a for a in argv)
    assert "sk-ant-oat-TESTTOKEN" not in " ".join(argv)
    assert any(m.endswith(":/run/secrets:rw") for m in mount_values(argv))
    token_file = home / ".claude-yolo-run" / "work" / "secrets" / "CLAUDE_CODE_OAUTH_TOKEN"
    assert token_file.read_text() == "sk-ant-oat-TESTTOKEN"
    assert oct(token_file.stat().st_mode)[-3:] == "600"
    # claude is launched through the loader-sourcing wrapper
    assert "--entrypoint" in argv and "/etc/yolo/load-secrets.sh" in " ".join(argv)


def test_oauth_token_coexists_with_user_secret_in_run_secrets(cy, run_cli, monkeypatch, dirs):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {"GH_TOKEN": "ghs_x"})
    run_cli(["--secret", "GH_TOKEN"], home=home, cwd=work)
    secrets_dir = home / ".claude-yolo-run" / "work" / "secrets"
    # both the token and the user secret share the one /run/secrets dir
    assert (secrets_dir / "CLAUDE_CODE_OAUTH_TOKEN").read_text() == "sk-ant-oat-TESTTOKEN"
    assert (secrets_dir / "GH_TOKEN").read_text() == "ghs_x"


def test_keychain_auth_keeps_token_off_run_secrets(cy, run_cli, dirs):
    # keychain mode has no env token, so nothing is staged for it.
    home, work = dirs
    argv = run_cli(["--auth", "keychain"], home=home, cwd=work)
    assert not (home / ".claude-yolo-run" / "work" / "secrets").exists()
    assert not any("/run/secrets" in m for m in mount_values(argv))


# --- config-layer concatenation ----------------------------------------------


def test_secrets_config_concatenates_across_layers(cy, run_cli, monkeypatch, dirs):
    home, work = dirs
    _stub_secret_values(cy, monkeypatch, {"A": "1", "B": "2"})
    # global config supplies one secret; the CLI adds another -> both injected
    (home / ".yolo.json").write_text(json.dumps({"secrets": ["A"]}))
    run_cli(["--secret", "B"], home=home, cwd=work)
    secrets_dir = home / ".claude-yolo-run" / "work" / "secrets"
    assert (secrets_dir / "A").exists()
    assert (secrets_dir / "B").exists()


# --- config verb edits --------------------------------------------------------


def test_config_add_remove_secret(cy, run_cli, dirs, capsys):
    home, work = dirs
    run_cli(["config", "--add-secret", "GH_TOKEN"], home=home, cwd=work)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    entry = next(iter(projects.values()))
    assert entry["secrets"] == ["GH_TOKEN"]

    run_cli(["config", "--add-secret", "DB:PGPASSWORD"], home=home, cwd=work)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert next(iter(projects.values()))["secrets"] == ["GH_TOKEN", "DB:PGPASSWORD"]

    run_cli(["config", "--remove-secret", "GH_TOKEN"], home=home, cwd=work)
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    assert next(iter(projects.values()))["secrets"] == ["DB:PGPASSWORD"]


def test_config_add_secret_validates_spec(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit):
        run_cli(["config", "--add-secret", "9bad"], home=home, cwd=work)


def test_secret_replace_conflicts_with_add(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["config", "--secret", "A", "--add-secret", "B"], home=home, cwd=work)
    assert "--add-secret" in str(exc.value)


# --- verb gating --------------------------------------------------------------


def test_project_flag_only_for_secret(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["start", "--project"], home=home, cwd=work)
    assert "--project" in str(exc.value)


def test_clipboard_flag_only_for_secret(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["start", "--clipboard"], home=home, cwd=work)
    assert "--clipboard" in str(exc.value)


def test_secret_set_dispatch_routes_name_through_main(cy, run_cli, monkeypatch, dirs):
    # `yolo secret set NAME` -> verb=secret, topic=set, extra_args=[NAME].
    home, work = dirs
    stored = {}
    monkeypatch.setattr(cy, "_store_secret_value", lambda svc, val: stored.update(svc=svc, val=val))
    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cy.getpass, "getpass", lambda prompt: "v")
    assert run_cli(["secret", "set", "GH_TOKEN"], home=home, cwd=work) is None  # terminal
    assert stored["svc"] == "claude-yolo-secret-GH_TOKEN"
    assert read_registry(home)["claude-yolo-secret-GH_TOKEN"]["name"] == "GH_TOKEN"


def test_secret_set_requires_exactly_one_name(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["secret", "set"], home=home, cwd=work)
    assert "usage" in str(exc.value)


def test_secret_without_subcommand_errors(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["secret"], home=home, cwd=work)
    assert "subcommand" in str(exc.value)


def test_extra_args_rejected_for_non_secret_verb(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit) as exc:
        run_cli(["start", "topicname", "stray"], home=home, cwd=work)
    assert "unexpected argument" in str(exc.value)


# --- run-dir GC ---------------------------------------------------------------


def test_gc_run_dir_removes_only_dead_sessions(cy, monkeypatch, tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    alive = run_root / "alive-container"
    dead = run_root / "dead-container"
    alive.mkdir()
    dead.mkdir()
    (alive / "credentials.json").write_text("{}")
    (dead / "credentials.json").write_text("{}")

    monkeypatch.setattr(cy, "_run_dir", lambda: run_root)
    monkeypatch.setattr(cy, "_running_container_names", lambda: {"alive-container"})
    cy._gc_run_dir()

    assert alive.is_dir()  # still running -> kept (parallel-safe)
    assert not dead.exists()  # finished -> reclaimed


def test_session_run_dir_is_700(cy, monkeypatch, tmp_path):
    monkeypatch.setattr(cy, "_run_dir", lambda: tmp_path / "run")
    d = cy._session_run_dir("my-container")
    assert d == tmp_path / "run" / "my-container"
    assert oct(d.stat().st_mode)[-3:] == "700"


# --- credential temp-file retrofit --------------------------------------------


def test_masking_credfile_lands_in_run_dir_chmod_600(cy, tmp_path):
    run_dir = tmp_path / "session"
    run_dir.mkdir()
    path = cy._masking_credfile(run_dir)
    assert os.path.dirname(path) == str(run_dir)
    p = tmp_path / "session" / "credentials-mask.json"
    assert p.read_text() == "{}"
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_extract_credentials_writes_run_dir(cy, monkeypatch, tmp_path):
    run_dir = tmp_path / "session"
    run_dir.mkdir()

    class R:
        stdout = b'{"creds": true}'

    monkeypatch.setattr(cy.subprocess, "run", lambda *a, **k: R())
    path = cy.extract_credentials(None, run_dir)
    assert os.path.dirname(path) == str(run_dir)
    assert (tmp_path / "session" / "credentials.json").read_bytes() == b'{"creds": true}'
    assert oct((tmp_path / "session" / "credentials.json").stat().st_mode)[-3:] == "600"
