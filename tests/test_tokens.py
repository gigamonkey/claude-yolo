"""Tests for the OAuth-token bookkeeping: the tokens.json registry, the launch-time
expiry warning, the implicit-mint consent prompt, and the tokens / forget-token verbs.

The credential store itself is never touched: the wrapping helpers
(_keychain_has / _keychain_delete) and the registry-backed _token_minted are
stubbed per test.
"""

import datetime
import json

import pytest


@pytest.fixture
def dirs(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


@pytest.fixture
def home_env(monkeypatch, dirs):
    """Point HOME (and so Path.home() / the registry) at the test home dir."""
    home, _ = dirs
    monkeypatch.setenv("HOME", str(home))
    return home


def read_registry(home):
    return json.loads((home / ".claude-yolo" / "tokens.json").read_text())


# --- registry read/write ------------------------------------------------------


def test_write_token_entry_records_and_returns_previous(cy, home_env):
    assert cy._write_token_entry(None) is None  # first mint: nothing replaced
    entry = read_registry(home_env)[cy.OAUTH_KC_SERVICE]
    assert entry["config_dir"] is None
    first_minted = entry["minted"]
    datetime.datetime.fromisoformat(first_minted)  # a parseable timestamp

    previous = cy._write_token_entry(None)  # re-mint: old entry handed back
    assert previous["minted"] == first_minted


def test_write_token_entry_is_keyed_per_config_dir(cy, home_env, tmp_path):
    cfg = tmp_path / "altcfg"
    cfg.mkdir()
    cy._write_token_entry(None)
    cy._write_token_entry(str(cfg))
    registry = read_registry(home_env)
    assert set(registry) == {cy.OAUTH_KC_SERVICE, cy._oauth_service(str(cfg))}
    assert registry[cy._oauth_service(str(cfg))]["config_dir"] == str(cfg.resolve())


def test_remove_token_entry(cy, home_env):
    cy._write_token_entry(None)
    removed = cy._remove_token_entry(cy.OAUTH_KC_SERVICE)
    assert removed is not None
    assert read_registry(home_env) == {}
    assert cy._remove_token_entry(cy.OAUTH_KC_SERVICE) is None  # already gone


def test_malformed_registry_exits_naming_the_file(cy, home_env):
    path = home_env / ".claude-yolo" / "tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    with pytest.raises(SystemExit) as exc:
        cy._read_tokens_file()
    assert "tokens.json" in str(exc.value)


# --- expiry warning (mint date sourced from the tokens.json registry) ----------


def _days_ago(n):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n)


def test_token_minted_reads_registry(cy, home_env):
    cy._write_token_entry(None)
    minted = cy._token_minted(None)
    assert minted is not None
    # within a few seconds of now (just minted)
    assert abs((datetime.datetime.now(datetime.timezone.utc) - minted).total_seconds()) < 60
    # missing/unparseable -> None
    assert cy._token_minted(str(cy.pathlib.Path("/no/such/cfg"))) is None


def test_warn_token_expiry_warns_inside_the_window(cy, monkeypatch, capsys):
    monkeypatch.setattr(cy, "_token_minted", lambda c: _days_ago(360))  # expires in ~5d
    cy._warn_token_expiry(None)
    err = capsys.readouterr().err
    assert "expires around" in err
    assert "yolo setup-token" in err


def test_warn_token_expiry_reports_already_expired(cy, monkeypatch, capsys):
    monkeypatch.setattr(cy, "_token_minted", lambda c: _days_ago(400))
    cy._warn_token_expiry(None)
    assert "expired around" in capsys.readouterr().err


def test_warn_token_expiry_quiet_when_fresh_or_unreadable(cy, monkeypatch, capsys):
    monkeypatch.setattr(cy, "_token_minted", lambda c: _days_ago(100))
    cy._warn_token_expiry(None)
    monkeypatch.setattr(cy, "_token_minted", lambda c: None)
    cy._warn_token_expiry(None)
    assert capsys.readouterr().err == ""


def test_ensure_oauth_token_checks_expiry_of_cached_token(cy, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cy, "_read_oauth_token", lambda c: "sk-ant-oat-CACHED")
    monkeypatch.setattr(cy, "_token_minted", lambda c: _days_ago(360))
    assert cy.ensure_oauth_token(None) == "sk-ant-oat-CACHED"
    assert "expires around" in capsys.readouterr().err


def test_ensure_oauth_token_env_token_skips_keychain_and_warning(cy, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-FROMENV")
    monkeypatch.setattr(cy, "_read_oauth_token", lambda c: pytest.fail("keychain read in env mode"))
    assert cy.ensure_oauth_token(None) == "sk-ant-oat-FROMENV"
    assert capsys.readouterr().err == ""


# --- implicit-mint consent prompt ----------------------------------------------


def _no_cached_token(cy, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(cy, "_read_oauth_token", lambda c: None)
    monkeypatch.setattr(cy.sys.stdin, "isatty", lambda: True)


def test_implicit_mint_asks_first_and_default_is_yes(cy, monkeypatch, capsys):
    _no_cached_token(cy, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(cy, "generate_oauth_token", lambda c: "sk-ant-oat-MINTED")
    assert cy.ensure_oauth_token(None) == "sk-ant-oat-MINTED"
    out = capsys.readouterr().out
    assert "1-year" in out
    assert "yolo forget-token" in out
    assert cy.TOKEN_REVOKE_URL in out


def test_implicit_mint_declined_exits_with_guidance(cy, monkeypatch):
    _no_cached_token(cy, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr(cy, "generate_oauth_token", lambda c: pytest.fail("minted despite decline"))
    with pytest.raises(SystemExit) as exc:
        cy.ensure_oauth_token(None)
    assert "--auth keychain" in str(exc.value)


# --- `tokens` verb -------------------------------------------------------------


def write_registry(home, mapping):
    d = home / ".claude-yolo"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tokens.json").write_text(json.dumps(mapping))


def test_tokens_verb_lists_with_status_and_expiry(cy, run_cli, dirs, monkeypatch, capsys):
    home, work = dirs
    write_registry(
        home,
        {
            "claude-yolo-oauth-token": {"config_dir": None, "minted": "2026-06-01T10:00:00+00:00"},
            "claude-yolo-oauth-token-aaaaaaaa": {
                "config_dir": "/Users/x/.claude-work",
                "minted": "2026-01-15T10:00:00+00:00",
            },
        },
    )
    # default entry present in the store and untouched; the alt one was deleted
    monkeypatch.setattr(cy, "_keychain_has", lambda s: s == "claude-yolo-oauth-token")

    assert run_cli(["tokens"], home=home, cwd=work) is None  # terminal verb
    out = capsys.readouterr().out
    assert "(default ~/.claude)" in out
    assert "/Users/x/.claude-work" in out
    assert "2027-06-01" in out  # minted 2026-06-01 + 365d (from the registry)
    assert "stale (not in store)" in out
    assert cy.TOKEN_REVOKE_URL in out


def test_tokens_verb_empty_registry(cy, run_cli, dirs, capsys):
    home, work = dirs
    run_cli(["tokens"], home=home, cwd=work)
    assert "No tokens recorded" in capsys.readouterr().out


# --- `forget-token` verb --------------------------------------------------------


def test_forget_token_deletes_keychain_and_registry(cy, run_cli, dirs, monkeypatch, capsys):
    home, work = dirs
    write_registry(
        home,
        {"claude-yolo-oauth-token": {"config_dir": None, "minted": "2026-06-01T10:00:00+00:00"}},
    )
    deleted = []
    monkeypatch.setattr(cy, "_keychain_delete", lambda s: (deleted.append(s), True)[1])

    assert run_cli(["forget-token"], home=home, cwd=work) is None  # terminal verb
    assert deleted == ["claude-yolo-oauth-token"]
    assert read_registry(home) == {}
    out = capsys.readouterr().out
    assert "minted 2026-06-01" in out
    # honest about what "forget" can and cannot do
    assert "still valid server-side" in out
    assert cy.TOKEN_REVOKE_URL in out
    assert "outside yolo's control" in out


def test_forget_token_honours_config_dir(cy, run_cli, dirs, monkeypatch, tmp_path):
    home, work = dirs
    cfg = tmp_path / "altcfg"
    cfg.mkdir()
    deleted = []
    monkeypatch.setattr(cy, "_keychain_delete", lambda s: (deleted.append(s), True)[1])
    run_cli(["forget-token", "--config-dir", str(cfg)], home=home, cwd=work)
    assert deleted == [cy._oauth_service(str(cfg))]


def test_forget_token_nothing_to_forget(cy, run_cli, dirs, monkeypatch, capsys):
    home, work = dirs
    monkeypatch.setattr(cy, "_keychain_delete", lambda s: False)
    run_cli(["forget-token"], home=home, cwd=work)
    assert "No token cached" in capsys.readouterr().out


def test_forget_token_removes_stale_registry_entry(cy, run_cli, dirs, monkeypatch, capsys):
    home, work = dirs
    write_registry(
        home,
        {"claude-yolo-oauth-token": {"config_dir": None, "minted": "2026-06-01T10:00:00+00:00"}},
    )
    monkeypatch.setattr(cy, "_keychain_delete", lambda s: False)  # not in the keychain
    run_cli(["forget-token"], home=home, cwd=work)
    assert read_registry(home) == {}
    assert "stale registry entry" in capsys.readouterr().out
