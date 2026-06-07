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


# .yolo.json config keys -> (argparse dest, kind). These are standing environment /
# credential preferences only; per-invocation *actions* (--worktree, --continue,
# --resume) are intentionally CLI-only and rejected if they appear in a .yolo.json.
# "path" values get ~ expanded (a JSON file can't rely on shell expansion).
YOLO_KEYS = {
    "config_dir":           ("config_dir", "path"),
    "bedrock":              ("bedrock", "bool"),
    "aws_profile":          ("aws_profile", "str"),
    "aws_region":           ("aws_region", "str"),
    "bedrock_model":        ("bedrock_model", "str"),
    "claude_json":          ("claude_json", "bool"),
    "ssh_agent":            ("ssh_agent", "bool"),
    "append_system_prompt": ("append_system_prompts", "list"),
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


PARSER = argparse.ArgumentParser(
    description="Run Claude Code in a Docker container.",
    epilog=(
        "Defaults can be set in a .yolo.json file (nearest at/above the cwd, "
        "overlaid on ~/.yolo.json); CLI flags override it. Arguments after -- are "
        "passed directly to docker run (last-one-wins, so they override defaults)."
    ),
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

    home = pathlib.Path.home()
    cwd = pathlib.Path.cwd()

    # Load .yolo.json config (nearest at/above the cwd, overlaid on ~/.yolo.json)
    # and apply it as argparse defaults, so explicit CLI flags still win. Uses the
    # real cwd, before any --worktree retargeting below.
    PARSER.set_defaults(**load_yolo_config(cwd, home))
    parsed = PARSER.parse_args(script_argv)

    # AWS knobs are inert without bedrock mode (the bedrock block below is the only
    # consumer), so just warn rather than failing — bedrock may be toggled off via
    # --no-bedrock over a .yolo.json that sets it.
    if not parsed.bedrock and (parsed.aws_profile or parsed.aws_region or parsed.bedrock_model):
        print("warning: aws-profile/aws-region/bedrock-model ignored without bedrock mode.",
              file=sys.stderr)
    config_dir = parsed.config_dir   # None => default ~/.claude
    if config_dir and not pathlib.Path(config_dir).is_dir():
        sys.exit(f"config-dir: not a directory: {config_dir}")

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
            "-v", "/run/host-services/ssh-auth.sock:/run/ssh-agent",
            "-e", "SSH_AUTH_SOCK=/run/ssh-agent",
            # Mount host known_hosts so SSH host key verification succeeds
            "-v", f"{home}/.ssh/known_hosts:/home/claude/.ssh/known_hosts:ro",
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
