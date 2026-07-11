# CLAUDE.md

> **Deep internals live in [`ARCHITECTURE.md`](ARCHITECTURE.md).** This file is the
> lean, always-loaded overview: what yolo is, the invariants you must not break,
> and the dev/release workflow. When you need the full mechanics of a specific
> subsystem (auth, secrets, the config layering, the `wip` dashboard, tmux mode,
> the worktree verbs, the run-dir GC, the per-file test coverage, …), read the
> matching section in `ARCHITECTURE.md`. User-facing feature docs are in
> `README.md`. When prose and code disagree, trust the code.

## What this is

`yolo.py` runs Claude Code inside an **ephemeral Docker container** with
`--dangerously-skip-permissions`. Containing the blast radius of "yolo mode" is
the whole point: Claude runs unattended inside the container without touching the
host beyond the bind-mounted working directory (plus any explicitly mounted
paths).

- **The runtime is `yolo.py` plus sibling data files** — `Dockerfile.default`,
  `Dockerfile.custom`, `container-prompt.txt` — loaded by `_read_data_file`
  (resolved relative to `__file__`, following PATH symlinks).
- **One runtime dependency: `keyring`** (cross-platform credential store).
  It is declared in **two** places that **must stay in sync**: the PEP 723
  `dependencies` block at the top of `yolo.py` *and* `pyproject.toml`.
- **Two ways to run it**, both executing identical code (`main()` is the console
  entry point *and* the `__main__` target):
  - **Standalone** — the `#!/usr/bin/env -S uv run --script` shebang + PEP 723
    header self-run under **uv** (which guarantees Python ≥3.10 and provisions
    `keyring`). Symlink onto PATH to track the repo with no build step.
  - **Installed** — `uv tool install` / `pipx install` builds the wheel and puts
    a `yolo` executable on PATH. The wheel ships `yolo.py` + its data files via
    `[tool.hatch.build.targets.wheel] only-include`.
- **PyPI/dist name `claude-yolo`; the command is `yolo`.** `--version`
  (`_version()`) reads installed package metadata, falling back to scraping
  `version` out of the adjacent `pyproject.toml` — one source of truth, no second
  copy of the number in `yolo.py`.
- **Host platforms:** macOS and Linux fully supported; Windows via WSL2 (presents
  as Linux). Native Windows is out of scope. The *container* is always Linux; only
  host-side glue (credential store, clipboard, ssh-agent socket, temp dir) varies
  by OS, gated through the `_HOST` / `_is_macos()` / `_is_linux()` helpers.

The full flag/verb surface is documented in `README.md` and `ARCHITECTURE.md`. The
shape at a glance:

- **Auth** is one mutually-exclusive choice — `--auth {oauth-token,keychain,bedrock}`
  (default `oauth-token`, a long-lived `claude setup-token` token). Everything else
  (`--config-dir`, `--claude-json`, `--ssh-agent`, `--mount`, `--port`, `--secret`,
  `--tmux`, …) is an orthogonal flag that composes freely.
- **Workflow verbs:** `start` / `resume` / `shell` / `stop` (optional `TOPIC` →
  git-worktree mode, else the cwd), plus `finish` / `rebase` / `merge` / `diff` /
  `list` / `ps` / `wip` / `browse` / `dir` / `config` / `secret` / `setup-token` /
  `tokens` / `forget-token` / `dockerfile`. A bare `yolo` == `yolo start`.
- **Config** is host-side only: global `~/.yolo.json` < per-project
  `~/.claude-yolo/projects.json` < per-worktree `~/.claude-yolo/worktrees.json` <
  CLI flags, written via the `config` verb. Saved multi-repo launch templates
  (`~/.claude-yolo/multirepos.json`, edited via `config --multi-repo NAME`) layer
  between the project entry and the CLI at `start --multi-repo` only; the topic's
  overlay carries the effective keys afterwards.

## Invariants you must not break

These are the load-bearing safety and correctness properties. Changing code near
them without preserving them is a regression even if tests pass.

- **Host-side-only config is a security property.** Nothing yolo reads to decide
  what to mount/expose is writable from inside a container. Config lives in
  `~/.yolo.json` / `~/.claude-yolo/*.json` (never mounted); an in-directory
  `.yolo.json` is deliberately **not** read. Don't add a config source that a
  container could edit to grant its next session new host access.
- **Secrets and the OAuth token never touch the docker-run argv.** They ride a
  chmod-600 file transport (`/run/secrets` + a baked loader) or a read-only bind
  mount — never `-e` — so they stay out of `docker inspect`, host `ps`, and tmux's
  retained pane command. Preserve this when touching `_stage_secrets` /
  `launch_container`.
- **The build context contains only the Dockerfile.** That empty context is what
  stops a custom Dockerfile's `COPY`/`ADD` from reaching host files;
  `build_docker_image` asserts it. yolo passes no `--secret`/`--ssh` to
  `docker build`.
- **Image tags are content-addressed** (`claude-yolo:{hash8}`, hashing Dockerfile
  text + host UID). Parallel sessions rely on distinct tags not racing; don't
  reintroduce a fixed tag.
- **Custom images must run as the `claude` user** (`_verify_image_user` exits
  otherwise) — yolo passes no `-u`, so an image left on `USER root` would write
  host files as root.
- **The `claude` user's uid = host uid** (`HOST_UID` build arg → `useradd`), so
  bind-mount ownership is correct. Keep it.
- **Don't switch Claude Code's install method.** The image uses the **native
  installer** (`curl https://claude.ai/install.sh | bash`) → `~/.local/bin/claude`.
  `npm install -g @anthropic-ai/claude-code` lands at `/usr/local/bin/claude`,
  which `/doctor` flags as broken and self-update can't manage.
- **The in-process sandbox is disabled deliberately — the *container* is the
  sandbox.** The `--settings` overlay sets `sandbox.enabled:false`. Do **not**
  "fix" this by installing `bubblewrap`: a default Docker container can't create
  unprivileged user namespaces, and granting that capability would weaken the very
  isolation yolo exists to provide.
- **`--settings` replaces each top-level key wholesale.** The overlay that carries
  the sandbox setting *and* the session-activity hooks must re-include the user's
  own mounted hooks (`_read_settings_hooks`), or they're lost.
- **Keep the two `keyring` declarations in sync** (PEP 723 block ↔ `pyproject.toml`).

## Conventions / gotchas

- **`YOLO_SESSION` marks a yolo container** (value `worktree` or `cwd`). Its
  *presence* is the "am I in yolo?" test; inside one, the worktree/branch is
  already the unit of isolation, so **commit on the current branch — don't branch
  first**, and never `git add -A`/`git add .`/`git commit -a` (the working tree is
  the user's live, often-uncommitted checkout).
- **Git identity is forwarded as `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env vars**, not a
  mounted `~/.gitconfig` (which would drag in macOS-only osxkeychain/GPG bits that
  break commits in the Linux container).
- **SSH agent forwarding is off by default** (`--ssh-agent` opts in). Its source
  socket differs per host (`_ssh_agent_sock_source`); under it, GitHub HTTPS git is
  rewritten to SSH so no token enters the container. Without the agent, plain HTTPS
  clones of public repos still work.
- **`--` splits argv before argparse**; everything after it is appended to
  `docker run` last (user flags win, last-one-wins).
- The `# https://claude.ai/chat/...` URL on line 2 of `yolo.py` and the gist
  reference in git history are provenance — yolo started as Migurski's gist.

## Development

`pyproject.toml` defines a **uv-managed project** (one runtime dep, `keyring`; a
`dev` group with `ruff` + `pytest`). It's packaged (hatchling; `[project.scripts]
yolo = "yolo:main"`; the wheel ships `yolo.py` + the data files) so it can
`uv tool install`. `uv.lock` is committed; `.venv/` / `dist/` / caches are
gitignored.

```bash
uv sync                 # create/refresh .venv with the dev tools
uv run pytest           # run the test suite (tests/)
uv run ruff check .     # lint
uv run ruff format .    # format
uv build                # build wheel/sdist into dist/ (for publishing)
uv run bump-my-version bump patch   # version bump (patch/minor/major): commit + tag
```

Version bumps use **bump-my-version** (`[tool.bumpversion]` in pyproject.toml):
one command updates the version in `pyproject.toml` *and* the project's own entry
in `uv.lock`, then commits both and tags `v{new_version}`. Requires a clean tree.

**Tests never touch the host or Docker.** They load `yolo.py` via `importlib` from
its file path so each test gets a fresh module instance (`main()` mutates the
module-global `PARSER`), and `tests/conftest.py`'s `run_cli` fixture stubs the
docker/credential/exec side effects and asserts on the captured `docker run` argv.
A conftest autouse fixture forces `YOLO_CREDENTIAL_STORE=file` so no test touches a
real keyring. See the per-file coverage map in `ARCHITECTURE.md` before changing a
flag or mount — keep the relevant test file green.

## Cutting a release

1. **Check README and CHANGELOG are current.** Diff commits since the last tag
   (`git log --oneline v{last}..HEAD`) and fill any gaps — new flag/verb/key,
   changed defaults, renamed behavior. README = feature-level user docs;
   CHANGELOG = per-version record.
2. **Consolidate the CHANGELOG `## Unreleased` section** into user-facing entries:
   fold the intermediate commits for one feature into a single entry; drop churn
   that cancels out (a thing added then reworked is just its final shape).
3. **Retitle the section** — `## vX.Y.Z — YYYY-MM-DD` (today) — and commit that
   CHANGELOG edit on its own.
4. **Run the bump** — `uv run bump-my-version bump {patch,minor,major}` (minor =
   new features, patch = fixes only). It updates `pyproject.toml` + `uv.lock`,
   commits, and tags `v{new_version}` (annotated). Requires a clean tree, so commit
   the docs first.
5. **Push from the host** — the yolo container has no SSH agent, so tell the user
   to run `git push origin main --follow-tags`.

If a doc gap surfaces *after* the bump (tag not pushed yet), commit the fix and
move the annotated tag onto it (`git tag -f -a v{ver} -m "…"`, preserving the
message), then confirm it stayed annotated (`git cat-file -t v{ver}` → `tag`; a
stray `git tag -f` without `-a` silently downgrades it to lightweight).
