# Plan: keychain-backed secrets + temp-file cleanup hardening

**Status:** draft / in discussion. More issues to fold in before implementing —
see *Open questions* at the end. Nothing here is built yet.

## Goal

Let the user store arbitrary secrets (PATs, API keys, etc.) in the macOS keychain
and have yolo inject them into a session's container — as **mounted files**
(preferred) or **env vars** (opt-in) — without ever writing a plaintext secrets
dotfile, and without leaving plaintext secret files lying around on the host.

Two threads, related but separable:

1. **The secrets feature** — keychain storage + a `secret` verb family + a
   `secrets` config key + launch-time injection.

2. **Temp-file cleanup hardening** — fix the fact that yolo's mounted credential
   files already leak onto the host disk, and make the secrets feature not add to
   that. Independently useful; groundwork for the feature.

## Context: what already exists (v0.14.0)

The substrate for *file* injection is already shipped:

- **`--yolorc PATH`** (`yolorc` config key) sources a shell file inside the
  container at startup — e.g. `gh auth login --with-token < tokenfile`. Claude
  launches are command-wrapped to source it then `exec claude`; `yolo shell`
  sources it via the baked `.bashrc`. Opt-in via a host-side key (a cloned repo's
  `.yolorc` is inert unless the key points at it).

- **`--mount` now accepts a file** (not just a directory), bind-mounted read-only
  at its identical host path; files are *not* forwarded to claude as `--add-dir`
  (that's dir-only).

So today a user can already do the manual version: `yolo --mount ~/secrets/gh-token
--yolorc ./setup.sh`. This plan is about making that first-class and keychain-backed
instead of plaintext-file-on-disk.

The keychain plumbing also already exists, specialized to two services:

- `extract_credentials` reads `Claude Code-credentials[-{hash8}]` via `security
  find-generic-password -s SVC -w`.

- OAuth-token helpers (`_oauth_service`, `_read_oauth_token`, `_store_oauth_token`,
  `_keychain_has`, `_keychain_delete`, `_keychain_mdat`) read/write/inspect
  `claude-yolo-oauth-token[-{hash8}]` via `add-generic-password -U` /
  `find-generic-password` / `delete-generic-password`.

- A **registry** `~/.claude-yolo/tokens.json` maps service → metadata, because the
  keychain can't enumerate yolo's items or map a hash back to its config dir.
  Host-side only, never mounted (same property as `projects.json`).

## How the macOS keychain organizes secrets (the model)

- **No hierarchy / folders.** A keychain (we care about the user's
  `login.keychain-db`) holds a flat list of **items**. We use **generic
  passwords** (`security ...-generic-password`).

- **Identity = attributes.** The de-facto primary key of a generic-password item
  is the pair **service** (`-s`, `svce`) + **account** (`-a`, `acct`). A second
  `add` with the same pair fails unless `-U` (upsert). Other attributes are
  metadata: **label** (shown in Keychain Access.app), kind/comment, and timestamps
  **`cdat`/`mdat`** (yolo already reads `mdat` for token-expiry warnings). The
  secret payload is read with `find-generic-password -s SVC -w`.

- **"Organization" is a naming convention you impose**, not structure the keychain
  provides. We pick a service/account scheme.

- **No enumeration by prefix.** There is no "list all items whose service starts
  with X". `security dump-keychain` dumps everything (and `-d`, to include secrets,
  prompts per item). This is *why* a side registry is mandatory — exactly the
  reason `tokens.json` exists.

- **ACL / access prompts.** Items added by the `security` CLI are readable by that
  same CLI without a GUI prompt (why yolo reads its own tokens silently), but a
  locked keychain prompts to unlock, and reading N secrets at every launch is N
  reads. To validate, not assume — see *Open questions*.

## Part 1 — the secrets feature

### Storage scheme — global + project scope (DECIDED)

Secrets have **two storage scopes**, global and **project** (not config-dir — that's
the Claude-account axis, the wrong one; and not worktree — worktrees are ephemeral,
so a value that dies with one is an odd thing to store). At injection a referenced
name resolves **most-specific-first: project, then global.** (Worktree sessions
share their main repo's project scope, since `_project_key` follows the shared
`.git` to the main repo root — consistent with how a worktree's project config
entry is found.)

- **Keychain service per (scope, name):** global → `claude-yolo-secret-{name}`;
  project → `claude-yolo-secret-{project-hash8}-{name}`, where `project-hash8` is the
  first 8 hex of the SHA-256 of the project key (the main repo root path from
  `_project_key`) — the same hashing idiom as the per-config-dir token service.
  Upsert with `add-generic-password -U`, mirroring `_store_oauth_token`.

- **Registry** `~/.claude-yolo/secrets.json`: keyed by service → metadata (scope,
  project key, created/modified), **never the value**, host-side only, never mounted
  — same safety property as `tokens.json` / `projects.json`. As with tokens, the
  registry is what enumerates secrets across scopes and maps a hashed service back to
  its project (the hash is one-way).

> Note: this stored-value scope is **independent of** the *injection* scope — which
> sessions get a secret is still controlled by which config layer (global / project
> / worktree) names it in the `secrets` key. The two hierarchies compose: the config
> layer decides *whether* a name is injected here; the storage scope decides *which
> value* that name resolves to.

### Verbs (mirroring the token verbs)

- `yolo secret set NAME [--project]` — keychain upsert + registry entry, at **global
  scope by default** or **project scope** with `--project` (keyed to the current
  repo's project key). The value is **never** passed as a CLI argument (that would
  leak it into shell history and the process argv visible in `ps`). Three input
  sources instead:

  - **stdin** when not a TTY — `... | yolo secret set NAME` (piping);
  - **interactive prompt** (no-echo, like a password prompt) when stdin is a TTY;
  - **`--clipboard`** — read the value straight from the macOS clipboard (`pbpaste`),
    for the common "I just copied the token from a web page" case. The clipboard is
    **left as-is** (not cleared, no warning) — its contents are the user's business.

  Secret **NAME is validated as a shell identifier** (`[A-Za-z_][A-Za-z0-9_]*`) at
  set time, because it becomes an env var name in the container (see *Injection*).

- `yolo secret list` — registry-backed table (NAME / SCOPE / CREATED / status
  reconciled against the keychain via `_keychain_has`), like `yolo tokens`; shows
  global secrets plus the current project's, with a `--all` to span every project's
  (the cross-project counterpart, like `list --all`).

- `yolo secret rm NAME [--project]` — delete keychain item (`_keychain_delete`) +
  registry row at the given scope (global by default, `--project` for the project's).

### Config key — `secrets`, a spec list (name → target)

All secrets live in the keychain; the **config decides which a session gets and how
each is injected**. A `secrets` config key (and matching repeatable `--secret` CLI
flag) is a **list/concat dest like `mounts`/`ports`** — it accumulates across the
global / project / worktree layers and the CLI. Each entry is a spec
`NAME[:TARGET]`, with **two injection targets** discriminated by whether TARGET
looks like a path (starts with `/` or `~`) — an env var name otherwise (which can't
start with either, so there's no ambiguity):

- **`NAME`** → inject as an **env var** named `NAME` (the common case; secret `NAME`
  from the keychain). Delivered via the `/run/secrets` loader (below).

- **`NAME:ENVNAME`** (TARGET is an identifier) → env var, but **renamed** to
  `ENVNAME` (e.g. `DB_PASSWORD:PGPASSWORD`).

- **`NAME:/abs/path`** or **`NAME:~/path`** (TARGET starts with `/` or `~`) →
  **mount as a file** at that container path (e.g.
  `DEPLOY_KEY:~/.ssh/id_ed25519`). **`~` expands to the *container* home
  (`/home/claude`), not the host home** — this is a container-side path, so yolo
  substitutes the container home explicitly rather than calling
  `os.path.expanduser` (which would wrongly resolve to the host's `$HOME`, the
  convention every *other* yolo path key uses). `~/.ssh/id_ed25519` →
  `/home/claude/.ssh/id_ed25519`.

Example:

```json
"secrets": ["GH_TOKEN", "DB_PASSWORD:PGPASSWORD", "DEPLOY_KEY:~/.ssh/id_ed25519"]
```

This is the **opt-in gate**: a secret in the keychain is injected only where a
config layer names it. Same trust model as `--yolorc` / `--dockerfile` (the *key* is
host-side; Claude can't grant its next session a new secret).

**Concat / dedup** (mirroring `mounts`/`ports`): specs accumulate across layers;
exact-duplicate specs are deduped; on a **target collision** (two specs hitting the
same env var name, or the same mount path) the **higher layer wins**. A secret
needed *both* ways is just two specs (`GH_TOKEN` and `GH_TOKEN:/some/path`).

**Validation** at launch (on the launch paths only, like mount/port resolution): the
secret must exist in the keychain (registry + `_keychain_has`); an env TARGET must be
a shell identifier; a file TARGET must be absolute or `~`-rooted (then expanded to
`/home/claude/…`). A resolved file path **under the cwd or `~/.claude` bind mounts**
(`/home/claude/.claude/…`) should warn — it'd land in the host-visible working tree /
mounted config rather than a private container location.

### Injection at launch — two mechanisms, by spec target

A secret's spec target picks the mechanism: **env-target** secrets go through the
`/run/secrets` loader; **file-target** secrets are each mounted at their path. Both
read from the keychain and stage chmod-600 files in the session run dir (Part 2); in
**neither** case does the value touch the docker-run argv or `docker inspect`
(`-e NAME=value` would leak it into both, plus `/proc/1/environ` and tmux's retained
`#{pane_start_command}` — the cost the OAuth token already pays).

#### Env-target secrets — file transport, env by convention

The delivered form is an **env var**, but the **transport is a file**:

1. For each configured secret, yolo reads it from the keychain and writes a
   **chmod-600 file** `<run-dir>/<container>/secrets/<NAME>` (file name = the env
   var name), value written **without a trailing newline**.

2. The per-session `secrets/` dir is bind-mounted **read-write** at `/run/secrets`
   (rw is required so the loader can delete — see below; the dir holds only this
   container's own secrets).

3. A **baked loader** `/etc/yolo/load-secrets.sh` loops the dir, `export`s each file
   as `NAME=$(cat file)`, and (per the *ephemeral* decision below) `rm`s it:

   ```sh
   [ -d /run/secrets ] || return 0
   for f in /run/secrets/*; do
     [ -f "$f" ] || continue                  # skips the literal glob when empty
     export "$(basename "$f")=$(cat "$f")"
     rm -f "$f"                               # only if ephemeral (needs the rw mount)
   done
   ```

**Crucially the loader is sourced from two places**, because **claude sessions never
run `.bashrc`** (claude isn't a shell):

- the **claude launch wrapper** we already build for `--yolorc`, and
- the baked **`.bashrc`** (for `yolo shell`, fresh or `docker exec`'d), sentinel-
  guarded like `YOLO_RC`.

It must be **sourced, not executed** (so the `export`s land in the calling shell),
and run **before** `.yolorc`, so an rc can use the exported values directly
(`echo "$GH_TOKEN" | gh auth login --with-token`). The wrapper becomes roughly:
`. /etc/yolo/load-secrets.sh; [ -f "$YOLO_RC" ] && . "$YOLO_RC"; exec "$@"`. When no
secrets are configured there is no `/run/secrets` mount and the loader is a no-op.

**Keep-vs-delete (ephemeral): keep by default (DECIDED).** The loader exports but
does **not** delete; files persist for the session and the run-dir GC (Part 2)
reclaims them. A **per-secret `ephemeral` opt-in** enables delete-after-export for a
specific secret. Rationale: the backup/sync exposure is already handled by putting
the run dir in `$TMPDIR`, so blanket self-delete buys little, while it has a real
multi-consumer cost — the first process to run the loader (the claude wrapper at
container start) would delete the files, so a later `yolo shell` exec'd into that
running container finds `/run/secrets` empty and gets **no** env vars (an exec'd
shell inherits the container's create-time env, not claude's process env). Keeping
by default avoids that; `ephemeral` is there for the rare secret you want gone the
instant it's consumed. (Since `rm` is now opt-in, the rw `/run/secrets` mount is only
strictly needed when some env secret is `ephemeral` — but mount it rw uniformly for
simplicity. Spec syntax for the marker is a minor detail — e.g. a trailing `!` or a
`:ephemeral` modifier — settle at implementation.)

#### File-target secrets — mounted at a path

For a secret a tool must re-read as a *file* all session (an SSH key, a config
credential, a token re-read on each call), the `NAME:/abs/path` spec stages the
value as a chmod-600 file in the session run dir and **bind-mounts it read-only at
`/abs/path`**. These are **not** placed in `/run/secrets` (so the loader doesn't
export or delete them) and are **not** self-deleted — they persist for the session
exactly like `.credentials.json`, and are reclaimed by the run-dir GC (Part 2).

Note this is necessarily the "persistent" category: a **single-file bind mount is a
mountpoint and can't be unlinked from inside** (`EBUSY`), so the ephemeral self-
delete trick only ever applied to the dir-mounted env secrets. File-target secrets
rely on the GC alone.

### Threat model (be explicit)

Injecting into a `--dangerously-skip-permissions` container means Claude and any
code in it can read the secret — that is inherent and acceptable *because it's
opt-in per project*. The keychain buys **encrypted-at-rest storage + no plaintext
secrets dotfile**, not in-container secrecy.

## Part 2 — temp-file cleanup hardening

### The problem

A bind-mounted secret file must exist for the **entire container lifetime**. yolo
ends with `os.execvp(run_cmd[0], run_cmd)` (`_dispatch_launch`) — the Python
process is **replaced** by docker, so there is no `finally`/`atexit`/post-container
hook to delete anything. (In tmux mode yolo returns and exits, but the file still
must outlive it because the container in the tmux window does.)

**This already leaks today, for credentials.** `extract_credentials` and
`_masking_credfile` both `NamedTemporaryFile(..., delete=False)` and never unlink —
so every keychain/oauth/bedrock launch leaves a file in `$TMPDIR`. The secrets
feature would add more of the same. Not a new exposure *class*, but worth fixing
for both.

### What bounds the current exposure

- macOS `$TMPDIR` = `/var/folders/<xx>/<yyy>/T/`, a **per-user dir mode 700** —
  other users can't read it.

- Files are **chmod 600** (owner-only).

- macOS **periodically purges `/var/folders`** (~3 days unaccessed) and wipes on
  reboot.

So not world-readable plaintext — but they linger, which is sloppy for a
deliberately-stored secret.

### The fix: a per-session run dir + docker-ps GC

Cleanup can't be synchronous (execvp), so reclaim out-of-band, **parallel-safely**:

- The run dir is **per-session, keyed by container name**:
  `<run-dir>/<container>/` (with `secrets/`, the mounted creds/mask files, etc.
  inside), the dir tree mode **700**, files **chmod 600**.

- **GC at launch** removes only `<run-dir>/<dir>/` whose container is **not in
  `docker ps`** (crash/leftover sessions) — **never a blanket `rm` of the whole run
  dir**, which would nuke a *concurrently running* session's secrets. This is the
  correction to the naive "unlink everything" idea: with parallel sessions that's
  unsafe. It stays crash-proof (a `kill -9`'d session's dir is collected next launch
  because its container is gone). Same *start-of-launch* philosophy as the existing
  `.yolo-status/<slug>.state` reset.

- **Per-session self-cleanup** is the loader's `rm` for *ephemeral* secrets (live
  case) plus, optionally, an `atexit`-unlink of this session's dir in tmux mode
  (where yolo survives). The docker-ps GC remains the guarantee regardless.

- **Retrofit** this onto the existing `extract_credentials` / `_masking_credfile`
  files: write them under the session's run dir instead of `NamedTemporaryFile(
  delete=False)` in `$TMPDIR`, so they're collected by the same GC.

### Run-dir location — `$TMPDIR` subdir (DECIDED)

The per-session run dir is a yolo-owned subdir under **`$TMPDIR`**
(`/var/folders/.../T/…`, the per-user temp dir), e.g. `$TMPDIR/claude-yolo-run/
<container>/`. Chosen because the per-user temp dir is **already excluded from Time
Machine and not in synced folders** (Dropbox/iCloud), so a session-long plaintext
secret file isn't copied off the machine — which is the exposure self-deleting was
mostly trying to solve. (Rejected: `~/.claude-yolo/run/` — predictable and next to
the other state, but inside `$HOME`, hence the backup/sync risk; a `tmutil`
exclusion is per-tool and fragile.)

Caveats accepted: the macOS periodic cleaner can delete `/var/folders` files (~3
days unaccessed) — only a risk for very long-lived sessions, and bind-mounted files
have their atime touched, so likely moot; and the path is opaque, so the docker-ps
GC must locate `$TMPDIR/claude-yolo-run/` explicitly rather than relying on a fixed
home-relative path. Note `$TMPDIR` resolves **host-side** (where yolo runs); the
staged files are bind-mounted into the container at the fixed `/run/secrets` (env
targets) or the configured path (file targets), so the opaque host path never
matters in-container.

### Rejected alternatives

- **Parent-waits-and-cleans** (drop `execvp`, use `subprocess.run` + `finally`):
  works only for the non-tmux foreground case and forces yolo to own TTY/signal/
  exit-code propagation for an interactive `-it` session — exactly what `execvp`
  avoids; CLAUDE.md calls process-replacement deliberate.

- **tmpfs / `--tmpfs`:** gives the container an empty in-memory fs, not a way to
  inject a host *value* — a bind mount still needs a host source file.

- **FIFO / stdin:** bind-mounting a named pipe across the macOS→Linux-VM boundary
  is fragile, and yolo `execvp`s away so it can't be the writer; `docker run -i`
  stdin is the session TTY, can't carry a side-channel secret.

## Sketch of the moving parts (subject to the open questions)

- New: `_run_dir()` (Option A or B — see *Run-dir location*), `_session_run_dir(
  container)` (the per-container subdir, 700), `_gc_run_dir()` (docker-ps-scoped,
  called early on the launch paths); a `_write_secret_file(dir, name, value)` helper
  replacing the raw `NamedTemporaryFile(delete=False)` pattern, reused by the creds/
  mask retrofit.

- New baked image bits: `/etc/yolo/load-secrets.sh` written in `DEFAULT_DOCKERFILE`;
  `.bashrc` sources it (sentinel-guarded); the claude launch wrapper in
  `launch_container` sources it before `--yolorc` (`. /etc/yolo/load-secrets.sh; [ -f
  "$YOLO_RC" ] && . "$YOLO_RC"; exec "$@"`).

- New: `secrets.json` registry helpers mirroring the `tokens.json` ones; a
  scope-aware `_secret_service(name, scope, project_key=None)` (global vs project-
  hash8) and a `_resolve_secret(name, project_key)` that reads most-specific-first
  (project then global); `do_secret_set/list/rm` (the `--project` scope flag; set
  reads stdin / prompt / `--clipboard` via `pbpaste`, validates NAME as a shell
  identifier; `list --all` spans projects).

- New: `secrets` in `YOLO_KEYS` + a repeatable `--secret` flag (a list/concat dest
  like `mounts`/`ports`, so the layers accumulate); a `_parse_secret_spec(spec)` →
  `(name, target)` where target is `("env", ENVNAME)` or `("file", PATH)`
  (discriminator: TARGET starts with `/` or `~` → file, with `~` expanded to
  `/home/claude`; else env), with the dedup/collision rule. Resolution happens
  on the launch paths only: read each secret from the keychain, stage a chmod-600
  file in the session run dir, then in `launch_container` assemble **one rw
  `/run/secrets` mount** for all env-target secrets (consumed by the loader) plus
  **one ro file mount per file-target secret** at its path.

- Tests: a `test_secrets.py` (keychain + `pbpaste` stubbed like `test_tokens.py`;
  registry; the verbs incl. the three input sources and NAME validation;
  `_parse_secret_spec` for both targets + the `/`-or-`~` discriminator + `~`→
  `/home/claude` expansion (and that it does *not* use the host `$HOME`) + the
  collision/dedup rule; the `secrets` config-layer concatenation; the `/run/secrets`
  rw mount + loader wiring for env targets and the per-path ro mount for file
  targets; the under-cwd/`~/.claude` path warning; the docker-ps-scoped GC and its
  parallel-safety) + extend the credential tests to assert the run-dir retrofit.

## Decisions (resolved)

- **Keying / value scope:** first-class **global + project** stored-value scope
  (project resolves over global); *not* config-dir, *not* worktree-for-storage. See
  *Storage scheme*.

- **Run-dir location:** a yolo-owned subdir under **`$TMPDIR`** (backup/sync-
  excluded), not `~/.claude-yolo/run/`. See *Run-dir location*.

- **Keep vs delete (ephemeral):** env-target secrets are **keep-for-session by
  default** (GC reclaims them), with a per-secret `ephemeral` opt-in for
  delete-after-export. File-target secrets are always keep + GC (`EBUSY` on a
  single-file mountpoint). See *Env-target secrets*.

- **Opt-in default:** a secret is injected **only where a config layer names it**
  in `secrets` (no implicit injection from merely storing one). Storing a secret and
  granting a session access to it are kept separate — the host-side config is the
  sole grant. The global `secrets` list in `~/.yolo.json` is the "inject everywhere"
  escape hatch, opted into once.

- **Access prompts:** **probe before deciding** — write a secret via yolo's
  `security add-generic-password` path and confirm that reading it with
  `security find-generic-password -w` at launch does *not* raise a GUI prompt in the
  normal unlocked-login-keychain case (a `*-probe.sh`, like the token investigation).
  Default to **no special ACL** (`-A`/`-T` only if the probe shows a prompt). This is
  an **implementation prerequisite**, not a runtime decision.

- **`--clipboard` hygiene:** **do nothing** — after `pbpaste`, the clipboard is left
  untouched (not cleared, no warning); its contents are the user's business.

## Open questions

None outstanding — all resolved above. Remaining pre-implementation work is the
**access-prompt probe** (listed under *Decisions*) and the **delete-propagation
probe** (Part 2 — confirm a delete inside a shared dir reaches the host on the actual
engine). Both are empirical checks, not design choices.
