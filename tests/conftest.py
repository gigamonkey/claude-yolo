"""Shared fixtures for the claude-yolo test suite.

The script under test is `yolo.py`. We load it via importlib from the file path
(rather than a plain `import yolo`) so each test gets a *fresh* module instance:
`main()` mutates the module-global argparse PARSER (via `set_defaults`), and a
fresh load keeps that state from leaking between tests. Loading from the path
also pins the tests to the source file regardless of any installed `yolo`.
"""

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "yolo.py"

# The throwaway creds file oauth-token/bedrock modes overlay at .credentials.json.
# Stubbed to a fixed path so tests can distinguish it from the keychain snapshot
# (creds_path, default /tmp/creds.json).
MASK_CREDFILE = "/tmp/mask-creds.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("yolo", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _force_file_credential_store(monkeypatch):
    """Never touch a real OS keyring during tests.

    Force the chmod-600 file fallback (the keyring backend would otherwise hit the
    dev machine's actual Keychain/Secret Service). Tests that exercise the real
    `_cred_*`/`_read_secret_value` paths should also point HOME at a tmp dir.
    """
    monkeypatch.setenv("YOLO_CREDENTIAL_STORE", "file")


@pytest.fixture
def cy():
    """A freshly-loaded yolo module (isolated global PARSER)."""
    return _load_module()


@pytest.fixture
def run_cli(cy, monkeypatch):
    """Run `cy.main()` with all the host-touching side effects stubbed.

    Controls HOME and the cwd, stubs the docker build / login / keychain / git
    calls, and captures the argv that would have been handed to `os.execvp`.
    Returns that argv list, or None if execvp was never reached (e.g. the `init`
    verb, which exits early).
    """
    # launch_container now does an up-front `running_container_for` (docker ps) on
    # every launch to guard against resuming an already-running session. Default it
    # to "nothing running" so launch tests don't shell out to docker — but only if
    # the test hasn't set its own stub (the tmux/verb tests patch it to a truthy id
    # *before* calling run_cli, and that must win).
    original_rcf = cy.running_container_for

    def _run(argv, *, home, cwd, creds_path="/tmp/creds.json"):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(cy, "build_docker_image", lambda *a, **k: None)
        monkeypatch.setattr(cy, "_verify_image_user", lambda tag: None)
        monkeypatch.setattr(cy, "ensure_logged_in", lambda c: None)
        monkeypatch.setattr(cy, "extract_credentials", lambda c, d: creds_path)
        monkeypatch.setattr(cy, "_masking_credfile", lambda d: MASK_CREDFILE)
        monkeypatch.setattr(cy, "ensure_oauth_token", lambda c: "sk-ant-oat-TESTTOKEN")
        monkeypatch.setattr(cy, "git_identity_args", lambda: [])
        if cy.running_container_for is original_rcf:
            monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: None)
        # The per-session run dir + docker-ps GC touch real $TMPDIR / docker; keep
        # them inside the controlled tmp HOME and skip the GC's `docker ps` call.
        monkeypatch.setattr(cy, "_run_dir", lambda: pathlib.Path(home) / ".claude-yolo-run")
        monkeypatch.setattr(cy, "_gc_run_dir", lambda: None)
        monkeypatch.setattr(cy.sys, "argv", ["yolo", *argv])

        captured = {}
        monkeypatch.setattr(cy.os, "execvp", lambda file, args: captured.__setitem__("argv", args))
        cy.main()
        return captured.get("argv")

    return _run


@pytest.fixture
def flag_values():
    """Return a helper: collect the values following every `flag` in an argv list."""

    def _values(argv, flag):
        return [argv[i + 1] for i, tok in enumerate(argv) if tok == flag]

    return _values
