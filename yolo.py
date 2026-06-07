#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
# https://claude.ai/chat/df7c14a7-6410-4b98-9799-1c9821557b81
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

DOCKER_IMAGE = "claude-yolo:latest"

# Dockerfile template — uid is substituted at runtime to match the host user so that
# files in the bind-mounted working directory are owned by (and writable as) the in-container
# user. The user is also put in group 0 so it can connect to Docker Desktop's root-owned
# ssh-auth.sock (see the useradd line and the ssh-auth.sock mount below).
DOCKERFILE_TEMPLATE = """\
FROM ubuntu:24.04

# Baked-in amenities used across most projects, so Claude doesn't re-install them in
# each ephemeral container. fd-find installs its binary as `fdfind`; symlink it to `fd`.
RUN apt-get update && apt-get install -y nodejs npm sudo jq git curl ripgrep fd-find build-essential vim && ln -s /usr/bin/fdfind /usr/local/bin/fd
# uv + uvx for fast Python tooling, copied from the official image (no curl, pinnable)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
# UID {uid} matches the host user so bind-mounted working-dir files are owned/writable.
# Group 0 (root) membership grants access to Docker Desktop's ssh-auth.sock, which is
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
ENV PATH=/home/claude/.local/bin:$PATH
ENTRYPOINT ["claude", "--dangerously-skip-permissions"]
"""


def build_docker_image() -> None:
    """Write the Dockerfile to a temporary directory and build the Docker image."""
    with tempfile.TemporaryDirectory(prefix="claude-yolo-build-") as build_dir:
        dockerfile = pathlib.Path(build_dir) / "Dockerfile"
        dockerfile.write_text(DOCKERFILE_TEMPLATE.format(uid=os.getuid()))
        subprocess.run(["docker", "build", "-t", DOCKER_IMAGE, build_dir], check=True)


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
    """Create or reuse a host git worktree for a parallel session.

    The worktree lives in a centralized state dir keyed by a slug of the main repo
    path (the same slug scheme Claude Code uses under ~/.claude/projects/). Returns
    (worktree_path, common_git, main_root). The caller bind-mounts both the worktree
    dir and the shared .git at their identical host paths, because the worktree
    records an absolute path to the shared .git and vice versa — so same-path
    mounting is what makes git work inside the container. A new branch NAME is
    created off `base` (default the current HEAD) with no upstream (a stray
    `git push` can't hit main); commits land in the shared .git on the host, so work
    survives container exit. An already-existing branch NAME is checked out as-is
    (base ignored).
    """
    common_git, main_root, slug = _repo_paths()
    worktree = home / ".claude-yolo" / "worktrees" / slug / name

    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if _branch_exists(name):
            subprocess.run(["git", "worktree", "add", str(worktree), name], check=True)
        else:
            subprocess.run(["git", "worktree", "add", "-b", name, str(worktree), base], check=True)

    return worktree, common_git, main_root


# .yolo.json config keys -> (argparse dest, kind). These are standing environment /
# credential preferences only; per-invocation *actions* (--worktree, --continue,
# --resume) are intentionally CLI-only and rejected if they appear in a .yolo.json.
# "path" values get ~ expanded (a JSON file can't rely on shell expansion).
YOLO_KEYS = {
    "config_dir": ("config_dir", "path"),
    "bedrock": ("bedrock", "bool"),
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
    "bedrock": False,
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


PARSER = argparse.ArgumentParser(
    description="Run Claude Code in a Docker container.",
    epilog=(
        "Defaults can be set in a .yolo.json file (nearest at/above the cwd, "
        "overlaid on ~/.yolo.json); CLI flags override it. Arguments after -- are "
        "passed directly to docker run (last-one-wins, so they override defaults)."
    ),
)
PARSER.add_argument(
    "verb",
    nargs="?",
    choices=["init", "start", "resume", "shell", "finish", "list"],
    help="Optional subcommand. start/resume/shell/finish take a TOPIC (a worktree "
    "name): start a new worktree+branch, resume/open a shell in an existing one, or "
    "finish (remove) one. 'list' shows this repo's worktrees; 'init' writes a "
    ".yolo.json. Omit the verb to run Claude in the current directory.",
)
PARSER.add_argument(
    "topic",
    nargs="?",
    help="Worktree/branch name for start/resume/shell/finish.",
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
    "--bedrock",
    action=argparse.BooleanOptionalAction,
    default=False,
    help="Authenticate/bill via AWS Bedrock instead of the Claude keychain. "
    "Mounts ~/.aws read-only and sets CLAUDE_CODE_USE_BEDROCK=1. "
    "Use --no-bedrock to override a .yolo.json that enables it.",
)
PARSER.add_argument(
    "--aws-profile",
    metavar="NAME",
    help="AWS profile to use (requires --bedrock). If omitted, the AWS SDK's "
    "default profile / env credentials are used.",
)
PARSER.add_argument(
    "--aws-region",
    metavar="REGION",
    help="AWS region for Bedrock (requires --bedrock; default: us-east-1).",
)
PARSER.add_argument(
    "--bedrock-model",
    metavar="ID",
    help="Bedrock model id (requires --bedrock).",
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
    "--append-system-prompt",
    "-p",
    dest="append_system_prompts",
    action="append",
    default=[],
    metavar="PROMPT",
    help="Extra --append-system-prompt value passed to claude inside the container repeatable in addition to one about the container itself",
)
PARSER.add_argument(
    "--worktree",
    metavar="NAME",
    help="Create/reuse a git worktree NAME (branch NAME) under "
    "~/.claude-yolo/worktrees/, run Claude in it, and name the session NAME. "
    "For parallel sessions on one repo without losing uncommitted work.",
)
# Resume a prior session. Session history lives in the bind-mounted ~/.claude/projects/
# and is keyed by the project path, which matches between host and container (cwd is mounted
# at its identical path), so sessions started in a yolo container are resumable here.
RESUME_GROUP = PARSER.add_mutually_exclusive_group()
RESUME_GROUP.add_argument(
    "--continue",
    "-c",
    dest="continue_session",
    action="store_true",
    help="Resume the most recent Claude session for this directory (claude --continue).",
)
RESUME_GROUP.add_argument(
    "--resume",
    "-r",
    dest="resume",
    nargs="?",
    const=True,
    default=None,
    metavar="SESSION_ID",
    help="Resume a Claude session by SESSION_ID, or omit it for an interactive picker "
    "(claude --resume).",
)


def running_container_for(slug: str, topic: str) -> str | None:
    """The id of a running yolo container for this repo+topic, or None.

    Containers are tagged with yolo.repo / yolo.worktree labels at launch, so we
    find them by label rather than reconstructing the (suffix-laden) name.
    """
    try:
        out = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=yolo.repo={slug}",
                "--filter",
                f"label=yolo.worktree={topic}",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except FileNotFoundError:
        return None
    return out.splitlines()[0] if out else None


def build_claude_args(
    append_system_prompts: list,
    *,
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

    Shared by every launch path (start / resume / shell / --worktree / bare). The
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
        # Forward the host git identity so commits made in the container are attributed correctly
        *git_identity_args(),
    ]

    if parsed.ssh_agent:
        # Forward the host ssh-agent via Docker Desktop's magic socket. We canNOT bind-mount
        # the raw host $SSH_AUTH_SOCK: that socket's listener lives in the macOS kernel, while
        # the container runs in Docker Desktop's Linux VM, so the mounted inode is dead
        # (connect() -> ECONNREFUSED). /run/host-services/ssh-auth.sock is a socket the VM
        # itself listens on and proxies through to the host agent. It's mounted srw-rw----
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

    # The four credential/config axes are independent (not mutually exclusive):
    #   (a) which config dir to mount        -- --config-dir
    #   (b) whether to mount ~/.claude.json   -- --claude-json/--no-claude-json
    #   (c) keychain creds (only non-Bedrock) -- derived from --bedrock
    #   (d) Bedrock env + ~/.aws              -- --bedrock (+ --aws-* / --bedrock-model)

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

    # (c) Keychain credentials, only when NOT using Bedrock (Bedrock authenticates via AWS).
    if not parsed.bedrock:
        ensure_logged_in(config_dir)
        credfile = extract_credentials(config_dir)
        args += ["-v", f"{credfile}:/home/claude/.claude/.credentials.json"]

    # (d) Bedrock: AWS creds + env. Composes with any config dir / claude-json choice.
    if parsed.bedrock:
        container = f"{container}-{parsed.aws_profile or 'bedrock'}"
        args += ["-v", f"{home}/.aws:/home/claude/.aws:ro"]
        args += ["-e", "CLAUDE_CODE_USE_BEDROCK=1"]
        if parsed.aws_profile:
            args += ["-e", f"AWS_PROFILE={parsed.aws_profile}"]
        args += ["-e", f"AWS_REGION={parsed.aws_region or 'us-east-1'}"]
        if parsed.bedrock_model:
            args += ["-e", f"BEDROCK_MODEL_ID={parsed.bedrock_model}"]

    # Labels let the verbs (shell/finish/list) find this container later, regardless
    # of the name suffixes above.
    if slug:
        args += ["--label", f"yolo.repo={slug}"]
    if worktree_name:
        args += ["--label", f"yolo.worktree={worktree_name}"]

    build_docker_image()

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

    if verb in ("start", "resume", "shell", "finish") and not topic:
        sys.exit(f"`{verb}` needs a topic name, e.g. `yolo {verb} my-topic`.")
    if topic and verb not in ("start", "resume", "shell", "finish"):
        sys.exit(f"unexpected argument: {topic!r}")
    if parsed.new and verb != "resume":
        sys.exit("--new only applies to `resume`.")
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
        worktree, _, slug = _worktree_dir(topic, home)
        if not worktree.is_dir():
            sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
        cid = running_container_for(slug, topic)
        if cid:
            print(f"Opening a shell in the running container for '{topic}' ({cid[:12]}).")
            os.execvp("docker", ["docker", "exec", "-it", cid, "/bin/bash"])
            return  # execvp doesn't return on success; guard the stubbed/failed case
        # No container running: fall through to launch a fresh bash container below.

    # AWS knobs are inert without bedrock mode (the bedrock block is the only
    # consumer), so just warn rather than failing — bedrock may be toggled off via
    # --no-bedrock over a .yolo.json that sets it.
    if not parsed.bedrock and (parsed.aws_profile or parsed.aws_region or parsed.bedrock_model):
        print(
            "warning: aws-profile/aws-region/bedrock-model ignored without bedrock mode.",
            file=sys.stderr,
        )
    if parsed.config_dir and not pathlib.Path(parsed.config_dir).is_dir():
        sys.exit(f"config-dir: not a directory: {parsed.config_dir}")

    # Resolve the worktree (if any) and the trailing command per verb.
    common_git = None
    slug = None
    worktree_name = None
    entrypoint = None

    if verb == "start":
        worktree, main_root, slug = _worktree_dir(topic, home)
        if worktree.exists() or _branch_exists(topic):
            sys.exit(f"'{topic}' already exists; resume it with `yolo resume {topic}`.")
        cwd, common_git, main_root = setup_worktree(topic, home, base=parsed.base)
        worktree_name = topic
        container_base = f"{main_root.name}-{topic}"
        command = build_claude_args(parsed.append_system_prompts, name=topic)

    elif verb == "resume":
        worktree, main_root, slug = _worktree_dir(topic, home)
        if not worktree.is_dir():
            sys.exit(f"no worktree '{topic}'; start one with `yolo start {topic}`.")
        if parsed.new and (parsed.continue_session or parsed.resume is not None):
            sys.exit("--new can't be combined with --continue/--resume.")
        cwd, common_git = worktree, _repo_paths()[0]
        worktree_name = topic
        container_base = f"{main_root.name}-{topic}"
        if parsed.new:
            command = build_claude_args(parsed.append_system_prompts, name=topic)
        elif parsed.resume is not None:
            command = build_claude_args(parsed.append_system_prompts, resume=parsed.resume)
        else:
            command = build_claude_args(parsed.append_system_prompts, continue_session=True)

    elif verb == "shell":
        # Fresh bash container (the exec-into-running case already returned above).
        worktree, main_root, slug = _worktree_dir(topic, home)
        cwd, common_git = worktree, _repo_paths()[0]
        worktree_name = topic
        container_base = f"{main_root.name}-{topic}"
        command = []
        entrypoint = "/bin/bash"

    else:
        # Bare launch, or the legacy --worktree flag.
        worktree_name = parsed.worktree
        if worktree_name:
            cwd, common_git, main_root = setup_worktree(worktree_name, home, base=parsed.base)
            slug = re.sub(r"[^a-zA-Z0-9]", "-", str(main_root))
            container_base = f"{main_root.name}-{worktree_name}"
            name = (
                worktree_name
                if not (parsed.continue_session or parsed.resume is not None)
                else None
            )
        else:
            slug = _repo_slug_or_none()
            container_base = cwd.name
            name = None
        command = build_claude_args(
            parsed.append_system_prompts,
            continue_session=parsed.continue_session,
            resume=parsed.resume,
            name=name,
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
