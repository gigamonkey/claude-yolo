#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import argparse
import hashlib
import json
import os
import pathlib
import pty
import re
import shutil
import subprocess
import sys
import tempfile

DOCKER_IMAGE = "claude-yolo:latest"

# The three mutually-exclusive auth mechanisms, selected by --auth (default the
# first). keychain = extract the rotating Claude.ai keychain creds into a mounted
# file; oauth-token = forward a long-lived CLAUDE_CODE_OAUTH_TOKEN env var;
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
# setup-token prints an OAuth token; detect it in the (ANSI-laden) terminal output.
_TOKEN_RE = re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _looks_like_token(token: str) -> bool:
    """A permissive sanity check: non-empty, no whitespace, plausibly long."""
    return bool(token) and not any(c.isspace() for c in token) and len(token) >= 20


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
    """Upsert the yolo OAuth token for this config dir into the keychain (-U)."""
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

    def _read(fd):
        data = os.read(fd, 1024)
        captured.extend(data)
        return data

    status = pty.spawn(["claude", "setup-token"], _read)
    if status != 0:
        print("\n`claude setup-token` did not exit cleanly.", file=sys.stderr)

    clean = _ANSI_RE.sub(b"", bytes(captured))
    matches = _TOKEN_RE.findall(clean)
    token = matches[-1].decode() if matches else ""
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
    """
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    cached = _read_oauth_token(config_dir)
    if cached:
        return cached
    print("No cached yolo OAuth token found; generating one.")
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


def _repo_slug_or_none() -> str | None:
    """The repo slug for the cwd, or None when the cwd isn't a git repo.

    Used to label bare (non-worktree) launches that happen to be inside a repo,
    without erroring out when they aren't.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return re.sub(r"[^a-zA-Z0-9]", "-", str(pathlib.Path(out).parent))


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


# .yolo.json config keys -> (argparse dest, kind). These are standing environment /
# credential preferences only; per-invocation *actions* (--resume and the verbs)
# are intentionally CLI-only and rejected if they appear in a .yolo.json.
# "path" values get ~ expanded (a JSON file can't rely on shell expansion).
YOLO_KEYS = {
    "config_dir": ("config_dir", "path"),
    "auth": ("auth", "auth"),
    "aws_profile": ("aws_profile", "str"),
    "aws_region": ("aws_region", "str"),
    "bedrock_model": ("bedrock_model", "str"),
    "claude_json": ("claude_json", "bool"),
    "ssh_agent": ("ssh_agent", "bool"),
    "base": ("base", "str"),
    "append_system_prompt": ("append_system_prompts", "list"),
}

# Scaffold written by the `init` verb. Mirrors YOLO_KEYS (dash form, like the
# flags). null means "leave at the built-in default" — the loader skips nulls —
# so a freshly-init'd file round-trips to "no config" until the user edits it.
YOLO_INIT_DEFAULTS = {
    "config-dir": None,
    "auth": "keychain",
    "aws-profile": None,
    "aws-region": None,
    "bedrock-model": None,
    "claude-json": True,
    "ssh-agent": True,
    "base": "HEAD",
    "append-system-prompt": [],
}


def _parse_yolo_file(path: pathlib.Path) -> dict:
    """Parse one .yolo.json file into {argparse_dest: value}, type-checked."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"{path}: cannot read .yolo.json config: {e}")
    if not isinstance(raw, dict):
        sys.exit(f"{path}: .yolo.json must contain a JSON object")

    out = {}
    for key, val in raw.items():
        norm = key.replace("-", "_")
        if norm not in YOLO_KEYS:
            sys.exit(f"{path}: unknown .yolo.json option {key!r}")
        dest, kind = YOLO_KEYS[norm]
        if val is None:
            continue  # explicit null = leave the key at its built-in default
        if kind == "bool":
            if not isinstance(val, bool):
                sys.exit(f"{path}: {key!r} must be true or false")
            out[dest] = val
        elif kind == "auth":
            # set_defaults bypasses argparse's `choices` check, so validate here.
            if val not in AUTH_CHOICES:
                sys.exit(f"{path}: {key!r} must be one of {', '.join(AUTH_CHOICES)}")
            out[dest] = val
        elif kind in ("str", "path"):
            if not isinstance(val, str):
                sys.exit(f"{path}: {key!r} must be a string")
            out[dest] = os.path.expanduser(val) if kind == "path" else val
        else:  # "list" (append_system_prompts): a string or list of strings
            if isinstance(val, str):
                val = [val]
            if not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
                sys.exit(f"{path}: {key!r} must be a string or list of strings")
            out[dest] = val
    return out


def load_yolo_config(start: pathlib.Path, home: pathlib.Path) -> dict:
    """Merge ~/.yolo.json (base) with the nearest .yolo.json at/above `start`.

    Precedence low->high: ~/.yolo.json < nearest .yolo.json < CLI args (the caller
    applies the returned dict via PARSER.set_defaults, so explicit flags still win).
    append_system_prompts concatenates across the two files; every other key is
    overridden by the higher-precedence layer. The two files may be the same path
    (e.g. cwd is under $HOME with no closer .yolo.json); it's then loaded once.
    """
    files = []
    home_file = home / ".yolo.json"
    if home_file.is_file():
        files.append(home_file.resolve())
    for cur in [start.resolve(), *start.resolve().parents]:
        cand = cur / ".yolo.json"
        if cand.is_file():
            if cand.resolve() not in files:
                files.append(cand.resolve())  # nearest overlays ~/.yolo.json
            break

    merged = {}
    for path in files:
        for dest, val in _parse_yolo_file(path).items():
            if dest == "append_system_prompts":
                merged[dest] = merged.get(dest, []) + val
            else:
                merged[dest] = val
    return merged


def write_default_yolo(dest_dir: pathlib.Path) -> None:
    """`init` verb: scaffold a .yolo.json of default values into dest_dir.

    Refuses to clobber an existing file. The scaffold's null values round-trip to
    "no config" (the loader skips nulls), so it's a safe, editable starting point.
    """
    path = dest_dir / ".yolo.json"
    if path.exists():
        sys.exit(f"{path} already exists; not overwriting.")
    path.write_text(json.dumps(YOLO_INIT_DEFAULTS, indent=2) + "\n")
    print(f"Wrote {path}")


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
        "Defaults can be set in a .yolo.json file (nearest at/above the cwd, "
        "overlaid on ~/.yolo.json); CLI flags override it. Arguments after -- are "
        "passed directly to docker run (last-one-wins, so they override defaults)."
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
    choices=["init", "start", "resume", "shell", "finish", "list", "setup-token"],
    help="Optional subcommand. start/resume/shell take an *optional* TOPIC: with a "
    "TOPIC they act on a git worktree of that name (start creates it, resume/shell "
    "require it); with no TOPIC they act on the current directory (start a fresh "
    "session, resume the most recent one, or open a shell). 'finish' removes a "
    "worktree and requires a TOPIC. 'list' shows this repo's worktrees; 'init' writes "
    "a .yolo.json; 'setup-token' mints/caches a long-lived OAuth token (for --auth "
    "oauth-token). A bare `yolo` is equivalent to `yolo start`.",
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
    default="keychain",
    help="Authentication mechanism (default: keychain). "
    "'keychain' extracts the rotating Claude.ai keychain credentials and mounts "
    "them; 'oauth-token' forwards a long-lived CLAUDE_CODE_OAUTH_TOKEN from "
    "`claude setup-token` (stable, safe for concurrent containers); 'bedrock' "
    "authenticates via AWS Bedrock (mounts ~/.aws, sets CLAUDE_CODE_USE_BEDROCK=1). "
    "Also settable as `auth` in .yolo.json.",
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
    "--ssh-agent",
    action=argparse.BooleanOptionalAction,
    default=True,
    help="Forward the host ssh-agent socket into the container (default: on). "
    "Use --no-ssh-agent to skip it (GitHub git auth won't work then).",
)
PARSER.add_argument(
    "--rebuild-image",
    action="store_true",
    default=False,
    dest="rebuild_image",
    help="Force a Docker image rebuild from scratch (passes --no-cache to docker build).",
)
PARSER.add_argument(
    "--append-system-prompt",
    "-p",
    dest="append_system_prompts",
    action="append",
    default=[],
    metavar="PROMPT",
    help="Extra --append-system-prompt value passed to claude inside the container repeatable in addition to one about the container itself",
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
    append_system_prompts: list,
    *,
    ssh_agent: bool = True,
    continue_session: bool = False,
    resume=None,
    name: str | None = None,
) -> list[str]:
    """The args passed to `claude` inside the container (everything after the image).

    Always includes the container-only sandbox override and the built-in
    "you're in a container" system prompt (plus any -p additions). Optionally adds
    --continue / --resume [ID] and a session --name.
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
        *append_system_prompts,
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
) -> None:
    """Assemble the `docker run` argv from the credential/config flags and exec it.

    Shared by every launch path (start / resume / shell, worktree or cwd). The
    container name starts from container_base and gains -{config}/-{profile}
    suffixes; yolo.repo / yolo.worktree labels are stamped so the verbs can find
    the container later. `command` is the args after the image; `entrypoint`
    overrides the image ENTRYPOINT (used to drop into bash for `shell`).
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
    #   - oauth-token: forward a long-lived CLAUDE_CODE_OAUTH_TOKEN env var. No
    #     keychain extraction, no login check, no .credentials.json mount — the
    #     token is stable (never rotated/written back), so concurrent containers
    #     and the host can all use it at once. The env var also out-ranks any file
    #     creds, so a stale mounted .credentials.json can't shadow it.
    #   - bedrock: AWS creds + env (mounts ~/.aws), no keychain/login.
    #   - keychain (default): extract the rotating keychain creds into a mounted file.
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
    os.execvp("docker", run_cmd)


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

    headers = ("TOPIC", "BRANCH", "STATUS", "DIRECTORY")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def fmt(cols):
        # pad every column except the last so there's no trailing whitespace
        return "  ".join(
            c if i == len(cols) - 1 else c.ljust(widths[i]) for i, c in enumerate(cols)
        )

    print(fmt(headers))
    for row in rows:
        print(fmt(row))


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

    # Parse once with built-in defaults to dispatch `init`, which is terminal and
    # must work even with a broken ancestor/global config (it writes a fresh one),
    # so it runs *before* .yolo.json is layered in.
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

    if verb == "init":
        write_default_yolo(cwd)
        return

    # Every other verb gets the .yolo.json defaults layered under the CLI flags
    # (so e.g. `list` honours a config-set `base`); re-parse so explicit flags win.
    # Uses the real cwd, before any worktree retargeting below.
    PARSER.set_defaults(**load_yolo_config(cwd, home))
    parsed = PARSER.parse_args(script_argv)

    # Terminal verbs (no credential config needed) — handle and return.
    if verb == "list":
        do_list(home, parsed.base)
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
            os.execvp("docker", ["docker", "exec", "-it", cid, "/bin/bash"])
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
    if verb == "setup-token":
        generate_oauth_token(parsed.config_dir)
        return

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
            parsed.append_system_prompts, ssh_agent=parsed.ssh_agent, resume=parsed.resume
        )
    elif verb == "resume" and not parsed.new:
        command = build_claude_args(
            parsed.append_system_prompts, ssh_agent=parsed.ssh_agent, continue_session=True
        )
    else:
        # start, or `resume TOPIC --new` (a fresh named session in the worktree).
        command = build_claude_args(
            parsed.append_system_prompts, ssh_agent=parsed.ssh_agent, name=session_name
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
    )


if __name__ == "__main__":
    main()
