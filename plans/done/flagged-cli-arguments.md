# Plan: Rationalize the command-line arguments into orthogonal flags

## Motivation

Today the credential mode is encoded in a single overloaded positional slot,
decided by `pathlib.Path(positional[0]).is_dir()` (claude-yolo.py:287–291):

```python
is_dir = len(positional) >= 1 and pathlib.Path(positional[0]).is_dir()
config_dir       = positional[0] if is_dir else None
aws_profile      = positional[0] if not is_dir and len(positional) >= 1 else None
aws_region       = positional[1] if aws_profile and len(positional) >= 2 else None
bedrock_model_id = positional[2] if aws_profile and len(positional) >= 3 else None
```

That single slot means **config-dir and Bedrock are mutually exclusive** — you
cannot run Bedrock against an alternate config dir, because the moment the first
arg names a directory it's interpreted as `config_dir` and Bedrock mode is
unreachable. The `if config_dir / elif aws_profile / else` chain
(claude-yolo.py:337–361) hardwires that exclusivity.

There are really **four independent axes**, only one of which (`--worktree`) is
currently a flag:

1. **Config dir** — which `~/.claude` to mount (default `~/.claude`).
2. **Worktree** — run in a git worktree vs. the cwd (default: cwd). *Already a flag.*
3. **Bedrock** — auth/bill via AWS Bedrock vs. the Claude keychain (default: keychain).
4. **SSH agent** — forward the host ssh-agent socket or not (default: on). *New.*

Goal: make every axis an explicit flag so all reasonable combinations are
expressible (notably **Bedrock + custom config dir**, and **disabling the ssh
agent**), and drop the positional-arg overloading entirely.

## Target CLI

```
claude-yolo [options] [-- DOCKER_ARGS...]

  --config-dir PATH        Config dir to mount at /home/claude/.claude
                           (default: ~/.claude)
  --worktree NAME          (unchanged) run in git worktree NAME
  --bedrock                Use AWS Bedrock instead of the Claude keychain
  --aws-profile NAME       AWS profile (implies/requires --bedrock)
  --aws-region REGION      AWS region (default: us-east-1; requires --bedrock)
  --bedrock-model ID       Bedrock model id (requires --bedrock)
  --claude-json / --no-claude-json
                           Mount the host ~/.claude.json (default: on)
  --ssh-agent / --no-ssh-agent
                           Forward the host ssh-agent socket (default: on)
  --append-system-prompt / -p PROMPT   (unchanged, repeatable)
  --continue / -c          (unchanged)
  --resume / -r [SESSION_ID]            (unchanged)
```

The `--` docker-args passthrough is **unchanged** (still split out before
argparse in `main`, still appended last so last-one-wins).

### Example combinations now possible

```bash
claude-yolo                                            # default ~/.claude + keychain
claude-yolo --config-dir ~/.claude-work                # alt dir + keychain
claude-yolo --bedrock --aws-profile prod --aws-region us-west-2
claude-yolo --bedrock --config-dir ~/.claude-bedrock   # NEW: Bedrock + alt config dir
claude-yolo --worktree feat --bedrock --aws-profile prod
claude-yolo --no-ssh-agent                             # NEW: skip agent forwarding
```

## Decoupling the credential logic

The current three-way `if/elif/else` conflates three genuinely independent
decisions. Split them so each axis is handled on its own:

### (a) Config-dir mount + `.claude.json` mount — independent of each other

```python
custom_config_dir = parsed.config_dir   # None means "use the default ~/.claude"
if custom_config_dir:
    # validated to be an existing directory (see Validation)
    args += ["-v", f"{custom_config_dir}:/home/claude/.claude"]
else:
    args += ["-v", f"{home}/.claude:/home/claude/.claude"]

if parsed.claude_json:                   # --claude-json / --no-claude-json, default on
    args += ["-v", f"{home}/.claude.json:/home/claude/.claude.json"]
```

**`.claude.json` rule:** mount the host `~/.claude.json` whenever
`--claude-json` is on (the default), **regardless of config dir or Bedrock**;
`--no-claude-json` skips it. This is now its own axis, decoupled from the
config-dir choice.

Context for the default being *on*: `.claude.json` ignores `CLAUDE_CONFIG_DIR`
and always lives at `$HOME/.claude.json`, so there is only ever one host
`~/.claude.json`. Mounting it gives the container the standard global config
(MCP servers, project history/trust, onboarding state) by default. The opt-out
exists because, with a **custom** config dir, mounting the global json bleeds the
*default* profile's MCP servers / history into the alternate profile — so
`--no-claude-json` is the way to get a cleanly isolated profile.

> **Behavior change to note:** previously an alternate config dir got *no*
> `.claude.json` (it was created fresh and ephemeral in-container). Under this
> plan the default flips to mounting it. Old "isolated alternate dir" behavior
> is still available via `--config-dir … --no-claude-json`.

**Drop `CLAUDE_CONFIG_DIR=/home/claude/.claude`** (currently
claude-yolo.py:343). It is redundant: the in-container config dir is *always*
mounted at `/home/claude/.claude`, which is `$HOME/.claude` for the `claude`
user — i.e. exactly Claude Code's default. The env var only relocates the
directory, and it's being set to the default location. Removing it also makes
all config-dir paths symmetric.

### (b) Keychain credentials — only when NOT Bedrock

```python
credfile = None
if not parsed.bedrock:
    ensure_logged_in(custom_config_dir)          # was: if not aws_profile
    credfile = extract_credentials(custom_config_dir)
...
if credfile:
    args += ["-v", f"{credfile}:/home/claude/.claude/.credentials.json"]
```

`extract_credentials(None)` uses the default keychain service; a custom dir uses
the hashed service name (unchanged logic in claude-yolo.py:64–100). This is the
key fix that lets **Bedrock + custom config dir** work: the config-dir mount
(a) and the credential extraction (b) are now independent, where before they
were welded together in the same branch.

### (c) Bedrock env + ~/.aws mount — only when Bedrock

```python
if parsed.bedrock:
    args += ["-v", f"{home}/.aws:/home/claude/.aws:ro"]
    args += ["-e", "CLAUDE_CODE_USE_BEDROCK=1"]
    if parsed.aws_profile:
        args += ["-e", f"AWS_PROFILE={parsed.aws_profile}"]
    args += ["-e", f"AWS_REGION={parsed.aws_region or 'us-east-1'}"]
    if parsed.bedrock_model:
        args += ["-e", f"BEDROCK_MODEL_ID={parsed.bedrock_model}"]
```

Note `--aws-profile` becomes **optional** (was mandatory as `positional[0]`): if
omitted, we don't set `AWS_PROFILE` and the AWS SDK falls back to its default
profile / env credentials. This is strictly more flexible and costs nothing.

## SSH-agent flag (new axis)

Gate the three ssh-related runtime args (currently unconditional at
claude-yolo.py:318–321) behind `--ssh-agent/--no-ssh-agent`, default on:

```python
PARSER.add_argument("--ssh-agent", action=argparse.BooleanOptionalAction,
                    default=True,
                    help="Forward the host ssh-agent socket into the container "
                         "(default: on). --no-ssh-agent disables it.")
```

```python
if parsed.ssh_agent:
    args += [
        "-v", "/run/host-services/ssh-auth.sock:/run/ssh-agent",
        "-e", "SSH_AUTH_SOCK=/run/ssh-agent",
        "-v", f"{home}/.ssh/known_hosts:/home/claude/.ssh/known_hosts:ro",
    ]
```

`BooleanOptionalAction` requires Python ≥3.9; we're already pinned to ≥3.10, so
it's fine. **Caveat to document:** the image bakes the GitHub HTTPS→SSH rewrite
(`url."git@github.com:".insteadOf`, Dockerfile line ~44), which depends on the
agent. With `--no-ssh-agent`, in-container git push/pull to GitHub remotes will
fail auth — that's the expected, user-chosen tradeoff (the image is unchanged;
only the runtime mounts are dropped). The group-0 / `useradd -G root` bit stays
in the image regardless; it's harmless when the socket isn't mounted.

## Container naming

Today: base name is `cwd.name` (or `{main_root.name}-{worktree_name}` in
worktree mode), suffixed with the config-dir basename **or** the aws profile.
Since axes can now stack, define a deterministic, composable scheme:

```python
container = f"{main_root.name}-{worktree_name}" if worktree_name else cwd.name
if custom_config_dir:
    container += f"-{pathlib.Path(custom_config_dir).resolve().name}"
if parsed.bedrock:
    container += f"-{parsed.aws_profile or 'bedrock'}"
```

So `--worktree feat --bedrock --aws-profile prod --config-dir ~/.claude-work`
yields `repo-feat-.claude-work-prod` (suffixes stack in a fixed order). Docker
container names allow `[a-zA-Z0-9][a-zA-Z0-9_.-]*`; the slugs we feed in are
already path basenames / profile names, matching today's assumptions.

## Argparse changes (claude-yolo.py:219–270)

- **Remove** the `positional` argument entirely and the
  claude-yolo.py:287–291 derivation block.
- **Add** `--config-dir`, `--bedrock` (`store_true`), `--aws-profile`,
  `--aws-region`, `--bedrock-model`, `--claude-json` (BooleanOptionalAction,
  default `True`), `--ssh-agent` (BooleanOptionalAction, default `True`).
- Update the parser `description`/`epilog` to drop the positional grammar and
  document the `--` passthrough only.
- Keep `--worktree`, the `--continue`/`--resume` mutually-exclusive group, and
  `--append-system-prompt` exactly as they are.

## Validation (new, in `main`)

argparse can't express cross-flag "requires", so add explicit checks after
`parse_args`:

1. `--aws-profile` / `--aws-region` / `--bedrock-model` given without
   `--bedrock` → `sys.exit` with a clear message.
   *(Alternative considered: auto-imply `--bedrock` if any AWS flag is present.
   Rejected for explicitness — recommend the error, but call it out as a cheap
   change if ergonomics win.)*
2. `--config-dir PATH` that isn't an existing directory → `sys.exit`. (Replaces
   the old implicit `is_dir()` discriminator, which silently reinterpreted a
   non-dir as an AWS profile — a footgun this refactor removes.)

## Backward compatibility

This is a **breaking change** to the invocation syntax (positional
`CONFIG_DIR` / `AWS_PROFILE ...` no longer accepted). Given this is a
single-user personal tool, recommend a **clean break** rather than a
positional-compat shim. Follow-ups:

- Update `CLAUDE.md` (the usage block at the top and the "Three credential
  modes" / positional-decision sections — they describe the old grammar).
- Update any host aliases/symlinks the user drives this with (e.g. the
  `~/.local/bin/claude-yolo` symlink invocation patterns).

*(Optional, if a clean break is unwanted: accept a lone leading positional and
map it to `--config-dir`/`--aws-profile` via the old `is_dir()` test, printing a
deprecation notice. Not recommended — it re-introduces the overloading this plan
exists to remove.)*

## Step-by-step implementation order

1. Add the new argparse flags; remove `positional`. Update help text.
2. Add the two validation checks in `main`.
3. Replace the `is_dir`/positional derivation with reads of
   `parsed.config_dir` / `parsed.bedrock` / `parsed.aws_profile` etc.
4. Refactor the `if/elif/else` credential block into the three independent
   blocks (a) config-dir mount + the separate `--claude-json` mount, (b) keychain
   creds (non-Bedrock), (c) Bedrock env (a, b, c are independent, not exclusive).
5. Gate the ssh-agent args behind `parsed.ssh_agent`.
6. Update the container-naming logic to the composable scheme.
7. Drop the redundant `CLAUDE_CONFIG_DIR` env arg.
8. Manually smoke-test each axis and a couple of combinations (the script
   prints the full `docker run` line before exec — eyeball the mounts/env for:
   default, `--config-dir`, `--bedrock`, `--bedrock --config-dir`,
   `--no-claude-json`, `--no-ssh-agent`, and `--worktree` crossed with one of
   them).
9. Update `CLAUDE.md` to document the new flag-based interface.
```
