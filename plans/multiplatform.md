# Making claude-yolo multiplatform

## Goal

Today `yolo.py` runs only on a macOS host. The aim is to make it run on **Linux**
and **Windows** as well, without giving up the properties that define it: a
single stdlib-only PEP 723 file, the container as the blast-radius boundary, and
secrets that never touch the docker-run argv.

## The key framing: host OS vs. container OS

There are two operating systems in play and only one of them is changing.

- The **container is always Linux** (Ubuntu, from `DEFAULT_DOCKERFILE`). Its
  `useradd`, `apt-get`, group-0 membership, `/home/claude`, chmod-600 mounts, and
  the baked secret-loader are *container-internal* and do **not** depend on the
  host OS. None of that needs porting. A Windows or Linux host runs the exact same
  Linux container.

- The **host** is what's macOS-specific today: the macOS Keychain, `pbpaste`,
  `open`, the Docker Desktop ssh-agent socket path, `pty`/`termios`, `os.execvp`
  semantics, and `$TMPDIR`'s mode-700/no-Time-Machine guarantees. This is the
  entire porting surface.

So "multiplatform" means **porting the host-side glue**, not touching the
container or the docker-run contract.

## Recommended platform tiers

A native-Windows (cmd/PowerShell) port is a large surface for marginal benefit,
because Windows users can run the *Linux* build under WSL2 essentially for free.
Two supported tiers:

- **Tier 1 — macOS + native Linux.** Full support. This is the bulk of the work
  and where almost all the value is.

- **Tier 2 — Windows via WSL2.** Falls out of the Linux port for nearly free
  (WSL2 is Linux, Docker Desktop's WSL2 backend exposes the engine). Document it
  as the recommended Windows path; verify and smoke-test it, but write little
  Windows-specific code.

**Native Windows (PowerShell/cmd, no WSL) is out of scope** — it carries several
hard problems (`pty`, `os.execvp` semantics, POSIX file modes) for marginal
benefit over WSL2, which gives Windows users the Linux path. The code shouldn't
go out of its way to support it, but where a change is no harder to write
OS-agnostically (e.g. lazy imports, `webbrowser`), do so — that keeps a future
native-Windows effort cheap without spending effort on it now.

The plan below builds Tier 1 first; Tier 2 is validation.

## Architectural approach: a thin platform layer

Rather than scatter `if sys.platform == ...` across 4,800 lines, introduce a
small **platform abstraction** near the top of the file (still one file, still
stdlib-only):

- A `_HOST` constant (`"darwin"` / `"linux"` / `"win32"`) from `sys.platform`,
  plus helpers like `_is_macos()`, `_is_linux()`, `_is_windows()`.

- A **credential-store interface** backed by `keyring` (with a file fallback for
  headless Linux) — the single biggest change; see below.

- Small per-OS helpers for clipboard, browser-open, temp-dir selection, and the
  ssh-agent mount.

- Lazy/guarded imports for the Unix-only modules.

Everything else (git, docker, config files, path handling via `pathlib`) is
already cross-platform.

## Blocker #0 — Unix-only imports break `import yolo` on Windows

`fcntl` (line 9), `pty` (line 15), `termios` (line 24), and `tty` (line 26) are
imported at module top level. None exist on Windows, so the module fails to
import before any code runs — this also breaks the `yolo = "yolo:main"` console
script and `--version`.

- **Fix:** move these into lazy imports inside the functions that use them
  (`generate_oauth_token` for `pty`/`termios`/`fcntl`/`struct`; `_ps_picker` /
  `_read_key` for `termios`/`tty`/`select`-on-stdin). Guard each call site so the
  feature degrades or errors cleanly when the module is absent.

- This must land first; nothing else on Windows works until the module imports.

## The big one — credential store (macOS Keychain → `keyring`)

Eight functions shell out to the macOS `security` CLI. This is the only **core**
porting problem (it sits in the default auth path and the secrets/token features):

- `extract_credentials` (line 331) — keychain-mode credential snapshot.

- `_read_oauth_token` (483) / `_store_oauth_token` (499) — OAuth token cache.

- `_keychain_has` (576) / `_keychain_delete` (590) / `_keychain_mdat` (615) —
  token bookkeeping (`tokens` / `forget-token`, expiry warning).

- `_read_secret_value` (943) / `_store_secret_value` (957) — the secrets feature.

Define a `CredentialStore` abstraction with the operations these reduce to:
`get(service) -> str|None`, `set(service, value)`, `delete(service)`,
`exists(service) -> bool`, `mdat(service) -> datetime|None`. Reimplement the
eight call sites in terms of it.

**Primary approach: the `keyring` package.** `keyring` is exactly the
cross-platform credential store this needs — it speaks the macOS Keychain,
Secret Service (libsecret) on Linux, and the Windows Credential Manager behind
one uniform API, all encrypted-at-rest. Adopting it collapses the whole
three-backend design into a single backend and makes the credential store —
otherwise the only hard core item — a near non-problem, while substantially
de-risking a future native-Windows backend. It's a runtime dependency, which the
project's install model handles in both modes (see "On adding dependencies"
below), so it does **not** break the standalone property.

- The `set`/`get`/`delete`/`exists` operations map directly onto
  `keyring.set_password` / `get_password` / `delete_password`. Keep the existing
  service-name scheme (the `hash8` suffixes) unchanged so existing keychain items
  are found.

- **`mdat` is the one thing `keyring` doesn't expose.** `_keychain_mdat`
  currently reads the macOS keychain item's own modify-date for the token-expiry
  warning. `keyring` has no portable "when was this set" API, so make the
  **`tokens.json` registry the sole date source** (it already records `minted`)
  instead of the current keychain-mdat-with-registry-fallback. The
  `re-minted outside yolo` reconciliation that compared keychain mdat vs. the
  registry goes away (there's no mdat to compare); `_store_oauth_token` already
  funnels every mint through the registry, so the `minted` stamp stays accurate.

**Headless Linux (no Secret Service / D-Bus):** on such a box `keyring` selects a
null/fail backend, so provide a **simple chmod-600 JSON file store** under
`~/.claude-yolo/credentials/` (one file per service) as the fallback, selected
when no working `keyring` backend is available. This is consistent with the
existing threat model — yolo already stages plaintext chmod-600 secret files in
the run dir at launch — so dropping encryption-at-rest in the headless case is
acceptable. Implement it behind the same `CredentialStore` interface (so it's
also the natural place a future native-Windows backend would slot in), with a
one-line note at launch when the file store is in use.

### On adding dependencies (why `keyring` is fine)

Adding a runtime dependency does **not** break either install path, because uv
handles declared deps in both:

- **Installed (`uv tool install` / pipx):** add `keyring` to
  `pyproject.toml`'s `dependencies`; `uv tool` resolves+installs it into the
  isolated venv. (The wheel still ships only `yolo.py` via `only-include`; the
  installer pulls the dep.)

- **Standalone PEP 723 (`uv run --script` / the symlink):** add `keyring` to the
  `# /// script` `dependencies = [...]` block at the top of `yolo.py`; uv builds
  an ephemeral cached environment from it. So the single-file standalone path
  keeps working.

The real costs are soft: two dep lists to keep in sync (`pyproject.toml` + the
PEP 723 block — same kind of dual-edit bump-my-version already does for
pyproject + uv.lock); a one-time cold-`uv run` resolve/download (cached
thereafter); `keyring`'s transitive backends (`SecretStorage`+`jeepney` on Linux,
`pywin32-ctypes` on Windows — pure-Python); and relaxing the stated "stdlib-only"
design value in CLAUDE.md. None are blockers. **Decision: accepted — adopt
`keyring`.**

## Auxiliary host-side ports (Tier 1, mostly easy)

### Browser open — `open` → `webbrowser` (easy win)

`_open_url` (line 4236) runs `subprocess.run(["open", url])`. Python's stdlib
**`webbrowser.open(url)`** is cross-platform (uses `open` / `xdg-open` / the
Windows shell as appropriate) and zero-dep. Straight replacement, keeps the test
seam.

### Clipboard — `pbpaste` (auxiliary, `--clipboard` only)

`do_secret_set` (line 1157) uses `pbpaste` for `secret set --clipboard`. Add
per-OS reads: macOS `pbpaste`; Linux `wl-paste` / `xclip -o` / `xsel -b`
(whichever exists, Wayland-first); Windows `powershell -c Get-Clipboard`. If none
found, the existing stdin/prompt input paths still work — so `--clipboard` just
errors with guidance on that platform rather than blocking the feature.

### Temp / run dir — `$TMPDIR` mode-700 assumption

`_run_dir` (line 785) uses `tempfile.gettempdir()` and the code comments assume
the macOS per-user temp dir is mode 700 and excluded from Time Machine. On Linux
`/tmp` is world-readable (mode 1777); the per-session dir is already created mode
700 and files mode 600, so *contents* stay protected, but prefer
**`$XDG_RUNTIME_DIR`** (per-user, mode 700, tmpfs) when set. On Windows, chmod is
largely a no-op (ACL model); document the weaker guarantee and rely on the
per-user temp location. Update the comments to state the per-OS reality.

### ssh-agent socket (core-optional, `--ssh-agent`)

The mount `/run/host-services/ssh-auth.sock` (line 3267) is Docker
Desktop/OrbStack magic. Make it host-aware:

- **macOS / Windows Docker Desktop / OrbStack** — keep
  `/run/host-services/ssh-auth.sock` (Docker Desktop exposes it identically on
  Windows).

- **native Linux Docker** — bind-mount the host's real `$SSH_AUTH_SOCK` directly
  (same kernel, the socket inode is live), e.g.
  `-v $SSH_AUTH_SOCK:/run/ssh-agent`. Read `$SSH_AUTH_SOCK` from the host env.

- The `known_hosts` mount and the HTTPS→SSH git rewrite are OS-agnostic and stay.

## Already fine on Linux (no work needed)

These are Unix-only but present and correct on Linux, so Tier 1 needs **no
change** beyond the lazy imports of Blocker #0. They only matter for native
Windows, which is out of scope:

- **`os.execvp`** — used at three launch tails: `_dispatch_launch` (line 3105,
  non-tmux launch), `_launch_in_tmux` (3071, the tmux client), and `do_shell`
  (4585, `docker exec`). Works on Linux exactly as on macOS.

- **tmux** — works natively on Linux, so `--tmux` and its dashboard are Tier 1.
  The non-tmux path is the default and unaffected either way.

- **The `ps --watch` picker** (`_ps_picker`, `_read_key`,
  `termios`/`tty`/`select`-on-stdin) — works on Linux.

(For the record, a future native-Windows port would here branch `os.execvp` to
`subprocess.run` + `sys.exit(rc)`, disable `--tmux` cleanly via the existing
`shutil.which("tmux")` guard at ~line 3020, and fall the picker back to the
passive redraw loop. Not built now.)

## setup-token PTY (Linux ok)

`generate_oauth_token` (line ~666) runs `claude setup-token` under a `pty` to
capture and scrape the token, resizing the pty wide to avoid token wrapping.
`pty`/`termios`/`fcntl` are Unix-only but present on **Linux**, so Tier 1 works
once imports are lazy. (Native Windows would skip the pty and lean on the existing
manual-paste fallback / host `CLAUDE_CODE_OAUTH_TOKEN` env — not built now.)

## Shebang / invocation

`#!/usr/bin/env -S uv run --script` is ignored on Windows. Under WSL2 it works
like Linux. (Native Windows would run via the console script or `uv run yolo.py`
— a docs note if ever pursued.)

## Scope summary

With native Windows out of scope and `keyring` adopted, **Tier 1 (macOS + Linux)
has no fundamentally hard blockers** — the remaining changes are mechanical: the
lazy imports, `webbrowser`, the ssh-agent socket selection, clipboard reads, and
the headless-Linux file fallback. Tier 2 (Windows-via-WSL2) is then a
test-and-document pass over the Linux build.

The things that *would* be hard — `os.execvp` semantics, `pty`, POSIX file modes,
tmux/picker — are all **native-Windows-only** and therefore deferred, not solved.
WSL2 sidesteps every one of them. The `CredentialStore` interface is the seam a
future native-Windows backend would plug into.

## Proposed phasing

1. **Make it import everywhere.** Lazy/guard the Unix-only imports (Blocker #0).
   Add the `_HOST` / `_is_*` helpers. Replace `_open_url` with `webbrowser`.
   (Unblocks `--version` and the console script on every OS.)

2. **Credential-store abstraction on `keyring`.** Add `keyring` to both dep
   lists; extract the `CredentialStore` interface; wrap the eight `security` call
   sites onto `keyring`; switch the token-expiry date source to `tokens.json`
   (drop `_keychain_mdat` and the mdat-based reconciliation). This is the gating
   piece for a usable Linux build, and `keyring` makes it work on Windows too.

3. **Linux host-side glue.** ssh-agent socket selection (`$SSH_AUTH_SOCK`),
   clipboard reads, `$XDG_RUNTIME_DIR` run dir, the headless-Linux chmod-600 file
   credential fallback, comment/doc updates. tmux and the picker already work on
   Linux once imports are lazy.

4. **Tier 1 done: validate on Linux.** Full smoke test (oauth-token default,
   keychain-equiv via the new store, secrets, worktrees, tmux, ports, browse) on
   both a desktop Linux (with Secret Service) and a headless box (file fallback).

5. **Tier 2: validate Windows-via-WSL2.** Mostly a test/doc pass over the Linux
   build.

## Testing

- The suite already stubs the platform seams (`build_docker_image`,
  `ensure_logged_in`, `extract_credentials`, `ensure_oauth_token`,
  `git_identity_args`, `os.execvp`, `_run_dir`, `_open_url`). Extend
  `tests/conftest.py` to also let tests pin `_HOST`, so the same tests can assert
  per-OS argv (e.g. ssh-agent socket path, clipboard command, credential
  backend).

- Add a `tests/test_platform.py` covering: the lazy-import guards don't fail on a
  simulated Windows; `_open_url` uses `webbrowser`; the `CredentialStore`
  round-trips get/set/delete/exists against a faked `keyring` backend, and `mdat`
  reads from `tokens.json`; ssh-agent socket selection per host; clipboard
  command selection. Stub `keyring` in `conftest.py` (an in-memory dict) so tests
  never touch a real keyring.

- CI: the project is currently developed on macOS. Add a **Linux CI job**
  (GitHub Actions `ubuntu-latest`) running `ruff` + `pytest` so the Linux path
  doesn't regress.

## Documentation updates

- `CLAUDE.md`: revise "macOS only as written" and the Conventions/gotchas to
  describe the host abstraction, the per-OS ssh-agent socket, the `keyring`
  credential store and headless-Linux file fallback, and the supported tiers
  (macOS + Linux; Windows via WSL2; native Windows out of scope).

- README/usage: note Windows-via-WSL2 as the recommended Windows path.

## Decisions made

- **Credential store: adopt `keyring`** as a runtime dependency (declared in both
  `pyproject.toml` and the PEP 723 block). Does not break the standalone
  property; covers macOS / Linux / (future) Windows behind one API.

- **Native Windows (no WSL) is out of scope.** Supported tiers are macOS + native
  Linux, and Windows via WSL2.

- **Headless Linux uses a simple chmod-600 file store** when no `keyring` backend
  is available, behind the `CredentialStore` interface.
