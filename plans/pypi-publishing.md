# Plan: Publish `claude-yolo` to PyPI

## Goal

Get `claude-yolo` onto PyPI so anyone can `uv tool install claude-yolo` (or
`pipx install claude-yolo`) and get the `yolo` command, instead of cloning the
repo. This is the **first-ever publish** of this package (confirmed
2026-06-16: `https://pypi.org/pypi/claude-yolo/json` → 404), so the plan covers
the one-time account/setup work as well as the steady-state release flow.

## Background: what "publishing to PyPI" actually is

PyPI (the Python Package Index, <https://pypi.org>) is the public registry `pip`,
`uv`, and `pipx` download from. "Publishing" means uploading two build artifacts
for a version — a **wheel** (`*.whl`, the installable build) and an **sdist**
(`*.tar.gz`, the source) — under a project name you own. We already build both
cleanly with `uv build` (they land in `dist/`). The only missing piece is
*authenticating the upload* and *claiming the name* `claude-yolo`.

Key facts that shape this plan:

- **The name is first-come, first-served and global.** The first successful
  upload creates the project and makes the uploading account its owner. `claude-yolo`
  is currently unclaimed.

- **A version is immutable and can't be reused.** Once `0.15.1` (the first
  published version — see below) is uploaded you can never re-upload it, even
  after deleting it. So we rehearse on TestPyPI first (optional but recommended)
  and only push the real thing once.

- **There are two ways to authenticate an upload**, below. We recommend the
  token-free one.

## Two ways to authenticate — and the recommendation

### Option A — Trusted Publishing via GitHub Actions (OIDC) — RECOMMENDED

GitHub Actions proves the repo's identity to PyPI with a short-lived OIDC token;
PyPI mints a one-off upload token for that single run. **No long-lived secret is
ever stored** in the repo or on your machine. This matches the project owner's
standing preference (see `~/.claude/CLAUDE.md` → "npm Publishing": Trusted
Publisher / OIDC, `id-token: write`, no stored token — PyPI's mechanism is the
direct analog of npm's).

PyPI supports configuring a **"pending" trusted publisher** *before* the project
exists, so even the very first publish can go through OIDC with no token. That's
the path this plan takes.

### Option B — Manual upload with an API token — FALLBACK

Create a PyPI API token, run `uv publish` (or `twine upload`) from your laptop
with it. Simple, works immediately, but the token is a long-lived bearer secret
you have to store somewhere and rotate. Documented at the end as a fallback if
OIDC gives trouble on the first run.

## Why the first published version will be 0.15.1, not 0.15.0

Two facts make a fresh release the clean path:

- **`v0.15.0` is already public.** It was tagged *and pushed to origin*
  (confirmed 2026-06-16: `git ls-remote --tags origin` shows
  `refs/tags/v0.15.0` → `cdc2fde`). It was never published to PyPI, but the tag
  is public — moving or rewriting a pushed tag is the thing you don't do (anyone
  who fetched it would now disagree about what `v0.15.0` is).

- **The workflow wouldn't be there anyway.** GitHub Actions runs a workflow **as
  that file existed at the tagged commit**, and `cdc2fde` predates the publish
  workflow. So even if we could reuse `v0.15.0`, its commit wouldn't contain
  `publish.yml`.

Both problems vanish if we just **cut a fresh release**: add the workflow +
metadata, then bump to **0.15.1** and let that new tag — created *after* the
workflow exists — be the first PyPI publish. No tag surgery, and the
steady-state flow (bump → push tag → CI publishes) starts working immediately.
0.15.1 is a *patch* because it adds only packaging/CI infrastructure; yolo's
behavior is unchanged from 0.15.0.

## Prerequisites / decisions

- A PyPI account with 2FA enabled (required to upload).
- (Optional) a TestPyPI account for the rehearsal — separate site, separate
  account.
- Decision: use a GitHub Actions **environment** named `pypi` for the publish
  job. PyPI recommends binding the trusted publisher to an environment as an
  extra guard (the OIDC claim must come from that environment). This plan uses
  one; it's a checkbox on both sides and worth it. Skippable if you'd rather not.

## Step-by-step — the recommended OIDC path

### 1. Add project URLs to `pyproject.toml`

Not strictly required for PyPI trusted publishing, but: it populates the PyPI
project sidebar (Homepage / Repository links), and it mirrors the npm Trusted
Publisher requirement the owner already follows (a `repository.url` matching the
GitHub repo). Add under `[project]`:

```toml
[project.urls]
Homepage = "https://github.com/gigamonkey/claude-yolo"
Repository = "https://github.com/gigamonkey/claude-yolo"
Changelog = "https://github.com/gigamonkey/claude-yolo/blob/main/CHANGELOG.md"
```

Consider also adding `readme = "README.md"` to `[project]` so PyPI renders the
README as the project's long description (otherwise the PyPI page body is empty).
Verify `uv build` still succeeds afterward.

### 2. Add the GitHub Actions publish workflow

Create `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - "v*"

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi          # must match the env named in the PyPI publisher (step 4)
    permissions:
      contents: read
      id-token: write          # REQUIRED — lets the job request the OIDC token
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv build
      - run: uv publish --trusted-publishing always
```

Notes:

- `uv publish --trusted-publishing always` makes uv fetch the OIDC token and
  exchange it with PyPI; no `--token`/credentials are passed. (`always` makes a
  misconfiguration fail loudly instead of silently falling back to looking for a
  token.)
- Trigger is any `v*` tag push, matching the existing `tag_name = "v{new_version}"`
  from bump-my-version — so the steady-state flow is just "push the tag the bump
  created."
- Alternative if you'd prefer the canonical PyPA action over uv:
  `pypa/gh-action-pypi-publish@release/v1` (it auto-detects OIDC). Either works;
  uv-native keeps the toolchain consistent with the rest of the repo.

### 3. Commit the metadata + workflow, push `main`

```bash
git add pyproject.toml .github/workflows/publish.yml
git commit -m "Add PyPI trusted-publishing workflow + project URLs"
git push origin main
```

(Also push the earlier release commits if not yet pushed — `git push origin main`
covers them all.)

### 4. Configure the PENDING trusted publisher on PyPI

Because the project doesn't exist yet, use the pending-publisher form:

1. Create/log into your account at <https://pypi.org>, enable 2FA.
2. Go to <https://pypi.org/manage/account/publishing/>.
3. Under "Add a new pending publisher", fill in:
   - **PyPI Project Name:** `claude-yolo`
   - **Owner:** `gigamonkey`
   - **Repository name:** `claude-yolo`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`  (must match `environment:` in the workflow;
     leave blank only if you dropped the environment in step 2)
4. Save. PyPI now trusts an OIDC token from that exact repo+workflow(+environment)
   to create and publish `claude-yolo`.

If you used an environment, also create it in GitHub: repo **Settings →
Environments → New environment → `pypi`** (no secrets needed; optionally add
required reviewers to gate publishes).

### 5. Cut the 0.15.1 release (the first one that publishes)

With the workflow committed and pushed (step 3), run the normal release flow.
This creates a brand-new tag at a commit that already contains `publish.yml`:

1. **CHANGELOG:** add a new `## v0.15.1 — <date>` heading noting the packaging
   change, e.g.:

   > - **Packaging: `claude-yolo` is now published to PyPI.** Releases upload
   >   automatically via GitHub Actions trusted publishing on each `v*` tag —
   >   `uv tool install claude-yolo` / `pipx install claude-yolo`.

   Commit it.
2. `uv run bump-my-version bump patch` — bumps to **0.15.1**, commits the bump,
   and creates the `v0.15.1` tag at `HEAD` (which now includes `publish.yml`).
   Requires a clean tree, so do this after the CHANGELOG commit.
3. `git push origin main`

### 6. Push the tag → watch the Action → verify

```bash
git push origin v0.15.1
```

Then:

- Watch the run under the repo's **Actions** tab (or `gh run watch`). The
  `Publish to PyPI` workflow should build and upload.
- On success, confirm the project page exists: <https://pypi.org/project/claude-yolo/>
  and `https://pypi.org/pypi/claude-yolo/json` no longer 404s.
- Smoke-test the install in a throwaway location:

  ```bash
  uv tool install claude-yolo
  yolo --version        # expect 0.15.1
  uv tool uninstall claude-yolo
  ```

## Optional but recommended: rehearse on TestPyPI first

TestPyPI (<https://test.pypi.org>) is a throwaway mirror — ideal for a
first-timer to see the whole flow without burning the real `0.15.0` or the real
name. To rehearse:

1. Make a TestPyPI account (separate from PyPI).
2. Add a pending publisher there the same way, at
   <https://test.pypi.org/manage/account/publishing/>.
3. Temporarily point the publish step at TestPyPI, e.g. a separate
   workflow or a manual local run:

   ```bash
   uv publish --publish-url https://test.pypi.org/legacy/ --trusted-publishing always
   ```

   (Local runs can't use OIDC — that only works inside Actions — so for a *local*
   TestPyPI rehearsal use a TestPyPI API token instead: `uv publish --publish-url
   https://test.pypi.org/legacy/ --token <testpypi-token>`.)
4. Install from TestPyPI to confirm:
   `uv tool install --index https://test.pypi.org/simple/ claude-yolo`.

TestPyPI versions are independent of PyPI, so a rehearsal upload of `0.15.1`
there does **not** block the real `0.15.1` on PyPI.

## Fallback: manual one-time publish with an API token (Option B)

If OIDC misbehaves on the first run and you want to unblock the release:

1. <https://pypi.org/manage/account/token/> → create a token. For the very
   first upload it must be account-scoped (the project doesn't exist yet to
   scope to); after the first publish, replace it with a project-scoped token.
2. From a clean checkout at the tagged commit:

   ```bash
   uv build
   uv publish --token pypi-XXXXXXXX...
   ```

3. After this first publish, the project exists — so the pending trusted
   publisher from step 4 becomes a *real* one automatically, and all future
   releases can go token-free through the workflow. Delete the account-scoped
   token once OIDC is confirmed working.

`UV_PUBLISH_TOKEN` env var works in place of `--token` to keep the secret off
your shell history.

## Steady-state: how every future release works after setup

Once the workflow + trusted publisher exist, a release is just the existing flow
plus a tag push:

1. `CHANGELOG.md`: rename `## Unreleased` → `## vX.Y.Z — <date>`, commit.
2. `uv run bump-my-version bump {patch,minor,major}` — commits the version bump
   and creates the `vX.Y.Z` tag.
3. `git push origin main && git push origin vX.Y.Z`.
4. The tag push triggers `publish.yml`, which builds and publishes to PyPI via
   OIDC. No tokens, no manual upload.

(The "cut a fresh 0.15.1" dance from this plan is a *first-time-only* artifact —
it exists only because `v0.15.0` shipped before the workflow did. From 0.15.1
onward every bump creates its tag on a commit that already contains the
workflow, so the four steps above are the whole release.)

## Risks / things to double-check

- **Name squatting / typo:** the project name on PyPI must be exactly
  `claude-yolo` and the owner/repo in the publisher must match `gigamonkey/claude-yolo`
  exactly, or the OIDC exchange is rejected.
- **Immutable versions:** don't test against real PyPI with a version you want to
  keep — that's what TestPyPI / the rehearsal is for.
- **2FA is mandatory** to upload to PyPI; set it up before step 4.
- **Environment-name mismatch** between `publish.yml` and the PyPI publisher is
  the most common OIDC failure — they must be identical (or both empty).
```