# Plan: Externalize the default Dockerfile and the default system prompt

## Goal

Move three pieces of content that currently live as triple-quoted string literals
in `yolo.py` into their own files in the repo:

1. `DEFAULT_DOCKERFILE` (yolo.py:114) — the built-in Dockerfile.
2. `CUSTOM_DOCKERFILE` (yolo.py:186) — the layer-on-the-default template that
   `yolo dockerfile --custom` prints.
3. The built-in "you're in an ephemeral container" **system-prompt** text
   (yolo.py:3169) that gets passed to `claude --append-system-prompt`.

This is purely an internal refactor: **no user-visible behavior changes**. The
same image is built, the same prompt is sent, `yolo dockerfile` prints the same
bytes, and every flag/verb behaves identically. The only goal is that these
blobs live in editable standalone files (real Dockerfiles with Dockerfile
syntax highlighting, a plain-text prompt) rather than as escaped Python strings.

## Approach: adjacent data files

yolo is installed with **`uv`** — either `uv tool install` (a wheel) or an
editable install (`uv pip install --editable` / `uv tool install --editable`); a
PATH symlink also works. The "single file" shape was an accident of history, not
a design goal, so there's nothing to preserve there and nothing to deliberate
about — we just ship the data files alongside `yolo.py` in all the ways it's
actually installed:

| Install mode | How files are found |
|---|---|
| `uv tool install` (wheel) | shipped in the wheel, land in site-packages next to `yolo.py` |
| editable (`uv --editable`) | `__file__` resolves into the source checkout, files are right there |
| PATH symlink | `Path(__file__).resolve().parent` follows the symlink back into the repo |

All three resolve the data files via `Path(__file__).resolve().parent` — the same
`__file__`-adjacent lookup `_pyproject_version()` / `_base_version()` already use
to read the sibling `pyproject.toml`. The only thing that needs doing beyond the
code is adding the files to the wheel's `only-include` (step 4).

The plan: move each blob to a file, load it into the existing module-level
constant at import time, ship the files in the wheel. The public symbols
(`DEFAULT_DOCKERFILE`, `CUSTOM_DOCKERFILE`, and a new `CONTAINER_PROMPT`) keep
their names, so the rest of the code and the test suite are essentially untouched
— only their *source* changes from an inline literal to a file read.

## Files to add

Place all three **next to `yolo.py`** at the repo root (so `__file__`-relative
resolution is trivial and matches the `pyproject.toml`-adjacent precedent):

- `Dockerfile.default` — the current `DEFAULT_DOCKERFILE` body, verbatim, as a
  real Dockerfile. One concrete improvement falls out: the secrets-loader
  `printf` block currently escapes newlines as `\\n` because it lives inside a
  Python string; in a real file that becomes a normal `\n`, so the file reads as
  the literal Dockerfile it is.
- `Dockerfile.custom` — the current `CUSTOM_DOCKERFILE` template, verbatim.
- `container-prompt.txt` — the static base system-prompt sentence (see the
  prompt section for exactly which lines move).

### Naming: `Dockerfile.default`, not `default.Dockerfile`

Use the `Dockerfile.<suffix>` form. The decisive reason is **consistency with the
project's own existing convention**: the custom-Dockerfile feature already names
its files `Dockerfile.yolo` everywhere (README, CLAUDE.md, the `--dockerfile`
docs, the unconfigured-`Dockerfile.yolo` warning). So `Dockerfile.default` /
`Dockerfile.custom` sit naturally beside `Dockerfile.yolo`, and they're the
familiar `Dockerfile.prod`/`Dockerfile.dev` shape. The syntax-highlighting
argument that might favor `*.Dockerfile` doesn't hold up — modern editors (VS
Code's Docker extension included) highlight the `Dockerfile.*` suffix form too —
so it loses to the consistency argument.

## Implementation

### 1. A small data-file loader

Add one helper that mirrors `_pyproject_version()`'s `__file__`-relative,
symlink-resolving lookup, but is mandatory (the data must exist):

```python
_DATA_DIR = pathlib.Path(__file__).resolve().parent

def _read_data_file(name: str) -> str:
    """Read a packaged data file that sits beside yolo.py.

    Resolves __file__ (following a PATH symlink, like _pyproject_version) so the
    standalone-script and symlink installs find the repo copy, and the wheel
    install finds the copy shipped next to yolo.py in site-packages.
    """
    return (_DATA_DIR / name).read_text()
```

### 2. Populate the constants from files

Keep the **same public names** so callers and tests don't move:

```python
DEFAULT_DOCKERFILE = _read_data_file("Dockerfile.default")
CUSTOM_DOCKERFILE = _read_data_file("Dockerfile.custom")
CONTAINER_PROMPT = _read_data_file("container-prompt.txt")
```

- `DEFAULT_DOCKERFILE` and `CUSTOM_DOCKERFILE` keep their names — `_image_tag`,
  `_build_image`, `build_docker_image`, `do_dockerfile`, and the tests all keep
  working unchanged (they read the constant's *value*, not the literal). The
  `do_dockerfile` verb (test_cli.py:992/1006) prints whichever constant exactly
  as before.
- The content-addressed image tag (`_image_tag` hashes the Dockerfile text +
  UID) is **unchanged in practice** as long as the file bytes equal the old
  literal bytes. ⚠️ Watch the trailing-newline/escaping details: the old literal
  ended with `\n` (the `"""` on its own line) and used `\\n` inside `printf`. The
  file must produce **byte-identical** text after the `\\n`→`\n` un-escaping, or
  the tag changes and every user rebuilds once. This is harmless (one rebuild)
  but call it out; a test asserts the bytes are stable (see Testing).

### 3. The prompt

`build_claude_args` (yolo.py:3168) builds `extra_system_prompt` as a list:

- **Move to the file:** the always-present base line
  `"You are running in an ephemeral Ubuntu container instead of MacOS host. Use
  sudo apt to install things you need."` → `CONTAINER_PROMPT`.
- **Leave in code:** the two *conditional* lines — the ssh-agent-off line and the
  forwarded-ports line — because they're gated on runtime state and the ports one
  is an f-string interpolation of the actual port numbers. They are logic, not
  static default text; externalizing them would mean templating a file for no
  real benefit.

So:

```python
extra_system_prompt = [
    CONTAINER_PROMPT.strip(),
    *([SSH_LINE] if not ssh_agent else []),
    *([ports_line] if forwarded_ports else []),
    *prompts,
]
```

`.strip()` (or storing the file with no trailing newline) keeps the joined
`"... ".join(...)` output identical to today. The test at test_cli.py:409
(`"ephemeral Ubuntu container" in joined`) keeps passing.

(Optional, scope permitting: the ssh-agent static sentence could also live in the
file as a second labeled block, but it's a single line of pure logic-gated text —
recommend leaving it inline to keep the file purely the unconditional prompt.)

### 4. Wheel packaging

Update `pyproject.toml` so the wheel ships the new files beside `yolo.py`:

```toml
[tool.hatch.build.targets.wheel]
only-include = ["yolo.py", "Dockerfile.default", "Dockerfile.custom", "container-prompt.txt"]
```

`only-include` places them at the wheel root → installed into site-packages
next to `yolo.py`, where `_DATA_DIR / name` finds them. Verify with
`uv build` + inspecting the wheel (`unzip -l dist/*.whl`) that both files are
present. The sdist includes repo files by default, but confirm.

Update the comment at pyproject.toml:18 ("ship just that in the wheel") to name
the three files and why.

### 5. `CUSTOM_DOCKERFILE`

Externalized the same way as `DEFAULT_DOCKERFILE`: move the body to
`Dockerfile.custom`, load it into the same-named constant, add it to
`only-include`. `do_dockerfile(custom=True)` and its test (test_cli.py:1006,
which reads `cy.CUSTOM_DOCKERFILE`) are unaffected. Nothing hashes this one into
an image tag, so there's no tag-stability concern for it — but keep the bytes
identical anyway so `yolo dockerfile --custom` output doesn't shift.

## Testing

The existing suite already pins the important invariants and mostly Just Works
because the constants keep their names and values:

- test_cli.py:886/887/913/981/992 — `builds[0]["text"] == cy.DEFAULT_DOCKERFILE`
  and `_image_tag(cy.DEFAULT_DOCKERFILE, …)` — pass unchanged (they compare
  against the loaded constant).
- test_cli.py:409 — `"ephemeral Ubuntu container" in joined` — pass unchanged.
- test_yolorc.py:131/132 — `"YOLO_RC" in cy.DEFAULT_DOCKERFILE` etc. — pass
  unchanged.

Add:

- **A byte-stability guard** so a future careless edit can't silently change the
  image tag: assert the loaded `DEFAULT_DOCKERFILE` contains the key markers
  (`FROM ubuntu:26.04`, the useradd line, `ENTRYPOINT ["claude"`) and that
  `load-secrets.sh` content has real (un-escaped) newlines. Optionally, during
  the migration, a one-shot assertion that the new file equals the old literal
  (paste the old literal into the test, confirm green, then delete it) to *prove*
  the bytes didn't drift.
- **A loader test** that `_read_data_file` resolves relative to `__file__` and
  raises a clear error if a file is missing (so a packaging regression fails loudly
  rather than at first launch).
- **A packaging test or manual check**: `uv build` then assert all three data
  files are in the wheel (could be a `tests/` check that shells out, or just a
  documented release-checklist step).

Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`.

Also do one real end-to-end smoke check that isn't mocked: `./yolo.py dockerfile`
prints the file, and an actual `./yolo.py shell` builds the image (since the test
suite stubs `build_docker_image`).

## Documentation updates (developer-facing only — not user-visible)

CLAUDE.md and the README describe the inline blobs in a few places; update them to
match the new reality, but **don't turn this into a discussion of the single-file
property** — that shape was incidental, and the docs should simply say the
Dockerfiles and prompt are data files shipped alongside `yolo.py` (via the wheel,
the editable install, or a symlink).

- **CLAUDE.md**: the "How it works" #1 paragraph describes building from the
  inline `DEFAULT_DOCKERFILE`; the section near it says "The default Dockerfile
  stays inline (not a shipped file) to preserve the single-file property" and the
  `--dockerfile` discussion calls the default "inline". Reword these to "the
  built-in `Dockerfile.default` / `Dockerfile.custom` data files" without
  relitigating single-file-ness. Drop the parenthetical "to preserve the
  single-file property" rationale rather than replacing it with a new one. Update
  the Development/packaging note to mention the three `only-include` files.
- **README**: wherever it leans on "single self-contained file," reword to "a tool
  installed with `uv`" plus its data files. Check for any pasted copy of the inline
  Dockerfile that would now drift from `Dockerfile.default`.
- These are developer-facing docs, so the change stays user-invisible.

## Risks / watch-items

- **Image-tag churn** from non-identical bytes (escaping/trailing newline). Pin
  with the byte-stability test; worst case is a single harmless rebuild for
  existing users.
- **Wheel packaging**: easy to forget the `only-include` edit; the result is a
  wheel that imports fine but crashes at first launch (`FileNotFoundError`). The
  loader test + a wheel-contents check catch it.
- **The `printf` un-escaping**: the single spot where copy-paste can go wrong.
  Triple-check `\\n` → `\n` and that the build still produces a working
  `load-secrets.sh`.

## Summary of changes

| File | Change |
|---|---|
| `Dockerfile.default` (new) | the `DEFAULT_DOCKERFILE` body, real Dockerfile, `\\n`→`\n` |
| `Dockerfile.custom` (new) | the `CUSTOM_DOCKERFILE` template body, verbatim |
| `container-prompt.txt` (new) | the static base system-prompt sentence |
| `yolo.py` | add `_read_data_file`; `DEFAULT_DOCKERFILE`/`CUSTOM_DOCKERFILE`/`CONTAINER_PROMPT` read from files; `build_claude_args` uses `CONTAINER_PROMPT`; delete the three literals |
| `pyproject.toml` | `only-include` ships the three data files; update comment |
| `tests/` | byte-stability guard, loader test, wheel-contents check |
| `CLAUDE.md`, `README` | update the single-file narrative |
