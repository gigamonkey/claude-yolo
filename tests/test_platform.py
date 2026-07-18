"""Tests for the host-platform abstraction added for multiplatform support:
the _HOST helpers, the keyring/file credential store, the ssh-agent socket
selection, the cross-platform clipboard reader, the run-dir location, and the
webbrowser-based _open_url. The real keyring is never touched (conftest forces
the file store); HOME is pointed at a tmp dir wherever the file store is hit.
"""

import pathlib

import pytest

# --- platform helpers ----------------------------------------------------------


def test_platform_helpers_track_host(cy, monkeypatch):
    for host, macos, linux, windows in [
        ("darwin", True, False, False),
        ("linux", False, True, False),
        ("win32", False, False, True),
    ]:
        monkeypatch.setattr(cy, "_HOST", host)
        assert cy._is_macos() is macos
        assert cy._is_linux() is linux
        assert cy._is_windows() is windows


# --- _open_url uses webbrowser -------------------------------------------------


def test_open_url_uses_webbrowser(cy, monkeypatch):
    opened = []
    monkeypatch.setattr(cy.webbrowser, "open", lambda url: opened.append(url))
    cy._open_url("http://127.0.0.1:8080/")
    assert opened == ["http://127.0.0.1:8080/"]


def test_open_url_swallows_webbrowser_error(cy, monkeypatch):
    def boom(url):
        raise cy.webbrowser.Error("no browser")

    monkeypatch.setattr(cy.webbrowser, "open", boom)
    cy._open_url("http://x/")  # must not raise


# --- credential store (file fallback, forced by conftest) ----------------------


def test_keyring_disabled_by_env_forces_file_store(cy):
    # conftest sets YOLO_CREDENTIAL_STORE=file
    assert cy._keyring_available() is False


def test_file_credential_store_round_trip(cy, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cy._cred_get("svc-a") is None
    assert cy._cred_exists("svc-a") is False
    cy._cred_set("svc-a", "value-a")
    cy._cred_set("svc-b", "value-b")
    assert cy._cred_get("svc-a") == "value-a"
    assert cy._cred_exists("svc-a") is True
    # stored mode 600, under ~/.claude-yolo/credentials
    path = cy._cred_file_path("svc-a")
    assert path.parent == tmp_path / ".claude-yolo" / "credentials"
    assert oct(path.stat().st_mode)[-3:] == "600"
    # delete is idempotent-ish: True once, False after
    assert cy._cred_delete("svc-a") is True
    assert cy._cred_delete("svc-a") is False
    assert cy._cred_get("svc-a") is None
    assert cy._cred_get("svc-b") == "value-b"  # unaffected


def test_macos_legacy_keychain_migration(cy, monkeypatch, tmp_path):
    # Upgrade path: a token/secret left by pre-keyring yolo lives in the macOS login
    # Keychain (read via `security`). _cred_get pulls it through, migrates it into the
    # active store, and the next read is native (no more `security` call).
    monkeypatch.setattr(cy, "_HOST", "darwin")  # _is_macos() -> True
    monkeypatch.setenv("HOME", str(tmp_path))  # file store (conftest forces it)
    calls = []

    class R:
        returncode = 0
        stdout = "legacy-token\n"  # `security -w` appends one newline

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return R()

    monkeypatch.setattr(cy.subprocess, "run", fake_run)
    assert cy._cred_get("claude-yolo-oauth-token") == "legacy-token"  # newline stripped
    assert calls and calls[0][0] == "security"
    # migrated into the file store, so a second read doesn't touch `security`
    calls.clear()
    assert cy._cred_get("claude-yolo-oauth-token") == "legacy-token"
    assert calls == []


def test_legacy_keychain_get_absent_returns_none(cy, monkeypatch):
    class R:
        returncode = 44
        stdout = ""

    monkeypatch.setattr(cy.subprocess, "run", lambda *a, **k: R())
    assert cy._legacy_keychain_get("nope") is None


def test_keyring_backend_used_when_available(cy, monkeypatch):
    # Simulate a real keyring backend and assert the store routes to it (not file).
    store = {}
    fake = type(
        "FakeKeyring",
        (),
        {
            "get_password": staticmethod(lambda s, a: store.get((s, a))),
            "set_password": staticmethod(lambda s, a, v: store.__setitem__((s, a), v)),
            "delete_password": staticmethod(lambda s, a: store.pop((s, a))),
        },
    )()
    monkeypatch.setattr(cy, "_use_keyring_cache", True)
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake)
    cy._cred_set("svc", "secret")
    assert store[("svc", cy._cred_account())] == "secret"
    assert cy._cred_get("svc") == "secret"
    assert cy._cred_delete("svc") is True


# --- ssh-agent socket selection ------------------------------------------------


def test_ssh_agent_sock_macos_uses_desktop_socket(cy, monkeypatch):
    monkeypatch.setattr(cy, "_HOST", "darwin")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ignored.sock")
    assert cy._ssh_agent_sock_source() == cy._DESKTOP_SSH_SOCK


def test_ssh_agent_sock_linux_prefers_host_sock(cy, monkeypatch):
    monkeypatch.setattr(cy, "_HOST", "linux")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/keyring/ssh")
    assert cy._ssh_agent_sock_source() == "/run/user/1000/keyring/ssh"


def test_ssh_agent_sock_linux_falls_back_to_desktop(cy, monkeypatch):
    monkeypatch.setattr(cy, "_HOST", "linux")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert cy._ssh_agent_sock_source() == cy._DESKTOP_SSH_SOCK


# --- clipboard reader ----------------------------------------------------------


def _stub_clipboard(cy, monkeypatch, *, ok_for):
    """Stub subprocess.run so only commands whose argv[0] is in `ok_for` succeed."""
    seen = []

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def run(cmd, *a, **k):
        seen.append(cmd)
        if cmd[0] in ok_for:
            return R(0, "clip-text\n")
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(cy.subprocess, "run", run)
    return seen


def test_clipboard_macos_uses_pbpaste(cy, monkeypatch):
    monkeypatch.setattr(cy, "_HOST", "darwin")
    seen = _stub_clipboard(cy, monkeypatch, ok_for={"pbpaste"})
    assert cy._read_clipboard() == "clip-text\n"
    assert seen[0][0] == "pbpaste"


def test_clipboard_linux_tries_wayland_then_x11(cy, monkeypatch):
    monkeypatch.setattr(cy, "_HOST", "linux")
    seen = _stub_clipboard(cy, monkeypatch, ok_for={"xclip"})  # wl-paste absent
    assert cy._read_clipboard() == "clip-text\n"
    assert [c[0] for c in seen] == ["wl-paste", "xclip"]


def test_clipboard_none_available_exits(cy, monkeypatch):
    monkeypatch.setattr(cy, "_HOST", "linux")
    _stub_clipboard(cy, monkeypatch, ok_for=set())
    with pytest.raises(SystemExit) as exc:
        cy._read_clipboard()
    assert "clipboard" in str(exc.value)


# --- run dir location ----------------------------------------------------------


def test_run_dir_uses_xdg_runtime_on_linux(cy, monkeypatch, tmp_path):
    monkeypatch.setattr(cy, "_HOST", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert cy._run_dir() == tmp_path / cy._RUN_DIR_NAME


def test_run_dir_falls_back_to_tmpdir(cy, monkeypatch, tmp_path):
    monkeypatch.setattr(cy, "_HOST", "linux")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(cy.tempfile, "gettempdir", lambda: str(tmp_path))
    assert cy._run_dir() == tmp_path / cy._RUN_DIR_NAME
    # and on macOS, always $TMPDIR
    monkeypatch.setattr(cy, "_HOST", "darwin")
    assert cy._run_dir() == pathlib.Path(tmp_path) / cy._RUN_DIR_NAME


# --- host timezone forwarding ---------------------------------------------------


def _stub_localtime(cy, monkeypatch, *, link=None, etc_timezone=None):
    """Point timezone_args at a fake host: /etc/localtime link and /etc/timezone."""

    def readlink(path):
        if link is None:
            raise OSError("not a symlink")
        return link

    monkeypatch.setattr(cy.os, "readlink", readlink)
    real_read_text = cy.pathlib.Path.read_text

    def read_text(self, *a, **k):
        if str(self) == "/etc/timezone":
            if etc_timezone is None:
                raise OSError("no such file")
            return etc_timezone
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(cy.pathlib.Path, "read_text", read_text)


def test_timezone_host_tz_env_wins(cy, monkeypatch):
    _stub_localtime(cy, monkeypatch, link="/usr/share/zoneinfo/Europe/Berlin")
    monkeypatch.setenv("TZ", "America/New_York")
    assert cy.timezone_args() == ["-e", "TZ=America/New_York"]


def test_timezone_from_macos_localtime_symlink(cy, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    _stub_localtime(cy, monkeypatch, link="/var/db/timezone/zoneinfo/America/Chicago")
    assert cy.timezone_args() == ["-e", "TZ=America/Chicago"]


def test_timezone_from_linux_localtime_symlink(cy, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    _stub_localtime(cy, monkeypatch, link="../usr/share/zoneinfo/Europe/Berlin")
    assert cy.timezone_args() == ["-e", "TZ=Europe/Berlin"]


def test_timezone_falls_back_to_etc_timezone(cy, monkeypatch):
    # /etc/localtime is a plain file copy (readlink fails) -> Debian /etc/timezone
    monkeypatch.delenv("TZ", raising=False)
    _stub_localtime(cy, monkeypatch, etc_timezone="Europe/Paris\n")
    assert cy.timezone_args() == ["-e", "TZ=Europe/Paris"]


def test_timezone_undeterminable_forwards_nothing(cy, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    _stub_localtime(cy, monkeypatch)
    assert cy.timezone_args() == []


def test_timezone_wrapper_runs_set_timezone_script(cy, run_cli, tmp_path):
    # The container-side half of TZ forwarding: a wrapped claude launch (the
    # default oauth-token mode wraps) runs the baked set-timezone.sh — which
    # repoints /etc/localtime at $TZ — before the secrets loader.
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    argv = run_cli([], home=home, cwd=work)
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    assert argv[i + 1] == "-c"
    wrapper = argv[i + 2]
    assert "/etc/yolo/set-timezone.sh" in wrapper
    assert wrapper.index("set-timezone.sh") < wrapper.index("load-secrets.sh")
