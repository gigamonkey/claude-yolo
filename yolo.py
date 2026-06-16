#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import argparse
import datetime
import fcntl
import getpass
import hashlib
import json
import os
import pathlib
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty

# Image tag repo. The actual tag is content-addressed: claude-yolo:<hash8> where
# hash8 derives from the Dockerfile text + host UID (see _image_tag). Each distinct
# Dockerfile (the inline default or a --dockerfile override) gets its own image, so
# parallel sessions can't race on a single shared tag and pick up each other's build.
DOCKER_IMAGE_REPO = "claude-yolo"

# The three mutually-exclusive auth mechanisms, selected by --auth (default
# oauth-token). oauth-token = forward a long-lived CLAUDE_CODE_OAUTH_TOKEN env
# var; keychain = extract the rotating Claude.ai keychain creds into a mounted
# file (hazard: the single-use refresh token means any session running when the
# access token expires either wins the refresh or is broken by it — see README);
# bedrock = AWS Bedrock creds.
AUTH_CHOICES = ["keychain", "oauth-token", "bedrock"]

# What `yolo finish` does with the branch after removing the worktree:
#   delete-if-merged = delete it iff reachable from base, else keep it (default);
#   merge          = merge it into the current checkout, then delete it;
#   push           = push it to a remote (--finish-remote, default origin), keep it locally;
#   keep           = leave the branch alone.
FINISH_CHOICES = ["delete-if-merged", "merge", "push", "keep"]

# The built-in default Dockerfile. The host UID is passed in as the HOST_UID build ARG
# (build_docker_image adds --build-arg HOST_UID=<os.getuid()>) so that files in the
# bind-mounted working directory are owned by (and writable as) the in-container user.
# The user is also put in group 0 so it can connect to the Docker engine's root-owned
# ssh-auth.sock (see the useradd line and the ssh-auth.sock mount below). This is a plain
# literal Dockerfile — no Python templating — so a --dockerfile override is the same kind
# of thing: Dockerfile bytes built with the same HOST_UID build-arg.
DEFAULT_DOCKERFILE = """\
FROM ubuntu:26.04

# Baked-in amenities used across most projects, so Claude doesn't re-install them in
# each ephemeral container. fd-find installs its binary as `fdfind`; symlink it to `fd`.
# Ubuntu's own `nodejs` package lags; install Node 24 from NodeSource instead (its setup
# script adds a codename-independent `nodistro` apt repo and pulls in npm, so no separate
# npm package).
RUN apt-get update && apt-get install -y sudo jq git curl ripgrep fd-find build-essential vim && ln -s /usr/bin/fdfind /usr/local/bin/fd
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && apt-get install -y nodejs
# uv + uvx for fast Python tooling, copied from the official image (no curl, pinnable)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
# HOST_UID (passed via --build-arg) matches the host user so bind-mounted working-dir
# files are owned/writable. Group 0 (root) membership grants access to the Docker engine's
# ssh-auth.sock, which is mounted srw-rw---- root:root — without it a non-root user gets
# EACCES on connect(). This adds no real privilege: the claude user already has NOPASSWD
# sudo, and the container is the sandbox.
ARG HOST_UID=1000
RUN useradd -m -s /bin/bash --uid ${HOST_UID} -G root claude
RUN echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude
RUN mkdir -p /home/claude/.ssh && chown claude:claude /home/claude/.ssh && chmod 700 /home/claude/.ssh

# Secrets loader: sourced (not run) at session start to export each file in the
# per-session /run/secrets bind mount as an env var (file name = env var name).
# A no-op when nothing is mounted there (no secrets configured). Files are kept
# for the session by default; a sibling `<name>.ephemeral` marker makes the
# loader delete the value right after exporting it (the rw mount allows that).
# `return 0` is valid because the script is always *sourced*, never executed.
# Written with printf (not a heredoc) so it doesn't depend on BuildKit; the single
# quotes keep the build shell from expanding $f / $(...).
RUN mkdir -p /etc/yolo && printf '%s\\n' \
  '[ -d /run/secrets ] || return 0' \
  'for f in /run/secrets/*; do' \
  '  case "$f" in *.ephemeral) continue ;; esac' \
  '  [ -f "$f" ] || continue' \
  '  export "$(basename "$f")=$(cat "$f")"' \
  '  if [ -f "$f.ephemeral" ]; then rm -f "$f" "$f.ephemeral"; fi' \
  'done' \
  > /etc/yolo/load-secrets.sh

USER claude
# Use the native installer (~/.local/bin/claude), NOT `npm install -g`. The npm global
# install lands at /usr/local/bin/claude, which Claude Code's `/doctor` flags as a broken
# install and which self-update can't manage. The native binary is standalone (no node needed).
RUN curl -fsSL https://claude.ai/install.sh | bash
# Adopt a yolo-provided prompt when the container is launched with -e YOLO_PS1
# (see _ps1_env_args): flags any bash as a yolo shell and shows where it is.
# Appended last so it wins over the distro default PS1.
RUN echo 'if [ -n "$YOLO_PS1" ]; then PS1="$YOLO_PS1"; fi' >> /home/claude/.bashrc
# Source the secrets loader once per interactive shell tree, so `yolo shell` (fresh
# or docker exec'd) gets the same exported secrets a claude launch does. Before the
# --yolorc line below so an rc can use the exported values. The sentinel keeps
# nested subshells from re-running it; claude launches source it via the launch
# wrapper (claude isn't a shell, so .bashrc never runs for it).
RUN echo 'if [ -z "$YOLO_SECRETS_SOURCED" ] && [ -f /etc/yolo/load-secrets.sh ]; then export YOLO_SECRETS_SOURCED=1; . /etc/yolo/load-secrets.sh; fi' >> /home/claude/.bashrc
# Source the --yolorc file (mounted at $YOLO_RC) once per interactive shell tree,
# so `yolo shell` gets the same per-session setup claude launches do. The sentinel
# keeps nested subshells from re-running it. claude launches don't read .bashrc;
# they source the rc via the launch wrapper (see launch_container).
RUN echo 'if [ -n "$YOLO_RC" ] && [ -f "$YOLO_RC" ] && [ -z "$YOLO_RC_SOURCED" ]; then export YOLO_RC_SOURCED=1; . "$YOLO_RC"; fi' >> /home/claude/.bashrc
ENV PATH=/home/claude/.local/bin:$PATH
ENTRYPOINT ["claude", "--dangerously-skip-permissions"]
"""


# Printed by `yolo dockerfile --custom`: a ready-to-edit Dockerfile that *layers on*
# the default rather than replacing it. Referencing YOLO_BASE is what triggers
# _build_image to build the default first and pass its tag in (see _build_image and
# the README), so this template inherits the claude user, sudo, the native Claude
# install, PATH, and the ENTRYPOINT — the user only fills in the marked block. (The
# GitHub HTTPS->SSH rewrite is applied at run time under --ssh-agent, not in the image,
# so a custom image gets it too.)
CUSTOM_DOCKERFILE = """\
# Custom yolo Dockerfile — layers your own steps on top of yolo's built-in image.
#
# Use it with:
#   yolo --dockerfile ./Dockerfile.yolo          # one run
#   yolo config --dockerfile ./Dockerfile.yolo   # persist it for this project
#
# Because this file references YOLO_BASE, yolo builds its default image first and
# passes the tag in as the YOLO_BASE build arg, so you inherit the `claude` user
# (with passwordless sudo), the native Claude install, PATH, and the ENTRYPOINT.
# Keep the two lines just below.

ARG YOLO_BASE
FROM ${YOLO_BASE}

# The base leaves you as the `claude` user, which has passwordless sudo. Bake in
# cross-cutting tools you want in every session here; project-specific or heavy
# ones are better installed on demand inside the container. For example:
#
#   RUN sudo apt-get update && sudo apt-get install -y postgresql-client

# --- your customizations go here ---


# Keep this last. yolo passes no -u, so the image's final USER is the runtime
# user, and it refuses to launch an image that doesn't run as `claude` (a root
# image would write your bind-mounted files as root).
USER claude
"""


def _image_tag(dockerfile_text: str, uid: int) -> str:
    """Content-addressed image tag for a Dockerfile + host UID.

    Hashing the Dockerfile text together with the UID (which is baked into the image
    by the useradd line) gives each distinct build its own tag, so two concurrent
    sessions building different Dockerfiles can't clobber a single shared tag and end
    up running each other's image.
    """
    hash8 = hashlib.sha256((dockerfile_text + str(uid)).encode()).hexdigest()[:8]
    return f"{DOCKER_IMAGE_REPO}:{hash8}"


# Build ARG name a custom Dockerfile uses to layer on yolo's default image:
# `ARG YOLO_BASE` / `FROM ${YOLO_BASE}` (see _build_image and the README).
YOLO_BASE_ARG = "YOLO_BASE"


def build_docker_image(
    dockerfile_text: str,
    tag: str,
    uid: int,
    *,
    build_args: dict | None = None,
    no_cache: bool = False,
) -> None:
    """Write the Dockerfile to a temporary directory and build the Docker image.

    The host UID is passed as the HOST_UID build ARG so the in-container `claude`
    user matches it (keeping bind-mount ownership correct); `tag` is the
    content-addressed tag from _image_tag; `build_args` carries any extra
    `--build-arg`s (e.g. YOLO_BASE for the layering path).
    """
    with tempfile.TemporaryDirectory(prefix="claude-yolo-build-") as build_dir:
        dockerfile = pathlib.Path(build_dir) / "Dockerfile"
        dockerfile.write_text(dockerfile_text)
        # Safety invariant: the build context must contain *only* the Dockerfile.
        # COPY/ADD in a (possibly user-supplied) Dockerfile can read any file in the
        # build context, so an empty context is exactly what stops a custom Dockerfile
        # from pulling host files into the image. We control this dir (we only wrote the
        # Dockerfile), so this is a guard against a future change quietly adding files
        # to the context — not against anything a Dockerfile itself can do.
        extra = sorted(p.name for p in pathlib.Path(build_dir).iterdir() if p.name != "Dockerfile")
        if extra:
            sys.exit(f"refusing to build: unexpected files in Docker build context: {extra}")
        cmd = ["docker", "build", "-t", tag, "--build-arg", f"HOST_UID={uid}"]
        for k, v in (build_args or {}).items():
            cmd += ["--build-arg", f"{k}={v}"]
        if no_cache:
            cmd.append("--no-cache")
        subprocess.run(cmd + [build_dir], check=True)


def _verify_image_user(tag: str) -> None:
    """Ensure a (custom) image runs as the `claude` user.

    yolo passes no `-u` to `docker run`, so the image's configured USER is the
    container's runtime user. A custom Dockerfile that ends on `USER root` (e.g.
    it switched to root to install packages and forgot to switch back) would run
    the container as root, breaking the HOST_UID bind-mount ownership model —
    working-dir edits would land on the host owned by root. Catch it here with a
    clear message instead of silently producing wrong-owner files.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Config.User}}", tag],
        capture_output=True,
        text=True,
    )
    user = result.stdout.strip()
    if user != "claude":
        sys.exit(
            f"custom Dockerfile produced an image whose user is "
            f"{user or 'root (USER unset)'!r}, not 'claude': the container would run as "
            "that user and bind-mount edits would land on the host with the wrong owner. "
            "End your Dockerfile with `USER claude` (or use `RUN sudo …` and never switch "
            "away from the claude user)."
        )


def _resolve_dockerfile(dockerfile: str, base: pathlib.Path) -> pathlib.Path:
    """Resolve a --dockerfile / `dockerfile`-config value to a filesystem path.

    A **relative** path — the common per-project case, a Dockerfile checked into
    the repo — is resolved against `base`, the session's working directory: the
    worktree dir in worktree mode, else the launch cwd. So the same checked-in
    path works in the main checkout and in every worktree, and a topical worktree
    can carry its own Dockerfile that differs from the others'. An **absolute**
    path (including a `~`-expanded one) is used as-is, for a generic image kept in
    some central collection rather than tied to a project.
    """
    path = pathlib.Path(os.path.expanduser(dockerfile))
    return path if path.is_absolute() else base / path


# Container path the resolved `--yolorc` file is bind-mounted to (read-only) and
# the value of the YOLO_RC env var. A fixed mount point — uniform regardless of
# the host path — that the .bashrc and the claude-launch wrapper both source.
_YOLORC_CONTAINER_PATH = "/home/claude/.yolorc"


def _resolve_yolorc(yolorc: str, base: pathlib.Path) -> pathlib.Path:
    """Resolve a --yolorc / `yolorc`-config value to a host filesystem path.

    Same rule as _resolve_dockerfile: a **relative** path resolves against `base`,
    the session working dir (the worktree dir in worktree mode, else the launch
    cwd), so a checked-in rc tracks the worktree; an **absolute** path (including a
    `~`-expanded one) is used as-is, for an out-of-tree rc the container can't edit.
    """
    path = pathlib.Path(os.path.expanduser(yolorc))
    return path if path.is_absolute() else base / path


def _build_image(parsed, cwd: pathlib.Path) -> str:
    """Build the container image for this launch and return its tag.

    Default path: build the inline DEFAULT_DOCKERFILE. Custom `--dockerfile` path:
    build that file instead (resolved via _resolve_dockerfile against `cwd`, the
    session working dir, so a relative path tracks the worktree). If the custom
    file references YOLO_BASE — i.e. it does `FROM ${YOLO_BASE}` to *layer on*
    yolo's default rather than replace it — first build the default as the base
    image and pass its tag in as the YOLO_BASE build arg, then verify the resulting
    image still runs as `claude` (a layering file that ends on `USER root` would
    break bind-mount ownership). A fully-custom file that doesn't reference
    YOLO_BASE is built as-is (the escape hatch).
    """
    uid = os.getuid()
    no_cache = parsed.rebuild_image
    dockerfile = getattr(parsed, "dockerfile", None)
    if not dockerfile:
        tag = _image_tag(DEFAULT_DOCKERFILE, uid)
        build_docker_image(DEFAULT_DOCKERFILE, tag, uid, no_cache=no_cache)
        return tag

    text = _resolve_dockerfile(dockerfile, cwd).read_text()
    build_args = {}
    if YOLO_BASE_ARG in text:
        base_tag = _image_tag(DEFAULT_DOCKERFILE, uid)
        build_docker_image(DEFAULT_DOCKERFILE, base_tag, uid, no_cache=no_cache)
        build_args[YOLO_BASE_ARG] = base_tag
        # Fold the base tag into the final tag so a base change (e.g. a yolo update)
        # yields a distinct image, not a stale reuse under an unchanged tag.
        tag = _image_tag(text + base_tag, uid)
    else:
        tag = _image_tag(text, uid)
    build_docker_image(text, tag, uid, build_args=build_args, no_cache=no_cache)
    _verify_image_user(tag)
    return tag


def extract_credentials(config_dir: str | None, run_dir: pathlib.Path) -> str:
    """Extract Claude API credentials from the macOS keychain via the `security` CLI.

    Claude Code stores OAuth credentials in the keychain under a service name of
    "Claude Code-credentials" (default) or "Claude Code-credentials-{hash8}" when
    multiple config directories are in use. The hash is the first 8 hex chars of the
    SHA-256 of the resolved config directory path — this makes the keychain entry name
    stable and unique per directory without embedding the full path.

    Returns the path of a file (chmod 600) in the per-session `run_dir` containing
    the credentials JSON, ready to bind-mount into the container. The file lives in
    the run dir — not a bare $TMPDIR NamedTemporaryFile — so the docker-ps GC
    (`_gc_run_dir`) reclaims it once the container is gone; the credentials must
    outlive yolo's own process (it execvp's into docker), so nothing here can unlink
    it synchronously.
    """
    if config_dir:
        config_path = pathlib.Path(config_dir).resolve()
        hash8 = hashlib.sha256(str(config_path).encode()).hexdigest()[:8]
        service = f"Claude Code-credentials-{hash8}"
    else:
        service = "Claude Code-credentials"

    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
    )

    if not result.stdout:
        print(f"Failed to extract credentials from keychain service '{service}'", file=sys.stderr)
        sys.exit(1)
    return _write_run_file(run_dir, "credentials.json", result.stdout)


def _masking_credfile(run_dir: pathlib.Path) -> str:
    """Create a throwaway `.credentials.json` to overlay in non-keychain auth modes.

    On macOS the host's Claude Code keeps its OAuth credentials in the Keychain, so
    `~/.claude/.credentials.json` should never exist on the host. But inside the Linux
    container Claude Code has no Keychain and falls back to that *file* store — and
    because yolo bind-mounts the host `~/.claude` read-write, a container's credential
    write would otherwise land in the real host dir. Worse, under Claude Code 2.1.x a
    present `.credentials.json` is preferred over the CLAUDE_CODE_OAUTH_TOKEN env var,
    so a stale file shadows the token and forces a /login (confirmed 2026-06-13).

    Overlaying this throwaway (containing `{}` — valid JSON, no stored creds) at that
    path in the oauth-token/bedrock modes both masks any pre-existing stale host file
    and captures the container's own credential writes in a temp file that never
    persists back to ~/.claude. It mirrors what keychain mode already does at the same
    path, where the overlay is the freshly-extracted creds. Returns a file path
    (chmod 600) in the per-session `run_dir`, ready to bind-mount — reclaimed by the
    docker-ps GC like the real extracted creds (it must outlive yolo's own process).
    """
    return _write_run_file(run_dir, "credentials-mask.json", b"{}")


def _is_logged_in(env: dict) -> bool:
    """Return True if `claude auth status` reports an active login.

    We check the `loggedIn` field rather than relying on the exit code (its
    behaviour isn't guaranteed). If the host has no `claude` binary (or one too
    old for the `auth` subcommand), we return True and defer to the empty-file
    check in `extract_credentials` rather than blocking here.
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        return True
    try:
        return json.loads(result.stdout).get("loggedIn") is True
    except (json.JSONDecodeError, AttributeError):
        return result.returncode == 0


def ensure_logged_in(config_dir: str | None) -> None:
    """Verify the host is logged in to Claude Code; offer to log in if not.

    Uses `claude auth status` (which reads the same macOS keychain we extract
    from) rather than inspecting the credentials blob: an expired accessToken is
    refreshed automatically at runtime via the stored refreshToken, so token
    expiry alone does not mean the user is logged out. For an alternate config
    directory we point CLAUDE_CONFIG_DIR at it so the check (and any login) target
    the right keychain entry. Not called in Bedrock mode, which uses AWS creds.
    """
    env = os.environ.copy()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(pathlib.Path(config_dir).resolve())

    if _is_logged_in(env):
        return

    print("Not logged in to Claude Code on the host.", file=sys.stderr)
    if input("Run `claude auth login` now? [y/N] ").strip().lower() != "y":
        sys.exit("Aborting: log in with `claude auth login` and try again.")

    subprocess.run(["claude", "auth", "login"], env=env, check=True)
    if not _is_logged_in(env):
        sys.exit("Still not logged in after `claude auth login`; aborting.")


# Long-lived OAuth token mode (--auth oauth-token). Unlike the keychain credentials
# (which rotate single-use on every refresh — so a snapshot mounted into one
# container invalidates the host's and every other container's copy the moment it
# refreshes), `claude setup-token` mints a *stable*, year-long token that is never
# rotated and never written back. We cache it once in the macOS keychain and
# forward it as the CLAUDE_CODE_OAUTH_TOKEN env var, which Claude Code reads
# directly — no .credentials.json mount, no login check. Because nothing ever
# rewrites it, any number of concurrent containers (and the host) can use it at
# once. Auth precedence: this env var sits above the file/keychain /login creds,
# so even a stale mounted .credentials.json can't shadow it.
OAUTH_KC_SERVICE = "claude-yolo-oauth-token"
# setup-token tokens last about a year. An assumption, not something we can read
# off the token (it's opaque, and the mint flow states no expiry date) — which is
# why the expiry warning fires a week early and says "estimated".
TOKEN_LIFETIME_DAYS = 365
TOKEN_EXPIRY_WARN_DAYS = 7
# setup-token prints an OAuth token; detect it in the (ANSI-laden) terminal output.
_TOKEN_RE = re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
# claude.ai page where (and only where) a minted token can actually be revoked.
TOKEN_REVOKE_URL = "https://claude.ai/settings/claude-code"


def _looks_like_token(token: str) -> bool:
    """A permissive sanity check: non-empty, no whitespace, plausibly long."""
    return bool(token) and not any(c.isspace() for c in token) and len(token) >= 20


def _scrape_token(raw: bytes) -> str:
    """Extract the OAuth token from captured `claude setup-token` output, or ''.

    ANSI-strips the capture and takes the last `sk-ant-…` match. Returns '' (a
    failed scrape, triggering the manual-paste fallback) when the match looks
    hard-wrapped: it runs right up to a line break and the next line continues in
    the token alphabet. `claude` wraps its output to the pty width, so a
    too-narrow pty splits the token across lines and the regex would otherwise
    silently capture just the first piece — a truncated token that stores fine
    but 401s at runtime. The wide TIOCSWINSZ window in `generate_oauth_token`
    prevents the wrap; this check keeps a truncated token from ever being cached
    if it recurs.
    """
    clean = _ANSI_RE.sub(b"", raw)
    matches = list(_TOKEN_RE.finditer(clean))
    if not matches:
        return ""
    last = matches[-1]
    if re.match(rb"\r?\n[A-Za-z0-9_\-]", clean[last.end() :]):
        return ""
    return last.group().decode()


def _oauth_service(config_dir: str | None) -> str:
    """Keychain service name for the yolo OAuth token, keyed to the config dir.

    Mirrors `extract_credentials`' scheme: the default config dir uses the bare
    service name, an alternate `--config-dir` gets a `-{hash8}` suffix where hash8
    is the first 8 hex chars of the SHA-256 of the resolved path (the same hash
    Claude itself uses for its per-dir keychain entry). So each config dir (≈ each
    account/profile) caches its own long-lived token, instead of one global token
    shadowing them all.
    """
    if config_dir:
        config_path = pathlib.Path(config_dir).resolve()
        hash8 = hashlib.sha256(str(config_path).encode()).hexdigest()[:8]
        return f"{OAUTH_KC_SERVICE}-{hash8}"
    return OAUTH_KC_SERVICE


def _read_oauth_token(config_dir: str | None) -> str | None:
    """The cached yolo OAuth token for this config dir, or None."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", _oauth_service(config_dir), "-w"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def _store_oauth_token(token: str, config_dir: str | None) -> None:
    """Upsert the yolo OAuth token for this config dir into the keychain (-U).

    Also records the mint in ~/.claude-yolo/tokens.json (the registry). On a
    re-mint the old token stays valid server-side — there's no revocation API —
    so print its mint date: the only handle for finding it on the claude.ai page.
    """
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-a",
            os.environ.get("USER", "claude-yolo"),
            "-s",
            _oauth_service(config_dir),
            "-w",
            token,
        ],
        check=True,
    )
    previous = _write_token_entry(config_dir)
    if previous and previous.get("minted"):
        print(
            f"Note: the previously-minted token (minted {previous['minted']}) is still "
            f"valid server-side; it can only be revoked at {TOKEN_REVOKE_URL}.",
            file=sys.stderr,
        )


# Token registry: ~/.claude-yolo/tokens.json maps keychain service name ->
# {"config_dir": ..., "minted": ...}. Non-secret metadata about tokens yolo has
# minted; the keychain holds the secret itself. The registry exists for what the
# keychain can't do: enumerate yolo's tokens across config dirs (`yolo tokens`),
# and map a service name back to its config dir — the hash8 in the name is
# one-way, so the mapping is recorded at mint time or lost. Host-side only and
# never mounted, like projects.json.


def _tokens_file() -> pathlib.Path:
    return pathlib.Path.home() / ".claude-yolo" / "tokens.json"


def _read_tokens_file() -> dict:
    """~/.claude-yolo/tokens.json as {service: entry}; {} if absent."""
    path = _tokens_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{path}: cannot read token registry: {e}")
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        sys.exit(f"{path}: must be a JSON object mapping service names to entries")
    return raw


def _write_token_entry(config_dir: str | None) -> dict | None:
    """Record a mint for this config dir's service; returns the replaced entry."""
    tokens = _read_tokens_file()
    service = _oauth_service(config_dir)
    previous = tokens.get(service)
    tokens[service] = {
        "config_dir": str(pathlib.Path(config_dir).resolve()) if config_dir else None,
        "minted": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = _tokens_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2) + "\n")
    return previous


def _remove_token_entry(service: str) -> dict | None:
    """Drop a service from the registry; returns the removed entry, if any."""
    tokens = _read_tokens_file()
    entry = tokens.pop(service, None)
    if entry is not None:
        _tokens_file().write_text(json.dumps(tokens, indent=2) + "\n")
    return entry


def _keychain_has(service: str) -> bool:
    """Whether a keychain item exists for `service` (attributes only, no secret)."""
    try:
        return (
            subprocess.run(
                ["security", "find-generic-password", "-s", service],
                capture_output=True,
            ).returncode
            == 0
        )
    except FileNotFoundError:
        return False


def _keychain_delete(service: str) -> bool:
    """Delete the keychain item for `service`; True if something was deleted."""
    try:
        return (
            subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True,
            ).returncode
            == 0
        )
    except FileNotFoundError:
        return False


_KC_DATE_RE = re.compile(r'"(?:mdat|cdat)".*?"(\d{14})[^"]*"')


def _keychain_mdat(service: str) -> datetime.datetime | None:
    """The last-modified time of a keychain item, or None.

    We upsert tokens with `add-generic-password -U`, so the item's modification
    date *is* the last mint time — the keychain timestamps every entry for free,
    which makes this the drift-proof source for the expiry estimate (it survives
    re-mints done outside yolo and predates the tokens.json registry). Attributes
    print without `-w`, so no secret is read and no auth prompt fires. Returns
    None on any trouble (missing item, parse change): the expiry warning is
    advisory and just stays quiet.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    # Attribute lines look like: "mdat"<timeb>=0x...  "20260610123456Z\000"
    # Prefer mdat (last upsert); fall back to cdat.
    dates = {m.group(0)[1:5]: m.group(1) for m in _KC_DATE_RE.finditer(result.stdout)}
    stamp = dates.get("mdat") or dates.get("cdat")
    if not stamp:
        return None
    try:
        return datetime.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None


def _token_expiry(minted: datetime.datetime) -> datetime.datetime:
    return minted + datetime.timedelta(days=TOKEN_LIFETIME_DAYS)


def _warn_token_expiry(config_dir: str | None) -> None:
    """Warn when the cached token is past or within a week of its estimated expiry.

    Without this, a token minted a year ago just starts 401ing inside containers
    with no hint from yolo. Estimate only (TOKEN_LIFETIME_DAYS); quiet when the
    keychain date can't be read.
    """
    mdat = _keychain_mdat(_oauth_service(config_dir))
    if mdat is None:
        return
    expiry = _token_expiry(mdat)
    now = datetime.datetime.now(datetime.timezone.utc)
    if expiry >= now + datetime.timedelta(days=TOKEN_EXPIRY_WARN_DAYS):
        return
    when = expiry.date().isoformat()
    state = f"expired around {when}" if expiry < now else f"expires around {when}"
    dir_label = config_dir or "~/.claude"
    print(
        f"warning: the OAuth token for {dir_label} (minted {mdat.date().isoformat()}) "
        f"{state}. Re-mint with `yolo setup-token`; the old token can only be revoked "
        f"at {TOKEN_REVOKE_URL}.",
        file=sys.stderr,
    )


def generate_oauth_token(config_dir: str | None) -> str:
    """Run `claude setup-token` interactively, capture the token, cache it.

    `claude setup-token` walks the user through a browser OAuth flow and prints a
    one-year token (it saves it nowhere). We run it under a pty so the child still
    sees a terminal — the browser/paste flow works — while we tee its output to our
    stdout *and* capture it, then scrape the token out. The pty path makes this
    robust to whether setup-token writes the token to stdout or the tty. If
    scraping fails (e.g. the token format changed), we fall back to asking the user
    to paste what was printed. The token is cached in the macOS keychain under the
    per-config-dir service name (`_oauth_service`) for reuse.

    Requires an interactive terminal (the OAuth flow needs a human to authorize in
    a browser and possibly paste a code). When stdin isn't a tty — a script, cron,
    or any non-interactive `--auth oauth-token` launch with no cached token — we
    bail with guidance instead of hanging on a flow nobody can drive.
    """
    if not sys.stdin.isatty():
        sys.exit(
            "Minting an OAuth token needs an interactive terminal (the browser OAuth "
            "flow). Run `yolo setup-token` from a terminal first, or set "
            "CLAUDE_CODE_OAUTH_TOKEN in the environment."
        )
    if not shutil.which("claude"):
        sys.exit("`claude` not found on host; install Claude Code to run `setup-token`.")
    print("Generating a long-lived (1-year) OAuth token via `claude setup-token`.")
    print("Authorize in the browser when prompted.\n", flush=True)

    captured = bytearray()
    resized = False

    def _read(fd):
        # pty.spawn leaves the pty window size unset (0x0), which `claude`
        # treats as 80 columns and hard-wraps to — splitting the token across
        # lines, where the scrape would only catch the first piece. Make the
        # pty wide enough that the token can never wrap. (Done here because
        # pty.spawn only exposes the master fd via this callback; the token
        # prints long after the first read, so the resize always lands first.)
        nonlocal resized
        if not resized:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 512, 0, 0))
            resized = True
        data = os.read(fd, 1024)
        captured.extend(data)
        return data

    status = pty.spawn(["claude", "setup-token"], _read)
    if status != 0:
        print("\n`claude setup-token` did not exit cleanly.", file=sys.stderr)

    token = _scrape_token(bytes(captured))
    if not token:
        # Couldn't auto-detect (unexpected output shape) — ask for a manual paste.
        token = input("\nCouldn't auto-detect the token. Paste it here: ").strip()
    if not _looks_like_token(token):
        sys.exit("That doesn't look like a valid OAuth token; aborting.")

    _store_oauth_token(token, config_dir)
    print(
        f"\nStored the OAuth token in the macOS keychain (service '{_oauth_service(config_dir)}')."
    )
    return token


def ensure_oauth_token(config_dir: str | None) -> str:
    """Return a long-lived OAuth token to forward into the container.

    Resolution order: an explicit CLAUDE_CODE_OAUTH_TOKEN in the host env wins (for
    CI / users who manage it themselves, and it's global by nature); else the
    yolo-managed keychain cache *for this config dir*; else mint a fresh one via
    `claude setup-token` and cache it under that config dir's service name.

    A keychain-cached token gets the expiry warning (an env token's age is
    unknowable — skip). The implicit mint asks first: minting creates a year-long
    credential the user didn't explicitly request (unlike `yolo setup-token`,
    where running the verb is the consent), and creating it silently was the
    original argument against defaulting to this auth mode.
    """
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    cached = _read_oauth_token(config_dir)
    if cached:
        _warn_token_expiry(config_dir)
        return cached
    if sys.stdin.isatty():
        dir_label = config_dir or "~/.claude"
        print(
            f"No OAuth token cached for {dir_label}. yolo will mint a 1-year Claude Code\n"
            "token (browser authorization), stored encrypted in your macOS keychain.\n"
            "It can later be removed locally with `yolo forget-token`; server-side\n"
            f"revocation is only possible at {TOKEN_REVOKE_URL}."
        )
        if input("Proceed? [Y/n] ").strip().lower() in ("n", "no"):
            sys.exit(
                "Aborting. Use `--auth keychain` for snapshot credentials (see the "
                "README for their refresh-boundary hazards), run `yolo setup-token` "
                "later, or set CLAUDE_CODE_OAUTH_TOKEN in the environment."
            )
    return generate_oauth_token(config_dir)


# --- Per-session run dir + docker-ps GC (temp-file cleanup hardening) -----------
#
# Every launch stages chmod-600 files that must be bind-mounted for the *entire*
# container lifetime: the keychain credentials snapshot / throwaway mask, and any
# injected secrets. yolo execvp's into docker, replacing its own process, so there
# is no finally/atexit to delete them — cleanup has to happen out-of-band.
#
# Each session gets its own dir, keyed by the container name: <run-dir>/<container>/
# (mode 700, files 600). At launch we GC any <run-dir>/<name>/ whose container is
# NOT in `docker ps` (crashed/finished sessions) — never a blanket wipe, which
# would nuke a concurrently-running session's still-mounted files. The run dir
# lives under $TMPDIR (a per-user dir, mode 700, excluded from Time Machine and not
# in synced folders like Dropbox/iCloud), so a session-long plaintext secret file
# isn't copied off the machine.
_RUN_DIR_NAME = "claude-yolo-run"


def _run_dir() -> pathlib.Path:
    """The root run dir: a yolo-owned subdir of $TMPDIR (mode 700)."""
    return pathlib.Path(tempfile.gettempdir()) / _RUN_DIR_NAME


def _session_run_dir(container: str) -> pathlib.Path:
    """The per-container run dir, created mode 700. Keyed by the docker --name."""
    root = _run_dir()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    d = root / container
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _running_container_names() -> set[str]:
    """Names of all running docker containers (for the run-dir GC). {} on trouble."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return set()
    if result.returncode != 0:
        return set()
    return set(result.stdout.split())


def _gc_run_dir() -> None:
    """Remove run-dir subdirs whose container is no longer running.

    Crash-proof and parallel-safe: a `kill -9`'d session's dir is collected on the
    next launch (its container is gone from `docker ps`), while a concurrently
    running session's dir — its container still listed — is left untouched. This is
    the guarantee that backs the staged credential/secret files regardless of how a
    session ended; it deliberately never blanket-wipes the run dir.
    """
    root = _run_dir()
    if not root.is_dir():
        return
    running = _running_container_names()
    for child in root.iterdir():
        if child.is_dir() and child.name not in running:
            shutil.rmtree(child, ignore_errors=True)


def _write_run_file(run_dir: pathlib.Path, name: str, data: bytes) -> str:
    """Write `data` to <run_dir>/<name>, chmod 600 from creation; return the path.

    O_CREAT|0o600 sets the mode atomically at open, so the file is never briefly
    world-readable (a plain write-then-chmod has that window).
    """
    path = pathlib.Path(run_dir) / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(0o600)  # in case the file pre-existed with a looser mode
    return str(path)


# --- Secrets: keychain-backed values injected into a session ---------------------
#
# Arbitrary user secrets (PATs, API keys, SSH keys, …) stored in the macOS keychain
# and injected into a session's container as env vars (file transport) or mounted
# files — never as `-e NAME=value`, which would leak the value into the docker-run
# argv, `docker inspect`, /proc/1/environ and tmux's retained pane command. Two
# storage scopes: global (`claude-yolo-secret-{name}`) and project
# (`claude-yolo-secret-{project-hash8}-{name}`); at injection a name resolves
# project-first, then global. A side registry (secrets.json) enumerates them and
# maps a hashed service back to its project, exactly as tokens.json does for tokens.
SECRET_KC_PREFIX = "claude-yolo-secret"
_SECRETS_CONTAINER_DIR = "/run/secrets"
_CONTAINER_HOME = "/home/claude"
# A secret NAME must be a shell identifier: it becomes an env var name in-container.
_SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _valid_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME_RE.match(name))


def _project_hash8(project_key: str) -> str:
    """First 8 hex of SHA-256 of the project key — the per-project service suffix."""
    return hashlib.sha256(project_key.encode()).hexdigest()[:8]


def _secret_service(name: str, scope: str, project_key: str | None = None) -> str:
    """Keychain service name for a (scope, name) secret.

    global → `claude-yolo-secret-{name}`; project →
    `claude-yolo-secret-{project-hash8}-{name}` (the same hashing idiom as the
    per-config-dir OAuth token service). The hash is one-way, so the registry is
    what maps a project service back to its project key.
    """
    if scope == "project":
        if project_key is None:
            raise ValueError("project scope needs a project key")
        return f"{SECRET_KC_PREFIX}-{_project_hash8(project_key)}-{name}"
    return f"{SECRET_KC_PREFIX}-{name}"


def _secrets_file() -> pathlib.Path:
    return pathlib.Path.home() / ".claude-yolo" / "secrets.json"


def _read_secrets_file() -> dict:
    """~/.claude-yolo/secrets.json as {service: entry}; {} if absent.

    Non-secret metadata only (scope, project key, name, timestamps) — the keychain
    holds the values. Host-side only and never mounted, like tokens.json.
    """
    path = _secrets_file()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{path}: cannot read secrets registry: {e}")
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        sys.exit(f"{path}: must be a JSON object mapping service names to entries")
    return raw


def _write_secret_entry(service: str, scope: str, name: str, project_key: str | None) -> None:
    """Upsert a secret's registry row (preserving its original `created` time)."""
    secrets = _read_secrets_file()
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    created = secrets.get(service, {}).get("created", now)
    secrets[service] = {
        "scope": scope,
        "name": name,
        "project_key": project_key,
        "created": created,
        "modified": now,
    }
    path = _secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(secrets, indent=2) + "\n")


def _remove_secret_entry(service: str) -> dict | None:
    """Drop a service from the secrets registry; return the removed entry, if any."""
    secrets = _read_secrets_file()
    entry = secrets.pop(service, None)
    if entry is not None:
        _secrets_file().write_text(json.dumps(secrets, indent=2) + "\n")
    return entry


def _read_secret_value(service: str) -> str | None:
    """The secret value stored under `service`, or None if absent.

    `security ... -w` appends a single newline to the password it prints; strip
    exactly that one (not arbitrary trailing whitespace, which could corrupt a
    value that legitimately ends in newlines).
    """
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    out = result.stdout
    return out[:-1] if out.endswith("\n") else out


def _store_secret_value(service: str, value: str) -> None:
    """Upsert a secret value into the keychain (-U), like _store_oauth_token."""
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-a",
            os.environ.get("USER", "claude-yolo"),
            "-s",
            service,
            "-w",
            value,
        ],
        check=True,
    )


def _strip_one_newline(value: str) -> str:
    """Drop a single trailing newline (the one a shell/echo or paste tends to add)."""
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


def _parse_secret_spec(spec: str) -> tuple[str, str, str, bool]:
    """One --secret / `secrets` spec, `NAME[:TARGET][!]` -> (name, kind, target, ephemeral).

    The injection target is discriminated by TARGET's first character: one that
    starts with `/` or `~` is a file mount path (with `~` expanded to the *container*
    home, /home/claude — NOT the host $HOME), anything else is an env var name. With
    no TARGET the secret injects as an env var named NAME. A trailing `!` marks the
    secret ephemeral (deleted right after the loader exports it); only env targets
    can be ephemeral (a single-file bind mount can't be unlinked from inside).
    """
    ephemeral = spec.endswith("!")
    if ephemeral:
        spec = spec[:-1]
    name, sep, target = spec.partition(":")
    if not _valid_secret_name(name):
        sys.exit(
            f"secret: invalid name {name!r} (must be a shell identifier, [A-Za-z_][A-Za-z0-9_]*)"
        )
    if not sep:
        return name, "env", name, ephemeral
    if target.startswith(("/", "~")):
        if ephemeral:
            sys.exit(
                f"secret {name!r}: a file-target secret can't be ephemeral "
                "(a single-file bind mount can't be deleted from inside the container)."
            )
        path = _CONTAINER_HOME + target[1:] if target.startswith("~") else target
        return name, "file", path, False
    if not _valid_secret_name(target):
        sys.exit(f"secret {name!r}: invalid env target {target!r} (must be a shell identifier)")
    return name, "env", target, ephemeral


def _resolve_secret_specs(specs: list[str]) -> list[tuple[str, str, str, bool]]:
    """Parse + dedupe merged secret specs into (name, kind, target, ephemeral) tuples.

    Keyed by (kind, target), lowest-precedence first (like _resolve_mounts/ports),
    so an exact-duplicate spec collapses and a target collision (two specs hitting
    the same env var name or mount path) is won by the later — higher — layer.
    """
    out: dict[tuple[str, str], tuple[str, bool]] = {}
    for spec in specs:
        name, kind, target, ephemeral = _parse_secret_spec(spec)
        out[(kind, target)] = (name, ephemeral)
    return [(name, kind, target, eph) for (kind, target), (name, eph) in out.items()]


def _resolve_secret_value(name: str, project_key: str | None) -> str | None:
    """A stored secret value for `name`, resolved most-specific-first (project, global)."""
    if project_key:
        svc = _secret_service(name, "project", project_key)
        if _keychain_has(svc):
            return _read_secret_value(svc)
    svc = _secret_service(name, "global")
    if _keychain_has(svc):
        return _read_secret_value(svc)
    return None


def _warn_secret_file_target(target: str, cwd: pathlib.Path) -> None:
    """Warn when a file-target lands in a host-visible mount (the cwd or ~/.claude).

    Such a path writes the plaintext secret into the bind-mounted working tree or
    the mounted config dir rather than a private container-only location.
    """
    danger = [f"{_CONTAINER_HOME}/.claude/", f"{cwd}/", str(cwd)]
    if any(target == d or target.startswith(d) for d in danger):
        print(
            f"warning: secret file target {target} is under a host-visible bind mount "
            "(the working tree or ~/.claude); the plaintext secret will be visible on "
            "the host. Prefer a private path like ~/.config or /tmp.",
            file=sys.stderr,
        )


def _stage_secrets(
    specs: list[str],
    project_key: str | None,
    run_dir: pathlib.Path,
    cwd: pathlib.Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], bool]:
    """Stage configured secrets into the run dir; return (docker `-v` args, have_env).

    Env-target secrets are written to <run-dir>/secrets/<ENVNAME> and the dir is
    bind-mounted rw at /run/secrets (the baked loader exports them; rw lets it
    delete an ephemeral one). File-target secrets are each staged and bind-mounted
    read-only at their container path. Both stage chmod-600 files in the run dir, so
    no value ever touches the docker-run argv. `extra_env` carries non-secret-store
    env values that ride the *same* file transport — the Anthropic OAuth token,
    which would otherwise sit on the docker-run argv (and so `docker inspect`, host
    `ps`, tmux's retained pane command); staged non-ephemeral so a `docker exec`'d
    `yolo shell` can re-read it. `have_env` reports whether any env value was staged
    (so the caller knows to source the loader in the claude launch wrapper). Exits
    if a referenced keychain secret isn't found.
    """
    resolved = _resolve_secret_specs(specs)
    extra_env = extra_env or {}
    if not resolved and not extra_env:
        return [], False
    args: list[str] = []
    env_dir = run_dir / "secrets"
    have_env = False

    def stage_env(name: str, value: str, ephemeral: bool = False) -> None:
        nonlocal have_env
        if not have_env:
            env_dir.mkdir(parents=True, exist_ok=True)
            env_dir.chmod(0o700)
            have_env = True
        _write_run_file(env_dir, name, value.encode())
        if ephemeral:
            _write_run_file(env_dir, f"{name}.ephemeral", b"")

    # The token first, so a user secret that deliberately reuses its env name
    # (odd, but their call) overwrites it rather than the reverse.
    for name, value in extra_env.items():
        stage_env(name, value)
    for idx, (name, kind, target, ephemeral) in enumerate(resolved):
        value = _resolve_secret_value(name, project_key)
        if value is None:
            sys.exit(
                f"secret {name!r} is not in the keychain; store it with "
                f"`yolo secret set {name}` (add --project for project scope)."
            )
        if kind == "env":
            stage_env(target, value, ephemeral)
        else:  # file
            _warn_secret_file_target(target, cwd)
            staged = _write_run_file(run_dir, f"secret-file-{idx}", value.encode())
            args += ["-v", f"{staged}:{target}:ro"]
    if have_env:
        args += ["-v", f"{env_dir}:{_SECRETS_CONTAINER_DIR}:rw"]
    return args, have_env


def do_secret(parsed, home: pathlib.Path, cwd: pathlib.Path) -> None:
    """`secret` verb: set/list/rm keychain-backed secrets. Terminal (no container).

    The subcommand is the TOPIC (`set`/`list`/`rm`); the secret NAME, when needed,
    is the first trailing positional. Storage scope is global by default or the
    current project with --project.
    """
    sub = parsed.topic
    names = parsed.extra_args
    if sub == "set":
        if len(names) != 1:
            sys.exit("usage: yolo secret set NAME [--project] [--clipboard]")
        do_secret_set(names[0], parsed.project, parsed.clipboard, cwd)
    elif sub == "rm":
        if len(names) != 1:
            sys.exit("usage: yolo secret rm NAME [--project]")
        do_secret_rm(names[0], parsed.project, cwd)
    elif sub == "list":
        if names:
            sys.exit("usage: yolo secret list [--all]")
        do_secret_list(cwd, all_projects=parsed.all_repos)
    else:
        sys.exit("`secret` needs a subcommand: set, list, or rm (e.g. `yolo secret set GH_TOKEN`).")


def do_secret_set(name: str, project: bool, clipboard: bool, cwd: pathlib.Path) -> None:
    """Store a secret value in the keychain + registry (never via the CLI argv).

    The value comes from --clipboard (pbpaste), stdin when piped, or a hidden
    interactive prompt — never a command-line argument (that would leak it into
    shell history and the process argv visible in `ps`).
    """
    if not _valid_secret_name(name):
        sys.exit(
            f"invalid secret name {name!r} (must be a shell identifier, [A-Za-z_][A-Za-z0-9_]*)."
        )
    scope = "project" if project else "global"
    project_key = _project_key(cwd) if project else None
    if clipboard:
        try:
            result = subprocess.run(["pbpaste"], capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit("--clipboard needs the macOS `pbpaste` command, which wasn't found.")
        if result.returncode != 0:
            sys.exit("--clipboard: `pbpaste` failed.")
        value = _strip_one_newline(result.stdout)
    elif not sys.stdin.isatty():
        value = _strip_one_newline(sys.stdin.read())
    else:
        value = getpass.getpass(f"Value for secret {name} (input hidden): ")
    if not value:
        sys.exit("refusing to store an empty secret value.")
    service = _secret_service(name, scope, project_key)
    _store_secret_value(service, value)
    _write_secret_entry(service, scope, name, project_key)
    where = f"project ({project_key})" if project else "global"
    print(f"Stored secret {name!r} at {where} scope (keychain service '{service}').")


def do_secret_rm(name: str, project: bool, cwd: pathlib.Path) -> None:
    """Delete a secret's keychain item + registry row at the given scope."""
    scope = "project" if project else "global"
    project_key = _project_key(cwd) if project else None
    service = _secret_service(name, scope, project_key)
    entry = _remove_secret_entry(service)
    deleted = _keychain_delete(service)
    where = f"project ({project_key})" if project else "global"
    if not deleted and entry is None:
        sys.exit(f"no {where}-scope secret {name!r} (keychain service '{service}').")
    print(f"Removed secret {name!r} at {where} scope.")


def do_secret_list(cwd: pathlib.Path, *, all_projects: bool) -> None:
    """List secrets from the registry: global + the current project's (or --all)."""
    secrets = _read_secrets_file()
    if not secrets:
        print(f"No secrets recorded. (yolo records them in {_secrets_file()}.)")
        return
    this_project = _project_key(cwd)
    rows = []
    for service, entry in sorted(secrets.items()):
        scope = entry.get("scope", "global")
        project_key = entry.get("project_key")
        if not all_projects and scope == "project" and project_key != this_project:
            continue
        scope_label = "global" if scope == "global" else f"project:{project_key}"
        created = (entry.get("created") or "")[:10] or "?"
        status = "ok" if _keychain_has(service) else "stale (not in keychain)"
        rows.append((entry.get("name", "?"), scope_label, created, status))
    if not rows:
        print("No secrets for this project or at global scope.")
        return
    _print_table(("NAME", "SCOPE", "CREATED", "STATUS"), rows)


def git_identity_args() -> list[str]:
    """Forward the host's git identity into the container as docker `-e` args.

    Reads the *effective* `user.name`/`user.email` for the current directory (so a
    repo-local identity wins over the global one, matching what a host commit would
    use) and exports them as GIT_AUTHOR_*/GIT_COMMITTER_*. This covers commits
    without mounting the whole ~/.gitconfig, which would also drag in macOS-only
    bits (osxkeychain credential helper, GPG signing) that break inside the
    container. Returns [] if git or an identity is unavailable.
    """

    def cfg(key: str) -> str:
        try:
            result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True)
        except FileNotFoundError:
            return ""
        return result.stdout.strip()

    name = cfg("user.name")
    email = cfg("user.email")
    env_args = []
    if name:
        env_args += ["-e", f"GIT_AUTHOR_NAME={name}", "-e", f"GIT_COMMITTER_NAME={name}"]
    if email:
        env_args += ["-e", f"GIT_AUTHOR_EMAIL={email}", "-e", f"GIT_COMMITTER_EMAIL={email}"]
    return env_args


def _repo_paths() -> tuple[pathlib.Path, pathlib.Path, str]:
    """Return (common_git, main_root, slug) for the repo containing the cwd.

    Exits if the cwd isn't in a git repo. `slug` is the main repo path run through
    the same scheme Claude Code uses for ~/.claude/projects/ buckets; it keys both
    the worktree state dir (~/.claude-yolo/worktrees/<slug>/) and the docker labels
    used to find a topic's container.
    """
    try:
        common_git_out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("must be run from inside a git repository.")
    common_git = pathlib.Path(common_git_out)
    main_root = common_git.parent
    return common_git, main_root, re.sub(r"[^a-zA-Z0-9]", "-", str(main_root))


def _main_root_or_none() -> pathlib.Path | None:
    """The main repo root for the cwd, or None when the cwd isn't a git repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return pathlib.Path(out).parent


def _repo_slug_or_none() -> str | None:
    """The repo slug for the cwd, or None when the cwd isn't a git repo.

    Used to label bare (non-worktree) launches that happen to be inside a repo,
    without erroring out when they aren't.
    """
    root = _main_root_or_none()
    return None if root is None else re.sub(r"[^a-zA-Z0-9]", "-", str(root))


# Per-session activity state (see _read_session_state / build_claude_args hooks):
# a session's Stop/UserPromptSubmit hooks write waiting/working + a timestamp to
# <config-dir>/.yolo-status/<cwd-slug>.state, which `ps` reads back. The dir lives
# under the config dir because that's the one host-writable bind mount available
# from inside the container (the project tree would pollute the repo, and
# ~/.claude-yolo is deliberately never mounted).
_STATUS_DIR_NAME = ".yolo-status"


def _cwd_slug(cwd) -> str:
    """A working dir path slugified the way Claude names ~/.claude/projects buckets.

    Keys the per-session status file. cwd is unique per running container (one per
    directory in cwd mode; distinct worktree paths otherwise), so launch and `ps`
    agree on the file by both slugging the same cwd.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def _branch_exists(name: str) -> bool:
    return (
        subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]).returncode
        == 0
    )


def setup_worktree(
    name: str, home: pathlib.Path, base: str = "HEAD"
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create a host git worktree on a new branch NAME for a parallel session.

    The worktree lives in a centralized state dir keyed by a slug of the main repo
    path (the same slug scheme Claude Code uses under ~/.claude/projects/). Returns
    (worktree_path, common_git, main_root). The caller bind-mounts both the worktree
    dir and the shared .git at their identical host paths, because the worktree
    records an absolute path to the shared .git and vice versa — so same-path
    mounting is what makes git work inside the container. Branch NAME is created off
    `base` (default the current HEAD) with no upstream (a stray `git push` can't hit
    main); commits land in the shared .git on the host, so work survives container
    exit. The sole caller (`start`) guarantees the worktree and branch don't already
    exist (it errors otherwise), so this always creates fresh.
    """
    common_git, main_root, slug = _repo_paths()
    worktree = home / ".claude-yolo" / "worktrees" / slug / name
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", "-b", name, str(worktree), base], check=True)
    return worktree, common_git, main_root


# Config keys -> (argparse dest, kind), shared by ~/.yolo.json and the per-project
# entries in ~/.claude-yolo/projects.json. These are standing environment /
# credential preferences only; per-invocation *actions* (--resume and the verbs)
# are intentionally CLI-only and rejected if they appear in a config file.
# "path" values get ~ expanded (a JSON file can't rely on shell expansion).
#
# Both config files live OUTSIDE every container mount by construction. An
# in-directory .yolo.json is no longer read: it sits inside the bind-mounted tree,
# so Claude in a container could edit it to grant its next session new host access
# (extra `mounts`, or an arbitrary read-write mount via `config-dir`) — and a
# .yolo.json committed in a cloned repo would apply someone else's config the
# first time yolo ran there. A leftover file draws a warning in load_yolo_config.
YOLO_KEYS = {
    "config_dir": ("config_dir", "path"),
    "dockerfile": ("dockerfile", "path"),
    "yolorc": ("yolorc", "path"),
    "auth": ("auth", "auth"),
    "aws_profile": ("aws_profile", "str"),
    "aws_region": ("aws_region", "str"),
    "bedrock_model": ("bedrock_model", "str"),
    "claude_json": ("claude_json", "bool"),
    "ssh_agent": ("ssh_agent", "bool"),
    "submodules": ("submodules", "bool"),
    "base": ("base", "str"),
    "finish_action": ("finish_action", "finish"),
    "finish_remote": ("finish_remote", "str"),
    "prompts": ("prompts", "list"),
    "mounts": ("mounts", "list"),
    "ports": ("ports", "list"),
    "secrets": ("secrets", "list"),
    "require_project_entry": ("require_project_entry", "bool"),
    "tmux": ("tmux", "bool"),
    "tmux_session": ("tmux_session", "str"),
}

# dests whose values concatenate across the config layers and the CLI (everything
# else is overridden by the higher-precedence layer)
_CONCAT_DESTS = ("prompts", "mounts", "ports", "secrets")

# sentinel default marking "flag not given" in _explicit_config_flags
_UNSET = object()


def _parse_yolo_dict(raw: dict, source: str) -> dict:
    """Validate one config object (YOLO_KEYS) into {argparse_dest: value}.

    `source` names the file (or projects.json entry) in error messages.
    """
    out = {}
    for key, val in raw.items():
        norm = key.replace("-", "_")
        if norm == "append_system_prompt":  # the key's pre-0.7 name
            sys.exit(
                f"{source}: {key!r} was renamed to 'prompts'; "
                "rename it (same value), e.g. via `yolo config --unset "
                f"{key} --add-prompt ...`."
            )
        if norm not in YOLO_KEYS:
            sys.exit(f"{source}: unknown config option {key!r}")
        dest, kind = YOLO_KEYS[norm]
        if val is None:
            continue  # explicit null = leave the key at its built-in default
        if kind == "bool":
            if not isinstance(val, bool):
                sys.exit(f"{source}: {key!r} must be true or false")
            out[dest] = val
        elif kind == "auth":
            # set_defaults bypasses argparse's `choices` check, so validate here.
            if val not in AUTH_CHOICES:
                sys.exit(f"{source}: {key!r} must be one of {', '.join(AUTH_CHOICES)}")
            out[dest] = val
        elif kind == "finish":
            # set_defaults bypasses argparse's `choices` check, so validate here.
            if val not in FINISH_CHOICES:
                sys.exit(f"{source}: {key!r} must be one of {', '.join(FINISH_CHOICES)}")
            out[dest] = val
        elif kind in ("str", "path"):
            if not isinstance(val, str):
                sys.exit(f"{source}: {key!r} must be a string")
            out[dest] = os.path.expanduser(val) if kind == "path" else val
        else:  # "list" (prompts, mounts): a string or list of strings
            if isinstance(val, str):
                val = [val]
            if not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
                sys.exit(f"{source}: {key!r} must be a string or list of strings")
            out[dest] = val
    return out


def _parse_yolo_file(path: pathlib.Path) -> dict:
    """Parse one .yolo.json file into {argparse_dest: value}, type-checked."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{path}: cannot read .yolo.json config: {e}")
    if not isinstance(raw, dict):
        sys.exit(f"{path}: .yolo.json must contain a JSON object")
    return _parse_yolo_dict(raw, str(path))


def _read_projects_file(path: pathlib.Path) -> dict:
    """~/.claude-yolo/projects.json as {directory: config object}; {} if absent."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{path}: cannot read projects config: {e}")
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        sys.exit(f"{path}: must be a JSON object mapping directory paths to config objects")
    return raw


# Per-worktree overlay config: ~/.claude-yolo/worktrees.json maps a worktree's
# absolute path -> a config object (same shape as a projects.json entry). It's the
# most specific persisted layer (projects.json entry < worktree overlay < CLI),
# populated from the CLI flags at `start`, edited via `yolo config TOPIC`, and
# removed by `finish`. Like projects.json it lives directly under ~/.claude-yolo/
# — a *sibling* of the worktrees/ dir, never inside a worktree — so it's outside
# every container mount: an overlay can grant host access (mounts, or an arbitrary
# rw mount via config-dir), so it must not be writable from inside a container.
def _worktrees_file(home: pathlib.Path) -> pathlib.Path:
    return home / ".claude-yolo" / "worktrees.json"


def _read_worktrees_file(path: pathlib.Path) -> dict:
    """~/.claude-yolo/worktrees.json as {worktree path: config object}; {} if absent."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{path}: cannot read worktree config: {e}")
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        sys.exit(f"{path}: must be a JSON object mapping worktree paths to config objects")
    return raw


def _write_worktrees_file(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _worktree_overlay_key(worktree_path: pathlib.Path) -> str:
    """The worktrees.json key for a worktree dir — its resolved absolute path.

    One definition shared by populate/edit/remove/load so they always agree.
    """
    return str(worktree_path.resolve())


def _merge_worktree_overlay(
    home: pathlib.Path, worktree_path: pathlib.Path, explicit: dict
) -> None:
    """Fold explicit CLI config flags into a worktree's overlay (resume updates it).

    Since `resume` restarts the container, config flags passed to it both apply
    now (load_yolo_config already layered them over the overlay for this run) and
    stick for next time. The merge rule matches load_yolo_config's: the list keys
    (prompts/mounts/ports) accumulate onto the stored list (exact-dup specs
    dropped, so a repeat is a no-op), every other key overrides. A no-op when no
    config flags were passed, so a plain `yolo resume TOPIC` never rewrites the
    file. The result equals what this run already resolved, so persisted == live.
    """
    if not explicit:
        return
    wt_file = _worktrees_file(home)
    worktrees = _read_worktrees_file(wt_file)
    key = _worktree_overlay_key(worktree_path)
    entry = dict(worktrees.get(key, {}))
    for k, v in explicit.items():
        if k.replace("-", "_") in _CONCAT_DESTS:
            existing = entry.get(k, [])
            if isinstance(existing, str):
                existing = [existing]
            entry[k] = existing + [item for item in v if item not in existing]
        else:
            entry[k] = v
    _parse_yolo_dict(entry, f"worktrees.json [{worktree_path.name}]")  # never persist unloadable
    worktrees[key] = entry
    _write_worktrees_file(wt_file, worktrees)


def _match_project_entry(projects: dict, start: pathlib.Path) -> tuple[str | None, dict | None]:
    """The (key, raw entry) whose directory contains `start`; longest key wins.

    An entry applies when `start` is at or under its key path, and only the most
    specific match is used — the same nearest-wins rule the retired in-directory
    .yolo.json search had, so running from a subdirectory picks up the project's
    entry.
    """
    start_res = start.resolve()
    best_key, best_entry, best_depth = None, None, -1
    for key, entry in projects.items():
        key_path = pathlib.Path(os.path.expanduser(key)).resolve()
        if start_res.is_relative_to(key_path) and len(key_path.parts) > best_depth:
            best_key, best_entry, best_depth = key, entry, len(key_path.parts)
    return best_key, best_entry


def _warn_dangling_keys(projects: dict, *, no_entry: bool) -> None:
    """Warn about projects.json keys whose directory no longer exists.

    A dangling key is the signature of a moved/renamed/deleted project: its entry
    silently stops matching, so a renamed project would otherwise fall back to the
    global defaults — exactly the account/profile mix-up per-project config exists
    to prevent. Entries are only ever created deliberately (`yolo config`), so a
    dangling key is always actionable, never noise. The rename case produces a
    dangling key *and* a no-entry cwd at once, in the renamed directory itself —
    when both hold, connect the dots explicitly.
    """
    dangling = [k for k in projects if not pathlib.Path(os.path.expanduser(k)).is_dir()]
    for k in dangling:
        print(
            f"warning: projects.json entry {k}: directory no longer exists (moved or renamed?)",
            file=sys.stderr,
        )
    if dangling and no_entry:
        print(
            "warning: if this directory used to be one of those, re-run `yolo config` "
            "here and remove the stale entry.",
            file=sys.stderr,
        )


def load_yolo_config(
    start: pathlib.Path, home: pathlib.Path, *, worktree_dir: pathlib.Path | None = None
) -> tuple[dict, str | None]:
    """Merge ~/.yolo.json with the matching ~/.claude-yolo/projects.json entry.

    Returns (merged_defaults, matched_project_key). Precedence low->high:
    ~/.yolo.json < projects.json entry < worktree overlay < CLI args (the caller
    applies the dict via PARSER.set_defaults, so explicit flags still win). When
    `worktree_dir` is given (the launch verbs in worktree mode), that worktree's
    ~/.claude-yolo/worktrees.json entry is layered on as the most specific
    persisted layer. prompts/mounts/ports concatenate across the layers; every
    other key is overridden by the higher layer. All three files are host-side
    only — outside every container mount — so nothing Claude writes inside a
    container can change what the next launch mounts or which credentials it uses.
    Also prints the config provenance line and the stale-state warnings (dangling
    project keys, leftover in-directory .yolo.json files) to stderr.
    """
    merged = {}
    layers = []

    def merge(updates):
        for dest, val in updates.items():
            if dest in _CONCAT_DESTS:
                merged[dest] = merged.get(dest, []) + val
            else:
                merged[dest] = val

    home_file = home / ".yolo.json"
    if home_file.is_file():
        merge(_parse_yolo_file(home_file))
        layers.append("~/.yolo.json")

    projects_file = home / ".claude-yolo" / "projects.json"
    projects = _read_projects_file(projects_file)
    matched_key, entry = _match_project_entry(projects, start)
    _warn_dangling_keys(projects, no_entry=matched_key is None)
    if matched_key is not None:
        merge(_parse_yolo_dict(entry, f"{projects_file} [{matched_key}]"))
        layers.append(f"projects.json[{matched_key}]")

    # Worktree overlay (when launching in worktree mode): the most specific
    # persisted layer, beating the project entry but still under the CLI flags.
    if worktree_dir is not None:
        wt_entry = _read_worktrees_file(_worktrees_file(home)).get(
            _worktree_overlay_key(worktree_dir)
        )
        if wt_entry:
            merge(_parse_yolo_dict(wt_entry, f"worktrees.json [{worktree_dir.name}]"))
            layers.append(f"worktrees.json[{worktree_dir.name}]")

    # Warn about (but never read) a leftover in-directory .yolo.json — loudly
    # enough that a file planted by a container can't go unnoticed, on every
    # launch until it's migrated. ~/.yolo.json itself is exempt: it's the global
    # layer, not an in-directory file.
    for cur in [start.resolve(), *start.resolve().parents]:
        cand = cur / ".yolo.json"
        if cand.is_file():
            if cand.resolve() != home_file.resolve():
                print(
                    f"warning: {cand} is no longer read; move its settings to "
                    "~/.yolo.json or to this project's entry in "
                    "~/.claude-yolo/projects.json (see `yolo config`).",
                    file=sys.stderr,
                )
            break

    provenance = " + ".join(layers) if layers else "built-in defaults"
    if matched_key is None:
        provenance += " (no project entry)"
    print(f"config: {provenance}", file=sys.stderr)
    return merged, matched_key


def _parse_mount_spec(spec: str) -> tuple[pathlib.Path, str]:
    """One --mount / `mounts` value, `PATH[:ro|:rw]` -> (resolved path, mode).

    A file or a directory; read-only is the default (the use case is reference
    material or a single secret like a token file; :rw is the explicit opt-in). The
    source must exist: docker silently creates a missing bind-mount source as a
    root-owned *directory* on the host, which we never want (and would be wrong for
    an intended file). Only directories are later forwarded to claude as --add-dir.
    """
    path_part, mode = spec, "ro"
    if spec.endswith((":ro", ":rw")):
        path_part, mode = spec[:-3], spec[-2:]
    path = pathlib.Path(os.path.expanduser(path_part))
    if not path.exists():
        sys.exit(f"mount: no such file or directory: {path_part}")
    return path.resolve(), mode


def _resolve_mounts(specs: list[str]) -> list[tuple[pathlib.Path, str]]:
    """Parse + dedupe the merged mount specs into (dir, mode) pairs.

    Specs arrive lowest-precedence first (~/.yolo.json, projects.json entry, then
    CLI values appended by argparse), so on a same-path ro/rw conflict the later
    spec — the higher layer — wins.
    """
    out: dict[pathlib.Path, str] = {}
    for spec in specs:
        path, mode = _parse_mount_spec(spec)
        out[path] = mode
    return list(out.items())


def _parse_port_spec(spec: str) -> tuple[int | None, int]:
    """One --port / `ports` value, `[HOST:]CONTAINER` -> (host or None, container).

    A bare container port is the normal form: the host side stays 0 so docker
    assigns a free ephemeral port, which is what lets parallel sessions of the
    same project coexist (`yolo browse` finds the assigned port). An explicit
    HOST: pins a stable, bookmarkable host port instead — single-session use; a
    second concurrent session fails at `docker run` with address-in-use. A host
    *address* is deliberately not expressible: forwards are always loopback-bound
    so the skip-permissions container's server never lands on the LAN (the raw
    `-- -p` passthrough remains the escape hatch).
    """
    host_part, sep, container_part = spec.rpartition(":")
    parts = [host_part, container_part] if sep else [container_part]
    if not all(p.isdigit() and 0 < int(p) < 65536 for p in parts):
        sys.exit(f"port: must be CONTAINER or HOST:CONTAINER (ports 1-65535): {spec!r}")
    return (int(host_part) if sep else None, int(container_part))


def _resolve_ports(specs: list[str]) -> list[tuple[int | None, int]]:
    """Parse + dedupe the merged port specs into (host, container) pairs.

    Keyed by container port, lowest-precedence first (like _resolve_mounts), so
    when two layers forward the same container port the later spec — the higher
    layer — wins (e.g. a project's `9000:8000` pin over a global `8000`).
    Insertion order is kept: the first-configured port is `browse`'s default.
    """
    out: dict[int, int | None] = {}
    for spec in specs:
        host, cport = _parse_port_spec(spec)
        out[cport] = host
    return [(host, cport) for cport, host in out.items()]


def _port_container(spec: str) -> str:
    """The container-port part of a `[HOST:]CONTAINER` port spec.

    Lenient on purpose (no validation), like _spec_path: it's used to *match*
    stored specs for --remove-port, whose point may be deleting a malformed one.
    """
    return spec.rpartition(":")[2]


def _project_key(cwd: pathlib.Path) -> str:
    """The projects.json key for this invocation.

    The main repo root when inside a git repo — so subdirectory runs and worktree
    sessions share the project's entry — else the cwd itself.
    """
    root = _main_root_or_none()
    return str(root if root is not None else cwd.resolve())


def _explicit_config_flags(script_argv: list[str]) -> dict:
    """{config-key: value} for every YOLO_KEYS flag explicitly present in argv.

    Re-parses with sentinel defaults: a plain parse can't distinguish "defaulted"
    from "explicitly set to the default value", and `yolo config --auth oauth-token`
    must persist auth even though oauth-token is the default. List-kind dests get a
    fresh marker list (argparse's append action copies the default before
    appending, so identity survives exactly when the flag never appeared).
    """
    markers = {dest: [] if kind == "list" else _UNSET for dest, kind in YOLO_KEYS.values()}
    saved = {dest: PARSER.get_default(dest) for dest in markers}
    PARSER.set_defaults(**markers)
    try:
        parsed = PARSER.parse_args(script_argv)
    finally:
        # Restore the real defaults: the sentinels must not leak into any later
        # parse (the process normally exits right after `config`, but don't bank
        # on it — a leaked _UNSET shows up as a bizarre downstream type error).
        PARSER.set_defaults(**saved)
    out = {}
    for norm, (dest, _) in YOLO_KEYS.items():
        val = getattr(parsed, dest)
        if val is not markers[dest]:
            out[norm.replace("_", "-")] = val
    return out


def _spec_path(spec: str) -> pathlib.Path:
    """The (expanded, resolved) directory a PATH[:ro|:rw] mount spec names.

    Unlike _parse_mount_spec this never requires the directory to exist — it's
    used to *match* stored specs, including for --remove-mount, whose whole point
    may be deleting a mount whose directory is gone.
    """
    if spec.endswith((":ro", ":rw")):
        spec = spec[:-3]
    return pathlib.Path(os.path.expanduser(spec)).resolve()


def _take_list_key(entry: dict, key: str, where: str) -> list[str]:
    """Pop `key` (in either dash/underscore spelling) out of `entry`, as a list.

    Config list keys accept a bare string or a list of strings; normalize to a
    list so the --add-*/--remove-* edits have one shape to work on. Any other
    type would fail validation at load time — fail here with the same message,
    before the edit code touches the value.
    """
    norm = key.replace("-", "_")
    vals: list[str] = []
    for ek in [k for k in entry if k.replace("-", "_") == norm]:
        v = entry.pop(ek)
        if isinstance(v, str):
            v = [v]
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            sys.exit(f"{where}: {key!r} must be a string or list of strings")
        vals.extend(v)
    return vals


def _apply_config_edits(
    current: dict, explicit: dict, parsed, where: str, base_dir: pathlib.Path
) -> dict:
    """One updated config object: whole-key sets, then --unset, then list edits.

    Shared by the project-entry, --global, and worktree-overlay paths of
    `do_config`; `where` names the target in error messages. `base_dir` is the
    directory a *relative* `dockerfile` path is validated against (the worktree
    dir for a TOPIC overlay, else the cwd) — mirroring how the launch resolves it.
    Conflicting instructions for the same key in one invocation (set + unset,
    --mount alongside --add/--remove-mount, -p alongside --add/--remove-prompt)
    are errors, not silently ordered.
    """
    if "mounts" in explicit and (parsed.add_mounts or parsed.remove_mounts):
        sys.exit(
            "--mount replaces the whole `mounts` list; "
            "don't combine it with --add-mount/--remove-mount."
        )
    if "prompts" in explicit and (parsed.add_prompts or parsed.remove_prompts):
        sys.exit(
            "--prompt/-p replaces the whole list; "
            "don't combine it with --add-prompt/--remove-prompt."
        )
    if "ports" in explicit and (parsed.add_ports or parsed.remove_ports):
        sys.exit(
            "--port replaces the whole `ports` list; "
            "don't combine it with --add-port/--remove-port."
        )
    if "secrets" in explicit and (parsed.add_secrets or parsed.remove_secrets):
        sys.exit(
            "--secret replaces the whole `secrets` list; "
            "don't combine it with --add-secret/--remove-secret."
        )
    unsets = [u.replace("_", "-") for u in parsed.unsets]
    for u in unsets:
        if u in explicit:
            sys.exit(f"can't both set and --unset {u!r}.")
    if "mounts" in unsets and (parsed.add_mounts or parsed.remove_mounts):
        sys.exit("can't combine --unset mounts with --add-mount/--remove-mount.")
    if "prompts" in unsets and (parsed.add_prompts or parsed.remove_prompts):
        sys.exit("can't combine --unset prompts with --add-prompt/--remove-prompt.")
    if "ports" in unsets and (parsed.add_ports or parsed.remove_ports):
        sys.exit("can't combine --unset ports with --add-port/--remove-port.")
    if "secrets" in unsets and (parsed.add_secrets or parsed.remove_secrets):
        sys.exit("can't combine --unset secrets with --add-secret/--remove-secret.")

    for spec in [*explicit.get("mounts", []), *parsed.add_mounts]:
        _parse_mount_spec(spec)  # validate now, so a typo'd path can't be pinned
    for spec in [*explicit.get("ports", []), *parsed.add_ports]:
        _parse_port_spec(spec)  # likewise: a malformed port spec can't be pinned
    for spec in [*explicit.get("secrets", []), *parsed.add_secrets]:
        _parse_secret_spec(spec)  # likewise: a malformed secret spec can't be pinned
    df = explicit.get("dockerfile")
    if df is not None and not _resolve_dockerfile(df, base_dir).is_file():
        sys.exit(f"dockerfile: not a file: {df}")  # a typo'd path can't be pinned
    rc = explicit.get("yolorc")
    if rc is not None and not _resolve_yolorc(rc, base_dir).is_file():
        sys.exit(f"yolorc: not a file: {rc}")  # a typo'd path can't be pinned

    entry = dict(current)
    for k, v in explicit.items():
        norm = k.replace("-", "_")
        for stale in [ek for ek in entry if ek.replace("-", "_") == norm and ek != k]:
            del entry[stale]  # the same key in its other (underscored) spelling
        entry[k] = v

    for u in unsets:
        norm = u.replace("-", "_")
        present = [ek for ek in entry if ek.replace("-", "_") == norm]
        if not present:
            sys.exit(f"--unset {u}: not set in {where}.")
        # Any *present* key may be unset — even one YOLO_KEYS doesn't know — so a
        # stale/unknown key that breaks loading can be repaired from here.
        for ek in present:
            del entry[ek]

    if parsed.add_mounts or parsed.remove_mounts:
        mounts = _take_list_key(entry, "mounts", where)
        for rm in parsed.remove_mounts:
            kept = [s for s in mounts if _spec_path(s) != _spec_path(rm)]
            if len(kept) == len(mounts):
                sys.exit(f"--remove-mount {rm}: no such mount in {where}.")
            mounts = kept
        for add in parsed.add_mounts:
            # Same path already listed -> replace it (so :ro/:rw can be flipped).
            mounts = [s for s in mounts if _spec_path(s) != _spec_path(add)]
            mounts.append(add)
        if mounts:  # an emptied list is dropped: for a concat key, [] ≡ absent
            entry["mounts"] = mounts

    if parsed.add_prompts or parsed.remove_prompts:
        prompts = _take_list_key(entry, "prompts", where)
        for rm in parsed.remove_prompts:
            if rm not in prompts:
                sys.exit(f"--remove-prompt {rm!r}: no such prompt in {where}.")
            prompts.remove(rm)
        for add in parsed.add_prompts:
            if add not in prompts:  # exact dup -> no-op, so re-runs are idempotent
                prompts.append(add)
        if prompts:
            entry["prompts"] = prompts

    if parsed.add_ports or parsed.remove_ports:
        ports = _take_list_key(entry, "ports", where)
        for rm in parsed.remove_ports:
            kept = [s for s in ports if _port_container(s) != _port_container(rm)]
            if len(kept) == len(ports):
                sys.exit(f"--remove-port {rm}: no such port in {where}.")
            ports = kept
        for add in parsed.add_ports:
            # Same container port already listed -> replace it (so a HOST: pin
            # can be added or dropped without a remove+add).
            ports = [s for s in ports if _port_container(s) != _port_container(add)]
            ports.append(add)
        if ports:
            entry["ports"] = ports

    if parsed.add_secrets or parsed.remove_secrets:
        # Secret specs are opaque strings (like prompts), so add/remove match the
        # exact spec rather than a parsed target — a name needed both as env and
        # file is two distinct specs.
        secrets = _take_list_key(entry, "secrets", where)
        for rm in parsed.remove_secrets:
            if rm not in secrets:
                sys.exit(f"--remove-secret {rm!r}: no such secret in {where}.")
            secrets.remove(rm)
        for add in parsed.add_secrets:
            if add not in secrets:  # exact dup -> no-op, so re-runs are idempotent
                secrets.append(add)
        if secrets:
            entry["secrets"] = secrets

    return entry


def _do_config_worktree(
    home: pathlib.Path, topic: str, explicit: dict, editing: bool, parsed
) -> None:
    """`yolo config TOPIC`: show or update a worktree's worktrees.json overlay.

    The worktree counterpart to the project-entry path in `do_config`, reusing the
    same `_apply_config_edits` machinery against worktrees.json keyed by the
    worktree's absolute path. --global/--init are project/global notions and don't
    combine with a worktree; editing requires the worktree to exist (configuring a
    non-existent one is meaningless).
    """
    if parsed.cfg_global:
        sys.exit("--global edits ~/.yolo.json; it can't target a worktree.")
    if parsed.init:
        sys.exit("--init registers a project entry, not a worktree overlay.")

    worktree, _, _ = _worktree_dir(topic, home)
    wt_file = _worktrees_file(home)
    worktrees = _read_worktrees_file(wt_file)
    key = _worktree_overlay_key(worktree)

    if not explicit and not editing:
        print(f"worktrees file: {wt_file}")
        if key in worktrees:
            print(json.dumps({topic: worktrees[key]}, indent=2))
        else:
            print(f"no overlay for '{topic}'")
        return

    if not worktree.is_dir():
        sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")

    where = f"{wt_file} [{topic}]"
    # A relative `dockerfile` is validated (and later resolved) against the
    # worktree dir, so `config TOPIC --dockerfile ./Dockerfile.yolo` pins the
    # worktree's own copy even when run from the main checkout.
    entry = _apply_config_edits(dict(worktrees.get(key, {})), explicit, parsed, where, worktree)
    _parse_yolo_dict(entry, where)  # never write an unloadable entry
    worktrees[key] = entry
    _write_worktrees_file(wt_file, worktrees)
    print(f"Updated {wt_file}:")
    print(json.dumps({topic: entry}, indent=2))


def _effective_config(
    home: pathlib.Path, cwd: pathlib.Path
) -> tuple[list[tuple[str, object, list[str]]], str | None]:
    """The merged global+project config that would apply at `cwd`, with per-key
    provenance — what a bare `yolo config` shows.

    Returns (items, matched_key). `items` is an ordered list of
    `(key, value, sources)`: the canonical dashed key, its raw JSON value (paths
    left un-expanded, so you see what's written), and the layer label(s) that
    set it — one for a scalar (the winning layer), possibly two for a concat key
    (`mounts`/`ports`/`prompts`/`secrets`) where global + project both
    contribute. Mirrors `load_yolo_config`'s precedence (`~/.yolo.json` <
    projects.json entry), but keeps values raw and tracks provenance for display.
    It does **not** add the worktree overlay (bare `config` is project-scoped;
    `yolo config TOPIC` shows the overlay) or the CLI layer or built-in defaults.

    Read leniently like the `--global` show path: a present-but-unloadable file
    errors pointedly, but an entry with an unknown key still displays (you fix it
    here with `--unset`), so this never validates via `_parse_yolo_dict`.
    """
    raw_layers: list[tuple[str, dict]] = []

    global_file = home / ".yolo.json"
    if global_file.is_file():
        try:
            g = json.loads(global_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"{global_file}: cannot read config: {e}")
        if not isinstance(g, dict):
            sys.exit(f"{global_file}: must contain a JSON object")
        raw_layers.append(("~/.yolo.json", g))

    projects = _read_projects_file(home / ".claude-yolo" / "projects.json")
    matched_key, entry = _match_project_entry(projects, cwd)
    if matched_key is not None:
        raw_layers.append(("projects.json", entry))

    values: dict[str, object] = {}
    sources: dict[str, list[str]] = {}
    order: list[str] = []
    for label, raw in raw_layers:
        for k, v in raw.items():
            norm = k.replace("-", "_")
            key = norm.replace("_", "-")
            if v is None:
                continue  # explicit null = leave at built-in default (skipped, like the loader)
            if key not in values:
                order.append(key)
            if norm in _CONCAT_DESTS:
                # concat keys accumulate across layers; normalize to a list so a
                # global string + a project list still merge cleanly.
                add = list(v) if isinstance(v, list) else [v]
                values[key] = (values[key] if key in values else []) + add  # type: ignore[operator]
                sources.setdefault(key, []).append(label)
            else:
                values[key] = v
                sources[key] = [label]

    items = [(k, values[k], sources[k]) for k in order]
    return items, matched_key


def do_config(
    script_argv: list[str], home: pathlib.Path, cwd: pathlib.Path, parsed, topic: str | None = None
) -> None:
    """`config` verb: show or update yolo's host-side config, then exit.

    Operates on this project's ~/.claude-yolo/projects.json entry, or — with
    --global — on ~/.yolo.json itself (a la `git config --global`), or — with a
    TOPIC — on that worktree's ~/.claude-yolo/worktrees.json overlay. With config
    flags, persists exactly the explicitly-passed YOLO_KEYS flags into the
    target, per-key (other keys are left alone) — `yolo config` is the *only*
    writer of the project layer; a plain launch never touches the file, so it
    stays a deliberate, auditable ledger of per-project grants. On top of
    whole-key sets, --add-mount/--remove-mount and --add-prompt/--remove-prompt
    edit single elements of the list-valued keys, and --unset KEY deletes a key
    entirely (see _apply_config_edits). With no flags, prints the target that
    currently applies (read-only), a la `git config --list`. Mount paths are
    validated on set/add so a typo can't be pinned.

    `--init` registers the project with an *empty* entry — no overrides, just
    "yolo knows about this project". That's all `require-project-entry` needs,
    and the alternative (pinning some explicitly-defaulted flag) would record a
    customization the user never meant. Bare `yolo config` stays read-only, so
    an explicit flag is the only way to create an empty entry.
    """
    projects_file = home / ".claude-yolo" / "projects.json"
    explicit = _explicit_config_flags(script_argv)
    editing = bool(
        parsed.add_mounts
        or parsed.remove_mounts
        or parsed.add_prompts
        or parsed.remove_prompts
        or parsed.add_ports
        or parsed.remove_ports
        or parsed.add_secrets
        or parsed.remove_secrets
        or parsed.unsets
    )

    if topic:
        _do_config_worktree(home, topic, explicit, editing, parsed)
        return

    if parsed.init:
        if explicit or editing:
            sys.exit(
                "--init registers the project with no overrides; to set config "
                "values, use `yolo config` with just those flags."
            )
        if parsed.cfg_global:
            sys.exit("--init registers a project entry; it can't combine with --global.")
        projects = _read_projects_file(projects_file)
        key = _project_key(cwd)
        if key in projects:
            sys.exit(f"{key} already has a projects.json entry; `yolo config` shows it.")
        matched_key, _ = _match_project_entry(projects, cwd)
        if matched_key is not None:
            # Longest key wins and only one entry applies, so an empty entry here
            # switches this project OFF the ancestor's config — flag it.
            print(
                f"warning: this empty entry now shadows the entry for {matched_key} "
                f"when running under {key}.",
                file=sys.stderr,
            )
        projects[key] = {}
        projects_file.parent.mkdir(parents=True, exist_ok=True)
        projects_file.write_text(json.dumps(projects, indent=2) + "\n")
        print(f"Registered {key} in {projects_file} (no overrides).")
        return

    if parsed.cfg_global:
        # Target the flat ~/.yolo.json. Read it raw (not via _parse_yolo_file,
        # which transforms values): this is read-modify-write, and a file that
        # fails validation must still be repairable here via --unset.
        global_file = home / ".yolo.json"
        where = str(global_file)
        current = {}
        if global_file.is_file():
            try:
                current = json.loads(global_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
                sys.exit(f"{global_file}: cannot read config: {e}")
            if not isinstance(current, dict):
                sys.exit(f"{global_file}: must contain a JSON object")
        if not explicit and not editing:
            print(f"global config file: {global_file}")
            print(json.dumps(current, indent=2) if global_file.is_file() else "no global config")
            return
        updated = _apply_config_edits(current, explicit, parsed, where, cwd)
        _parse_yolo_dict(updated, where)  # never write an unloadable config
        global_file.write_text(json.dumps(updated, indent=2) + "\n")
        print(f"Updated {global_file}:")
        print(json.dumps(updated, indent=2))
        return

    projects = _read_projects_file(projects_file)
    key = _project_key(cwd)

    if not explicit and not editing:
        # Show the *complete* effective config that would apply here — the global
        # ~/.yolo.json values that aren't overridden, merged with this project's
        # entry — not just the project entry, with per-key provenance.
        items, matched_key = _effective_config(home, cwd)
        print(f"projects file: {projects_file}")
        if not items:
            note = "" if matched_key is not None else " (no project entry)"
            print(f"no config applies for {key}; built-in defaults{note}")
        else:
            print(f"effective config for {key}:")
            width = max(len(k) for k, _, _ in items)
            for k, v, srcs in items:
                print(f"  {k.ljust(width)}  {json.dumps(v)}  [{' + '.join(srcs)}]")
        _warn_dangling_keys(projects, no_entry=matched_key is None)
        return

    where = f"{projects_file} [{key}]"
    entry = _apply_config_edits(dict(projects.get(key, {})), explicit, parsed, where, cwd)
    _parse_yolo_dict(entry, where)  # never write an unloadable entry
    projects[key] = entry
    projects_file.parent.mkdir(parents=True, exist_ok=True)
    projects_file.write_text(json.dumps(projects, indent=2) + "\n")
    print(f"Updated {projects_file}:")
    print(json.dumps({key: entry}, indent=2))


def _pyproject_version() -> str | None:
    """`version` from an *adjacent* claude-yolo pyproject.toml, else None.

    A pyproject sits next to the running yolo.py only when it's run standalone (the
    PEP 723 script, possibly via a PATH symlink — hence `resolve()`) or installed
    **editable** (`__file__` resolves into the source checkout). A regular wheel
    ships only yolo.py (`only-include`), so there's no adjacent pyproject and this
    returns None. The `name` guard keeps an unrelated pyproject that happens to sit
    beside a stray copy of yolo.py from being mistaken for ours."""
    try:
        pyproject = (pathlib.Path(__file__).resolve().parent / "pyproject.toml").read_text()
    except OSError:
        return None
    if not re.search(r'^name\s*=\s*"claude-yolo"', pyproject, re.MULTILINE):
        return None
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    return match.group(1) if match else None


def _base_version() -> str:
    """Best-effort package version, tracing to pyproject.toml.

    Prefer an *adjacent* pyproject.toml when there is one: that's the live source of
    truth, present for a standalone-script or editable install, and reading it keeps
    `--version` correct after a bump with no reinstall (an editable install's
    recorded metadata is a frozen install-time snapshot and would otherwise lag).
    A regular wheel has no adjacent pyproject, so fall back to the recorded package
    metadata. Neither (a stray copy) → "unknown".
    """
    from_pyproject = _pyproject_version()
    if from_pyproject is not None:
        return from_pyproject
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("claude-yolo")
    except PackageNotFoundError:
        return "unknown"


def _git(*args: str) -> str | None:
    """Run a git command in the running yolo.py's own directory; None on any
    failure. The repo is the source checkout when yolo.py is run standalone or
    installed editable (`__file__` resolves into the checkout); a regular wheel
    install lands in an isolated site-packages dir that isn't a repo, so every
    call here fails and the version stays clean."""
    repo = pathlib.Path(__file__).resolve().parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_suffix(base: str) -> str:
    """A local-version suffix flagging that the running yolo.py is live code (a
    checkout — editable install or the standalone script), not a wheel of a tagged
    release. Empty *only* when not in a git repo, which means a released wheel
    (a wheel ships just yolo.py into site-packages, so `git` finds no repo there).

    Reaching past that check means `__file__` is inside a checkout, so the version
    is never left bare — it always carries a marker saying which live state it's in:
    `+g{sha}` when HEAD isn't the commit tagged `v{base}` (committed work past the
    release, or the tag isn't fetched locally); `+editable` when HEAD *is* that
    commit with a clean tree (otherwise indistinguishable from a wheel of the tag);
    plus `.dirty` (or a bare `+dirty`) when the working tree has uncommitted
    changes."""
    head = _git("rev-parse", "--short=7", "HEAD")
    if head is None:
        return ""  # not a git repo → released wheel, leave base clean
    dirty = _git("status", "--porcelain") not in ("", None)
    tagged = _git("rev-parse", "--verify", "--quiet", f"v{base}^{{commit}}")
    full_head = _git("rev-parse", "HEAD")
    on_release = tagged is not None and tagged == full_head
    if on_release:
        # On the release commit, but it's a live checkout (a wheel returned above),
        # so still mark it rather than report a bare version a real wheel would.
        return "+dirty" if dirty else "+editable"
    return f"+g{head}.dirty" if dirty else f"+g{head}"


def _version() -> str:
    """Package version for `--version`, with a local-version suffix
    (`+editable` / `+g{sha}` / `[.]dirty`) when running live code from a checkout
    rather than an installed wheel (see _git_suffix)."""
    base = _base_version()
    if base == "unknown":
        return base
    return base + _git_suffix(base)


PARSER = argparse.ArgumentParser(
    description="Run Claude Code in a Docker container.",
    epilog=(
        "Defaults come from ~/.yolo.json overlaid by this project's entry in "
        "~/.claude-yolo/projects.json (created with `yolo config`); CLI flags "
        "override both. Both files are host-side only — an in-directory .yolo.json "
        "is no longer read. Arguments after -- are passed directly to docker run "
        "(last-one-wins, so they override defaults)."
    ),
)
PARSER.add_argument(
    "--version",
    action="version",
    version=f"%(prog)s {_version()}",
)
PARSER.add_argument(
    "verb",
    nargs="?",
    choices=[
        "config",
        "start",
        "resume",
        "shell",
        "browse",
        "finish",
        "rebase",
        "list",
        "ps",
        "dir",
        "dockerfile",
        "setup-token",
        "tokens",
        "forget-token",
        "secret",
    ],
    help="Optional subcommand. start/resume/shell/browse take an *optional* TOPIC: "
    "with a TOPIC they act on a git worktree of that name (start creates it, the "
    "others require it); with no TOPIC they act on the current directory (start a "
    "fresh session, resume the most recent one, or open a shell). 'browse' opens "
    "the host browser at the running session's forwarded port (see --port/`ports` "
    "config). 'finish' removes a "
    "worktree and requires a TOPIC; 'rebase' rebases a worktree's branch onto "
    "--base (default HEAD), replaying it on top of commits landed on the base "
    "since it branched (requires a TOPIC). 'list' shows this repo's worktrees; 'ps' shows "
    "all running yolo containers across repos (see --watch); 'dir' prints a "
    "session's directory (a worktree's root with a TOPIC, else the current "
    "directory) for `cd $(yolo dir TOPIC)`; 'config' "
    "shows this project's ~/.claude-yolo/projects.json entry (or ~/.yolo.json "
    "with --global), or — given config flags — persists exactly those flags into "
    "it (see also --unset, --add-mount/--remove-mount, --add-prompt/"
    "--remove-prompt); 'dockerfile' prints the built-in default Dockerfile (a "
    "starting point for --dockerfile; --custom prints a layer-on-top template "
    "instead); 'setup-token' mints/caches a "
    "long-lived OAuth token (for --auth oauth-token); 'tokens' lists the tokens "
    "yolo has minted; 'forget-token' deletes the active config dir's token from "
    "the keychain (local only — see `tokens` output for revocation). A bare "
    "`yolo` is equivalent to `yolo start`.",
)
PARSER.add_argument(
    "topic",
    nargs="?",
    help="Worktree/branch name. Required for finish and rebase; optional for "
    "start/resume/shell (omit it to act on the current directory). For the "
    "`secret` verb this is the subcommand (set/list/rm) instead.",
)
PARSER.add_argument(
    "extra_args",
    nargs="*",
    metavar="ARGS",
    help="Trailing positionals. Used by `secret set NAME` / `secret rm NAME` for "
    "the secret name; not accepted by other verbs.",
)
PARSER.add_argument(
    "--base",
    metavar="REF",
    default="HEAD",
    help="For `start`: git ref the new branch is created from; for `rebase`: the "
    "ref a worktree's branch is rebased onto; for `list`/`finish`: the ref a branch "
    "is judged merged against (default: HEAD). Also settable as `base` in config "
    '(e.g. "origin/main").',
)
PARSER.add_argument(
    "--finish-action",
    choices=FINISH_CHOICES,
    default="delete-if-merged",
    help="For `finish`: what to do with the branch after removing the worktree "
    "(default: delete-if-merged). 'delete-if-merged' deletes the branch iff it's "
    "reachable from --base, else keeps it; 'merge' merges it into the current "
    "checkout then deletes it; 'push' pushes it to --finish-remote and keeps it "
    "locally; 'keep' leaves the branch alone. Also settable as `finish-action` "
    "in config.",
)
PARSER.add_argument(
    "--finish-remote",
    metavar="NAME",
    default="origin",
    help="For `finish --finish-action push`: the remote to push the branch to "
    "(default: origin). Also settable as `finish-remote` in config.",
)
PARSER.add_argument(
    "--new",
    action="store_true",
    help="For `resume`: start a fresh session in the worktree instead of continuing.",
)
PARSER.add_argument(
    "--init",
    action="store_true",
    help="For `config`: register this project in projects.json with an empty entry "
    "(no overrides) — enough to satisfy require-project-entry. Errors if the "
    "project already has its own entry.",
)
PARSER.add_argument(
    "--global",
    dest="cfg_global",
    action="store_true",
    help="For `config`: show or update the global ~/.yolo.json instead of this "
    "project's projects.json entry (a la `git config --global`).",
)
PARSER.add_argument(
    "--unset",
    dest="unsets",
    action="append",
    default=[],
    metavar="KEY",
    help="For `config`: delete KEY (e.g. `auth`, `mounts`) from the entry entirely, "
    "so it falls back to the lower config layers / built-in default. Errors if the "
    "key isn't set. Repeatable.",
)
PARSER.add_argument(
    "--add-mount",
    dest="add_mounts",
    action="append",
    default=[],
    metavar="PATH[:ro|:rw]",
    help="For `config`: add one directory to the stored `mounts` list (or update "
    "its :ro/:rw mode if the path is already listed), leaving the rest of the "
    "list alone — unlike --mount, which replaces the whole list. Repeatable.",
)
PARSER.add_argument(
    "--remove-mount",
    dest="remove_mounts",
    action="append",
    default=[],
    metavar="PATH",
    help="For `config`: remove PATH's entry from the stored `mounts` list (any "
    ":ro/:rw suffix is ignored; the directory needn't exist, so a stale mount "
    "can be removed). Errors if the path isn't listed. Repeatable.",
)
PARSER.add_argument(
    "--add-port",
    dest="add_ports",
    action="append",
    default=[],
    metavar="[HOST:]CONTAINER",
    help="For `config`: add one port to the stored `ports` list (or update its "
    "HOST: pin if the container port is already listed), leaving the rest of "
    "the list alone — unlike --port, which replaces the whole list. Repeatable.",
)
PARSER.add_argument(
    "--remove-port",
    dest="remove_ports",
    action="append",
    default=[],
    metavar="CONTAINER",
    help="For `config`: remove a container port's entry from the stored `ports` "
    "list (any HOST: prefix is ignored). Errors if the port isn't listed. "
    "Repeatable.",
)
PARSER.add_argument(
    "--add-secret",
    dest="add_secrets",
    action="append",
    default=[],
    metavar="NAME[:TARGET]",
    help="For `config`: add one secret spec to the stored `secrets` list (no-op if "
    "already present), leaving the rest alone — unlike --secret, which replaces the "
    "whole list. Repeatable.",
)
PARSER.add_argument(
    "--remove-secret",
    dest="remove_secrets",
    action="append",
    default=[],
    metavar="NAME[:TARGET]",
    help="For `config`: remove an exact secret spec from the stored `secrets` list. "
    "Errors if not present. Repeatable.",
)
PARSER.add_argument(
    "--add-prompt",
    dest="add_prompts",
    action="append",
    default=[],
    metavar="PROMPT",
    help="For `config`: add one prompt to the stored `prompts` list (no-op if "
    "already present), leaving the rest alone — unlike --prompt, which replaces "
    "the whole list. Repeatable.",
)
PARSER.add_argument(
    "--remove-prompt",
    dest="remove_prompts",
    action="append",
    default=[],
    metavar="PROMPT",
    help="For `config`: remove an exact prompt string from the stored `prompts` "
    "list. Errors if not present. Repeatable.",
)
PARSER.add_argument(
    "--force",
    action="store_true",
    help="For `finish`: remove the worktree even with uncommitted changes. For "
    "`rebase`: rebase even when a container is running and its session isn't "
    "confirmed idle.",
)
PARSER.add_argument(
    "--config-dir",
    metavar="PATH",
    help="Config directory to mount at /home/claude/.claude "
    "(default: ~/.claude). Credentials are pulled from the keychain entry "
    "for this directory.",
)
PARSER.add_argument(
    "--auth",
    choices=AUTH_CHOICES,
    default="oauth-token",
    help="Authentication mechanism (default: oauth-token). "
    "'oauth-token' forwards a long-lived CLAUDE_CODE_OAUTH_TOKEN from "
    "`claude setup-token` (stable; no refresh boundary, so safe regardless of "
    "session timing or concurrency); 'keychain' extracts the rotating Claude.ai "
    "keychain credentials and mounts a snapshot (CAUTION: the refresh token "
    "rotates single-use, so any session running when the access token expires "
    "either wins the refresh or is broken by it — and a container win logs out "
    "the host too); 'bedrock' authenticates via AWS Bedrock (mounts ~/.aws, "
    "sets CLAUDE_CODE_USE_BEDROCK=1). Also settable as `auth` in .yolo.json.",
)
PARSER.add_argument(
    "--aws-profile",
    metavar="NAME",
    help="AWS profile to use (requires --auth bedrock). If omitted, the AWS SDK's "
    "default profile / env credentials are used.",
)
PARSER.add_argument(
    "--aws-region",
    metavar="REGION",
    help="AWS region for Bedrock (requires --auth bedrock; default: us-east-1).",
)
PARSER.add_argument(
    "--bedrock-model",
    metavar="ID",
    help="Bedrock model id (requires --auth bedrock).",
)
PARSER.add_argument(
    "--claude-json",
    action=argparse.BooleanOptionalAction,
    default=True,
    help="Mount the host ~/.claude.json into the container (default: on). "
    "Use --no-claude-json for a cleanly isolated profile (e.g. with an "
    "alternate --config-dir).",
)
PARSER.add_argument(
    "--tmux",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Spawn the session as a window of a shared tmux session instead of "
    "running in this terminal (default: off). Ensures the tmux session exists — "
    "seeding window 0 with a `yolo ps --watch` dashboard — opens a new window "
    "running the container, and attaches to it (or switches the current client, "
    "when already inside tmux). Also settable as `tmux` in config; "
    "--no-tmux overrides it for one run.",
)
PARSER.add_argument(
    "--tmux-session",
    metavar="NAME",
    default="yolo",
    help="With --tmux: name of the shared tmux session (default: yolo). One "
    "global session is the point — every yolo session lands in it regardless of "
    "repo — but `tmux-session` in per-project config can group differently.",
)
PARSER.add_argument(
    "--watch",
    action="store_true",
    help="For `ps`: refresh the listing every 2 seconds until interrupted. Run "
    "interactively inside tmux it's a picker: j/k/arrows move, Enter switches "
    "to the selected session's window, q quits.",
)
PARSER.add_argument(
    "--all",
    action="store_true",
    dest="all_repos",
    help="For `list`: show worktrees across all repos under ~/.claude-yolo/worktrees "
    "(with a leading REPO column), not just the current repo's.",
)
PARSER.add_argument(
    "--ssh-agent",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Forward the host ssh-agent socket into the container (default: off). "
    "Off keeps your SSH keys out of the skip-permissions container; turn it on "
    "with --ssh-agent (or `ssh-agent: true` in config) when you need in-container "
    "GitHub git auth, which won't work without it.",
)
PARSER.add_argument(
    "--submodules",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Before launch, populate git submodules in the session's working dir "
    "(`git submodule update --init --recursive`, host-side). Off by default since "
    "most repos have none; turn it on with --submodules (or `submodules: true` in "
    "config). No-op when the dir has no .gitmodules.",
)
PARSER.add_argument(
    "--rebuild-image",
    action="store_true",
    default=False,
    dest="rebuild_image",
    help="Force a Docker image rebuild from scratch (passes --no-cache to docker build).",
)
PARSER.add_argument(
    "--dockerfile",
    dest="dockerfile",
    default=None,
    metavar="PATH",
    help="Build the container image from this Dockerfile instead of the built-in "
    "default (or set `dockerfile` in config). The host UID is passed in as the "
    "HOST_UID build ARG, so the custom Dockerfile should `ARG HOST_UID` and use it "
    "for its non-root user to keep bind-mount ownership correct.",
)
PARSER.add_argument(
    "--yolorc",
    dest="yolorc",
    default=None,
    metavar="PATH",
    help="Source this shell file inside the container before the session starts "
    "(or set `yolorc` in config). A relative path resolves against the session "
    "working dir (a checked-in rc); an absolute path (~ ok) is used as-is, for an "
    "out-of-tree rc the container can't edit. Use it for per-session setup such as "
    "`gh auth login --with-token < tokenfile` (tokenfile supplied via --mount); "
    "`export`s reach Claude's env. Opt-in by design: a repo's rc is inert unless "
    "you point this key at it. Code it runs is container-confined, like the session.",
)
PARSER.add_argument(
    "--mount",
    dest="mounts",
    action="append",
    default=[],
    metavar="PATH[:ro|:rw]",
    help="Extra host file or directory to bind-mount into the container at its "
    "identical host path, read-only unless :rw is appended. Repeatable; also "
    "settable as `mounts` in config, where the lists concatenate across the layers "
    "and the CLI. Each mounted *directory* is also passed to claude as --add-dir so "
    "it shows up as a working directory (files aren't — --add-dir is dir-only).",
)
PARSER.add_argument(
    "--port",
    dest="ports",
    action="append",
    default=[],
    metavar="[HOST:]CONTAINER",
    help="Forward a container port to the host, bound to 127.0.0.1. With a bare "
    "CONTAINER port docker picks a free host port per session — so parallel "
    "sessions never collide — discoverable with `yolo browse`; HOST: pins a "
    "stable host port instead. Repeatable; also settable as `ports` in config, "
    "where the lists concatenate across the layers and the CLI. With the "
    "`browse` verb: which forwarded container port to open.",
)
PARSER.add_argument(
    "--secret",
    dest="secrets",
    action="append",
    default=[],
    metavar="NAME[:TARGET]",
    help="Inject a keychain-stored secret (set with `yolo secret set`) into the "
    "session. Bare NAME -> env var NAME; NAME:ENVNAME -> env var ENVNAME; "
    "NAME:/path or NAME:~/path -> mounted file at that container path (~ is the "
    "container home /home/claude). A trailing ! on an env target makes it ephemeral "
    "(deleted right after it's exported). Repeatable; also settable as `secrets` in "
    "config, where the lists concatenate across the layers and the CLI. The value "
    "never enters the docker-run argv — env secrets transit a private /run/secrets "
    "file mount, file secrets a read-only bind mount.",
)
PARSER.add_argument(
    "--project",
    action="store_true",
    help="For `secret set`/`secret rm`: act on this project's scope (keyed to the "
    "main repo root) instead of the global scope.",
)
PARSER.add_argument(
    "--clipboard",
    action="store_true",
    help="For `secret set`: read the value from the macOS clipboard (pbpaste) "
    "instead of stdin / an interactive prompt.",
)
PARSER.add_argument(
    "--print",
    "-n",
    dest="print_url",
    action="store_true",
    help="For `browse`: print the session's URL without opening a browser.",
)
PARSER.add_argument(
    "--custom",
    action="store_true",
    help="For `dockerfile`: print a ready-to-edit custom Dockerfile that layers on "
    "the default via `FROM ${YOLO_BASE}` (with a marked block for your steps), "
    "instead of dumping the default itself.",
)
PARSER.add_argument(
    "--require-project-entry",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Refuse to launch unless a ~/.claude-yolo/projects.json entry matches the "
    "cwd (default: off). Set it in ~/.yolo.json to keep a renamed project from "
    "silently falling back to the global defaults; --no-require-project-entry "
    "overrides it for one run.",
)
PARSER.add_argument(
    "--dangerously-allow-home",
    action="store_true",
    default=False,
    help="Allow launching with the working directory at or above $HOME, which "
    "mounts your entire home directory — including ~/.ssh and yolo's own config — "
    "read-write into the container. Deliberately CLI-only: it cannot be set from "
    "a config file.",
)
PARSER.add_argument(
    "--prompt",
    "-p",
    dest="prompts",
    action="append",
    default=[],
    metavar="PROMPT",
    help="Extra system-prompt text passed to claude inside the container (via its "
    "--append-system-prompt), on top of a built-in prompt about the container "
    "itself. Repeatable; also settable as `prompts` in config, where the lists "
    "concatenate across the layers and the CLI.",
)
# Resume a prior session. Session history lives in the bind-mounted ~/.claude/projects/
# and is keyed by the project path, which matches between host and container (cwd is mounted
# at its identical path), so sessions started in a yolo container are resumable here.
# For the `resume` verb. A plain `resume` continues the most recent session
# (claude --continue); -r picks a specific session by SESSION_ID, or opens the
# interactive picker when given no ID (claude --resume).
PARSER.add_argument(
    "--resume",
    "-r",
    dest="resume",
    nargs="?",
    const=True,
    default=None,
    metavar="SESSION_ID",
    help="With `resume`: resume a specific Claude session by SESSION_ID, or omit it "
    "for an interactive picker (claude --resume). A plain `resume` (no -r) continues "
    "the most recent session.",
)


def running_container_for(
    slug: str | None, topic: str | None = None, *, cwd: pathlib.Path | None = None
) -> str | None:
    """The id of a running yolo container for this repo, or None.

    Containers are tagged with yolo.repo / yolo.worktree / yolo.cwd labels at launch,
    so we find them by label rather than reconstructing the (suffix-laden) name. Pass
    `topic` to match a worktree container, or `cwd` to match a plain current-directory
    container (a worktree runs under its own path, so the cwd label disambiguates the
    two even though they share a repo slug).
    """
    filters = []
    if slug:
        filters += ["--filter", f"label=yolo.repo={slug}"]
    if topic:
        filters += ["--filter", f"label=yolo.worktree={topic}"]
    if cwd:
        filters += ["--filter", f"label=yolo.cwd={cwd}"]
    try:
        out = subprocess.run(
            ["docker", "ps", *filters, "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except FileNotFoundError:
        return None
    return out.splitlines()[0] if out else None


def _read_settings_hooks(config_dir: str | None, home: pathlib.Path) -> dict:
    """The `hooks` from the mounted settings files, to re-add under `--settings`.

    yolo injects its session-state hooks via `claude --settings`, which *replaces*
    the whole `hooks` key from the mounted settings rather than merging it (only
    `permissions` merges across Claude Code's setting scopes — everything else,
    like the `sandbox` override, is per-key replace). So to keep a user's own
    hooks working inside the container we read them here and concatenate yolo's
    onto them (see build_claude_args). Best-effort: a missing or malformed file
    contributes nothing. Covers the config dir's settings.json + settings.local.json,
    not enterprise-managed settings (rare, and managed settings outrank --settings).
    """
    base = pathlib.Path(config_dir) if config_dir else home / ".claude"
    merged: dict = {}
    for fname in ("settings.json", "settings.local.json"):
        try:
            data = json.loads((base / fname).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        for event, groups in hooks.items():
            if isinstance(groups, list):
                merged.setdefault(event, []).extend(groups)
    return merged


def build_claude_args(
    prompts: list,
    *,
    ssh_agent: bool = False,
    continue_session: bool = False,
    resume=None,
    name: str | None = None,
    add_dirs=(),
    forwarded_ports=(),
    status_state_path: str | None = None,
    extra_hooks: dict | None = None,
) -> list[str]:
    """The args passed to `claude` inside the container (everything after the image).

    Always includes the container-only sandbox override and the built-in
    "you're in a container" system prompt (plus any -p additions). Extra mounts
    are forwarded as --add-dir so they're first-class working directories;
    forwarded container ports get a prompt line telling Claude servers must bind
    0.0.0.0 — the single most common reason a forwarded port "doesn't work" is a
    dev server defaulting to loopback inside the container, where docker's
    forward can't reach it. Optionally adds --continue / --resume [ID] and a
    session --name.
    """
    extra_system_prompt = [
        "You are running in an ephemeral Ubuntu container instead of MacOS host. Use sudo apt to install things you need.",
        *(
            [
                "The SSH agent is not forwarded into this container. You do not have SSH access and cannot git push."
            ]
            if not ssh_agent
            else []
        ),
        *(
            [
                f"Container port(s) {', '.join(str(p) for p in forwarded_ports)} are "
                "forwarded to the host. A server must listen on 0.0.0.0 (not "
                "127.0.0.1) to be reachable from the host browser; the user opens "
                "it with `yolo browse`."
            ]
            if forwarded_ports
            else []
        ),
        *prompts,
    ]
    # The container is the sandbox, so disable Claude's in-process OS sandbox.
    # Otherwise it warns at startup that bubblewrap/socat are missing (they're
    # deliberately not installed — they can't create namespaces in a container
    # anyway). This is a container-only --settings overlay; the host's settings
    # files are untouched. --settings *replaces* each key it sets (only
    # `permissions` merges across scopes), so sandbox.enabled and the whole
    # `hooks` key below override the mounted settings — which is why we fold the
    # user's own hooks back in via extra_hooks.
    settings: dict = {"sandbox": {"enabled": False}}
    if status_state_path:
        # Session-activity hooks: Stop = "now waiting for input", UserPromptSubmit
        # = "working again". Each writes "<state> <epoch>" to the status file `ps`
        # reads. The absolute container path is baked in (not via an env var) so
        # nothing depends on docker -e reaching the hook subprocess; the path has
        # no shell-special chars but quote defensively.
        target = shlex.quote(status_state_path)
        hooks: dict = {}
        for event, groups in (extra_hooks or {}).items():
            hooks.setdefault(event, []).extend(groups)
        hooks.setdefault("Stop", []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"printf 'waiting %s' \"$(date +%s)\" > {target}",
                    }
                ]
            }
        )
        hooks.setdefault("UserPromptSubmit", []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"printf 'working %s' \"$(date +%s)\" > {target}",
                    }
                ]
            }
        )
        settings["hooks"] = hooks
    args = [
        "--settings",
        json.dumps(settings, separators=(",", ":")),
        "--append-system-prompt",
        "... ".join(extra_system_prompt),
    ]
    for d in add_dirs:
        # Extra mounts double as claude working dirs so they're visible in /context;
        # a mount Claude doesn't know about only helps if the user mentions it.
        args += ["--add-dir", str(d)]
    if continue_session:
        args += ["--continue"]
    elif resume is not None:
        args += ["--resume"] + ([resume] if isinstance(resume, str) else [])
    if name:
        # Name the Claude session so it's identifiable in the prompt box / picker.
        # Only for a fresh session: claude rejects --name alongside --continue/--resume.
        args = ["--name", name, *args]
    return args


def _worktree_ps1_label(worktree: pathlib.Path) -> str:
    """Short display label for a worktree path, used in the in-container PS1.

    The raw path (~/.claude-yolo/worktrees/<slug>/<topic>) is far too long for a
    prompt, and the slug — the slugified absolute repo path — mostly repeats what
    every other slug under the worktrees root says. So the label drops the
    worktrees root and the prefix shared by *all* slugs under it, keeping just the
    distinguishing tail plus the topic (e.g. "claude-yolo/fix-auth"). With a
    single slug the shared prefix is the whole slug and the label is just the
    topic.
    """
    slug_dir = worktree.parent
    siblings = [p.name for p in slug_dir.parent.iterdir() if p.is_dir()]
    shared = os.path.commonprefix(siblings)
    short = slug_dir.name[len(shared) :].lstrip("-")
    return f"{short}/{worktree.name}" if short else worktree.name


def _ps1_env_args(cwd: pathlib.Path, worktree_name: str | None) -> list[str]:
    """docker -e args giving in-container bash a yolo-flagged PS1.

    The image's .bashrc adopts $YOLO_PS1 when set, so any bash — a fresh
    `yolo shell` container or a `docker exec` into a running one (exec inherits
    the run-time env) — shows it's a yolo shell and the working directory. In
    worktree mode the long worktree prefix of $PWD is rewritten to the short
    label at prompt time (bash expands PS1 itself); YOLO_WT_DIR/YOLO_WT_LABEL
    carry the pieces because a literal path inlined into ${PWD/#.../...} would
    clash with the expansion's / delimiters.
    """
    tag = r"\[\e[1;33m\]yolo\[\e[0m\]:"
    blue, reset = r"\[\e[1;34m\]", r"\[\e[0m\]"
    if worktree_name:
        where = "${PWD/#$YOLO_WT_DIR/$YOLO_WT_LABEL}"
        extra = ["-e", f"YOLO_WT_DIR={cwd}", "-e", f"YOLO_WT_LABEL={_worktree_ps1_label(cwd)}"]
    else:
        where = r"\w"
        extra = []
    return [*extra, "-e", f"YOLO_PS1={tag}{blue}{where}{reset}\\$ "]


TMUX_DASHBOARD_WINDOW = "yolo-ps"


def _tmux(*args: str) -> subprocess.CompletedProcess:
    """Run one tmux command with output captured; callers branch on returncode."""
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _self_invocation() -> str:
    """An absolute path for re-invoking yolo (the dashboard window's command).

    sys.argv[0] is however we were invoked — a console script found on PATH, a
    ./yolo.py relative path, or a symlink. which() resolves the PATH case, and
    the result is absolutized so the command still works from tmux's own working
    directory. A symlink is deliberately *not* resolve()d away: the symlink is
    the install (and resolving could land on a non-executable file).
    """
    argv0 = sys.argv[0]
    found = shutil.which(argv0) or argv0
    return str(pathlib.Path(found).absolute())


def _pin_tmux_window_name(target: str) -> None:
    """Lock a window's name so tmux can't relabel it out from under us.

    `new-window -n` sets the name, but tmux's automatic-rename (and a program's
    own title escape, governed by allow-rename) can later overwrite it with the
    foreground process name — node, python, bash — so the status bar stops
    showing which container/topic each window is. Turning both off for our
    windows keeps the explicit name (and #W in the terminal title) stable.
    """
    _tmux("set-window-option", "-t", target, "automatic-rename", "off")
    _tmux("set-window-option", "-t", target, "allow-rename", "off")


def _tmux_window_command(run_cmd: list) -> str:
    """The shell command a tmux window runs: run_cmd, held open on failure.

    tmux windows close when their command exits (remain-on-exit is off by
    default) — right for a clean `claude` exit, but it would eat the error when
    docker fails instantly (name conflict, daemon down): the window flashes and
    is gone. The wrapper keeps a *failed* window alive until Enter.
    """
    cmd = shlex.join(str(a) for a in run_cmd)
    return (
        f"{cmd}; ec=$?; if [ $ec -ne 0 ]; then "
        'printf "\\n[exited %d -- press Enter to close]\\n" "$ec"; read -r _; fi'
    )


def _find_tmux_window(session: str, name: str) -> str | None:
    """The window id of the window called `name` in `session`, or None."""
    res = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_id}\t#{window_name}")
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        wid, _, wname = line.partition("\t")
        if wname == name:
            return wid
    return None


def _session_has_client(session: str) -> bool:
    """Whether a tmux client is already attached to `session` (in some terminal)."""
    res = _tmux("list-clients", "-t", f"={session}", "-F", "#{client_name}")
    return res.returncode == 0 and bool(res.stdout.strip())


def _ensure_tmux_session(session: str) -> None:
    """Make sure the shared tmux session exists, creating it detached if not.

    A fresh session gets the dashboard as window 0: `yolo ps --watch`, re-invoked
    via the absolute path we were launched from (a bare `yolo` may not be on the
    tmux server's PATH). The dashboard gets the same keep-open-on-failure wrapper
    as the container windows — which also keeps a bad self-invocation from
    killing the just-created session before the real window is added.
    """
    if _tmux("has-session", "-t", f"={session}").returncode == 0:
        return
    dashboard = _tmux_window_command([_self_invocation(), "ps", "--watch"])
    res = _tmux("new-session", "-d", "-s", session, "-n", TMUX_DASHBOARD_WINDOW, dashboard)
    if res.returncode != 0:
        sys.exit(f"tmux new-session failed: {res.stderr.strip()}")
    # Make the OS terminal title reflect the focused yolo window: tmux's set-titles
    # is off by default, so the title would otherwise stay whatever it was before
    # attaching (#W is the window name = the container/topic). Scoped to the session
    # we just created — a pre-existing session (incl. a personal one aimed at via
    # --tmux-session) is never reconfigured, since we return above when it exists.
    _tmux("set-option", "-t", f"={session}", "set-titles", "on")
    _tmux("set-option", "-t", f"={session}", "set-titles-string", "yolo · #S · #W")
    _pin_tmux_window_name(f"={session}:{TMUX_DASHBOARD_WINDOW}")


def _launch_in_tmux(
    run_cmd: list, window_name: str, *, session: str, reuse_existing: bool = False
) -> None:
    """Spawn run_cmd as a named window of the shared tmux session and focus it.

    Focusing depends on where yolo was invoked: inside tmux (this session or
    another) the current client is switched over; outside, the invoking terminal
    execs into `tmux attach`, mirroring the default mode's
    this-terminal-becomes-the-session feel — *unless* the session already has a
    client attached in another terminal, in which case we don't attach a second
    (mirroring) client but just switch that terminal to the new window and leave
    this one a normal shell. With reuse_existing (the caller
    found the matching container already running), an existing same-named window
    is focused instead of spawning a duplicate `docker run` that would only die
    on the container-name conflict; if no window matches (the container was
    started outside tmux mode), we still spawn and let docker report the
    conflict in the new window.
    """
    if not shutil.which("tmux"):
        sys.exit("--tmux needs tmux installed and on PATH (brew install tmux).")
    _ensure_tmux_session(session)

    window_id = _find_tmux_window(session, window_name) if reuse_existing else None
    if window_id:
        print(
            f"'{window_name}' is already running in tmux; switching to its window.",
            file=sys.stderr,
        )
    else:
        res = _tmux(
            "new-window",
            "-t",
            f"={session}:",
            "-n",
            window_name,
            "-P",
            "-F",
            "#{window_id}",
            _tmux_window_command(run_cmd),
        )
        if res.returncode != 0:
            sys.exit(f"tmux new-window failed: {res.stderr.strip()}")
        window_id = res.stdout.strip()
        _pin_tmux_window_name(window_id)

    if os.environ.get("TMUX"):
        # Already a tmux client: re-point it at the session and window. (Window
        # ids are server-global, so select-window works across sessions.)
        _tmux("select-window", "-t", window_id)
        _tmux("switch-client", "-t", f"={session}")
        print(f"Spawned '{window_name}' in tmux session '{session}'.")
    elif _session_has_client(session):
        # Another terminal is already attached to this session. Attaching a
        # second client here would make both terminals *mirror* the one session
        # (tmux clamps every attached client to the smallest one's size and
        # shows them the same window) — the duplicate-session-in-two-terminals
        # surprise. Instead just point the already-attached client at the new
        # window; the session shows up over there, and this terminal stays a
        # normal shell.
        _tmux("select-window", "-t", window_id)
        print(
            f"Spawned '{window_name}' in tmux session '{session}', which is "
            "already attached in another terminal — switched that terminal to "
            "the new window."
        )
    else:
        # No client yet: become the tmux client, focused on the new window.
        # select-window runs first (it works detached — it just moves the
        # session's current-window pointer); the ";" argument is tmux's command
        # separator, not shell syntax — there's no shell here, this is an exec.
        os.execvp(
            "tmux",
            [
                "tmux",
                "select-window",
                "-t",
                window_id,
                ";",
                "attach-session",
                "-t",
                f"={session}",
            ],
        )


def _dispatch_launch(
    run_cmd: list,
    parsed,
    *,
    window_name: str,
    slug: str | None = None,
    worktree_name: str | None = None,
    cwd: pathlib.Path | None = None,
) -> None:
    """Run an assembled launch command: exec it here, or spawn it into tmux.

    The seam between assembling a launch and deciding where it runs. Default:
    exec in the invoking terminal, exactly the pre-tmux behavior. With --tmux
    (or `tmux: true` in config) the command becomes a window of the shared tmux
    session instead; when the matching container is already running (the same
    label query the `shell` verb uses), its window is reused rather than
    spawning a docker run that's doomed to the container-name conflict.
    """
    if not parsed.tmux:
        os.execvp(run_cmd[0], run_cmd)
        return
    reuse = bool(running_container_for(slug, worktree_name, cwd=None if worktree_name else cwd))
    _launch_in_tmux(run_cmd, window_name, session=parsed.tmux_session, reuse_existing=reuse)


def _init_submodules(cwd: pathlib.Path) -> None:
    """Populate git submodules in `cwd` before launch (opt-in via --submodules).

    Run host-side, on purpose: it needs the host's git credentials and network.
    git (2.53, tested) gives each worktree/checkout its *own* submodule git dir —
    a new worktree clones fresh from the remote rather than reusing the objects in
    a sibling worktree or the shared .git/modules/<name> — so populating generally
    fetches. The host has the creds/network for that; an in-container clone of a
    private submodule would fail with the ssh-agent off by default. The files land
    in the bind-mounted working dir, so Claude sees them. A no-op when there's no
    .gitmodules (a plain repo, or a cwd that isn't a git repo at all); best-effort,
    so a failure (network/auth, or a submodule already populated) warns but doesn't
    block the session.
    """
    if not (cwd / ".gitmodules").is_file():
        return
    print("Populating git submodules (--submodules)…", file=sys.stderr)
    result = subprocess.run(["git", "-C", str(cwd), "submodule", "update", "--init", "--recursive"])
    if result.returncode != 0:
        print(
            "warning: `git submodule update --init --recursive` failed; continuing "
            "without populated submodules.",
            file=sys.stderr,
        )


def launch_container(
    parsed,
    *,
    home: pathlib.Path,
    cwd: pathlib.Path,
    common_git: pathlib.Path | None,
    worktree_name: str | None,
    slug: str | None,
    container_base: str,
    command: list,
    entrypoint: str | None = None,
    docker_args=(),
    mounts=(),
    ports=(),
) -> None:
    """Assemble the `docker run` argv from the credential/config flags and exec it.

    Shared by every launch path (start / resume / shell, worktree or cwd). The
    container name starts from container_base and gains -{config}/-{profile}
    suffixes; yolo.repo / yolo.worktree labels are stamped so the verbs can find
    the container later. `command` is the args after the image; `entrypoint`
    overrides the image ENTRYPOINT (used to drop into bash for `shell`); `mounts`
    is the resolved (dir, mode) list from --mount / the `mounts` config key;
    `ports` the resolved (host-or-None, container) pairs from --port / `ports`.
    """
    # Finalize the container name up front (the -{config}/-{profile} suffixes the
    # auth/config blocks below would otherwise tack on), because the per-session
    # run dir is keyed by the *final* name so the docker-ps GC can match it.
    config_dir = parsed.config_dir
    container = container_base
    if config_dir:
        container = f"{container}-{pathlib.Path(config_dir).resolve().name}"
    if parsed.auth == "bedrock":
        container = f"{container}-{parsed.aws_profile or 'bedrock'}"

    # Reclaim leftover run dirs of finished sessions, then make this session's dir
    # (mode 700). It holds the chmod-600 credential/secret files bind-mounted for
    # the container's lifetime; yolo execvp's into docker, so the GC — not a
    # finally/atexit — is what cleans them up once the container is gone.
    _gc_run_dir()
    run_dir = _session_run_dir(container)

    args = [
        "-w",
        str(cwd),
        "-v",
        f"{cwd}:{cwd}",
        # Hostname set to working dir basename so Claude Code status line shows project name without git
        "--hostname",
        cwd.name,
        # A deterministic marker that this is a yolo container, so anything inside
        # (Claude, scripts, hooks) can tell — e.g. to commit freely on the current
        # branch, since the worktree/branch is already the unit of isolation.
        "-e",
        "YOLO_SESSION=1",
        # A yolo-flagged bash prompt for `yolo shell` (fresh or exec'd into this container)
        *_ps1_env_args(cwd, worktree_name),
        # Forward the host git identity so commits made in the container are attributed correctly
        *git_identity_args(),
    ]

    # Extra reference mounts (--mount / `mounts` config): bind-mounted at their
    # identical host paths, like the cwd, so paths match host<->container.
    for path, mode in mounts:
        args += ["-v", f"{path}:{path}:{mode}"]

    # Port forwards (--port / `ports` config): loopback-bound, never the LAN.
    # Host port 0 = docker assigns a free ephemeral port, so parallel sessions
    # of the same project can't collide; `docker port` (via `yolo browse`) is
    # the registry of what was assigned — yolo keeps no port state of its own.
    for host_port, container_port in ports:
        args += ["-p", f"127.0.0.1:{host_port or 0}:{container_port}"]

    # --yolorc: bind-mount the rc read-only at a fixed path and point YOLO_RC at
    # it. The .bashrc sources it for `yolo shell` (fresh or exec'd in — the env
    # var rides the container's runtime env); claude launches source it via the
    # command wrapper below (claude isn't a shell, so .bashrc never runs for it).
    # Read-only here even for an in-tree rc; the rw cwd mount is the editable copy
    # (same Claude-can-edit-between-runs caveat as an in-tree --dockerfile).
    yolorc_host = _resolve_yolorc(parsed.yolorc, cwd) if parsed.yolorc else None
    if yolorc_host:
        args += ["-v", f"{yolorc_host}:{_YOLORC_CONTAINER_PATH}:ro"]
        args += ["-e", f"YOLO_RC={_YOLORC_CONTAINER_PATH}"]

    if parsed.ssh_agent:
        # Forward the host ssh-agent via the Docker engine's magic socket. We canNOT bind-mount
        # the raw host $SSH_AUTH_SOCK: that socket's listener lives in the macOS kernel, while
        # the container runs in the engine's Linux VM (Docker Desktop or OrbStack), so the
        # mounted inode is dead (connect() -> ECONNREFUSED). /run/host-services/ssh-auth.sock
        # is a socket the VM itself listens on and proxies to the host agent — both Docker
        # Desktop and OrbStack expose it at that path. It's mounted srw-rw----
        # root:root, so the claude user must be in group 0 to connect (see the useradd line).
        # --no-ssh-agent skips all of this.
        args += [
            "-v",
            "/run/host-services/ssh-auth.sock:/run/ssh-agent",
            "-e",
            "SSH_AUTH_SOCK=/run/ssh-agent",
            # Mount host known_hosts so SSH host key verification succeeds
            "-v",
            f"{home}/.ssh/known_hosts:/home/claude/.ssh/known_hosts:ro",
            # Route GitHub HTTPS git operations over SSH so they reuse the forwarded agent —
            # no tokens ever enter the container (HTTPS auth is a bearer token, which would
            # have to; SSH is challenge-response, so the key stays on the host). Remotes can
            # stay https://github.com/...; git rewrites them to git@github.com: before
            # connecting. Applied as run-time git config via GIT_CONFIG_* env (highest
            # precedence) ONLY here, so without --ssh-agent plain HTTPS clones of public
            # repos still work instead of being rewritten to an SSH URL that can't auth.
            "-e",
            "GIT_CONFIG_COUNT=1",
            "-e",
            "GIT_CONFIG_KEY_0=url.git@github.com:.insteadOf",
            "-e",
            "GIT_CONFIG_VALUE_0=https://github.com/",
        ]

    # Worktree mode: mount the shared .git at its real host path so the worktree's
    # absolute gitdir pointers resolve and commits persist to the host.
    if common_git:
        args += ["-v", f"{common_git}:{common_git}"]

    # Credential/config assembly. The config axes (a, b) are independent of the
    # auth mechanism (c), which is a single mutually-exclusive choice (--auth):
    #   (a) which config dir to mount        -- --config-dir
    #   (b) whether to mount ~/.claude.json   -- --claude-json/--no-claude-json
    #   (c) the auth mechanism                -- --auth keychain|oauth-token|bedrock

    # (a) Config dir. Always mounted at /home/claude/.claude (= the claude user's
    # $HOME/.claude, i.e. Claude Code's default), so no CLAUDE_CONFIG_DIR is needed.
    # (The container name's -{config} suffix was applied up front, with the run dir.)
    if config_dir:
        args += ["-v", f"{config_dir}:/home/claude/.claude"]
        host_claude_dir = config_dir
    else:
        args += ["-v", f"{home}/.claude:/home/claude/.claude"]
        host_claude_dir = f"{home}/.claude"

    # (b) ~/.claude.json (global config: MCP servers, project history/trust). Always at
    # $HOME/.claude.json on the host (it ignores CLAUDE_CONFIG_DIR). Opt out with
    # --no-claude-json to keep an alternate --config-dir profile cleanly isolated.
    if parsed.claude_json:
        args += ["-v", f"{home}/.claude.json:/home/claude/.claude.json"]

    # On macOS the host stores Claude Code's creds in the Keychain, so a
    # `.credentials.json` in the host config dir should never exist. One almost always
    # means a past container wrote it back through the rw mount (Linux Claude Code has
    # no Keychain and falls back to the file store). Such a file is stale and, under
    # Claude Code 2.1.x, shadows the OAuth-token env var → /login. The auth block below
    # overlays that path for this run (keychain with real creds; oauth-token/bedrock
    # with a throwaway), so it's masked here regardless; warn so the user can delete it.
    if (pathlib.Path(host_claude_dir) / ".credentials.json").exists():
        print(
            f"warning: {host_claude_dir}/.credentials.json exists on the host. On macOS\n"
            "  Claude Code uses the Keychain, so this file shouldn't exist — it was likely\n"
            "  written back by a past yolo container and can shadow the OAuth token. This\n"
            "  run masks it; consider deleting it.",
            file=sys.stderr,
        )

    # (c) Auth mechanism (--auth), one of three mutually-exclusive paths:
    #   - oauth-token (default): forward a long-lived CLAUDE_CODE_OAUTH_TOKEN env var.
    #     No keychain extraction, no login check. The token is stable (never
    #     rotated/written back), so concurrent containers and the host can all use it
    #     at once. We *do* overlay a throwaway .credentials.json (_masking_credfile):
    #     under Claude Code 2.1.x a stale mounted creds file is preferred over the env
    #     token and shadows it, and the overlay also stops the container persisting
    #     creds back to the host ~/.claude.
    #   - bedrock: AWS creds + env (mounts ~/.aws), no keychain/login; same throwaway
    #     creds overlay so a container can't pollute the host ~/.claude.
    #   - keychain: extract the rotating keychain creds into a mounted file. All
    #     snapshots (and the host keychain) share one refresh boundary — the access
    #     token's expiry — and whoever refreshes first there breaks every other
    #     holder, host login included.
    token_env: dict[str, str] = {}
    if parsed.auth == "oauth-token":
        # Deliver the token through the /run/secrets file transport (staged below),
        # NOT `-e`: an `-e NAME=value` lands on the host docker-run argv (which yolo
        # prints, and which shows in host `ps`), in `docker inspect`'s Config.Env,
        # and in tmux's retained pane command. A chmod-600 run-dir file + the loader
        # keeps it off all three. (It still ends up in claude's *in-container*
        # process environ — unavoidable, since claude reads CLAUDE_CODE_OAUTH_TOKEN
        # from there — but that's inside claude's own trust boundary; claude holds
        # the token regardless.)
        token_env["CLAUDE_CODE_OAUTH_TOKEN"] = ensure_oauth_token(config_dir)
        args += ["-v", f"{_masking_credfile(run_dir)}:/home/claude/.claude/.credentials.json"]
    elif parsed.auth == "bedrock":
        # (the container name's -{profile} suffix was applied up front, with the run dir.)
        args += ["-v", f"{home}/.aws:/home/claude/.aws:ro"]
        args += ["-e", "CLAUDE_CODE_USE_BEDROCK=1"]
        if parsed.aws_profile:
            args += ["-e", f"AWS_PROFILE={parsed.aws_profile}"]
        args += ["-e", f"AWS_REGION={parsed.aws_region or 'us-east-1'}"]
        if parsed.bedrock_model:
            args += ["-e", f"BEDROCK_MODEL_ID={parsed.bedrock_model}"]
        args += ["-v", f"{_masking_credfile(run_dir)}:/home/claude/.claude/.credentials.json"]
    else:  # keychain
        ensure_logged_in(config_dir)
        credfile = extract_credentials(config_dir, run_dir)
        args += ["-v", f"{credfile}:/home/claude/.claude/.credentials.json"]

    # Secrets (--secret / `secrets` config) + the OAuth token (token_env): stage
    # chmod-600 files in the run dir and bind-mount them — env targets via the rw
    # /run/secrets loader dir, file targets read-only at their path. No value ever
    # reaches the docker-run argv. The project key (for project-scope resolution of
    # user secrets) is the main repo root, which _project_key derives from the host
    # process cwd regardless of any worktree retargeting of `cwd`; the token needs
    # no project key, so skip the git call when there are no user secrets.
    project_key = _project_key(cwd) if parsed.secrets else None
    secret_args, have_env_secrets = _stage_secrets(
        parsed.secrets, project_key, run_dir, cwd, extra_env=token_env
    )
    args += secret_args

    # Labels let the verbs (shell/finish/list) find this container later, regardless
    # of the name suffixes above. yolo.cwd is stamped on every launch so a plain
    # `shell` (no topic) can find the container running in this exact directory.
    if slug:
        args += ["--label", f"yolo.repo={slug}"]
    if worktree_name:
        args += ["--label", f"yolo.worktree={worktree_name}"]
    args += ["--label", f"yolo.cwd={cwd}"]
    # yolo.config-dir tells the cross-repo `ps` where to find this session's
    # activity status file (under <config-dir>/.yolo-status/), since containers
    # from different repos may use different config dirs.
    args += ["--label", f"yolo.config-dir={host_claude_dir}"]
    if ports:
        # The container ports forwarded at launch, in config order (first =
        # `browse`'s default). The label — not config — is what browse/ps read:
        # it describes the *actual* container, which can't change after launch,
        # while config describes the next one.
        args += ["--label", "yolo.ports=" + ",".join(str(c) for _, c in ports)]

    # Reset the session's activity status file (claude launches only — the
    # `shell` bash entrypoint has no hooks). The Stop/UserPromptSubmit hooks
    # write into <config-dir>/.yolo-status/ (visible in-container via the config
    # mount); clearing the stale file means a fresh session doesn't briefly show
    # a prior one's "waiting" time before the first hook fires.
    if entrypoint is None:
        status_dir = pathlib.Path(host_claude_dir) / _STATUS_DIR_NAME
        status_dir.mkdir(parents=True, exist_ok=True)
        (status_dir / f"{_cwd_slug(cwd)}.state").unlink(missing_ok=True)

    if parsed.submodules:
        _init_submodules(cwd)

    image_tag = _build_image(parsed, cwd)

    # For a claude launch (entrypoint is None → the image's `claude
    # --dangerously-skip-permissions` ENTRYPOINT) with env values to load —
    # env-target secrets, the OAuth token in oauth-token mode (the default, so this
    # is the common path), and/or --yolorc — drop into bash to source the secrets
    # loader (which exports each /run/secrets file, the token included) and then the
    # rc, before exec'ing the reconstructed claude command. claude isn't a shell, so
    # it never reads .bashrc (where `yolo shell` gets these) — the wrapper is how a
    # claude session picks them up. `source` (not run) so the exports reach claude's
    # env; the loader runs *before* the rc so an rc can use the exported values; a
    # nonzero rc warns but doesn't block. The claude args are passed positionally to
    # "$@" so the --settings JSON needs no re-quoting.
    if (yolorc_host or have_env_secrets) and entrypoint is None:
        entrypoint = "/bin/bash"
        command = [
            "-c",
            "[ -f /etc/yolo/load-secrets.sh ] && . /etc/yolo/load-secrets.sh; "
            '[ -f "$YOLO_RC" ] && { . "$YOLO_RC" || '
            'echo "yolo: .yolorc exited nonzero, continuing" >&2; }; exec "$@"',
            "yolo-rc",
            "claude",
            "--dangerously-skip-permissions",
            *command,
        ]

    entry = ["--entrypoint", entrypoint] if entrypoint else []
    run_cmd = [
        "docker",
        "run",
        "-it",
        "--rm",
        "--name",
        container,
        *args,
        *entry,
        *docker_args,
        image_tag,
        *command,
    ]

    sep = "- " * 40
    print(sep)
    print(" ".join(run_cmd))
    print(sep)
    _dispatch_launch(
        run_cmd,
        parsed,
        window_name=container,
        slug=slug,
        worktree_name=worktree_name,
        cwd=cwd,
    )


def _remove_worktree(worktree: pathlib.Path, topic: str, force: bool) -> None:
    """`git worktree remove` the worktree, falling back to manual removal.

    git unconditionally refuses to remove a worktree containing populated
    submodules ("working trees containing submodules cannot be moved or removed")
    — the check predates the dirty/locked checks and `--force` doesn't bypass it.
    In that one case we do the documented manual workaround: delete the directory
    ourselves, then `git worktree prune` the now-stale admin entry. Our own dirty
    guard has already run (or been waived by --force), so the rm is gated. Any
    *other* git failure (e.g. a locked worktree) is surfaced verbatim, not forced.
    """
    remove = ["git", "worktree", "remove"] + (["--force"] if force else []) + [str(worktree)]
    result = subprocess.run(remove, capture_output=True, text=True)
    if result.returncode != 0:
        if "submodule" not in result.stderr.lower():
            sys.exit(result.stderr.strip() or f"failed to remove worktree '{topic}'.")
        # Submodule case: git won't, so we do it by hand.
        shutil.rmtree(worktree)
    subprocess.run(["git", "worktree", "prune"])


def do_finish(
    topic: str,
    home: pathlib.Path,
    base: str,
    *,
    force: bool,
    action: str = "delete-if-merged",
    remote: str = "origin",
) -> None:
    """`finish` verb: remove a worktree, then handle its branch per `action`.

    Guards against the real loss vectors — a running container holding the mount,
    and uncommitted changes (unless --force) — then removes the worktree. What
    happens to the branch is controlled by `action` (--finish-action):

    - `delete-if-merged` (default): delete the branch iff it's reachable from
      `base` (merged or never diverged); otherwise keep it with a note about
      where it stands vs. upstream.
    - `merge`: merge the branch into the current checkout, then delete it (on a
      merge failure the branch is kept and the worktree is already gone).
    - `push`: push the branch to `remote` (--finish-remote) and keep it locally.
    - `keep`: leave the branch alone.
    """
    _, _, slug = _repo_paths()
    worktree = home / ".claude-yolo" / "worktrees" / slug / topic
    if not worktree.is_dir():
        sys.exit(f"no worktree '{topic}'; nothing to finish.")
    if running_container_for(slug, topic):
        sys.exit(f"a container is running for '{topic}'; exit it first.")
    dirty = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty and not force:
        sys.exit(f"worktree '{topic}' has uncommitted changes; commit them or re-run with --force.")

    _remove_worktree(worktree, topic, force)

    # The worktree is gone, so its overlay config goes too (only finish removes it;
    # a manual `git worktree remove` would leave a stale entry that the next `start`
    # of the same topic overwrites).
    wt_file = _worktrees_file(home)
    worktrees = _read_worktrees_file(wt_file)
    if worktrees.pop(_worktree_overlay_key(worktree), None) is not None:
        _write_worktrees_file(wt_file, worktrees)

    prefix = f"Removed worktree for '{topic}'."

    if action == "merge":
        _finish_merge(topic, prefix)
        return

    if action == "push":
        _finish_push(topic, remote, prefix)
        return

    if action == "keep":
        print(f"{prefix} Branch '{topic}' kept ({_branch_status_note(topic)}).")
        return

    # delete-if-merged (default): if the branch is already integrated into `base`,
    # there's nothing left to preserve — delete it. (-d is the safe form: it
    # refuses an unmerged branch, but _branch_merged has confirmed reachability.)
    if _branch_merged(topic, base):
        subprocess.run(["git", "branch", "-d", topic], check=True)
        print(f"{prefix} Branch '{topic}' was merged; deleted it.")
        return
    print(
        f"{prefix} Branch '{topic}' still exists and needs to be merged or pushed "
        f"({_branch_status_note(topic)})."
    )


def _branch_status_note(branch: str) -> str:
    """A short note on where `branch` stands vs. its upstream, for finish output."""
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
        capture_output=True,
        text=True,
    )
    if upstream.returncode != 0:
        return "local only — push it to open a PR"
    unpushed = subprocess.run(
        ["git", "rev-list", "--count", f"{upstream.stdout.strip()}..{branch}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return "fully pushed" if unpushed in ("0", "") else f"{unpushed} commit(s) not pushed"


def _finish_merge(topic: str, prefix: str) -> None:
    """Merge `topic` into the current checkout, then delete it (the `merge` action).

    On any merge failure (conflicts, dirty tree, no common history) the merge is
    aborted and the branch is kept — the worktree is already gone, but the commits
    live on in the branch, recoverable by merging or re-checking-out manually.
    """
    merge = subprocess.run(["git", "merge", topic], capture_output=True, text=True)
    if merge.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], capture_output=True)
        detail = merge.stderr.strip() or merge.stdout.strip()
        print(
            f"{prefix} Merging '{topic}' failed (the branch is kept); resolve it "
            f"manually.\n{detail}"
        )
        return
    subprocess.run(["git", "branch", "-d", topic], check=True)
    target = _current_branch() or "the current branch"
    print(f"{prefix} Merged '{topic}' into {target} and deleted the branch.")


def _finish_push(topic: str, remote: str, prefix: str) -> None:
    """Push `topic` to `remote` and keep it locally (the `push` action).

    Uses `-u` so the local branch tracks `<remote>/<topic>`: the `push` action
    exists for the open-a-PR flow, where you'll want a later bare `git push` /
    `git pull` on that branch to just work.
    """
    push = subprocess.run(["git", "push", "-u", remote, topic], capture_output=True, text=True)
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip()
        print(
            f"{prefix} Pushing '{topic}' to '{remote}' failed (the branch is kept "
            f"locally).\n{detail}"
        )
        return
    print(f"{prefix} Pushed '{topic}' to '{remote}'; the branch is kept locally.")


def _current_branch() -> str | None:
    """The current branch name of the repo at cwd, or None if detached/unknown."""
    r = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    name = r.stdout.strip()
    return name if r.returncode == 0 and name and name != "HEAD" else None


def _branch_merged(branch: str, base: str, cwd: pathlib.Path | None = None) -> bool:
    """Whether `branch` is already contained in `base` (the integration ref).

    Matches `git branch --merged <base>`: true when the branch tip is reachable
    from `base`. Run from the current dir (the main repo) so a `base` like HEAD
    resolves to the main checkout — not a worktree's own branch. A branch that
    hasn't diverged from `base` — just-created, or **fast-forward**-merged (tip ==
    base) — therefore reads as merged, exactly as git reports it. A *squash*-merge
    creates a new commit, so the tip isn't reachable and reads as unmerged (a safe
    false negative for a display hint). Pass `cwd` to resolve `branch`/`base` in
    another repo (used by `list --all`, where each worktree's branch lives in its
    own repo, not the current one).
    """

    def run(args):
        return subprocess.run(
            ["git", *(["-C", str(cwd)] if cwd else []), *args],
            capture_output=True,
            text=True,
        )

    if run(["rev-parse", "--verify", "--quiet", base]).returncode != 0:
        return False
    return run(["merge-base", "--is-ancestor", branch, base]).returncode == 0


def do_rebase(
    topic: str,
    home: pathlib.Path,
    base: str,
    *,
    config_dir: str | None,
    force: bool,
) -> None:
    """`rebase` verb: rebase a worktree's branch onto `base` (e.g. main's new work).

    Resolves `base` (default HEAD) to a commit in the *main* checkout — run from
    the current dir, so HEAD means the main repo's tip, not the worktree's own
    branch — then runs `git rebase` inside the worktree onto that commit. So
    commits landed on the base since the worktree branched are replayed under the
    worktree's work, exactly like `git rebase main` from the branch.

    Unlike `finish` (which removes the worktree and so can't tolerate a live
    container at all), rebase only rewrites commits in a worktree that stays put,
    so a running container isn't a hard blocker — only an *active* session is. So
    when a container is running, we consult the session-activity state file the
    hooks write (the same one `ps` reads): a `waiting` session (idle at a prompt)
    is rebased through; a `working` one — or an unknown state (`-`: a `yolo shell`,
    which has no hooks, or a session that hasn't taken a turn yet) — is refused
    unless `--force`. The only residual race (the user prompting the session in
    the instant between our check and the rebase) needs them driving the same
    session from two places at once, so in practice it's a non-issue.

    A dirty worktree is always refused (no `--force` bypass): `git rebase` needs a
    clean tree regardless. A rebase that hits conflicts is left in-progress in the
    worktree for the user to resolve (`git rebase --continue`) or abort.
    """
    _, _, slug = _repo_paths()
    worktree = home / ".claude-yolo" / "worktrees" / slug / topic
    if not worktree.is_dir():
        sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
    if running_container_for(slug, topic):
        state_dir = pathlib.Path(config_dir) if config_dir else home / ".claude"
        state_file = state_dir / _STATUS_DIR_NAME / f"{_cwd_slug(worktree)}.state"
        state = _read_session_state(state_file, time.time())
        activity = state.split()[0]  # "waiting" | "working" | "-"
        if activity == "waiting":
            print(f"Session for '{topic}' is idle ({state}); rebasing.")
        elif force:
            print(f"--force: rebasing '{topic}' despite a running container ({state}).")
        else:
            detail = (
                f"its session is active ({state})"
                if activity == "working"
                else f"can't confirm its session is idle (state: {state})"
            )
            sys.exit(
                f"a container is running for '{topic}' and {detail}; wait for it "
                "to finish or re-run with --force."
            )
    dirty = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        sys.exit(
            f"worktree '{topic}' has uncommitted changes; commit or stash them "
            "first (git rebase requires a clean tree)."
        )

    # Resolve `base` to a concrete commit in the main checkout (cwd), so a ref
    # like HEAD means the main repo's tip rather than the worktree's own branch.
    rev = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", base],
        capture_output=True,
        text=True,
    )
    target = rev.stdout.strip()
    if rev.returncode != 0 or not target:
        sys.exit(f"can't resolve base ref '{base}'.")

    # Stream git's own output (no capture) so the user sees the rebase progress.
    rebase = subprocess.run(["git", "-C", str(worktree), "rebase", target])
    if rebase.returncode != 0:
        sys.exit(
            f"rebasing '{topic}' onto '{base}' hit conflicts; resolve them in "
            f"{worktree} and run `git rebase --continue`, or `git rebase --abort` "
            "there to back out."
        )
    print(f"Rebased '{topic}' onto '{base}'.")


def _format_table(headers: tuple, rows: list) -> list[str]:
    """Rows as column-aligned table lines (no trailing whitespace)."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def fmt(cols):
        # pad every column except the last so there's no trailing whitespace
        return "  ".join(
            c if i == len(cols) - 1 else c.ljust(widths[i]) for i, c in enumerate(cols)
        )

    return [fmt(headers)] + [fmt(row) for row in rows]


def _print_table(headers: tuple, rows: list) -> None:
    """Print rows as a column-aligned table."""
    for line in _format_table(headers, rows):
        print(line)


def do_dir(topic: str | None, home: pathlib.Path, cwd: pathlib.Path) -> None:
    """`dir` verb: print a session's working directory (only the path, on stdout).

    With a TOPIC, the worktree's root dir — erroring if it doesn't exist, so
    `cd $(yolo dir TOPIC)` fails loudly instead of cd-ing somewhere wrong. With no
    TOPIC, the current directory (the main checkout). Nothing else is written to
    stdout, so it composes cleanly in command substitution.
    """
    if topic:
        worktree, _, _ = _worktree_dir(topic, home)
        if not worktree.is_dir():
            sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
        print(worktree)
    else:
        print(cwd)


def _worktree_main_repo(wt: pathlib.Path) -> pathlib.Path | None:
    """The main checkout backing a linked worktree (its shared `.git`'s parent).

    Used by `list --all` to judge `merged` in each worktree's own repo: its branch
    and a `base` like HEAD only resolve there, not in the current repo. None if `wt`
    isn't a git worktree.
    """
    out = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return pathlib.Path(out.stdout.strip()).parent


def do_list(home: pathlib.Path, base: str, all_repos: bool = False) -> None:
    """`list` verb: show worktrees, their status, and directory.

    By default just this repo's worktrees; with `all_repos` (--all), every worktree
    under ~/.claude-yolo/worktrees across all repos, with a leading REPO column.

    The TOPIC column normally equals the branch (yolo names them alike), so the
    branch is only shown — as `topic (branch: X)` — when the worktree has a
    *different* branch checked out (someone switched it inside the container).

    `merged` is judged against `base` (the same ref `start` branches off — default
    HEAD, or whatever config/--base set). Under --all it's judged in each worktree's
    own main repo, since the branch/base only resolve there.
    """
    root = home / ".claude-yolo" / "worktrees"
    if all_repos:
        slug_dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    else:
        _, _, slug = _repo_paths()
        sd = root / slug
        slug_dirs = [sd] if sd.is_dir() else []

    rows = []
    for slug_dir in slug_dirs:
        slug = slug_dir.name
        topics = sorted(p for p in slug_dir.iterdir() if p.is_dir())
        for wt in topics:
            topic = wt.name
            branch = subprocess.run(
                ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            dirty = bool(
                subprocess.run(
                    ["git", "-C", str(wt), "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            # Under --all, resolve the branch in its own repo (the current dir
            # isn't it); also names the REPO column.
            repo = _worktree_main_repo(wt) if all_repos else None
            running = running_container_for(slug, topic)
            flags = (["running"] if running else []) + (["dirty"] if dirty else [])
            # `merged` vs `unmerged` only matters when it's idle and clean — i.e.
            # when it's actually a candidate to `finish`.
            if not flags:
                flags.append("merged" if _branch_merged(branch, base, repo) else "unmerged")
            status = ", ".join(flags)
            try:
                directory = "~/" + str(wt.relative_to(home))
            except ValueError:
                directory = str(wt)
            # Fold the branch into TOPIC, surfaced only when it differs (the
            # off-the-happy-path case of a branch switched inside the container).
            label = topic if branch in (topic, "") else f"{topic} (branch: {branch})"
            repo_name = repo.name if repo else slug
            rows.append((repo_name, label, status, directory))

    if not rows:
        print("No worktrees." if all_repos else "No worktrees for this repo.")
        return

    if all_repos:
        _print_table(("REPO", "TOPIC", "STATUS", "DIRECTORY"), rows)
    else:
        _print_table(("TOPIC", "STATUS", "DIRECTORY"), [r[1:] for r in rows])


PS_WATCH_INTERVAL = 2  # seconds between `ps --watch` refreshes


# One docker-ps port mapping, e.g. `127.0.0.1:55001->8000/tcp`; group 1 is the
# host port, group 2 the container port.
_PORT_MAP_RE = re.compile(r"(?:[\d.]+:)?(\d+)->(\d+)/")


def _condense_ports(raw: str) -> str:
    """docker ps's PORTS blob as compact `host->container` pairs.

    `127.0.0.1:55001->8000/tcp, [::]:8000->8000/tcp` -> `55001->8000` — drops
    the address and protocol noise and dedupes the IPv6 twin docker lists for
    a 0.0.0.0 binding (possible via the raw `-- -p` passthrough).
    """
    pairs = []
    for part in raw.split(","):
        m = _PORT_MAP_RE.search(part)
        if m and (pair := f"{m.group(1)}->{m.group(2)}") not in pairs:
            pairs.append(pair)
    return ",".join(pairs)


def _humanize_secs(s: int) -> str:
    """A whole-number duration as the largest single unit: `45s`/`3m`/`2h`/`4d`."""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _read_session_state(path: pathlib.Path, now: float) -> str:
    """A session's activity state for the `ps` STATE column, from its status file.

    The file (written by the Stop/UserPromptSubmit hooks) holds "<state> <epoch>",
    rendered with the elapsed time since that transition: `waiting 5m` (since the
    main agent last finished) or `working 12s` (since the last user prompt).
    Anything missing or unparseable is `-`.
    """
    try:
        parts = path.read_text().split()
    except OSError:
        return "-"
    if len(parts) != 2:
        return "-"
    state, ts = parts
    try:
        age = max(0, int(now - int(ts)))
    except ValueError:
        return "-"
    if state in ("waiting", "working"):
        return f"{state} {_humanize_secs(age)}"
    return "-"


def _ps_rows(home: pathlib.Path) -> list[tuple[str, str, str, str, str]]:
    """(name, topic, ports, created, state) for every running yolo container.

    Read from the yolo.* labels every launch stamps; the yolo.cwd filter is what
    distinguishes yolo's containers from everything else `docker ps` knows. The
    port mappings come straight from docker ps's own PORTS column — free, unlike
    a per-container `docker port` call, which matters at the 2s --watch cadence.
    STATE comes from the session's status file under its (labelled) config dir,
    so it too needs no extra docker calls.
    """
    fmt = "\t".join(
        (
            "{{.Names}}",
            '{{.Label "yolo.worktree"}}',
            '{{.Label "yolo.cwd"}}',
            "{{.Ports}}",
            "{{.RunningFor}}",
            '{{.Label "yolo.config-dir"}}',
        )
    )
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "label=yolo.cwd", "--format", fmt],
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("docker not found; is it installed and on PATH?")
    now = time.time()
    rows = []
    for line in out.splitlines():
        name, topic, rawcwd, ports, up, cfgdir = (line.split("\t") + [""] * 6)[:6]
        base = cfgdir or str(home / ".claude")
        state_file = pathlib.Path(base) / _STATUS_DIR_NAME / f"{_cwd_slug(rawcwd)}.state"
        state = _read_session_state(state_file, now)
        rows.append((name, topic or "-", _condense_ports(ports) or "-", up, state))
    return rows


PS_HEADERS = ("NAME", "TOPIC", "PORTS", "CREATED", "STATE")


def do_ps(home: pathlib.Path, *, watch: bool) -> None:
    """`ps` verb: every running yolo container, across all repos.

    The cross-repo counterpart to `list` (which shows one repo's worktrees,
    running or not, and needs a git repo to run from). `--watch` redraws every
    PS_WATCH_INTERVAL seconds — and when it's interactive *and* inside tmux
    (the dashboard window tmux mode seeds is both), the table is a picker:
    j/k/arrows move the highlight, Enter switches the tmux client to the
    selected session's window. Outside a TTY or tmux, `--watch` falls back to
    the plain passive redraw loop, so the verb stays usable anywhere.
    """
    if not watch:
        rows = _ps_rows(home)
        if rows:
            _print_table(PS_HEADERS, rows)
        else:
            print("No yolo containers running.")
        return
    if sys.stdin.isatty() and os.environ.get("TMUX"):
        _ps_picker(home)
    else:
        _ps_watch_passive(home)


def _ps_watch_passive(home: pathlib.Path) -> None:
    """`ps --watch` outside tmux / without a TTY: a plain auto-refreshing table."""
    try:
        while True:
            rows = _ps_rows(home)
            print("\x1b[H\x1b[2J", end="")  # clear screen, cursor home
            if rows:
                _print_table(PS_HEADERS, rows)
            else:
                print("No yolo containers running.")
            now = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"\nupdated {now} — Ctrl-C exits; in tmux, prefix+<n> switches windows")
            time.sleep(PS_WATCH_INTERVAL)
    except KeyboardInterrupt:
        print()


def _tmux_session_name() -> str | None:
    """The tmux session this process's pane belongs to (None if that fails)."""
    res = _tmux("display-message", "-p", "#{session_name}")
    name = res.stdout.strip() if res.returncode == 0 else ""
    return name or None


def _all_tmux_windows() -> dict[str, tuple[str, str]]:
    """{window_name: (window_id, session_name)} across every tmux session.

    The picker looks across sessions, not just its own, so it can switch to a
    yolo window wherever it lives — e.g. when `ps --watch` runs in a personal
    tmux session separate from the shared yolo one. On a duplicate name the
    first window wins, matching _find_tmux_window.
    """
    res = _tmux("list-windows", "-a", "-F", "#{window_id}\t#{session_name}\t#{window_name}")
    if res.returncode != 0:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in res.stdout.splitlines():
        wid, session, name = line.split("\t", 2)
        out.setdefault(name, (wid, session))
    return out


def _read_key(fd: int) -> str:
    """One keypress from a cbreak-mode fd; arrow keys decoded to 'up'/'down'.

    Reads raw bytes from the fd, NOT sys.stdin: Python's buffered reader can
    slurp the tail of an escape sequence into its own buffer, where select()
    can't see it — making a real arrow key indistinguishable from a bare ESC.
    The bare-ESC case is the opposite: nothing follows, which the short
    timeouts detect.
    """
    ch = os.read(fd, 1)
    if ch != b"\x1b":
        return ch.decode(errors="replace")
    if not select.select([fd], [], [], 0.05)[0] or os.read(fd, 1) != b"[":
        return "\x1b"
    if not select.select([fd], [], [], 0.05)[0]:
        return "\x1b"
    return {b"A": "up", b"B": "down"}.get(os.read(fd, 1), "\x1b")


def _wait_key(fd: int, timeout: float) -> str | None:
    """The next keypress within `timeout` seconds, or None on the deadline."""
    if not select.select([fd], [], [], timeout)[0]:
        return None
    return _read_key(fd)


def _ps_picker(home: pathlib.Path) -> None:
    """Interactive `ps --watch`: cbreak terminal setup around the picker loop.

    Only the terminal plumbing lives here — cbreak mode (key-at-a-time, no
    echo; ISIG stays on so Ctrl-C still works), hidden cursor, and the
    restore-on-any-exit in the finally (without which the dashboard window's
    shell is left wrecked). The loop itself takes an injectable key source.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l")  # hide the cursor; restored in the finally
    try:
        _ps_picker_loop(home, _tmux_session_name(), lambda timeout: _wait_key(fd, timeout))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _ps_picker_loop(home: pathlib.Path, session: str | None, wait_key) -> None:
    """The picker's event loop, separated from the terminal setup for testing.

    `wait_key(timeout)` returns the next key ('up'/'down'/'q'/'\\r'/...) or
    None when the refresh deadline passes. The selection is tracked by
    container *name*, not row index, so a refresh that adds or removes
    containers doesn't silently move the highlight to a different session.
    Enter switches the tmux client to the selected container's window
    (switch-client too when it lives in another session) and the picker keeps
    running: selection IS select-window, and the dashboard persists for next
    time.
    """
    rows = _ps_rows(home)
    windows = _all_tmux_windows()
    selected = None
    deadline = time.monotonic() + PS_WATCH_INTERVAL
    while True:
        names = [r[0] for r in rows]
        if selected not in names:
            selected = names[0] if names else None
        _draw_picker(rows, windows, selected)
        key = wait_key(max(0.0, deadline - time.monotonic()))
        if key is None:  # refresh deadline, no keypress
            rows = _ps_rows(home)
            windows = _all_tmux_windows()
            deadline = time.monotonic() + PS_WATCH_INTERVAL
        elif key in ("q", "\x1b"):
            return
        elif key in ("up", "k") and selected in names:
            selected = names[max(0, names.index(selected) - 1)]
        elif key in ("down", "j") and selected in names:
            selected = names[min(len(names) - 1, names.index(selected) + 1)]
        elif key in ("\r", "\n") and selected in windows:
            wid, wsession = windows[selected]
            _tmux("select-window", "-t", wid)
            if session and wsession != session:
                _tmux("switch-client", "-t", f"={wsession}")


def _draw_picker(rows: list, windows: dict, selected: str | None) -> None:
    """One picker frame: the ps table with the selected row highlighted.

    Containers with no tmux window anywhere (started outside tmux mode) are
    marked with ' *' — Enter has nowhere to switch for those.
    """
    print("\x1b[H\x1b[2J", end="")  # clear screen, cursor home
    orphans = False
    if rows:
        display = []
        for name, topic, ports, up, state in rows:
            mark = "" if name in windows else " *"
            orphans = orphans or bool(mark)
            display.append((name + mark, topic, ports, up, state))
        lines = _format_table(PS_HEADERS, display)
        print(lines[0])
        for row, line in zip(rows, lines[1:], strict=True):
            print(f"\x1b[7m{line}\x1b[0m" if row[0] == selected else line)
    else:
        print("No yolo containers running.")
    now = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\nupdated {now} — j/k/arrows move, Enter switches, q quits")
    if orphans:
        print("* no tmux window (started outside tmux mode)")


def _container_label(cid: str, key: str) -> str:
    """The value of one docker label on a container, or '' if unset/unreadable."""
    res = subprocess.run(
        ["docker", "inspect", "-f", f'{{{{index .Config.Labels "{key}"}}}}', cid],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _docker_port(cid: str, container_port: int) -> int:
    """The host port docker mapped `container_port` to, via `docker port`.

    Docker is the registry of assigned ports (yolo keeps no port state); this
    asks it directly. The output can list an IPv6 line too — take the first
    plain host:port one.
    """
    res = subprocess.run(
        ["docker", "port", cid, str(container_port)],
        capture_output=True,
        text=True,
    )
    for line in res.stdout.splitlines():
        host, sep, port = line.strip().rpartition(":")
        if sep and port.isdigit() and not host.startswith("["):
            return int(port)
    sys.exit(f"docker reports no host mapping for container port {container_port}.")


def _open_url(url: str) -> None:
    """Open a URL in the host browser (the macOS `open`). A seam for tests."""
    subprocess.run(["open", url], check=False)


def do_browse(
    topic: str | None,
    home: pathlib.Path,
    cwd: pathlib.Path,
    *,
    select: int | None = None,
    print_only: bool = False,
) -> None:
    """`browse` verb: open the host browser at a running session's forwarded port.

    The discoverability counterpart to the docker-assigned host ports: finds the
    session's container by the same label query `shell` uses (yolo.worktree for a
    TOPIC, yolo.cwd otherwise), reads the yolo.ports label for which container
    ports were forwarded at launch (first = default; --port N selects another),
    resolves the assigned host port with `docker port`, prints the URL — always,
    so it's copy-pasteable — and opens it. No poll for the server actually
    listening: browse may legitimately run before Claude has started the server,
    and a not-yet-listening tab is self-explanatory and refreshable.
    """
    if topic:
        worktree, _, slug = _worktree_dir(topic, home)
        cid = running_container_for(slug, topic)
        where = f"'{topic}'"
    else:
        cid = running_container_for(_repo_slug_or_none(), cwd=cwd)
        where = "this directory"
    if not cid:
        sys.exit(f"no yolo session running for {where}; start one with `yolo start`.")
    label = _container_label(cid, "yolo.ports")
    if not label:
        sys.exit(
            f"the session for {where} was launched without any forwarded ports, and "
            "docker can't add a port mapping to a running container. Configure one "
            "(e.g. `yolo config --add-port 8000`), exit the session, and `yolo resume`."
        )
    forwarded = [int(p) for p in label.split(",")]
    port = select if select is not None else forwarded[0]
    if port not in forwarded:
        sys.exit(f"container port {port} isn't forwarded for this session (forwarded: {label}).")
    url = f"http://127.0.0.1:{_docker_port(cid, port)}/"
    print(url)
    if not print_only:
        _open_url(url)


def do_dockerfile(custom: bool = False) -> None:
    """`dockerfile` verb: print a Dockerfile to stdout.

    Default: the built-in DEFAULT_DOCKERFILE — for inspection, or for the rare case
    where you want to start over and replace it wholesale.

    With `--custom`: a ready-to-edit CUSTOM_DOCKERFILE that *layers on* the default
    rather than forking it — it already has the `ARG YOLO_BASE` / `FROM ${YOLO_BASE}`
    lines (yolo injects the base tag at build time; see _build_image and the README),
    a marked block for your own steps, and the trailing `USER claude` yolo requires.
    This is the recommended way to customize:

        yolo dockerfile --custom > Dockerfile.yolo
        yolo config --dockerfile ./Dockerfile.yolo
    """
    sys.stdout.write(CUSTOM_DOCKERFILE if custom else DEFAULT_DOCKERFILE)


def do_tokens() -> None:
    """`tokens` verb: list the tokens yolo has minted (the tokens.json registry).

    The MINTED column is the practical reason this exists: the claude.ai token
    list shows almost no per-token metadata, so a recorded mint timestamp is the
    only handle for identifying yolo's token there. EXPIRES~ is minted +
    TOKEN_LIFETIME_DAYS — an estimate, hence the tilde. STATUS reconciles against
    the keychain: `stale` flags a registry entry whose keychain item is gone
    (deleted outside yolo), `re-minted` one whose keychain date is materially
    newer than the recorded mint (re-minted outside yolo — trust the keychain).
    """
    tokens = _read_tokens_file()
    if not tokens:
        print(
            f"No tokens recorded. (yolo records tokens it mints in {_tokens_file()};\n"
            "tokens minted by older yolo versions or by hand aren't listed.)"
        )
        return

    rows = []
    for service, entry in sorted(tokens.items()):
        config_dir = entry.get("config_dir") or "(default ~/.claude)"
        minted_raw = entry.get("minted", "")
        minted_day, expires = minted_raw[:10] or "?", "?"
        try:
            minted_dt = datetime.datetime.fromisoformat(minted_raw)
            expires = _token_expiry(minted_dt).date().isoformat()
        except ValueError:
            minted_dt = None
        if not _keychain_has(service):
            status = "stale (not in keychain)"
        else:
            mdat = _keychain_mdat(service)
            if (
                minted_dt is not None
                and mdat is not None
                and abs(mdat - minted_dt.astimezone(datetime.timezone.utc))
                > datetime.timedelta(days=1)
            ):
                status = f"re-minted outside yolo (keychain says {mdat.date().isoformat()})"
            else:
                status = "ok"
        rows.append((service, config_dir, minted_day, expires, status))

    _print_table(("SERVICE", "CONFIG DIR", "MINTED", "EXPIRES~", "STATUS"), rows)
    print()
    print(f"Tokens can only be revoked at {TOKEN_REVOKE_URL} — match by the MINTED date.")


def do_forget_token(config_dir: str | None) -> None:
    """`forget-token` verb: delete the active config dir's token, locally.

    "Forget", not "revoke": there is no revocation API (no CLI command, no OAuth
    endpoint — see the README's token section), so all yolo can do is delete its
    keychain copy and registry entry, then be honest that the token itself stays
    valid server-side and that finding it on the claude.ai page may not even be
    possible. Honours --config-dir / a config-file config-dir, so it targets the
    same service name a launch would read.
    """
    service = _oauth_service(config_dir)
    dir_label = config_dir or "~/.claude"
    entry = _remove_token_entry(service)
    deleted = _keychain_delete(service)
    if not deleted and entry is None:
        print(f"No token cached for {dir_label} (keychain service '{service}').")
        return
    minted = f" (minted {entry['minted']})" if entry and entry.get("minted") else ""
    if deleted:
        print(f"Forgotten: deleted the cached token for {dir_label}{minted} from the keychain.")
    else:
        print(f"Removed the stale registry entry for {dir_label}{minted}; not in the keychain.")
    print("yolo will no longer use it.")
    print(
        f"\nNOTE: the token itself is still valid server-side until roughly a year\n"
        f"after it was minted. Anthropic provides no API or CLI to revoke it — the\n"
        f"only revocation path is manual, at {TOKEN_REVOKE_URL} —\n"
        f"and in practice identifying one token there may be impossible (the list\n"
        f"shows no usable metadata and accumulates entries from normal Claude Code\n"
        f"usage; see claude-code issues #48373 and #59378). Revocation, when it\n"
        f"works, may also lag by days (#43801). This is outside yolo's control."
    )


def _worktree_dir(topic: str, home: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, str]:
    """(worktree_path, main_root, slug) for an existing topic; doesn't create it."""
    common_git, main_root, slug = _repo_paths()
    return home / ".claude-yolo" / "worktrees" / slug / topic, main_root, slug


def main():
    # Split on "--" before argparse sees argv so docker_args don't confuse it
    # docker_args come after $ARGS so last-one-wins gives user-supplied flags precedence
    if "--" in sys.argv:
        sep_idx = sys.argv.index("--")
        script_argv = sys.argv[1:sep_idx]
        docker_args = sys.argv[sep_idx + 1 :]
    else:
        script_argv = sys.argv[1:]
        docker_args = []

    home = pathlib.Path.home()
    cwd = pathlib.Path.cwd()

    # Parse once with built-in defaults to dispatch `config`, which is terminal and
    # runs *before* the config files are layered in: it must work even when those
    # are broken (it reads only projects.json itself, with a pointed error), and
    # its sentinel re-parse needs the pristine parser defaults.
    parsed = PARSER.parse_args(script_argv)
    verb, topic = parsed.verb, parsed.topic
    # --port values *explicitly on the CLI*: this pristine parse has no config
    # defaults layered in yet, so parsed.ports here is exactly the CLI values.
    # `browse` needs that distinction — its --port selects a port, and a
    # config-supplied `ports` list must not read as a selection.
    cli_ports = list(parsed.ports)

    # `finish` only makes sense against a worktree, so it still requires a TOPIC;
    # start/resume/shell take an optional TOPIC (no TOPIC ⇒ current directory).
    if verb == "finish" and not topic:
        sys.exit("`finish` needs a topic name, e.g. `yolo finish my-topic`.")
    if verb == "rebase" and not topic:
        sys.exit("`rebase` needs a topic name, e.g. `yolo rebase my-topic`.")
    if topic and verb not in (
        "start",
        "resume",
        "shell",
        "browse",
        "finish",
        "rebase",
        "dir",
        "config",
        "secret",
    ):
        sys.exit(f"unexpected argument: {topic!r}")
    # Only `secret` consumes trailing positionals (the secret NAME); for any other
    # verb they're a mistake.
    if parsed.extra_args and verb != "secret":
        sys.exit(f"unexpected argument: {parsed.extra_args[0]!r}")
    if parsed.new and verb != "resume":
        sys.exit("--new only applies to `resume`.")
    if parsed.new and not topic:
        sys.exit(
            "--new applies to `resume TOPIC` (a fresh session in a worktree); "
            "for the current directory, use `start`."
        )
    if parsed.resume is not None and verb != "resume":
        sys.exit("--resume/-r only applies to `resume`.")
    if parsed.new and parsed.resume is not None:
        sys.exit("--new can't be combined with --resume/-r.")
    if parsed.force and verb not in ("finish", "rebase"):
        sys.exit("--force only applies to `finish` and `rebase`.")
    if parsed.watch and verb != "ps":
        sys.exit("--watch only applies to `ps`.")
    if parsed.all_repos and verb not in ("list", "secret"):
        sys.exit("--all only applies to `list` and `secret list`.")
    if parsed.project and verb != "secret":
        sys.exit("--project only applies to `secret set`/`secret rm`.")
    if parsed.clipboard and verb != "secret":
        sys.exit("--clipboard only applies to `secret set`.")
    if parsed.print_url and verb != "browse":
        sys.exit("--print/-n only applies to `browse`.")
    if parsed.custom and verb != "dockerfile":
        sys.exit("--custom only applies to `dockerfile`.")
    for flag, val in (
        ("--init", parsed.init),
        ("--global", parsed.cfg_global),
        ("--unset", parsed.unsets),
        ("--add-mount", parsed.add_mounts),
        ("--remove-mount", parsed.remove_mounts),
        ("--add-prompt", parsed.add_prompts),
        ("--remove-prompt", parsed.remove_prompts),
        ("--add-port", parsed.add_ports),
        ("--remove-port", parsed.remove_ports),
        ("--add-secret", parsed.add_secrets),
        ("--remove-secret", parsed.remove_secrets),
    ):
        if val and verb != "config":
            sys.exit(f"{flag} only applies to `config`.")

    if verb == "config":
        do_config(script_argv, home, cwd, parsed, topic)
        return

    # `dockerfile` just prints a Dockerfile — no config, no container.
    if verb == "dockerfile":
        do_dockerfile(parsed.custom)
        return

    # `dir` just prints a path — no config, no container. Dispatched before the
    # config load so its stdout is *only* the path (the provenance note goes to
    # stderr, but keeping it out entirely is cleaner for `cd $(yolo dir TOPIC)`).
    if verb == "dir":
        do_dir(topic, home, cwd)
        return

    # `secret` manages keychain-backed secrets and launches no container; it needs
    # no yolo config (the project key comes from git), so dispatch it before the
    # config load to keep its output clean.
    if verb == "secret":
        do_secret(parsed, home, cwd)
        return

    # Every other verb gets the config defaults layered under the CLI flags
    # (so e.g. `list` honours a config-set `base`); re-parse so explicit flags win.
    # Uses the real cwd, before any worktree retargeting below. `resume`/`shell` in
    # worktree mode also layer that worktree's overlay on top of the project entry;
    # `start` is excluded — it *creates* the overlay from the CLI flags (below), so
    # it must not also consume a stale same-path entry left by a manual removal.
    overlay_dir = None
    if topic and verb in ("resume", "shell"):
        overlay_dir, _, _ = _worktree_dir(topic, home)
    config_defaults, matched_project_key = load_yolo_config(cwd, home, worktree_dir=overlay_dir)
    PARSER.set_defaults(**config_defaults)
    parsed = PARSER.parse_args(script_argv)

    # Terminal verbs (no credential config needed) — handle and return.
    if verb == "list":
        do_list(home, parsed.base, parsed.all_repos)
        return
    if verb == "ps":
        do_ps(home, watch=parsed.watch)
        return
    if verb == "browse":
        # Selection comes from cli_ports (the pre-config parse), NOT parsed.ports:
        # after the re-parse the config layers' `ports` list is mixed in, and a
        # configured port must not masquerade as an explicit selection.
        select = None
        if cli_ports:
            if len(cli_ports) > 1 or not cli_ports[0].isdigit():
                sys.exit(
                    "browse: pass at most one --port, as the bare *container* port "
                    "to open (e.g. `yolo browse --port 3000`)."
                )
            select = int(cli_ports[0])
        do_browse(topic, home, cwd, select=select, print_only=parsed.print_url)
        return
    if verb == "tokens":
        do_tokens()
        return
    # forget-token honours a config-file/--config-dir (the re-parse above already
    # layered those in) but is dispatched *before* the config-dir-must-exist check
    # below: forgetting a token for an already-deleted config dir must work, and
    # _oauth_service only hashes the resolved path — it never touches the dir.
    if verb == "forget-token":
        do_forget_token(parsed.config_dir)
        return
    if verb == "finish":
        do_finish(
            topic,
            home,
            parsed.base,
            force=parsed.force,
            action=parsed.finish_action,
            remote=parsed.finish_remote,
        )
        return
    if verb == "rebase":
        do_rebase(topic, home, parsed.base, config_dir=parsed.config_dir, force=parsed.force)
        return
    if verb == "shell":
        if topic:
            worktree, _, slug = _worktree_dir(topic, home)
            if not worktree.is_dir():
                sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
            cid = running_container_for(slug, topic)
        else:
            # Plain current-directory shell: match the container running in this dir.
            cid = running_container_for(_repo_slug_or_none(), cwd=cwd)
        if cid:
            where = f"for '{topic}'" if topic else "in this directory"
            print(f"Opening a shell in the running container {where} ({cid[:12]}).")
            exec_cmd = ["docker", "exec", "-it", cid, "/bin/bash"]
            if parsed.tmux:
                # docker exec never conflicts, so no reuse_existing: a second
                # `yolo shell` deliberately opens a second shell window.
                _launch_in_tmux(exec_cmd, f"{topic or cwd.name}-shell", session=parsed.tmux_session)
                return
            os.execvp("docker", exec_cmd)
            return  # execvp doesn't return on success; guard the stubbed/failed case
        # No container running: fall through to launch a fresh bash container below.

    # AWS knobs are inert unless --auth bedrock (the bedrock block is the only
    # consumer), so just warn rather than failing — the auth mode may be set to
    # bedrock in a .yolo.json and overridden back to keychain/oauth-token on the CLI.
    if parsed.auth != "bedrock" and (
        parsed.aws_profile or parsed.aws_region or parsed.bedrock_model
    ):
        print(
            "warning: aws-profile/aws-region/bedrock-model ignored without --auth bedrock.",
            file=sys.stderr,
        )
    if parsed.config_dir and not pathlib.Path(parsed.config_dir).is_dir():
        sys.exit(f"config-dir: not a directory: {parsed.config_dir}")

    # setup-token is terminal: mint/cache the OAuth token and exit. Dispatched here
    # (after config load) so it honours a `.yolo.json`/--config-dir — the token is
    # cached under that config dir's service name, matching what a launch will read.
    # It needs no consent prompt: running the verb is the consent (unlike the
    # implicit mint on launch).
    if verb == "setup-token":
        generate_oauth_token(parsed.config_dir)
        return

    # Guardrails — every path past this point launches a container. (The terminal
    # verbs above, and `shell` exec'd into a running container, are exempt: they
    # add no mounts.)
    #
    # Launching at or above $HOME mounts the whole home directory read-write into
    # a skip-permissions container: ~/.ssh, shell rc files, and yolo's own trusted
    # config (~/.yolo.json, ~/.claude-yolo) included. That dissolves the security
    # model and is almost always a cd mistake, so it's a hard error; the override
    # is deliberately CLI-only (a config key that permanently allowed it would
    # quietly defeat the guard).
    if not parsed.dangerously_allow_home and home.resolve().is_relative_to(cwd.resolve()):
        sys.exit(
            f"refusing to launch at or above your home directory ({cwd}): the "
            f"container would mount {home} read-write, including ~/.ssh and yolo's "
            "own config. Pass --dangerously-allow-home to do it anyway."
        )
    # Opt-in (usually via ~/.yolo.json): insist on a projects.json entry, so a
    # renamed/moved project fails loudly here instead of silently falling back to
    # the global defaults (wrong account/profile being the real hazard).
    if parsed.require_project_entry and matched_project_key is None:
        sys.exit(
            f"require-project-entry is set and no projects.json entry matches {cwd}; "
            "run `yolo config` here to create one, or pass --no-require-project-entry."
        )

    # Extra mounts and port forwards, merged across config layers and the CLI.
    # Resolved only on the launch paths so a stale mount path or malformed port
    # spec can't break `list`/`finish`/`config`.
    mounts = _resolve_mounts(parsed.mounts)
    # Only directories are forwarded to claude as --add-dir (it's dir-only); a
    # mounted file is still bind-mounted, just not announced as a working dir.
    mount_dirs = [path for path, _ in mounts if path.is_dir()]
    ports = _resolve_ports(parsed.ports)
    container_ports = [c for _, c in ports]

    # Resolve where we run and the trailing command per verb.
    common_git = None
    slug = None
    worktree_name = None
    entrypoint = None

    # A bare `yolo` (no verb) is equivalent to `yolo start` in the current directory.
    if verb is None:
        verb = "start"

    # Locate the run: an explicit TOPIC means a git worktree (start creates it,
    # resume/shell require it); no TOPIC means the current directory.
    if topic:
        worktree, main_root, slug = _worktree_dir(topic, home)
        if verb == "start":
            if worktree.exists() or _branch_exists(topic):
                sys.exit(f"'{topic}' already exists; resume it with `yolo resume {topic}`.")
            cwd, common_git, main_root = setup_worktree(topic, home, base=parsed.base)
            # Snapshot the explicit CLI config flags into the worktree overlay so a
            # later `yolo resume {topic}` relaunches with the same config (and `yolo
            # config {topic}` can edit it). Always written, even {} — symmetric with
            # the worktree lifecycle (created here, removed by `finish`).
            wt_overlay = _explicit_config_flags(script_argv)
            _parse_yolo_dict(wt_overlay, f"worktrees.json [{topic}]")  # never persist unloadable
            wt_file = _worktrees_file(home)
            worktrees = _read_worktrees_file(wt_file)
            worktrees[_worktree_overlay_key(cwd)] = wt_overlay
            _write_worktrees_file(wt_file, worktrees)
        else:
            if not worktree.is_dir():
                sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
            cwd, common_git = worktree, _repo_paths()[0]
            # `resume` restarts the container, so config flags passed to it update
            # the overlay (add mounts/ports, change auth, …) and persist for next
            # time. `shell` is excluded: shelling into a *running* container can't
            # change its mounts, so persisting there would mislead.
            if verb == "resume":
                _merge_worktree_overlay(home, cwd, _explicit_config_flags(script_argv))
        worktree_name = topic
        container_base = f"{main_root.name}-{topic}"
        session_name = topic
    else:
        slug = _repo_slug_or_none()
        container_base = cwd.name
        session_name = None  # a plain cwd session is unnamed

    # A custom Dockerfile must exist and be a readable file. Checked here on the
    # launch paths only (like the mount/port resolution above), so a stale
    # `dockerfile` config path can't break `list`/`finish`/`config`. Resolved
    # against the now-final `cwd` (the worktree dir in worktree mode), so a
    # relative path points at the session's own copy — matching _build_image.
    if parsed.dockerfile and not _resolve_dockerfile(parsed.dockerfile, cwd).is_file():
        sys.exit(f"dockerfile: not a file: {parsed.dockerfile}")

    # A --yolorc rc file must exist and be a readable file, resolved against the
    # final cwd (the worktree dir in worktree mode) so a relative path points at
    # the session's own copy — matching launch_container's resolution. Launch-path
    # only, like the dockerfile check, so a stale config path can't break the
    # terminal verbs.
    if parsed.yolorc and not _resolve_yolorc(parsed.yolorc, cwd).is_file():
        sys.exit(f"yolorc: not a file: {parsed.yolorc}")

    # Session-activity hooks (Stop/UserPromptSubmit) write to this file, which
    # `ps` reads for the STATE column. Path is the container-side mount location
    # (always /home/claude/.claude); the slug keys it to this cwd, matching what
    # launch_container resets and what `ps` recomputes. The user's own hooks from
    # the mounted settings are folded back in (--settings replaces the hooks key).
    session_status_path = f"/home/claude/.claude/{_STATUS_DIR_NAME}/{_cwd_slug(cwd)}.state"
    session_hooks = _read_settings_hooks(parsed.config_dir, home)

    # Build the trailing command for the verb.
    if verb == "shell":
        command = []
        entrypoint = "/bin/bash"
    elif verb == "resume" and parsed.resume is not None:
        command = build_claude_args(
            parsed.prompts,
            ssh_agent=parsed.ssh_agent,
            resume=parsed.resume,
            add_dirs=mount_dirs,
            forwarded_ports=container_ports,
            status_state_path=session_status_path,
            extra_hooks=session_hooks,
        )
    elif verb == "resume" and not parsed.new:
        command = build_claude_args(
            parsed.prompts,
            ssh_agent=parsed.ssh_agent,
            continue_session=True,
            add_dirs=mount_dirs,
            forwarded_ports=container_ports,
            status_state_path=session_status_path,
            extra_hooks=session_hooks,
        )
    else:
        # start, or `resume TOPIC --new` (a fresh named session in the worktree).
        command = build_claude_args(
            parsed.prompts,
            ssh_agent=parsed.ssh_agent,
            name=session_name,
            add_dirs=mount_dirs,
            forwarded_ports=container_ports,
            status_state_path=session_status_path,
            extra_hooks=session_hooks,
        )

    launch_container(
        parsed,
        home=home,
        cwd=cwd,
        common_git=common_git,
        worktree_name=worktree_name,
        slug=slug,
        container_base=container_base,
        command=command,
        entrypoint=entrypoint,
        docker_args=docker_args,
        mounts=mounts,
        ports=ports,
    )


if __name__ == "__main__":
    main()
