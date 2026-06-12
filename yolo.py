#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import argparse
import datetime
import fcntl
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

DOCKER_IMAGE = "claude-yolo:latest"

# The three mutually-exclusive auth mechanisms, selected by --auth (default
# oauth-token). oauth-token = forward a long-lived CLAUDE_CODE_OAUTH_TOKEN env
# var; keychain = extract the rotating Claude.ai keychain creds into a mounted
# file (hazard: the single-use refresh token means any session running when the
# access token expires either wins the refresh or is broken by it — see README);
# bedrock = AWS Bedrock creds.
AUTH_CHOICES = ["keychain", "oauth-token", "bedrock"]

# Dockerfile template — uid is substituted at runtime to match the host user so that
# files in the bind-mounted working directory are owned by (and writable as) the in-container
# user. The user is also put in group 0 so it can connect to the Docker engine's
# root-owned ssh-auth.sock (see the useradd line and the ssh-auth.sock mount below).
DOCKERFILE_TEMPLATE = """\
FROM ubuntu:24.04

# Baked-in amenities used across most projects, so Claude doesn't re-install them in
# each ephemeral container. fd-find installs its binary as `fdfind`; symlink it to `fd`.
RUN apt-get update && apt-get install -y nodejs npm sudo jq git curl ripgrep fd-find build-essential vim && ln -s /usr/bin/fdfind /usr/local/bin/fd
# uv + uvx for fast Python tooling, copied from the official image (no curl, pinnable)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
# UID {uid} matches the host user so bind-mounted working-dir files are owned/writable.
# Group 0 (root) membership grants access to the Docker engine's ssh-auth.sock, which is
# mounted srw-rw---- root:root — without it a non-root user gets EACCES on connect(). This
# adds no real privilege: the claude user already has NOPASSWD sudo, and the container is
# the sandbox.
RUN useradd -m -s /bin/bash --uid {uid} -G root claude
RUN echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude
RUN mkdir -p /home/claude/.ssh && chown claude:claude /home/claude/.ssh && chmod 700 /home/claude/.ssh
# Route GitHub HTTPS git operations over SSH so they reuse the forwarded ssh-agent — no
# tokens ever enter the container (HTTPS auth is a bearer token, which would have to; SSH is
# challenge-response, so the key stays on the host). Remotes can stay https://github.com/...;
# git rewrites them to git@github.com: before connecting. --system so it applies to the claude
# user without mounting any gitconfig (the host's ~/.gitconfig is deliberately never mounted).
RUN git config --system url."git@github.com:".insteadOf "https://github.com/"

USER claude
# Use the native installer (~/.local/bin/claude), NOT `npm install -g`. The npm global
# install lands at /usr/local/bin/claude, which Claude Code's `/doctor` flags as a broken
# install and which self-update can't manage. The native binary is standalone (no node needed).
RUN curl -fsSL https://claude.ai/install.sh | bash
# Adopt a yolo-provided prompt when the container is launched with -e YOLO_PS1
# (see _ps1_env_args): flags any bash as a yolo shell and shows where it is.
# Appended last so it wins over the distro default PS1.
RUN echo 'if [ -n "$YOLO_PS1" ]; then PS1="$YOLO_PS1"; fi' >> /home/claude/.bashrc
ENV PATH=/home/claude/.local/bin:$PATH
ENTRYPOINT ["claude", "--dangerously-skip-permissions"]
"""


def build_docker_image(*, no_cache: bool = False) -> None:
    """Write the Dockerfile to a temporary directory and build the Docker image."""
    with tempfile.TemporaryDirectory(prefix="claude-yolo-build-") as build_dir:
        dockerfile = pathlib.Path(build_dir) / "Dockerfile"
        dockerfile.write_text(DOCKERFILE_TEMPLATE.format(uid=os.getuid()))
        cmd = ["docker", "build", "-t", DOCKER_IMAGE]
        if no_cache:
            cmd.append("--no-cache")
        subprocess.run(cmd + [build_dir], check=True)


def extract_credentials(config_dir: str | None) -> str:
    """Extract Claude API credentials from the macOS keychain via the `security` CLI.

    Claude Code stores OAuth credentials in the keychain under a service name of
    "Claude Code-credentials" (default) or "Claude Code-credentials-{hash8}" when
    multiple config directories are in use. The hash is the first 8 hex chars of the
    SHA-256 of the resolved config directory path — this makes the keychain entry name
    stable and unique per directory without embedding the full path.

    Returns the path of a temporary file containing the credentials JSON,
    chmod 600, ready to bind-mount into the container.
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

    tmp = tempfile.NamedTemporaryFile(prefix="claude-credentials-", suffix=".json", delete=False)
    tmp.write(result.stdout)
    tmp.close()

    credpath = pathlib.Path(tmp.name)
    if credpath.stat().st_size == 0:
        print(f"Failed to extract credentials from keychain service '{service}'", file=sys.stderr)
        sys.exit(1)
    credpath.chmod(0o600)

    return tmp.name


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
    "auth": ("auth", "auth"),
    "aws_profile": ("aws_profile", "str"),
    "aws_region": ("aws_region", "str"),
    "bedrock_model": ("bedrock_model", "str"),
    "claude_json": ("claude_json", "bool"),
    "ssh_agent": ("ssh_agent", "bool"),
    "base": ("base", "str"),
    "prompts": ("prompts", "list"),
    "mounts": ("mounts", "list"),
    "require_project_entry": ("require_project_entry", "bool"),
    "tmux": ("tmux", "bool"),
    "tmux_session": ("tmux_session", "str"),
}

# dests whose values concatenate across the config layers and the CLI (everything
# else is overridden by the higher-precedence layer)
_CONCAT_DESTS = ("prompts", "mounts")

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


def load_yolo_config(start: pathlib.Path, home: pathlib.Path) -> tuple[dict, str | None]:
    """Merge ~/.yolo.json with the matching ~/.claude-yolo/projects.json entry.

    Returns (merged_defaults, matched_project_key). Precedence low->high:
    ~/.yolo.json < projects.json entry < CLI args (the caller applies the dict via
    PARSER.set_defaults, so explicit flags still win). prompts and
    mounts concatenate across the layers; every other key is overridden by the
    higher layer. Both files are host-side only — outside every container mount —
    so nothing Claude writes inside a container can change what the next launch
    mounts or which credentials it uses. Also prints the config provenance line
    and the stale-state warnings (dangling project keys, leftover in-directory
    .yolo.json files) to stderr.
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
    """One --mount / `mounts` value, `PATH[:ro|:rw]` -> (resolved dir, mode).

    Read-only is the default (the use case is reference material; :rw is the
    explicit opt-in). The directory must exist: docker silently creates a missing
    bind-mount source as a root-owned dir on the host, which we never want.
    """
    path_part, mode = spec, "ro"
    if spec.endswith((":ro", ":rw")):
        path_part, mode = spec[:-3], spec[-2:]
    path = pathlib.Path(os.path.expanduser(path_part))
    if not path.is_dir():
        sys.exit(f"mount: not a directory: {path_part}")
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


def _apply_config_edits(current: dict, explicit: dict, parsed, where: str) -> dict:
    """One updated config object: whole-key sets, then --unset, then list edits.

    Shared by the project-entry and --global paths of `do_config`; `where` names
    the target in error messages. Conflicting instructions for the same key in
    one invocation (set + unset, --mount alongside --add/--remove-mount, -p
    alongside --add/--remove-prompt) are errors, not silently ordered.
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
    unsets = [u.replace("_", "-") for u in parsed.unsets]
    for u in unsets:
        if u in explicit:
            sys.exit(f"can't both set and --unset {u!r}.")
    if "mounts" in unsets and (parsed.add_mounts or parsed.remove_mounts):
        sys.exit("can't combine --unset mounts with --add-mount/--remove-mount.")
    if "prompts" in unsets and (parsed.add_prompts or parsed.remove_prompts):
        sys.exit("can't combine --unset prompts with --add-prompt/--remove-prompt.")

    for spec in [*explicit.get("mounts", []), *parsed.add_mounts]:
        _parse_mount_spec(spec)  # validate now, so a typo'd path can't be pinned

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

    return entry


def do_config(script_argv: list[str], home: pathlib.Path, cwd: pathlib.Path, parsed) -> None:
    """`config` verb: show or update yolo's host-side config, then exit.

    Operates on this project's ~/.claude-yolo/projects.json entry, or — with
    --global — on ~/.yolo.json itself (a la `git config --global`). With config
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
        or parsed.unsets
    )

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
        updated = _apply_config_edits(current, explicit, parsed, where)
        _parse_yolo_dict(updated, where)  # never write an unloadable config
        global_file.write_text(json.dumps(updated, indent=2) + "\n")
        print(f"Updated {global_file}:")
        print(json.dumps(updated, indent=2))
        return

    projects = _read_projects_file(projects_file)
    key = _project_key(cwd)

    if not explicit and not editing:
        matched_key, entry = _match_project_entry(projects, cwd)
        print(f"projects file: {projects_file}")
        if matched_key is None:
            print(f"no entry for {key}")
        else:
            print(json.dumps({matched_key: entry}, indent=2))
        _warn_dangling_keys(projects, no_entry=matched_key is None)
        return

    where = f"{projects_file} [{key}]"
    entry = _apply_config_edits(dict(projects.get(key, {})), explicit, parsed, where)
    _parse_yolo_dict(entry, where)  # never write an unloadable entry
    projects[key] = entry
    projects_file.parent.mkdir(parents=True, exist_ok=True)
    projects_file.write_text(json.dumps(projects, indent=2) + "\n")
    print(f"Updated {projects_file}:")
    print(json.dumps({key: entry}, indent=2))


def _version() -> str:
    """Best-effort package version for `--version`, tracing to pyproject.toml.

    Installed as a wheel (`uv tool install`) → read the recorded package metadata.
    Run standalone as the PEP 723 script (possibly via a PATH symlink, hence the
    `resolve()`) → scrape `version` from the adjacent pyproject.toml. Neither (a
    stray copy with no metadata and no pyproject) → "unknown".
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("claude-yolo")
    except PackageNotFoundError:
        pass
    try:
        pyproject = (pathlib.Path(__file__).resolve().parent / "pyproject.toml").read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "unknown"


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
        "finish",
        "list",
        "ps",
        "setup-token",
        "tokens",
        "forget-token",
    ],
    help="Optional subcommand. start/resume/shell take an *optional* TOPIC: with a "
    "TOPIC they act on a git worktree of that name (start creates it, resume/shell "
    "require it); with no TOPIC they act on the current directory (start a fresh "
    "session, resume the most recent one, or open a shell). 'finish' removes a "
    "worktree and requires a TOPIC. 'list' shows this repo's worktrees; 'ps' shows "
    "all running yolo containers across repos (see --watch); 'config' "
    "shows this project's ~/.claude-yolo/projects.json entry (or ~/.yolo.json "
    "with --global), or — given config flags — persists exactly those flags into "
    "it (see also --unset, --add-mount/--remove-mount, --add-prompt/"
    "--remove-prompt); 'setup-token' mints/caches a "
    "long-lived OAuth token (for --auth oauth-token); 'tokens' lists the tokens "
    "yolo has minted; 'forget-token' deletes the active config dir's token from "
    "the keychain (local only — see `tokens` output for revocation). A bare "
    "`yolo` is equivalent to `yolo start`.",
)
PARSER.add_argument(
    "topic",
    nargs="?",
    help="Worktree/branch name. Required for finish; optional for start/resume/shell "
    "(omit it to act on the current directory).",
)
PARSER.add_argument(
    "--base",
    metavar="REF",
    default="HEAD",
    help="For `start`: git ref the new branch is created from (default: HEAD). "
    'Also settable as `base` in .yolo.json (e.g. "origin/main").',
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
    help="For `finish`: remove the worktree even with uncommitted changes.",
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
    "--ssh-agent",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Forward the host ssh-agent socket into the container (default: off). "
    "Off keeps your SSH keys out of the skip-permissions container; turn it on "
    "with --ssh-agent (or `ssh-agent: true` in config) when you need in-container "
    "GitHub git auth, which won't work without it.",
)
PARSER.add_argument(
    "--rebuild-image",
    action="store_true",
    default=False,
    dest="rebuild_image",
    help="Force a Docker image rebuild from scratch (passes --no-cache to docker build).",
)
PARSER.add_argument(
    "--mount",
    dest="mounts",
    action="append",
    default=[],
    metavar="PATH[:ro|:rw]",
    help="Extra host directory to bind-mount into the container at its identical "
    "host path, read-only unless :rw is appended. Repeatable; also settable as "
    "`mounts` in config, where the lists concatenate across the layers and the "
    "CLI. Each directory is also passed to claude as --add-dir so it shows up as "
    "a working directory.",
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


def build_claude_args(
    prompts: list,
    *,
    ssh_agent: bool = False,
    continue_session: bool = False,
    resume=None,
    name: str | None = None,
    add_dirs=(),
) -> list[str]:
    """The args passed to `claude` inside the container (everything after the image).

    Always includes the container-only sandbox override and the built-in
    "you're in a container" system prompt (plus any -p additions). Extra mounts
    are forwarded as --add-dir so they're first-class working directories.
    Optionally adds --continue / --resume [ID] and a session --name.
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
        *prompts,
    ]
    args = [
        # The container is the sandbox, so disable Claude's in-process OS sandbox.
        # Otherwise it warns at startup that bubblewrap/socat are missing (they're
        # deliberately not installed — they can't create namespaces in a container
        # anyway). This overrides sandbox.enabled from the mounted settings.json for
        # this container only; the host's settings are untouched.
        "--settings",
        '{"sandbox":{"enabled":false}}',
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
) -> None:
    """Assemble the `docker run` argv from the credential/config flags and exec it.

    Shared by every launch path (start / resume / shell, worktree or cwd). The
    container name starts from container_base and gains -{config}/-{profile}
    suffixes; yolo.repo / yolo.worktree labels are stamped so the verbs can find
    the container later. `command` is the args after the image; `entrypoint`
    overrides the image ENTRYPOINT (used to drop into bash for `shell`); `mounts`
    is the resolved (dir, mode) list from --mount / the `mounts` config key.
    """
    container = container_base
    args = [
        "-w",
        str(cwd),
        "-v",
        f"{cwd}:{cwd}",
        # Hostname set to working dir basename so Claude Code status line shows project name without git
        "--hostname",
        cwd.name,
        # A yolo-flagged bash prompt for `yolo shell` (fresh or exec'd into this container)
        *_ps1_env_args(cwd, worktree_name),
        # Forward the host git identity so commits made in the container are attributed correctly
        *git_identity_args(),
    ]

    # Extra reference mounts (--mount / `mounts` config): bind-mounted at their
    # identical host paths, like the cwd, so paths match host<->container.
    for path, mode in mounts:
        args += ["-v", f"{path}:{path}:{mode}"]

    if parsed.ssh_agent:
        # Forward the host ssh-agent via the Docker engine's magic socket. We canNOT bind-mount
        # the raw host $SSH_AUTH_SOCK: that socket's listener lives in the macOS kernel, while
        # the container runs in the engine's Linux VM (Docker Desktop or OrbStack), so the
        # mounted inode is dead (connect() -> ECONNREFUSED). /run/host-services/ssh-auth.sock
        # is a socket the VM itself listens on and proxies to the host agent — both Docker
        # Desktop and OrbStack expose it at that path. It's mounted srw-rw----
        # root:root, so the claude user must be in group 0 to connect (see the useradd line).
        # --no-ssh-agent skips all of this; in-container GitHub git auth won't work then,
        # since the baked HTTPS->SSH rewrite relies on the forwarded agent.
        args += [
            "-v",
            "/run/host-services/ssh-auth.sock:/run/ssh-agent",
            "-e",
            "SSH_AUTH_SOCK=/run/ssh-agent",
            # Mount host known_hosts so SSH host key verification succeeds
            "-v",
            f"{home}/.ssh/known_hosts:/home/claude/.ssh/known_hosts:ro",
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
    config_dir = parsed.config_dir
    if config_dir:
        configpath = pathlib.Path(config_dir).resolve()
        container = f"{container}-{configpath.name}"
        args += ["-v", f"{config_dir}:/home/claude/.claude"]
    else:
        args += ["-v", f"{home}/.claude:/home/claude/.claude"]

    # (b) ~/.claude.json (global config: MCP servers, project history/trust). Always at
    # $HOME/.claude.json on the host (it ignores CLAUDE_CONFIG_DIR). Opt out with
    # --no-claude-json to keep an alternate --config-dir profile cleanly isolated.
    if parsed.claude_json:
        args += ["-v", f"{home}/.claude.json:/home/claude/.claude.json"]

    # (c) Auth mechanism (--auth), one of three mutually-exclusive paths:
    #   - oauth-token (default): forward a long-lived CLAUDE_CODE_OAUTH_TOKEN env
    #     var. No keychain extraction, no login check, no .credentials.json mount —
    #     the token is stable (never rotated/written back), so concurrent containers
    #     and the host can all use it at once. The env var also out-ranks any file
    #     creds, so a stale mounted .credentials.json can't shadow it.
    #   - bedrock: AWS creds + env (mounts ~/.aws), no keychain/login.
    #   - keychain: extract the rotating keychain creds into a mounted file. All
    #     snapshots (and the host keychain) share one refresh boundary — the access
    #     token's expiry — and whoever refreshes first there breaks every other
    #     holder, host login included.
    if parsed.auth == "oauth-token":
        args += ["-e", f"CLAUDE_CODE_OAUTH_TOKEN={ensure_oauth_token(config_dir)}"]
    elif parsed.auth == "bedrock":
        container = f"{container}-{parsed.aws_profile or 'bedrock'}"
        args += ["-v", f"{home}/.aws:/home/claude/.aws:ro"]
        args += ["-e", "CLAUDE_CODE_USE_BEDROCK=1"]
        if parsed.aws_profile:
            args += ["-e", f"AWS_PROFILE={parsed.aws_profile}"]
        args += ["-e", f"AWS_REGION={parsed.aws_region or 'us-east-1'}"]
        if parsed.bedrock_model:
            args += ["-e", f"BEDROCK_MODEL_ID={parsed.bedrock_model}"]
    else:  # keychain
        ensure_logged_in(config_dir)
        credfile = extract_credentials(config_dir)
        args += ["-v", f"{credfile}:/home/claude/.claude/.credentials.json"]

    # Labels let the verbs (shell/finish/list) find this container later, regardless
    # of the name suffixes above. yolo.cwd is stamped on every launch so a plain
    # `shell` (no topic) can find the container running in this exact directory.
    if slug:
        args += ["--label", f"yolo.repo={slug}"]
    if worktree_name:
        args += ["--label", f"yolo.worktree={worktree_name}"]
    args += ["--label", f"yolo.cwd={cwd}"]

    build_docker_image(no_cache=parsed.rebuild_image)

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
        DOCKER_IMAGE,
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


def do_finish(topic: str, home: pathlib.Path, *, force: bool) -> None:
    """`finish` verb: remove a worktree, keep its branch.

    Guards against the real loss vectors — a running container holding the mount,
    and uncommitted changes (unless --force) — then removes the worktree and prints
    a reminder that the branch is kept (and whether it's been pushed).
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

    remove = ["git", "worktree", "remove"] + (["--force"] if force else []) + [str(worktree)]
    subprocess.run(remove, check=True)
    subprocess.run(["git", "worktree", "prune"])

    # Best-effort note about where the (kept) branch stands relative to its upstream.
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", f"{topic}@{{upstream}}"],
        capture_output=True,
        text=True,
    )
    if upstream.returncode == 0:
        unpushed = subprocess.run(
            ["git", "rev-list", "--count", f"{upstream.stdout.strip()}..{topic}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        note = "fully pushed" if unpushed in ("0", "") else f"{unpushed} commit(s) not pushed"
    else:
        note = "local only — push it to open a PR"
    print(f"Removed worktree for '{topic}'. Branch '{topic}' kept ({note}).")


def _branch_merged(branch: str, base: str) -> bool:
    """Whether `branch` is already contained in `base` (the integration ref).

    Matches `git branch --merged <base>`: true when the branch tip is reachable
    from `base`. Run from the current dir (the main repo) so a `base` like HEAD
    resolves to the main checkout — not a worktree's own branch. A branch that
    hasn't diverged from `base` — just-created, or **fast-forward**-merged (tip ==
    base) — therefore reads as merged, exactly as git reports it. A *squash*-merge
    creates a new commit, so the tip isn't reachable and reads as unmerged (a safe
    false negative for a display hint).
    """
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", base],
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return False
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base],
            capture_output=True,
        ).returncode
        == 0
    )


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


def do_list(home: pathlib.Path, base: str) -> None:
    """`list` verb: show this repo's worktrees, their branch, status, and directory.

    `merged` is judged against `base` (the same ref `start` branches off — default
    HEAD, or whatever `.yolo.json`/--base set).
    """
    _, _, slug = _repo_paths()
    root = home / ".claude-yolo" / "worktrees" / slug
    topics = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not topics:
        print("No worktrees for this repo.")
        return

    rows = []
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
        running = running_container_for(slug, topic)
        flags = (["running"] if running else []) + (["dirty"] if dirty else [])
        # `merged` vs `unmerged` only matters when it's idle and clean — i.e. when
        # it's actually a candidate to `finish`.
        if not flags:
            flags.append("merged" if _branch_merged(branch, base) else "unmerged")
        status = ", ".join(flags)
        try:
            directory = "~/" + str(wt.relative_to(home))
        except ValueError:
            directory = str(wt)
        rows.append((topic, branch, status, directory))

    _print_table(("TOPIC", "BRANCH", "STATUS", "DIRECTORY"), rows)


PS_WATCH_INTERVAL = 2  # seconds between `ps --watch` refreshes


def _ps_rows(home: pathlib.Path) -> list[tuple[str, str, str, str]]:
    """(name, topic, directory, up) for every running yolo container, any repo.

    Read from the yolo.* labels every launch stamps; the yolo.cwd filter is what
    distinguishes yolo's containers from everything else `docker ps` knows.
    """
    fmt = "\t".join(
        ("{{.Names}}", '{{.Label "yolo.worktree"}}', '{{.Label "yolo.cwd"}}', "{{.RunningFor}}")
    )
    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "label=yolo.cwd", "--format", fmt],
            capture_output=True,
            text=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("docker not found; is it installed and on PATH?")
    rows = []
    for line in out.splitlines():
        name, topic, cwd, up = (line.split("\t") + [""] * 4)[:4]
        if cwd.startswith(f"{home}/"):
            cwd = "~" + cwd[len(str(home)) :]
        rows.append((name, topic or "-", cwd, up))
    return rows


PS_HEADERS = ("NAME", "TOPIC", "DIRECTORY", "UP")


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
        for name, topic, cwd, up in rows:
            mark = "" if name in windows else " *"
            orphans = orphans or bool(mark)
            display.append((name + mark, topic, cwd, up))
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

    # `finish` only makes sense against a worktree, so it still requires a TOPIC;
    # start/resume/shell take an optional TOPIC (no TOPIC ⇒ current directory).
    if verb == "finish" and not topic:
        sys.exit("`finish` needs a topic name, e.g. `yolo finish my-topic`.")
    if topic and verb not in ("start", "resume", "shell", "finish"):
        sys.exit(f"unexpected argument: {topic!r}")
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
    if parsed.force and verb != "finish":
        sys.exit("--force only applies to `finish`.")
    if parsed.watch and verb != "ps":
        sys.exit("--watch only applies to `ps`.")
    for flag, val in (
        ("--init", parsed.init),
        ("--global", parsed.cfg_global),
        ("--unset", parsed.unsets),
        ("--add-mount", parsed.add_mounts),
        ("--remove-mount", parsed.remove_mounts),
        ("--add-prompt", parsed.add_prompts),
        ("--remove-prompt", parsed.remove_prompts),
    ):
        if val and verb != "config":
            sys.exit(f"{flag} only applies to `config`.")

    if verb == "config":
        do_config(script_argv, home, cwd, parsed)
        return

    # Every other verb gets the config defaults layered under the CLI flags
    # (so e.g. `list` honours a config-set `base`); re-parse so explicit flags win.
    # Uses the real cwd, before any worktree retargeting below.
    config_defaults, matched_project_key = load_yolo_config(cwd, home)
    PARSER.set_defaults(**config_defaults)
    parsed = PARSER.parse_args(script_argv)

    # Terminal verbs (no credential config needed) — handle and return.
    if verb == "list":
        do_list(home, parsed.base)
        return
    if verb == "ps":
        do_ps(home, watch=parsed.watch)
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
        do_finish(topic, home, force=parsed.force)
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

    # Extra mounts, merged across config layers and the CLI. Resolved only on the
    # launch paths so a stale mount path can't break `list`/`finish`/`config`.
    mounts = _resolve_mounts(parsed.mounts)
    mount_dirs = [path for path, _ in mounts]

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
        else:
            if not worktree.is_dir():
                sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
            cwd, common_git = worktree, _repo_paths()[0]
        worktree_name = topic
        container_base = f"{main_root.name}-{topic}"
        session_name = topic
    else:
        slug = _repo_slug_or_none()
        container_base = cwd.name
        session_name = None  # a plain cwd session is unnamed

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
        )
    elif verb == "resume" and not parsed.new:
        command = build_claude_args(
            parsed.prompts,
            ssh_agent=parsed.ssh_agent,
            continue_session=True,
            add_dirs=mount_dirs,
        )
    else:
        # start, or `resume TOPIC --new` (a fresh named session in the worktree).
        command = build_claude_args(
            parsed.prompts,
            ssh_agent=parsed.ssh_agent,
            name=session_name,
            add_dirs=mount_dirs,
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
    )


if __name__ == "__main__":
    main()
