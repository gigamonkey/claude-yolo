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
# bind-mounted sockets (e.g. SSH agent) are accessible inside the container.
DOCKERFILE_TEMPLATE = """\
FROM ubuntu:24.04

# Baked-in amenities used across most projects, so Claude doesn't re-install them in
# each ephemeral container. fd-find installs its binary as `fdfind`; symlink it to `fd`.
RUN apt-get update && apt-get install -y nodejs npm sudo jq git curl ripgrep fd-find build-essential vim && ln -s /usr/bin/fdfind /usr/local/bin/fd
# uv + uvx for fast Python tooling, copied from the official image (no curl, pinnable)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
# UID {uid} matches the host user so bind-mounted sockets (e.g. SSH agent) are accessible
RUN useradd -m -s /bin/bash --uid {uid} claude
RUN echo "claude ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/claude
RUN mkdir -p /home/claude/.ssh && chown claude:claude /home/claude/.ssh && chmod 700 /home/claude/.ssh

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

    tmp = tempfile.NamedTemporaryFile(
        prefix="claude-credentials-", suffix=".json", delete=False
    )
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
            result = subprocess.run(
                ["git", "config", "--get", key], capture_output=True, text=True
            )
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


def setup_worktree(name: str, home: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create or reuse a host git worktree for a parallel session.

    The worktree lives in a centralized state dir keyed by a slug of the main repo
    path (the same slug scheme Claude Code uses under ~/.claude/projects/). Returns
    (worktree_path, common_git, main_root). The caller bind-mounts both the worktree
    dir and the shared .git at their identical host paths, because the worktree
    records an absolute path to the shared .git and vice versa — so same-path
    mounting is what makes git work inside the container. Branch NAME is created off
    the current HEAD with no upstream (a stray `git push` can't hit main); commits
    land in the shared .git on the host, so work survives container exit.
    """
    try:
        common_git_out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("--worktree must be run from inside a git repository.")

    common_git = pathlib.Path(common_git_out)
    main_root = common_git.parent
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(main_root))
    worktree = home / ".claude-yolo" / "worktrees" / slug / name

    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]
        ).returncode == 0
        if branch_exists:
            subprocess.run(["git", "worktree", "add", str(worktree), name], check=True)
        else:
            subprocess.run(["git", "worktree", "add", "-b", name, str(worktree)], check=True)

    return worktree, common_git, main_root


PARSER = argparse.ArgumentParser(
    description="Run Claude Code in a Docker container.",
    epilog=(
        "Positional args: [CONFIG_DIR | AWS_PROFILE [AWS_REGION [BEDROCK_MODEL_ID]]].\n"
        "Arguments after -- are passed directly to docker run "
        "(last-one-wins, so they override defaults)."
    ),
)
PARSER.add_argument(
    "positional",
    nargs="*",
    metavar="ARG",
    help="CONFIG_DIR, or AWS_PROFILE [AWS_REGION [BEDROCK_MODEL_ID]]",
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


def main():
    # Split on "--" before argparse sees argv so docker_args don't confuse it
    # docker_args come after $ARGS so last-one-wins gives user-supplied flags precedence
    if "--" in sys.argv:
        sep_idx = sys.argv.index("--")
        script_argv = sys.argv[1:sep_idx]
        docker_args = sys.argv[sep_idx + 1:]
    else:
        script_argv = sys.argv[1:]
        docker_args = []

    parsed = PARSER.parse_args(script_argv)
    positional = parsed.positional

    is_dir = len(positional) >= 1 and pathlib.Path(positional[0]).is_dir()
    config_dir       = positional[0] if is_dir else None
    aws_profile      = positional[0] if not is_dir and len(positional) >= 1 else None
    aws_region       = positional[1] if aws_profile and len(positional) >= 2 else None
    bedrock_model_id = positional[2] if aws_profile and len(positional) >= 3 else None

    home = pathlib.Path.home()
    cwd = pathlib.Path.cwd()

    # --worktree: run the session in a host-managed git worktree (durable) so you
    # can work on one repo in parallel containers without a data-loss window. We
    # retarget cwd to the worktree; the shared .git is mounted below.
    common_git = None
    worktree_name = parsed.worktree
    if worktree_name:
        cwd, common_git, main_root = setup_worktree(worktree_name, home)
        container = f"{main_root.name}-{worktree_name}"
    else:
        container = cwd.name

    args = [
        "-w", str(cwd),
        "-v", f"{cwd}:{cwd}",
        # Hostname set to working dir basename so Claude Code status line shows project name without git
        "--hostname", cwd.name,
        # Forward host ssh-agent into the container using the live socket path
        # Claude user ID in container is set to os.getuid() at build time so the socket is accessible
        "-v", f"{os.environ['SSH_AUTH_SOCK']}:/run/ssh-agent",
        "-e", "SSH_AUTH_SOCK=/run/ssh-agent",
        # Mount host known_hosts so SSH host key verification succeeds
        "-v", f"{home}/.ssh/known_hosts:/home/claude/.ssh/known_hosts:ro",
        # Forward the host git identity so commits made in the container are attributed correctly
        *git_identity_args(),
    ]

    # Worktree mode: mount the shared .git at its real host path so the worktree's
    # absolute gitdir pointers resolve and commits persist to the host.
    if common_git:
        args += ["-v", f"{common_git}:{common_git}"]

    credfile = None

    # Bedrock mode authenticates via AWS, so there's no Claude login to check.
    if not aws_profile:
        ensure_logged_in(config_dir)

    if config_dir:
        # First arg is a directory like ~/.claude-something
        configpath = pathlib.Path(config_dir).resolve()
        container = f"{container}-{configpath.name}"
        credfile = extract_credentials(config_dir)
        args += ["-v", f"{config_dir}:/home/claude/.claude"]
        args += ["-e", "CLAUDE_CONFIG_DIR=/home/claude/.claude"]

    elif aws_profile:
        # First arg is an AWS profile name, optionally followed by region and model ID
        container = f"{container}-{aws_profile}"
        args += ["-v", f"{home}/.claude.json:/home/claude/.claude.json"]
        args += ["-v", f"{home}/.claude:/home/claude/.claude"]
        args += ["-v", f"{home}/.aws:/home/claude/.aws:ro"]
        args += ["-e", "CLAUDE_CODE_USE_BEDROCK=1"]
        args += ["-e", f"AWS_PROFILE={aws_profile}"]
        args += ["-e", f"AWS_REGION={aws_region or 'us-east-1'}"]
        if bedrock_model_id:
            args += ["-e", f"BEDROCK_MODEL_ID={bedrock_model_id}"]

    else:
        # No args: use default ~/.claude
        credfile = extract_credentials(None)
        args += ["-v", f"{home}/.claude.json:/home/claude/.claude.json"]
        args += ["-v", f"{home}/.claude:/home/claude/.claude"]

    if credfile:
        args += ["-v", f"{credfile}:/home/claude/.claude/.credentials.json"]

    build_docker_image()
    
    extra_system_prompt = [
        "You are running in an ephemeral Ubuntu container instead of MacOS host. Use sudo apt to install things you need.",
        *parsed.append_system_prompts,
    ]

    claude_args = [
        # The container is the sandbox, so disable Claude's in-process OS sandbox.
        # Otherwise it warns at startup that bubblewrap/socat are missing (they're
        # deliberately not installed — they can't create namespaces in a container
        # anyway). This overrides sandbox.enabled from the mounted settings.json for
        # this container only; the host's settings are untouched.
        "--settings", '{"sandbox":{"enabled":false}}',
        "--append-system-prompt", "... ".join(extra_system_prompt),
    ]

    # Forward a resume/continue flag to claude. The two are mutually exclusive (argparse
    # enforces it). --resume takes an optional SESSION_ID; bare --resume (const True) opens
    # claude's interactive picker, which works because we run -it.
    if parsed.continue_session:
        claude_args += ["--continue"]
    elif parsed.resume is not None:
        claude_args += ["--resume"] + ([parsed.resume] if isinstance(parsed.resume, str) else [])

    if worktree_name and not (parsed.continue_session or parsed.resume is not None):
        # Name the Claude session so it's identifiable in the prompt box / /resume picker.
        # Skipped when resuming: the session already exists with its own name, and claude
        # rejects --name alongside --continue/--resume.
        claude_args = ["--name", worktree_name, *claude_args]

    run_cmd = ["docker", "run", "-it", "--rm", "--name", container, *args, *docker_args, DOCKER_IMAGE, *claude_args]

    sep = "- " * 40
    print(sep)
    print(" ".join(run_cmd))
    print(sep)
    os.execvp("docker", run_cmd)


if __name__ == "__main__":
    main()
