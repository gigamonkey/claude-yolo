# Extra directory mounts (`--mount` / `mounts`) + host-side-only config

## Goal

Let a yolo container see additional host directories ("reference" dirs) beyond
the cwd/worktree — via a repeatable CLI flag and via config — without creating
a channel by which Claude *inside* a container can grant its next session new
host access.

Today's escape hatch (`yolo -- -v /ref:/ref:ro`) already works but is CLI-only,
unergonomic, and defaults to rw. This feature adds first-class, configurable,
read-only-by-default mounts.

## Threat model → design driver: no config inside the mount, period

Writable container mounts today: the cwd (or worktree dir), the shared `.git`
(worktree mode), `~/.claude` (or `--config-dir`), and `~/.claude.json`.

The nearest-`.yolo.json` search starts *at* the cwd, so the in-directory
config file can live **inside the mounted tree**. Two attack classes follow:

1. **Self-jailbreak:** Claude writes `{"mounts": ["~"]}` (or, *already today*,
   `{"config-dir": "~"}` — an arbitrary rw mount at `/home/claude/.claude`)
   into `cwd/.yolo.json`; the user's next launch silently grants it.
2. **Cloned repo:** a `.yolo.json` committed in someone else's repo applies
   their config to your machine the first time you run `yolo` there.

Rather than classifying keys as trusted/untrusted and policing file locations,
**in-directory `.yolo.json` is removed entirely.** All config is host-side by
construction — outside every container mount — so the invariant is structural:

- **`~/.yolo.json`** — global defaults (not mounted; safe).
- **`~/.claude-yolo/projects.json`** — per-project config (not mounted; only
  `~/.claude-yolo/worktrees/<slug>/<topic>` dirs are. Note `~/.claude` is
  mounted rw — trusted config must never live there).

The degenerate edge — running yolo with cwd at/above `$HOME`, which would
mount the trusted config files themselves (plus `~/.ssh` and shell rc files)
read-write — is closed by a default-on hard refusal; see §5 Guardrails.

## Design

### 1. CLI flag

`--mount PATH[:ro|:rw]`, repeatable (`action="append"`, dest `mounts`).

- Mounted at the **identical host path** (`-v {src}:{src}:{mode}`), same
  philosophy as the cwd mount — paths in transcripts/sessions match the host.
  No custom destination; `yolo -- -v src:dst` remains the escape hatch.
- **Default `ro`** (the use case is reference material); `:rw` is the explicit
  opt-in.
- `~` expanded, path resolved. Must exist and be a directory — otherwise
  docker silently creates a root-owned dir on the host. Hard error on
  missing/non-dir, per house style.
- Mode parse: `rsplit(":", 1)` only when the suffix is exactly `ro`/`rw`.

### 2. Config: two host-side layers, no in-directory file

Layering (low → high): `~/.yolo.json` < matching `projects.json` entry < CLI.

- **`~/.yolo.json`**: same flat `.yolo.json` schema as today (all `YOLO_KEYS`),
  now also accepting `mounts` (string or list of strings, `PATH[:ro|:rw]`,
  `~`-expanded).
- **`~/.claude-yolo/projects.json`**: a JSON object mapping an absolute (or
  `~`-relative) directory path to a `.yolo.json`-style object of the same
  keys (validated by the same `_parse_yolo_file` logic). An entry applies
  when the real cwd (pre-worktree retargeting, as today) is at or under the
  key path; if several keys match, the longest (nearest) wins and only that
  entry is used — preserving the old nearest-file semantics, including
  running from a subdirectory. Keyed by path, not repo slug: hand-editable,
  greppable, no git dependency, one file shows every project's config (and
  every mount grant) at a glance.
- `mounts` and `append-system-prompt` **concatenate** across layers and the
  CLI; every other key overrides. For mounts: exact duplicates deduped,
  same-path/different-mode → highest layer wins.
- **Migration/deprecation:** `.yolo.json` at/above the cwd is no longer read.
  If one is found, print a loud warning naming the file and the exact
  `projects.json` key to migrate to (the cwd / git toplevel). Warn, don't
  error — the file is now inert, and a hard error would block running yolo in
  any repo with a leftover or committed copy.

### 3. `init` verb replaced by `config`: persist flags as the project's entry

`yolo config [CONFIG FLAGS]` writes the explicitly-passed config-backed flags
into the project's `projects.json` entry (keyed by git toplevel when in a
repo, else the cwd), creating the file/dir if needed, then exits (a terminal
verb — no launch). `yolo config --config-dir ~/.claude-work --mount
~/refdocs` persists exactly that entry. This matches the workflow: experiment
with flags on the CLI, then re-run with `config` prepended to pin them. The
verb is named `config` (not `init`) to match `git config`: it's re-runnable
with per-key-update semantics, and bare invocation lists rather than writes.
`init` is removed from the verb choices.

- **Only `YOLO_KEYS` flags persist** (config-dir, auth, aws-*, claude-json,
  ssh-agent, base, append-system-prompt, mounts) — never per-invocation
  actions (verbs, `-r`, `--new`, `--force`, `--rebuild-image`).
- **Only explicitly-passed flags** are written. Implementation: the init path
  re-parses argv against sentinel defaults for every config-backed dest
  (`set_defaults(**{dest: SENTINEL})`), since a normal parse can't
  distinguish "defaulted" from "explicitly set to the default value".
- **Existing entry → per-key replace**: `config --mount X` sets that entry's
  `mounts` to `["X"]`, leaving other keys alone (idempotent; "add a mount" is
  a hand-edit or a re-run with the full list). Malformed `projects.json` →
  error with a pointed message, never clobber.
- **Bare `yolo config` (no flags) is read-only**: print the project's current
  entry (or "no entry") plus the `projects.json` path, à la
  `git config --list`. This replaces the old init scaffold/null-template as
  the discoverability story — no more flagless writes, and
  `YOLO_INIT_DEFAULTS` goes away entirely (the built-in defaults live only
  in argparse).
- **Validate at config time**: `--mount` values go through `_parse_mount_spec`
  (exists/is-dir) before persisting, so a typo can't be pinned.
- A plain `yolo` run never writes to `projects.json` — no auto-added null
  entries. The file doubles as the audit ledger of mount grants; keeping it
  signal-only (and launches side-effect-free) is deliberate.
- The two-tier dispatch in `main` (`config` before the config load) stays;
  `config` reads only `projects.json` itself and fails with a pointed message
  rather than a generic config-load failure.

### 4. Container/claude wiring

- `launch_container`: append `-v {src}:{src}:{mode}` per mount, after the cwd
  mount. No container-name suffix.
- `build_claude_args`: pass `--add-dir {src}` per mount so the directories are
  first-class working dirs Claude can see in `/context` — a mount Claude
  doesn't know about would only get used if the user remembers to mention it.
  (ro mounts + `--add-dir` is fine; writes fail at the filesystem.)
- Applies to all launch paths (`start`/`resume`/fresh `shell`). `shell` into a
  *running* container necessarily joins it with the mounts it was started
  with — docker can't add mounts to a live container; document this.

### 5. Guardrails

- **`require-project-entry`** (bool, default false; an ordinary `YOLO_KEYS`
  bool with `--require-project-entry/--no-require-project-entry` flags, so a
  CLI override works for a deliberate one-off). Intended home: `~/.yolo.json`
  (in a `projects.json` entry it's trivially satisfied, hence inert). When
  true, container-launching verbs (`start`/`resume`/fresh `shell`/bare
  `yolo`) refuse to start unless a `projects.json` entry matches the cwd;
  the error names the cwd and suggests `yolo config` (or the `--no-` flag).
  This is the hard-mode answer to the rename caveat: a renamed project
  *fails* instead of silently falling back to global defaults. Exempt:
  `config` (must work in an unconfigured dir to create the entry), `list`,
  `finish`, `setup-token`, and `shell` into a running container (no new
  mounts).
- **Home-directory refusal, default ON**: launching with cwd at or equal to
  *any ancestor of* `$HOME` (i.e. `cwd == $HOME` or `$HOME` under cwd) is a
  hard error, overridable only by a CLI-only `--dangerously-allow-home` flag (not a
  config key — a persistent allow would quietly defeat the guard; the
  friction is meant to be per-invocation). Rationale: such a launch mounts
  `~/.ssh`, shell rc files (host code execution on the next shell), and the
  trusted config files themselves (`~/.yolo.json`, `~/.claude-yolo`) into a
  skip-permissions container — it dissolves the security model and is
  almost always a `cd` mistake. This closes the degenerate edge that was
  previously documented-not-coded.

## Implementation steps

1. `YOLO_KEYS["mounts"] = ("mounts", "mounts")`; new `"mounts"` kind in
   `_parse_yolo_file` (string-or-list, per-item syntax + `~` validation).
   Delete `YOLO_INIT_DEFAULTS` (built-in defaults live only in argparse).
2. `load_yolo_config`: drop the ancestor walk; load `~/.yolo.json`, then the
   longest-matching `projects.json` entry; concatenate `mounts` like
   `append_system_prompts`. Add the leftover-`.yolo.json` deprecation warning
   (check cwd ancestors, warn only), the dangling-key warning (any
   `projects.json` key whose directory no longer exists), and the
   provenance line (which layers applied). Malformed `projects.json` →
   `sys.exit` with path, matching `_parse_yolo_file` style.
3. `PARSER.add_argument("--mount", action="append", dest="mounts", ...)` —
   mirror the existing `-p`/`append_system_prompts` set_defaults-concat
   pattern (including its double-parse handling in `main`).
4. `_parse_mount_spec(spec) -> (Path, mode)` helper: expand, resolve,
   exists/is_dir check, mode default `ro`; dedupe + mode-conflict resolution
   across the merged list.
5. `launch_container` + `build_claude_args` wiring (`-v` specs, `--add-dir`).
   Guardrails in `main` before any launch: home-refusal check (cwd at/above
   `$HOME`, `--dangerously-allow-home` CLI-only override) and `require-project-entry`
   enforcement (launch verbs only; `YOLO_KEYS["require_project_entry"]` +
   bool flag pair).
6. Replace `init`/`write_default_yolo` with the `config` verb (per §3):
   sentinel-defaults re-parse to collect explicit flags, per-key entry
   update, bare-`config` read-only print.
7. Tests: `test_config.py` — projects.json parsing/matching (incl.
   longest-key-wins, `~` keys), two-layer merge + mounts/prompt concat,
   deprecation warning on leftover `.yolo.json`, dangling-key warning +
   provenance line, `config` verb behaviors (flags → entry, per-key update
   of existing entry, bare `config` prints without writing and flags stale
   keys, mount validation at config time, malformed file), guardrails
   (`require-project-entry` blocks bare `yolo` without an entry but not
   `yolo config`, `--no-` override works; home refusal at `$HOME` and at an
   ancestor of `$HOME`, `--dangerously-allow-home` override);
   `test_cli.py` — `-v` specs with ro/rw in captured docker argv, `--add-dir`
   in claude args, CLI-over-config concat, missing-dir error.
8. Docs: CLAUDE.md + README — rewrite the `.yolo.json` section as the
   two-layer host-side scheme, new mounts section + threat-model note,
   replace the in-repo `config-dir` example, note the `shell`-into-running
   limitation.

## Decisions (settled 2026-06-10)

1. **In-directory `.yolo.json` is removed** — config is `~/.yolo.json`
   (global) or `~/.claude-yolo/projects.json` (per-project) only. This
   replaces the earlier trusted-only-keys design and also closes the
   pre-existing `config-dir` hole and the cloned-repo vector. Leftover files
   warn (inert), not error.
2. **Default mode is `ro`**; `:rw` is the explicit opt-in.
3. **Flag name is `--mount`** (config key `mounts`).
4. **`init` is replaced by `config`** (named to match `git config`), which
   persists explicit CLI config flags into the project's `projects.json`
   entry (per-key replace); bare `config` prints the current entry read-only.
   Plain runs never auto-add entries (keeps the file a signal-only grant
   ledger and launches side-effect-free).
5. **`require-project-entry`** opt-in guardrail (launch verbs refuse without
   a matching `projects.json` entry) — the hard-mode mitigation for the
   rename caveat.
6. **Refusing to launch at/above `$HOME` is default-on** (not opt-in as first
   floated), overridable per-invocation via CLI-only `--dangerously-allow-home`.

## Known caveat: path-keyed entries don't survive renames

`projects.json` keys are directory paths, so moving/renaming a project (or a
mounted reference dir) requires editing the entry by hand.

How each piece fails, and the mitigations:

- A moved mount *value* fails **loud**: the exists-check errors at launch.
- A stale *key* is fail-safe only for mount grants (nothing new is granted) —
  but for keys like `auth`/`aws-profile`/`config-dir` it fails **silently in
  the dangerous direction**: the renamed project falls back to the global/
  default setup, defeating exactly the account/profile isolation the entry
  was for. Two mitigations:
  1. **Dangling-key warning**: at config load (every launch, plus bare
     `yolo config` output), warn for any `projects.json` key whose directory
     no longer exists — the signature of a rename/move/delete. One stat per
     entry. This only works because entries are never auto-created (decision
     4): every key is a deliberate claim about a real directory, so a
     dangling one is always actionable, never noise. When the cwd *also* has
     no matching entry, the warning connects the dots explicitly ("if this
     directory used to be one of these, re-run `yolo config` here and remove
     the stale entry") — the rename case produces exactly that combination,
     in the renamed directory itself. Deliberately a warning, not an error:
     a hard stop on dangling keys would let one stale entry from a deleted
     project block launches everywhere.
  2. **Provenance line at launch**: print which layers applied, e.g.
     `config: ~/.yolo.json + projects.json[/Users/peter/hacks/foo]` or
     `config: ~/.yolo.json (no project entry)` — makes a silent fallback
     visible in the renamed directory itself.
- Undetectable residue (accepted): a *new* project created at the *old* path
  inherits the old entry — path identity is genuinely ambiguous there.
- Path-as-identity matches the rest of the tooling: Claude Code's
  `~/.claude/projects/` buckets and `~/.claude-yolo/worktrees/<slug>` break
  identically on rename (slugs are just encoded paths). Rename-proof identity
  would need an in-repo ID file (back inside the tamperable mount) or a
  git-derived key; not worth it.
