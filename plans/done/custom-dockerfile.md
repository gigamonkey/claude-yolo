# Plan: external/custom Dockerfile + UID via build ARG

## Context

`yolo.py` carries its Dockerfile as an inline `DOCKERFILE_TEMPLATE` string whose
only dynamic bit is the host UID, substituted with `.format(uid=os.getuid())`
(yolo.py:41, 85). We want two things:

1. **Let a project supply its own Dockerfile** so projects with special build
   needs aren't forced to extend the baked image at runtime via `sudo apt`.
2. **Replace the string-substitution of the UID with a Docker build `ARG`** —
   `ARG HOST_UID` + `--build-arg` is the idiomatic mechanism, and once the UID
   is an ARG the Dockerfile needs no Python templating at all, so the inline
   default and any custom file become *the same thing*: Dockerfile bytes built
   with `--build-arg HOST_UID=$(id -u)`.

**Decisions:**

- The built-in Dockerfile **stays inline as the default** (preserving the
  single-file PEP 723 / wheel-ships-only-`yolo.py` property). A new optional
  `--dockerfile PATH` flag / `dockerfile` config key points at an external file
  to use *instead*. We are **not** fully externalizing the default.
- The image tag is **derived from the Dockerfile content + UID**
  (`claude-yolo:<hash8>`) instead of the fixed `claude-yolo:latest`. yolo
  explicitly supports parallel sessions; a single shared tag lets two concurrent
  sessions (one default, one custom) race on the tag so one `docker run` picks
  up the other's image. A content-hash tag makes that impossible.

**Risk to document (not block on):** a `dockerfile` key pointing at a file
*inside* the bind-mounted working tree (e.g. `./Dockerfile.yolo`) is editable by
Claude between runs, so Claude could alter the next image build. Same class of
hazard that made config host-side-only — but the *path* still lives in host-side
`projects.json` (Claude can't add the key), only the referenced file is in-tree.
Acceptable for an explicitly opt-in feature; noted in the CLAUDE.md caveats.

## Changes (all in `yolo.py` unless noted)

### 1. Dockerfile → ARG, drop templating

- Rename `DOCKERFILE_TEMPLATE` → `DEFAULT_DOCKERFILE` (yolo.py:41). Remove the
  `{uid}` substitution: add `ARG HOST_UID` immediately before the `useradd`
  line and change `--uid {uid}` → `--uid ${HOST_UID}` (yolo.py:57). Update the
  adjacent comment (yolo.py:52) to describe the ARG. No other `{`/`}` exist, so
  it is now a literal Dockerfile.

### 2. Tag derived from content + UID

- Replace `DOCKER_IMAGE = "claude-yolo:latest"` (yolo.py:27) with a base name
  `DOCKER_IMAGE_REPO = "claude-yolo"` and a helper
  `_image_tag(dockerfile_text, uid) -> str` returning
  `f"{DOCKER_IMAGE_REPO}:{sha256((dockerfile_text + str(uid)).encode()).hexdigest()[:8]}"`.

### 3. Resolve which Dockerfile to build

- New helper `_resolve_dockerfile(parsed) -> tuple[str, str]`:
  - `text = read_text(parsed.dockerfile)` when set, else `DEFAULT_DOCKERFILE`.
  - returns `(text, _image_tag(text, os.getuid()))`.

### 4. `build_docker_image` takes text/tag/uid

- New signature `build_docker_image(dockerfile_text, tag, uid, *, no_cache=False)`
  (yolo.py:81). Write `dockerfile_text` verbatim to the temp build dir; build
  with `["docker", "build", "-t", tag, "--build-arg", f"HOST_UID={uid}"]`
  (+ `--no-cache`). Drop the `.format()`.

### 5. Thread the tag through `launch_container`

- Inside `launch_container` (yolo.py:2019) at the build site (yolo.py:2197):
  `text, tag = _resolve_dockerfile(parsed)`, then
  `build_docker_image(text, tag, os.getuid(), no_cache=parsed.rebuild_image)`,
  and use `tag` in `run_cmd` in place of `DOCKER_IMAGE` (yolo.py:2210). Tag is
  computed locally so it stays consistent between build and run even when tests
  stub `build_docker_image`.

### 6. `--dockerfile` flag + `dockerfile` config key

- Add `PARSER.add_argument("--dockerfile", dest="dockerfile", default=None,
  metavar="PATH", ...)` near `--rebuild-image` (yolo.py:1547).
- Add `"dockerfile": ("dockerfile", "path")` to `YOLO_KEYS` (yolo.py:698) — the
  `"path"` kind already `~`-expands (yolo.py:750-753). Override semantics, so
  **not** in `_CONCAT_DESTS`.
- **Existence validation** mirroring `--config-dir` (yolo.py:2948): on launch
  paths, if `parsed.dockerfile` is set and not a readable file, `sys.exit` with
  a pointed message — placed with the other launch-time path checks so
  `list`/`config`/`finish` don't trip on a stale path.

### 7. `config` verb support

- `yolo config --dockerfile X` persists automatically (it is a `YOLO_KEYS` flag
  via `_explicit_config_flags`). Validate path exists + is a file **before**
  persisting, mirroring `--mount` validation. No new editing flags (scalar).

### 8. Tests (`tests/`)

- `conftest.py`: update the `build_docker_image` stub to
  `(dockerfile_text, tag, uid, *, no_cache)`.
- Existing argv assertions expecting `claude-yolo:latest` now see
  `claude-yolo:<hash8>` — assert via `yolo._image_tag(yolo.DEFAULT_DOCKERFILE,
  os.getuid())` rather than a literal.
- New cases: `--dockerfile` reads the custom file and yields a *different* tag;
  the assembled `docker build` carries `--build-arg HOST_UID=<uid>`; a missing
  `--dockerfile` path exits; `dockerfile` via `projects.json` is parsed +
  `~`-expanded; `yolo config --dockerfile` persists and rejects a bad path.
  CLI cases in `test_cli.py`, config cases in `test_config.py`.

### 9. Docs (`CLAUDE.md`)

- "How it works" #1/#2: built with `--build-arg HOST_UID` (no string
  substitution); note the optional custom Dockerfile and content+UID-hashed tag.
- Orthogonal-flags section: add `--dockerfile`.
- Config section: add `dockerfile` to the `YOLO_KEYS` list.
- Gotchas: the in-tree-Dockerfile caveat from **Context**.
- Usage block: a `./yolo.py --dockerfile ./Dockerfile.yolo` line.

## Verification

- `uv run pytest` — full suite green.
- `uv run ruff check . && uv run ruff format .`.
- Manual default: `./yolo.py shell` builds `claude-yolo:<hash8>`; `id -u` inside
  == host UID (ARG path works).
- Manual custom: a minimal `Dockerfile.yolo` (`FROM ubuntu:24.04` + `ARG
  HOST_UID` + the `useradd`/sudo lines + a marker package) with `./yolo.py
  --dockerfile ./Dockerfile.yolo shell` builds a *distinct* tag and the marker is
  present; default runs in the same repo are unaffected.
- `./yolo.py config --dockerfile ./Dockerfile.yolo` persists; a later bare
  `./yolo.py shell` picks it up; a bad path is rejected by flag and writer.
