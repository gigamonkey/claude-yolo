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


def _load_module():
    spec = importlib.util.spec_from_file_location("yolo", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def _run(argv, *, home, cwd, creds_path="/tmp/creds.json"):
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(cy, "build_docker_image", lambda: None)
        monkeypatch.setattr(cy, "ensure_logged_in", lambda c: None)
        monkeypatch.setattr(cy, "extract_credentials", lambda c: creds_path)
        monkeypatch.setattr(cy, "ensure_oauth_token", lambda c: "sk-ant-oat-TESTTOKEN")
        monkeypatch.setattr(cy, "git_identity_args", lambda: [])
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
