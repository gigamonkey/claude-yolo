"""Tests for the externalized data files.

The built-in Dockerfiles and the container system prompt live in files shipped
beside yolo.py (Dockerfile.default / Dockerfile.custom / container-prompt.txt),
loaded into the DEFAULT_DOCKERFILE / CUSTOM_DOCKERFILE / CONTAINER_PROMPT module
constants by _read_data_file. These tests pin the loader, the file<->constant
correspondence, the content markers that an image-tag-shifting careless edit
would break, and the wheel-packaging that ships the files.
"""

import pathlib

import pytest
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_constants_match_their_files(cy):
    """Each constant is exactly its data file (CONTAINER_PROMPT stripped)."""
    assert cy.DEFAULT_DOCKERFILE == (ROOT / "Dockerfile.default").read_text()
    assert cy.CUSTOM_DOCKERFILE == (ROOT / "Dockerfile.custom").read_text()
    assert cy.CONTAINER_PROMPT == (ROOT / "container-prompt.txt").read_text().strip()


def test_default_dockerfile_markers(cy):
    """The key lines that make the image what it is. A drift here changes the
    content-addressed image tag (harmless one-time rebuild) — this catches an
    accidental one early."""
    df = cy.DEFAULT_DOCKERFILE
    assert "FROM ubuntu:26.04" in df
    assert "useradd -m -s /bin/bash --uid ${HOST_UID} -G root claude" in df
    assert 'ENTRYPOINT ["claude", "--dangerously-skip-permissions"]' in df
    # the baked timezone fix-up: written while root, run from .bashrc for shells
    # (the claude launch wrapper in yolo.py runs it for claude sessions)
    assert "> /etc/yolo/set-timezone.sh" in df
    assert "bash /etc/yolo/set-timezone.sh" in df


def test_secrets_loader_uses_real_newlines(cy):
    """The load-secrets.sh printf carries a real `\\n` (one backslash), not the
    `\\\\n` Python-escape artifact that the old in-string literal needed. This is
    the one spot the file<-literal migration could have mangled."""
    df = cy.DEFAULT_DOCKERFILE
    assert r"printf '%s\n'" in df
    assert r"printf '%s\\n'" not in df


def test_custom_dockerfile_markers(cy):
    """The template must layer on the default (FROM ${YOLO_BASE}) and end as the
    `claude` user — the invariants _build_image / _verify_image_user enforce."""
    cf = cy.CUSTOM_DOCKERFILE
    assert "ARG YOLO_BASE" in cf
    assert "FROM ${YOLO_BASE}" in cf
    assert cf.rstrip().endswith("USER claude")


def test_container_prompt_content(cy):
    assert cy.CONTAINER_PROMPT.startswith("You are running in an ephemeral Ubuntu container")
    # Stripped, so the join in build_claude_args stays byte-identical to before.
    assert cy.CONTAINER_PROMPT == cy.CONTAINER_PROMPT.strip()
    # Points the agent at the mounted docs dir (and the Dockerfile.yolo guide),
    # which is the only way it learns about yolo mechanics yolo can't run in-container.
    assert cy._DOCS_CONTAINER_DIR in cy.CONTAINER_PROMPT
    assert "custom-dockerfile.md" in cy.CONTAINER_PROMPT


def test_docs_dir_is_shipped_and_mount_target_is_absolute(cy):
    """The reference-docs directory exists beside yolo.py and mounts at a fixed
    absolute container path the container prompt can point at."""
    assert cy._DOCS_DATA_DIR == cy._DATA_DIR / "container-docs"
    assert cy._DOCS_DATA_DIR.is_dir()
    assert cy._DOCS_CONTAINER_DIR.startswith("/")


def test_dockerfile_guide_embeds_the_custom_template(cy):
    """The mounted Dockerfile.yolo guide must carry the exact template `yolo
    dockerfile --custom` prints (the agent can't run yolo to fetch it), and the
    host commands that wire it up. Guards against the two drifting apart."""
    guide = (ROOT / "container-docs" / "custom-dockerfile.md").read_text()
    assert cy.CUSTOM_DOCKERFILE.strip() in guide
    assert "yolo config --dockerfile ./Dockerfile.yolo" in guide


def test_read_data_file_missing_is_a_clear_error(cy):
    """A missing data file fails loudly at the call (sys.exit), not with an opaque
    error at first launch."""
    with pytest.raises(SystemExit) as exc:
        cy._read_data_file("does-not-exist.txt")
    assert "does-not-exist.txt" in str(exc.value)


def test_wheel_ships_the_data_files():
    """Packaging regression guard: the wheel must include the data files next to
    yolo.py, or an installed yolo crashes at import (_read_data_file -> sys.exit).
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    for name in (
        "yolo.py",
        "Dockerfile.default",
        "Dockerfile.custom",
        "container-prompt.txt",
        "container-docs",
    ):
        assert name in include
