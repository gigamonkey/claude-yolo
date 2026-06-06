#!/usr/bin/env python3
# https://claude.ai/chat/df7c14a7-6410-4b98-9799-1c9821557b81
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

DOCKER_IMAGE = "claude-yolo:latest"

# Dockerfile template — uid is substituted at runtime to match the host user so that
# bind-mounted sockets (e.g. SSH agent) are accessible inside the container.
DOCKERFILE_TEMPLATE = """\
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y nodejs npm sudo jq git curl
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

    cwd = pathlib.Path.cwd()
    home = pathlib.Path.home()
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
    ]

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

    run_cmd = ["docker", "run", "-it", "--rm", "--name", container, *args, *docker_args, DOCKER_IMAGE, "--append-system-prompt", "... ".join(extra_system_prompt)]

    sep = "- " * 40
    print(sep)
    print(" ".join(run_cmd))
    print(sep)
    os.execvp("docker", run_cmd)


if __name__ == "__main__":
    main()
