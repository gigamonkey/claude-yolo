# CLAUDE.md

## What this is

`yolo.py` is a single-file Python script (no dependencies beyond the
stdlib) that runs Claude Code inside an ephemeral Docker container with
`--dangerously-skip-permissions`. Containing the blast radius of "yolo mode"
is the whole point: Claude can run unattended inside the container without
touching the host beyond the bind-mounted working directory.

The script itself is **stdlib-only and standalone** — it ships as one PEP 723
file with no runtime dependencies and is run directly. The repo *also* carries a
small uv-managed dev setup (`pyproject.toml`, `tests/`) for linting and tests;
that tooling is never needed to *run* the script, only to develop it (see
**Development** below). Run it directly:

```bash
./yolo.py                          # default: long-lived OAuth token (consent-prompted mint on first run)
./yolo.py --config-dir ~/.claude-work          # alternate config dir
./yolo.py --auth keychain          # mount a snapshot of the rotating keychain creds instead
./yolo.py --auth bedrock --aws-profile myprofile --aws-region us-west-2 --bedrock-model some.model.id
./yolo.py --auth bedrock --config-dir ~/.claude-bdr # Bedrock + alternate config dir
./yolo.py --no-claude-json         # don't mount the host ~/.claude.json
./yolo.py --no-ssh-agent           # don't forward the host ssh-agent
./yolo.py --mount ~/refdocs        # also mount ~/refdocs (read-only) at its host path
./yolo.py --mount ~/other:rw       # extra mount, writable
./yolo.py setup-token              # mint+cache the long-lived OAuth token explicitly
./yolo.py tokens                   # list minted tokens (mint date, est. expiry, status)
./yolo.py forget-token             # delete this config dir's token (local only)
./yolo.py -- --network host        # extra docker run args
./yolo.py                          # == `yolo start`: fresh session in the cwd
./yolo.py resume                   # continue most recent session in this dir
./yolo.py resume -r                # interactive session picker (cwd)
./yolo.py resume -r SESSION_ID     # resume a specific session (cwd)
./yolo.py shell                    # bash shell in the cwd's container (or fresh)
./yolo.py start fix-auth           # new worktree+branch, launch a session (see verbs)
./yolo.py resume fix-auth          # re-enter that worktree, continue the session
./yolo.py shell fix-auth           # bash shell in that worktree's container
./yolo.py finish fix-auth          # remove the worktree, keep the branch
./yolo.py list                     # this repo's worktrees
./yolo.py --version                # print the version and exit
```

The **auth mechanism** is a single mutually-exclusive choice via `--auth`
(`oauth-token` [default] / `keychain` / `bedrock`). Everything else —
`--config-dir`, `--claude-json`, `--ssh-agent`, `--mount` — is an **orthogonal
flag** that composes freely with the chosen auth mode and with each other. The
only positional args are an optional `verb`
(`config`/`start`/`resume`/`shell`/`finish`/`list`/`setup-token`/`tokens`/
`forget-token`) and its `TOPIC`; see [Workflow verbs](#workflow-verbs).

Defaults for most flags can also live in **host-side config** — global
`~/.yolo.json` plus a per-project entry in `~/.claude-yolo/projects.json`,
written with the `config` verb (see the config section below; an in-directory
`.yolo.json` is deliberately **no longer read**):

```bash
echo '{"ssh-agent": false}' > ~/.yolo.json   # global defaults
./yolo.py config --config-dir ~/.claude-work --mount ~/refdocs
                          # persist those flags as THIS project's entry
./yolo.py                 # picks up both layers; equals passing those flags
./yolo.py --ssh-agent     # explicit flag still overrides the files
./yolo.py config          # show the entry that currently applies (read-only)
```

The shebang is `#!/usr/bin/env -S uv run --script` with a PEP 723 metadata block
(`requires-python = ">=3.10"`, no dependencies), so the script self-runs under
**uv**, which guarantees a Python ≥3.10 (the `str | None` annotations need it;
macOS system `python3` is often 3.9). Running it therefore requires `uv` to be
installed. It's still stdlib-only — uv just selects the interpreter. uv preserves
the `--` separator, so docker-arg passthrough still works.

`yolo.py` is dual-purpose — the *same file* is both the standalone PEP 723 script
and an importable module with a `main()` entry point. So there are two ways to run
it from anywhere:

- **Installed** (preferred): `uv tool install <repo-or-PyPI>` (or `pipx install`)
  builds the wheel and puts a `yolo` executable on PATH in its own isolated venv,
  pulling in zero runtime deps. `uv tool upgrade claude-yolo` updates it. The
  console-script wiring is `[project.scripts] yolo = "yolo:main"` in
  `pyproject.toml`; the wheel ships only `yolo.py` (`[tool.hatch.build.targets.wheel]
  only-include`).
- **Standalone**: `chmod +x yolo.py` and symlink it onto PATH
  (`ln -s "$PWD/yolo.py" ~/.local/bin/yolo`); the PEP 723 header makes it self-run,
  and a symlink keeps it tracking the repo with no build step.

The PyPI/dist name is `claude-yolo`; the command it installs is `yolo`. `main()`
is the console-script entry point *and* the `if __name__ == "__main__"` target, so
both paths run identical code.

`--version` (the argparse `version` action) prints `_version()`, which mirrors this
dual nature: it first reads the recorded package metadata
(`importlib.metadata.version("claude-yolo")`, present in the installed wheel), and
falls back to scraping `version` out of the **adjacent pyproject.toml** (resolving
the script's symlink, so the PATH-symlink standalone install works) — so both modes
trace back to the single source of truth, pyproject.toml, with no second copy of the
number in `yolo.py`. A stray copy with neither metadata nor pyproject reports
`unknown`.

## How it works

1. **Builds the image** (`build_docker_image`) from an inline
   `DOCKERFILE_TEMPLATE` written to a temp dir. Ubuntu 24.04 + nodejs/npm + a few
   baked-in amenities used across most projects (`ripgrep`, `fd-find` symlinked to
   `fd`, `build-essential`, `vim`, and `uv`/`uvx` copied from `ghcr.io/astral-sh/uv`) +
   Claude Code installed via the **native installer**
   (`curl https://claude.ai/install.sh | bash`) at `~/.local/bin/claude`. The
   image is rebuilt on every run (Docker layer cache makes this cheap), so baked
   amenities cost ~nothing per launch and save Claude from re-installing common
   tools in each ephemeral container. Reserve the image for *cross-cutting* tools;
   project-specific/heavy ones stay on-demand via `sudo apt` inside the container.
   Do NOT switch to `npm install -g @anthropic-ai/claude-code` — that lands at
   `/usr/local/bin/claude`, which Claude Code's `/doctor` flags as a broken
   install and which self-update can't manage.
2. **Substitutes the host UID** into the Dockerfile's `useradd` so the
   in-container `claude` user matches `os.getuid()`. This keeps bind-mount file
   ownership correct: working-dir edits land on the host owned by the user, and
   the chmod-600 credentials file and mounted `~/.claude` stay readable inside —
   keep it. (SSH-agent socket access is *not* what needs this; that's granted
   separately by group-0 membership — see the gotchas.)
3. **Checks host login** (`ensure_logged_in` / `_is_logged_in`) before launch in
   keychain mode only (the default oauth-token mode and Bedrock skip it). Runs
   `claude auth status --json` and
   reads the `loggedIn` field; if logged out, offers to run `claude auth login`
   then re-checks. Checks login *status*, not token expiry, on purpose: an expired
   accessToken is auto-refreshed at runtime via the stored refreshToken, so expiry
   alone doesn't mean logged out. For an alternate `--config-dir` it sets host-side
   `CLAUDE_CONFIG_DIR` so the check targets the right keychain entry. If host
   `claude` is missing/too old for `auth`, it returns True and defers to the
   empty-file check in `extract_credentials`.
4. **Extracts credentials** (`extract_credentials`; keychain mode only) from the
   macOS keychain via the `security` CLI, into a chmod-600 temp file that gets
   bind-mounted to `.credentials.json`. In the default oauth-token mode this
   step is replaced by forwarding `CLAUDE_CODE_OAUTH_TOKEN` (see the oauth-token
   section). Service name is `Claude Code-credentials` by default,
   or `Claude Code-credentials-{hash8}` for a non-default config dir, where
   `hash8` is the first 8 hex chars of the SHA-256 of the resolved config path.
   This mirrors how Claude Code itself names keychain entries — if that scheme
   changes upstream, this breaks.
5. **Assembles `docker run` args** and `os.execvp`s into docker (replacing the
   process, so it's interactive `-it --rm`). The args also forward the host git
   identity (`git_identity_args`) and the SSH agent (see gotchas).

## Auth mechanism (`--auth`) + orthogonal config axes

The old single overloaded positional (config dir *or* AWS profile, decided by
`is_dir()`) is gone. The **auth mechanism** is now a single mutually-exclusive
choice — `--auth {keychain,oauth-token,bedrock}` (default `oauth-token`,
`AUTH_CHOICES`) — so argparse's `choices` enforces the exclusivity structurally
(no hand-written "these two can't combine" guard). The config axes compose freely
on top of whichever auth is chosen:

- **`--config-dir PATH`** (default `~/.claude`) → mounted at `/home/claude/.claude`.
  When set, credentials are pulled with the hashed service name and the container
  name gets a `-{basename}` suffix. The mount is *always* at `/home/claude/.claude`
  (= the `claude` user's `$HOME/.claude`, Claude Code's default), so **no in-container
  `CLAUDE_CONFIG_DIR` is set** — it would be redundant.
- **`--claude-json` / `--no-claude-json`** (default on) → whether to mount the host
  `~/.claude.json` (global config: MCP servers, project history/trust). It lives at
  `$HOME/.claude.json` regardless of `CLAUDE_CONFIG_DIR`, so there's only ever one.
  `--no-claude-json` gives a cleanly isolated profile — the intended pairing with an
  alternate `--config-dir`.
- **`--ssh-agent` / `--no-ssh-agent`** (default on) → forward the host ssh-agent
  socket (see gotchas). `--no-ssh-agent` drops the socket mount, `SSH_AUTH_SOCK`, and
  the `known_hosts` mount; in-container GitHub git auth then won't work, since the
  baked HTTPS→SSH rewrite relies on the agent.
- **`--mount PATH[:ro|:rw]`** (repeatable; `mounts` in config) → bind-mount extra
  host directories ("reference" dirs) at their **identical host paths**, like the
  cwd. **Read-only by default**; `:rw` opts in. The path must exist (docker would
  otherwise create it root-owned on the host). Each mount is also forwarded to
  claude as `--add-dir`, so the dirs are working directories Claude actually knows
  about. Mount lists **concatenate** across the config layers and the CLI (exact
  dups deduped; on a same-path ro/rw conflict the higher layer wins). A `shell`
  exec'd into a *running* container necessarily joins it with the mounts it was
  started with — docker can't add mounts to a live container.
- **`--rebuild-image`** (default off) → pass `--no-cache` to `docker build`, forcing
  a full image rebuild from scratch (useful when a baked tool is stale or the
  Dockerfile changed).
- **Guardrails** (checked just before any container launch; the terminal verbs and
  `shell`-into-running are exempt): launching with the cwd **at or above `$HOME`**
  is a hard error — it would mount the whole home dir (incl. `~/.ssh` and yolo's
  own trusted config) read-write into a skip-permissions container — overridable
  only by the deliberately CLI-only `--dangerously-allow-home`. And the opt-in
  **`require-project-entry`** (bool; set it in `~/.yolo.json`) refuses to launch
  when no `projects.json` entry matches the cwd, so a renamed project fails loudly
  instead of silently falling back to global defaults; `--no-require-project-entry`
  overrides for one run.

The three `--auth` values (the (c) block in `launch_container`):

- **`oauth-token`** (default) → authenticate with a long-lived
  `CLAUDE_CODE_OAUTH_TOKEN` env var; **skips keychain extraction, the login check,
  and the `.credentials.json` mount**, just adding `-e CLAUDE_CODE_OAUTH_TOKEN=…`.
  It's the default because it's the only mode that's safe regardless of session
  length or concurrency. See
  [Long-lived OAuth token](#long-lived-oauth-token---auth-oauth-token-the-default) below.
- **`keychain`** → `ensure_logged_in` + `extract_credentials`, mounting
  the rotating keychain creds at `.credentials.json`. The only mode that runs the
  login check. Unsafe for concurrent/overlapping sessions (see the oauth-token
  section for why); kept for plans without `setup-token` and as an explicit
  opt-in.
- **`bedrock`** (+ optional `--aws-profile`, `--aws-region` [default `us-east-1`],
  `--bedrock-model`) → sets `CLAUDE_CODE_USE_BEDROCK=1`, mounts `~/.aws` read-only,
  **skips keychain extraction and the login check**. Container name gets a
  `-{profile-or-bedrock}` suffix. The three AWS sub-flags only apply under
  `--auth bedrock` (a `main` warning fires if they're set otherwise);
  `--aws-profile` is optional (SDK default creds used if omitted).

The config-dir mount, the `~/.claude.json` mount, and the auth mechanism are
independent — so e.g. `--auth bedrock --config-dir ~/.claude-bdr` (Bedrock auth,
separate profile) works, which the old positional scheme could not express.
Overriding a config file that sets `auth` is just an explicit `--auth keychain`
(etc.) on the CLI.

## Long-lived OAuth token (`--auth oauth-token`, the default)

The keychain credentials are an OAuth pair whose **refresh token rotates
single-use on every refresh** — proven on 2026-06-08 (see `token-investigation.md`).
yolo mounts a *snapshot* of that pair into each container, so the first party (a
container *or* the host) to refresh silently invalidates every other snapshot's
refresh token; the loser gets a 401 on its next refresh. That makes **concurrent
(and even sequential-with-overlap) yolo sessions unsafe** under the snapshot model.
We also confirmed (2026-06-09, `precedence-probe.sh` + `host-write-probe*.sh`) that
a non-empty `~/.claude/.credentials.json` can override the host keychain, so simply
co-locating a shared file in `~/.claude` is *not* a safe fix.

`--auth oauth-token` sidesteps the whole problem by using a **different credential
family** for containers: `claude setup-token` mints a **one-year token that is
never rotated and never written back**. Because nothing ever rewrites it, any
number of concurrent containers — and the host on its own keychain creds — can use
it simultaneously with no interference. It's delivered purely as the
`CLAUDE_CODE_OAUTH_TOKEN` env var, which in Claude Code's auth precedence
out-ranks the file/keychain `/login` creds, so even a stale mounted
`.credentials.json` can't shadow it. This is why it became the default in 0.6.0:
keychain mode was an attractive nuisance, fine in a quick test and broken once
sessions got long, parallel, or overlapped host use.

Mechanics (`ensure_oauth_token` / `generate_oauth_token`):

- **Resolution order:** an explicit `CLAUDE_CODE_OAUTH_TOKEN` in the *host* env
  wins (for CI / self-managed tokens; it's global by nature) → else the
  yolo-managed macOS keychain entry **for the active config dir** → else mint a
  fresh one interactively and cache it there. That last (auto-mint) step is
  **consent-prompted and gated on `sys.stdin.isatty()`**: interactively, yolo
  explains what's about to be minted (1-year token, keychain storage,
  `forget-token` / the claude.ai revoke page) and asks `Proceed? [Y/n]` before
  running the flow — minting a year-long credential the user didn't explicitly
  ask for was the original argument against making this mode the default, so it
  is never done silently (`yolo setup-token` skips the prompt: running the verb
  *is* the consent). A non-interactive launch with no cached token (script/cron/
  no TTY) exits with guidance to run `yolo setup-token` or set the env var,
  rather than hanging on a browser flow nobody can drive.
- **Per-config-dir, like the keychain creds.** The token is cached under
  `claude-yolo-oauth-token` for the default config dir, or
  `claude-yolo-oauth-token-{hash8}` for an alternate `--config-dir`, where `hash8`
  is the first 8 hex chars of the SHA-256 of the resolved path (`_oauth_service`) —
  the *same* hash Claude itself uses for its per-dir keychain entry. So each
  config dir (≈ each account/profile) gets its own long-lived token instead of one
  global token silently authenticating as the wrong account.
- **`yolo setup-token`** (a terminal verb) forces a (re)generation —
  use it for first-time setup or when the year is up. Honours `--config-dir`
  (and a config-file `config-dir`), caching under that dir's service name, so it
  matches what a launch will read. It runs `claude setup-token`
  under a **pty** so the child sees a real terminal (the browser/paste OAuth flow
  works) while yolo tees *and* captures the output, then scrapes the `sk-ant-…`
  token out (`_scrape_token`: ANSI-stripped, last match). The pty is resized
  **wide (512 cols)** via `TIOCSWINSZ` on first read — `pty.spawn` leaves the
  window 0×0, which `claude` treats as 80 columns and hard-wraps to, splitting
  the ~108-char token across lines so the scrape silently cached a truncated
  token that 401'd at runtime. As a backstop, a scraped match that ends at a
  line break with the next line continuing in the token alphabet is treated as
  wrapped and rejected. If scraping fails (wrap detected, output shape changed),
  it falls back to prompting for a manual paste. The token is upserted into the
  keychain with `security add-generic-password -U`.
- **Storage rationale:** the keychain (not a dotfile) keeps the secret encrypted
  at rest, consistent with how Claude Code stores its own creds, and it's
  *extract-only* — never rotated, never written back — so none of the precedence/
  rotation hazards of the mounted `.credentials.json` apply.
- **Caveat:** this *does* put a bearer token inside the container env (a shift from
  the "secret never enters the container" SSH-agent philosophy), but it's a scoped,
  inference-only token — and no worse than the mounted refresh-token snapshot,
  which it replaces. Requires a Pro/Max/Team/Enterprise plan.

### Token bookkeeping: registry, expiry warning, `tokens` / `forget-token`

Because revocation is effectively out of our hands (verified 2026-06-10: no CLI
command, no documented OAuth revocation endpoint; the only path is manual at
<https://claude.ai/settings/claude-code>, whose token list shows near-zero
per-token metadata, accumulates entries from normal Claude Code usage, and has a
reported multi-day revocation lag — claude-code issues #34198/#48373/#59378/
#43801), yolo does its own bookkeeping:

- **Registry** (`~/.claude-yolo/tokens.json`; `_read_tokens_file` /
  `_write_token_entry` / `_remove_token_entry`): maps keychain **service name →
  `{config_dir, minted}`**. Non-secret metadata, host-side only, never mounted
  (same safety property as `projects.json`). Written by `_store_oauth_token`
  (the single funnel both mint paths go through); a re-mint replaces the entry
  and prints the *previous* mint timestamp, since the old token stays valid
  server-side. It exists for what the keychain can't do: enumerate yolo's tokens
  across config dirs, and map a service name back to its config dir (the hash8
  is one-way — the mapping is recorded at mint time or lost). The **mint
  timestamp is the practical point**: it's the only handle for identifying a
  token on the claude.ai page.
- **Expiry warning** (`_warn_token_expiry`, called from `ensure_oauth_token` on
  the cached-keychain-token path; skipped for env-supplied tokens, whose age is
  unknowable): warns at launch when the token is past or within
  `TOKEN_EXPIRY_WARN_DAYS` (7) of `mdat + TOKEN_LIFETIME_DAYS` (365 — an
  *assumption*; the token is opaque and states no expiry). The date source is
  the **keychain item's own `mdat`** (`_keychain_mdat`: `security
  find-generic-password` *without* `-w` — attributes only, no secret read —
  regex-parsed, falling back to `cdat`), not the registry: we upsert with
  `add-generic-password -U`, so mdat = last mint, which can't drift and covers
  tokens minted before the registry existed. Parse trouble → `None` → silently
  no warning (it's advisory).
- **`yolo tokens`** (`do_tokens`, terminal verb, registry-only — needs no config
  dir): table of SERVICE / CONFIG DIR / MINTED / EXPIRES~ / STATUS. STATUS
  reconciles against the keychain via `_keychain_has` (attributes-only
  existence check): `stale (not in keychain)` for a deleted item,
  `re-minted outside yolo` when keychain mdat disagrees with the registry mint
  by > 1 day, else `ok`. Footer points at the claude.ai page and the
  match-by-MINTED trick.
- **`yolo forget-token`** (`do_forget_token`, terminal verb): deletes the active
  config dir's keychain entry (`_keychain_delete`) and registry row, then is
  explicit that the token is only *forgotten*, not revoked — still valid
  server-side, revocable only at the claude.ai page, and probably impossible to
  identify there (reasons above, outside yolo's control). Named `forget-token`
  deliberately: the verb must not claim a power it doesn't have. Honours
  `--config-dir`/config-file `config-dir`, and is dispatched *before* the
  config-dir-must-exist check so a token for an already-deleted config dir can
  still be forgotten (`_oauth_service` only hashes the resolved path).

## Host-side config: `~/.yolo.json` + `~/.claude-yolo/projects.json`

Config supplies defaults for most flags; `load_yolo_config` applies them via
`PARSER.set_defaults` *before* the re-`parse_args`, so explicit CLI flags still
win. Two layers, merged low→high, **both host-side only**:

1. **`~/.yolo.json`** (global) — a flat JSON object of config keys.
2. **`~/.claude-yolo/projects.json`** (per-project) — a JSON object mapping a
   **directory path** to a config object of the same keys. An entry applies when
   the *real* cwd (before any worktree `TOPIC` retargeting) is **at or under**
   the key path; when several keys match, the **longest wins** and only that one
   entry is used — the same nearest-wins rule the old in-directory search had,
   so running from a subdirectory picks up the project's entry. Written by
   `yolo config` (its only writer); a plain launch never touches it.

**An in-directory `.yolo.json` is deliberately no longer read.** It lives inside
the bind-mounted tree, so Claude in a container could edit it and grant its next
session new host access — extra `mounts`, or an arbitrary *read-write* host mount
via `config-dir` — and a `.yolo.json` committed in a cloned repo would apply
someone else's config the first time you ran yolo there. Host-side-only config
makes the safety property structural: nothing yolo reads is writable from inside
a container. (`~/.claude` *is* mounted rw, which is why the project store lives
under `~/.claude-yolo` — only `worktrees/<slug>/<topic>` dirs under it are ever
mounted, never `projects.json`.) A leftover `.yolo.json` found at/above the cwd
draws a **warning on every run** (never an error — the file is inert) naming the
migration path; `~/.yolo.json` itself is exempt from the walk.

Precedence overall: `~/.yolo.json` < `projects.json` entry < CLI flags. Per key
the higher layer **overrides**, except `append-system-prompt` and `mounts`
(`_CONCAT_DESTS`), which **concatenate** across the layers and then the CLI
values (prompts and mounts accumulate; everything else replaces).

Keys mirror the flag names (dashes or underscores both accepted). Supported:
`config-dir`, `auth` (one of `keychain`/`oauth-token`/`bedrock` — validated against
`AUTH_CHOICES` in `_parse_yolo_dict`, since `set_defaults` bypasses argparse's
`choices` check), `aws-profile`, `aws-region`, `bedrock-model`, `claude-json`,
`ssh-agent`, `base`, `append-system-prompt` (string or list of strings),
`mounts` (string or list, `PATH[:ro|:rw]`), `require-project-entry`.
Per-invocation **actions** — `--resume` and the verbs (with their `TOPIC`) — are
deliberately **not** config keys, and neither is `--dangerously-allow-home`
(CLI-only by design); any of them in a config file is a hard error (not in
`YOLO_KEYS`). `config-dir` gets `~` expanded (a JSON file can't lean on shell
expansion). Booleans must be JSON `true`/`false`. A JSON **`null`** for any key
means "leave at the built-in default" (the loader skips it). Unknown keys, wrong
types, and malformed JSON all `sys.exit` naming the offending file/entry
(`_parse_yolo_dict` / `_read_projects_file`).

Every load also prints a one-line **provenance note** to stderr — e.g.
`config: ~/.yolo.json + projects.json[/Users/peter/hacks/foo]` or
`config: built-in defaults (no project entry)` — and warns about **dangling
project keys** (entries whose directory no longer exists: the signature of a
moved/renamed project, whose config would otherwise *silently* fall back to the
global defaults — wrong account/profile being the real hazard). When the cwd
also has no matching entry — the rename case produces both at once — the warning
suggests re-running `yolo config` and removing the stale entry. This detection
only works because entries are never auto-created: a plain run in a fresh
directory configures nothing and writes nothing. The hard-mode version is the
`require-project-entry` guardrail (see above). Note `projects.json` keys are
**paths-as-identity**: renames must be hand-migrated (matching how Claude's
`~/.claude/projects/` buckets and the worktree slugs behave).

### `config` verb

`yolo.py config [CONFIG FLAGS]` (`do_config`) shows or updates this project's
`projects.json` entry, then exits — it does **not** run a container. The entry
key is the **main repo root** when inside a git repo (so subdirectory runs and
worktree sessions share it; `_project_key`), else the cwd. Behavior à la
`git config`:

- **With config flags** — `yolo config --auth bedrock --mount ~/refdocs` —
  persists **exactly the explicitly-passed `YOLO_KEYS` flags** into the entry,
  per-key (other keys in the entry are left alone; re-running with one flag
  updates just that key). "Explicitly passed" is detected by a **sentinel
  re-parse** (`_explicit_config_flags`): a plain parse can't distinguish
  "defaulted" from "explicitly set to the default", and `config --auth oauth-token`
  must persist. List-kind dests use a fresh marker list, since argparse's append
  action copies the default before appending (identity survives exactly when the
  flag never appeared). `--mount` values are validated (exist + is-dir) *before*
  persisting, so a typo can't be pinned; the final entry is also re-validated so
  an unloadable entry is never written.
- **Bare `yolo config`** is **read-only**: prints the entry that currently
  applies (or "no entry for &lt;key&gt;") plus the projects.json path, and flags
  dangling keys. There is no scaffold/template behavior (and no
  `YOLO_INIT_DEFAULTS` anymore — built-in defaults live only in argparse).

`config` is dispatched off the *first* `parse_args`, **before** the config files
are layered in — a broken config can't block fixing the config — and it reads
only `projects.json` itself, failing with a pointed message on a malformed file
(never clobbering it).

AWS sub-keys without `auth: bedrock` just **warn** (and are ignored) rather than
erroring, since the auth mode may legitimately be set to bedrock in a config file
and overridden back to `keychain`/`oauth-token` on the CLI over a file that also
set the AWS knobs.

## Workflow verbs

The opinionated front door. `start`/`resume`/`shell` take an **optional** `TOPIC`:
**with** a `TOPIC` they act on a git worktree of that name (the worktree workflow —
most work is meant to land on a branch that can be merged or PR'd); **without** one
they act on the **current directory** (no worktree), so the same verbs work whether
or not you want a branch. `finish` only makes sense against a worktree, so it still
**requires** a `TOPIC`. A bare `yolo` (no verb) is equivalent to `yolo start` (a
fresh session in the cwd). All run from inside a git repo (the cwd-mode verbs degrade
gracefully outside one — there's just no repo slug to label/find by).

- **`start [TOPIC]`** — *with `TOPIC`:* create a new worktree + branch `TOPIC` off
  `--base` (default `HEAD`; see `base` below) and launch a container with a fresh
  session named `TOPIC`; **errors if the worktree or branch already exists** (use
  `resume`). *No `TOPIC`:* a fresh (unnamed) session in the current directory.
- **`resume [TOPIC]`** — continue the most recent session (`claude --continue`).
  *With `TOPIC`:* on that existing worktree (**errors if it doesn't exist** — use
  `start`); `--new` starts a fresh named session there instead. *No `TOPIC`:* in the
  current directory. `-r [ID]` (either mode) resumes a specific session / opens the
  picker. `--new` is worktree-only (for the cwd, a fresh session *is* `start`).
- **`shell [TOPIC]`** — a bash shell. If a container is **running** (label match —
  by worktree for `TOPIC`, by cwd otherwise) → `docker exec -it <id> /bin/bash`;
  otherwise a fresh ephemeral container with `--entrypoint /bin/bash`. Either way
  the prompt is yolo-flagged: every launch exports `YOLO_PS1` (`_ps1_env_args`),
  which the image's `.bashrc` adopts, giving `yolo:<dir>$`. In worktree mode PS1
  rewrites the long worktree prefix of `$PWD` at prompt time (via the
  `YOLO_WT_DIR`/`YOLO_WT_LABEL` env vars in a `${PWD/#…/…}` expansion) to a short
  label (`_worktree_ps1_label`): the `~/.claude-yolo/worktrees/` root and the
  prefix shared by *all* repo slugs under it are dropped, e.g.
  `claude-yolo/fix-auth`; with a single slug the label is just the topic. The
  exec'd case works because `docker exec` inherits the container's run-time env —
  so the env vars are stamped on *every* launch, not just `shell` ones.
- **`finish TOPIC`** — `git worktree remove` the worktree, **keep the branch**.
  Refuses if a container is running, or on uncommitted changes (unless `--force`).
  Leaves transcripts (they self-expire via `cleanupPeriodDays`). Prints whether
  the kept branch is pushed.
- **`list`** — the repo's worktrees as a table (TOPIC/BRANCH/STATUS/DIRECTORY).
  STATUS is `running`/`dirty`, else `merged`/`unmerged` (idle+clean) judged by
  whether the branch is reachable from **`base`** — exactly `git branch --merged
  <base>` (default `base` is `HEAD` = the main checkout; honours
  the `base` config key/`--base`). So a fast-forward-merged or never-diverged branch reads
  `merged`; a *squash*-merge isn't reachable and reads `unmerged`. `do_list` runs
  the check in the main repo (not `git -C <worktree>`) so a `HEAD` base resolves
  to the main checkout, not the worktree's own branch.

Implementation shape:

- **Dispatch is two-tier** (`main`). `config` runs off the *first* `parse_args`,
  before the config files are layered in, so a broken config can't block fixing
  the config (and its sentinel re-parse needs pristine parser defaults).
  Everything else re-parses with the config defaults layered in first. The other
  terminal verbs (`list`, `tokens`, `forget-token`, `finish`, `setup-token`, and
  `shell`'s exec-into-running case) then handle-and-return — `setup-token` sits
  after the config-dir resolution specifically so it caches the token under the
  right per-dir service name, while `forget-token` is dispatched *before* the
  config-dir-must-exist check (forgetting a token for a deleted config dir must
  work). Launch verbs (`start`, `resume`, `shell`-fresh, and a
  bare `yolo`) pass the guardrails (home refusal, `require-project-entry` — see
  the orthogonal-flags section), then call `launch_container`; extra mounts are
  resolved only on these paths, so a stale mount path can't break
  `list`/`finish`/`config`.
- **`launch_container`** is the single assembly+exec path shared by every launch
  (extracted from the old inline `main`): mounts (cwd + the extra `--mount`
  dirs), ssh-agent block, the credential/config blocks, labels, `--entrypoint`
  override, then `os.execvp`. It takes `container_base`, `command` (args after
  the image), optional `entrypoint`, and the resolved `mounts`.
  `build_claude_args` builds the `claude` command (settings, built-in prompt,
  `--add-dir` per extra mount, `--continue`/`--resume`, `--name`).
- **Containers are found by docker label, not name.** Every launch is stamped
  `--label yolo.repo=<repo-slug>`, `--label yolo.cwd=<cwd>`, and (for worktrees)
  `--label yolo.worktree=<topic>`. `running_container_for(slug, topic=None, *,
  cwd=None)` queries `docker ps --filter label=…`: by `yolo.worktree` for a worktree
  `shell`/`finish`/`list`, by `yolo.cwd` for a plain cwd `shell`. The cwd filter is
  what disambiguates a current-directory container from this repo's worktree
  containers (they share a repo slug but run under different paths). Robust to the
  `-{config}`/`-{profile}` name suffixes.
- **Verb dispatch / topic-optionality** (`main`). `finish` without a `TOPIC` errors;
  `start`/`resume`/`shell` without one run in the cwd. A bare invocation (no verb) is
  normalized to `start`. The single launch path then branches on whether a `TOPIC` is
  set: `_worktree_dir`/`setup_worktree` for a worktree, or `_repo_slug_or_none()` +
  `cwd.name` for the cwd.
- Verb-only flags: `--base REF` (config-backed via the `base` key; consumed by
  `start` and `list`), `--new` (resume, worktree-only), `--force` (finish),
  `--resume`/`-r` (resume). Each is validated against its verb in dispatch (e.g. `-r`
  outside `resume`, `--new` without a `TOPIC`, or `--new` with `-r` all error).

## The worktree mechanics (`setup_worktree`)

When a verb gets a `TOPIC`, this is what backs it. Orthogonal to the credential
modes (composes with any of them). `setup_worktree` creates a git worktree on a new
branch `TOPIC` (off `base`, default current `HEAD`, no upstream) at
`~/.claude-yolo/worktrees/<repo-slug>/TOPIC`, where `<repo-slug>` is the main repo
path slugified the way Claude names `~/.claude/projects/` buckets
(`re.sub(r"[^a-zA-Z0-9]", "-", path)`, factored into `_repo_paths`). `start` is its
sole caller and guards existence (`worktree.exists() or _branch_exists(topic)`)
*before* calling it, so `setup_worktree` always creates fresh — a single
unconditional `git worktree add -b`. `resume`/`shell` don't call it; they locate the
existing worktree via `_worktree_dir`. `main` then retargets `cwd` to the worktree
(so `-w` and the `{cwd}:{cwd}` mount point there) and **additionally mounts the
shared `.git` at its identical host path** — both same-path mounts are required
because a linked worktree stores *absolute* paths to its `.git` and back. The session
is named via `claude --name TOPIC`. Durability is the point: commits land in the
host's shared `.git` and uncommitted edits live in the host worktree dir, so a
container exit loses nothing. Must be run from inside a git repo.

## Resuming a session (`resume`, `--resume [SESSION_ID]` / `-r`)

Resuming is the `resume` verb's job (there is no longer a bare `--continue`/`-c`
flag; it was retired in favour of the verb). A plain `resume` forwards
`claude --continue` (most recent session); `resume -r [ID]` forwards
`claude --resume [ID]`, opening Claude's interactive picker when given no ID (works
because we run `-it`). `-r` is **only** valid with `resume` (argparse default `None`;
dispatch errors otherwise). Resuming needs no new mounts: session transcripts live
in `~/.claude/projects/<slug>/*.jsonl`, which is already bind-mounted, and the
slug is derived from the project path — which matches host↔container because the
cwd is mounted at its identical path. So a session started in a yolo container
(or even on the host, same dir) is resumable. With a `TOPIC`, resume is keyed to the
worktree's path. The `--name` injection is **suppressed** when resuming, because
`claude` rejects `--name` alongside `--continue`/`--resume` (the session already has
its identity); `resume TOPIC --new` is the exception — it *does* name a fresh
worktree session and so omits the resume flags.

## Conventions / gotchas

- **macOS only as written; Docker Desktop or OrbStack as the engine.** Credential
  extraction uses the macOS `security` CLI. SSH agent forwarding (on by default,
  disabled with `--no-ssh-agent`) mounts the Docker engine's
  `/run/host-services/ssh-auth.sock` (the VM-side socket the engine proxies to
  the host agent — both Docker Desktop and OrbStack expose it at that path), NOT
  the raw host `$SSH_AUTH_SOCK` — that socket's listener lives in the macOS kernel
  and is unreachable from the container's Linux VM (the mounted inode is dead:
  `connect()` → ECONNREFUSED). The host must have a running ssh-agent for
  forwarding to work. The engine socket is mounted `srw-rw---- root:root`, so the
  in-container `claude` user (uid = host uid, a non-root gid) can't `connect()` to
  it by default — `connect()` needs write perm on the socket inode, and the user
  is neither owner nor in group 0. Fix: `useradd -G root` puts `claude` in group 0,
  granting the socket's group-rw. No real privilege added (the user already has
  NOPASSWD sudo; the container is the sandbox).
- **GitHub HTTPS git is rewritten to SSH so it reuses the agent.** The image bakes
  `git config --system url."git@github.com:".insteadOf "https://github.com/"`, so
  in-container git operations on `https://github.com/...` remotes (fetch *and* push)
  transparently route over SSH and authenticate via the forwarded ssh-agent — **no
  token ever enters the container**. This is the only HTTPS-auth approach that keeps
  the secret-never-in-container property: HTTPS auth is a bearer token (the token
  must reach whoever makes the request), whereas SSH is challenge-response (the key
  stays on the host, the agent only signs). The host's `osxkeychain` credential
  helper is a macOS binary backed by the macOS Keychain — neither exists in the
  Linux container, which is the other reason plain HTTPS push can't work here. Host
  config is untouched (we never mount `~/.gitconfig`); remotes can stay HTTPS.
- **In-process sandbox is disabled deliberately — the *container* is the
  sandbox.** We append `--settings '{"sandbox":{"enabled":false}}'` to the claude
  args so that, when the mounted `~/.claude/settings.json` has
  `sandbox.enabled: true`, Claude doesn't warn at startup that `bubblewrap`/`socat`
  are missing and run unsandboxed. `--settings` is a container-only overlay (host
  settings untouched). Do NOT instead install `bubblewrap` to "fix" it — a default
  Docker container can't create unprivileged user namespaces (`bwrap: No
  permissions to create new namespace`), and granting that capability would weaken
  the very isolation this tool exists to provide. (A `/doctor` sandbox note may
  still appear; that's expected.)
- **Argument splitting:** `main` splits `sys.argv` on `--` *before* argparse
  sees it. Everything after `--` is appended to `docker run` last, so
  user-supplied flags win (last-one-wins).
- **`--append-system-prompt` / `-p`** is repeatable and is added *on top of* a
  built-in prompt telling Claude it's in an ephemeral Ubuntu container.
- **Git identity is forwarded as env vars, not a mounted gitconfig.**
  `git_identity_args` reads the host's *effective* `user.name`/`user.email` (so a
  repo-local identity wins) and exports them as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`.
  Mounting `~/.gitconfig` instead would drag in macOS-only bits (osxkeychain
  credential helper, GPG signing) that break commits in the Linux container. Note
  these env vars override any repo-local identity set *inside* the container.
- The container name is the cwd basename (or `{main_repo_name}-{TOPIC}` for a
  worktree), then suffixed with `-{config-dir-basename}` when
  `--config-dir` is set and `-{aws-profile-or-"bedrock"}` under `--auth bedrock`.
  Suffixes stack, so the axes compose in the name too.
- The `# https://claude.ai/chat/...` URL on line 2 and the upstream gist
  reference in git history are the script's provenance — this started as
  Migurski's gist.

## Development

`pyproject.toml` defines a **uv-managed project** with no runtime dependencies.
Its `dev` dependency group carries the only deps — `ruff` and `pytest`. The
project *is* packaged (hatchling build backend, `[project.scripts] yolo =
"yolo:main"`, wheel ships only `yolo.py`) so it can `uv tool install`, but the
runtime module stays stdlib-only — packaging adds no runtime dependency. `uv.lock`
is committed; `.venv/`, `dist/`, and the tool caches are gitignored.

```bash
uv sync                 # create/refresh .venv with the dev tools
uv run pytest           # run the test suite (tests/)
uv run ruff check .     # lint
uv run ruff format .    # format
uv build                # build the wheel/sdist into dist/ (for publishing)
uv run bump-my-version bump patch   # version bump (or minor/major): commit + tag
```

Version bumps are automated with **bump-my-version** (configured under
`[tool.bumpversion]` in pyproject.toml). One command updates the version in
`pyproject.toml` *and* the project's own entry in `uv.lock` (so the lockfile
doesn't go stale), then commits both and tags `v{new_version}`. It requires a
clean working tree. The pyproject search is line-anchored (regex) so it doesn't
also match the `current_version` line in the bumpversion config itself; the
uv.lock search is two-line (`name = "claude-yolo"\nversion = ...`) so it can't
hit a same-versioned dependency.

Tests load `yolo.py` via `importlib` **from its file path** (not a plain
`import yolo`) so each test gets a **fresh module instance** — `main()` mutates
the module-global `PARSER` through `set_defaults`, so isolation matters; loading
from the path also pins the tests to the source file regardless of any installed
`yolo`. They
never touch the host or Docker: `tests/conftest.py`'s `run_cli` fixture stubs
`build_docker_image`, `ensure_logged_in`, `extract_credentials`,
`ensure_oauth_token`, `git_identity_args`, and `os.execvp`, then asserts on the
captured `docker run` argv. `test_config.py` covers config parsing/merging
(`~/.yolo.json` + `projects.json`), mount-spec parsing, the stale-state
warnings, and the `config` verb; `test_cli.py` covers verb dispatch and arg
assembly across the credential/config axes, extra mounts, and the guardrails.
`test_verbs.py` covers the worktree verbs against a
**real throwaway git repo** (so the actual `git worktree` machinery runs),
stubbing only `running_container_for` (docker) plus the `run_cli` side effects.
`test_tokens.py` covers the token registry, the `_keychain_mdat` parsing and
expiry warning, the implicit-mint consent prompt, and the `tokens` /
`forget-token` verbs (the `security`-wrapping helpers stubbed).
Keep them green when changing flags or mounts.
