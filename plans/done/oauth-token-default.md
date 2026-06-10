# Make `oauth-token` the default auth mode, with token bookkeeping

## Motivation

Keychain mode is an attractive nuisance. It works perfectly in the
single-session demo case, but the mounted `.credentials.json` is a *snapshot*
of an OAuth pair whose refresh token rotates single-use: the first party
(container or host) to refresh silently invalidates every other snapshot's
refresh token, and the loser 401s (proven 2026-06-08/09, see
token-investigation in plans/done). Concurrent — even
sequential-with-overlap — sessions are structurally unsafe, and the failure is
silent and confusing.

`--auth oauth-token` is immune by construction: a `claude setup-token`-minted
one-year token is never rotated and never written back, so any number of
containers plus the host coexist. The historical objection to defaulting to it
was that it mints a new long-lived credential. That objection has weakened:

- The keychain snapshot *also* puts an account credential in the container —
  a refresh token with indefinite (self-renewing) life. The setup-token is
  year-capped and inference-only. Like-for-like, it is not a step down.
- Claude Code itself now mints these tokens routinely (one per interactive
  session under `remoteControlAtStartup: true` — see
  [#59378](https://github.com/anthropics/claude-code/issues/59378)); a typical
  power user's claude.ai token list already has dozens-to-hundreds of
  indistinguishable entries. One deliberately-minted, locally-tracked,
  keychain-stored yolo token per config dir would be among the best-managed
  credentials on that list.
- The objection was really to doing it *silently*. The fix is consent at mint
  time, not avoidance.

Revocation reality (verified 2026-06-10): there is **no programmatic
revocation** — no CLI command (`claude auth` has only login/logout/status;
[#48373](https://github.com/anthropics/claude-code/issues/48373) requests
`setup-token --list/--revoke`, unimplemented), no documented OAuth revocation
endpoint, and `claude auth logout` is local-only
([#34198](https://github.com/anthropics/claude-code/issues/34198)). The only
kill switch is manual: <https://claude.ai/settings/claude-code>, trash-icon
per token — with no bulk action, near-zero per-token metadata, and a reported
3–4 day revocation lag
([#43801](https://github.com/anthropics/claude-code/issues/43801)). Plan
accordingly: yolo must be honest that "revoke" is mostly out of our hands, and
the most useful thing we can do is record exact mint timestamps so a token
stands a chance of being identified on that page.

## Changes

### 1. Flip the default: `--auth oauth-token`

- `yolo.py`: change the argparse default for `--auth` (currently `keychain`,
  ~line 824) to `oauth-token`. `AUTH_CHOICES` is unchanged; `keychain` remains
  fully supported as an explicit choice (CLI or `auth` config key).
- The launch path already branches correctly: oauth-token mode skips
  `ensure_logged_in`/`extract_credentials` and just adds
  `-e CLAUDE_CODE_OAUTH_TOKEN=…` (line ~1151). No structural change.
- **Consent prompt on implicit mint.** `ensure_oauth_token`'s fall-through to
  `generate_oauth_token` happens on a plain launch; today it prints one line
  and goes straight into the browser flow. Add an explicit confirm before the
  flow (only on this implicit path — running `yolo setup-token` *is* consent,
  so the verb skips the prompt):

  ```
  No OAuth token cached for ~/.claude. yolo will mint a 1-year Claude Code
  token (browser authorization), stored encrypted in your macOS keychain.
  It can later be removed locally with `yolo forget-token`; server-side
  revocation is only possible at https://claude.ai/settings/claude-code.
  Proceed? [Y/n]
  ```

  Decline → `sys.exit` with guidance: `--auth keychain` for the old behavior,
  or `yolo setup-token` / `CLAUDE_CODE_OAUTH_TOKEN` to supply a token. The
  existing `isatty` gate already covers non-interactive launches.
- Keep the AWS-flags warning logic and everything else as-is. Note in the
  `--auth` help text that `keychain` is unsafe for concurrent sessions.

### 2. Token registry: `~/.claude-yolo/tokens.json`

Non-secret metadata about tokens yolo has minted. The keychain remains the
sole store of the secret; the registry exists so the user can (a) enumerate
yolo's tokens across config dirs and (b) match a token by mint timestamp on
the claude.ai page — the hash8 in the service name is one-way, so the
service-name → config-dir mapping is recorded here or lost.

- Location: `~/.claude-yolo/tokens.json`, beside `projects.json` — host-side
  only, never mounted (same safety property as the rest of the config).
- Shape: an object keyed by keychain service name:

  ```json
  {
    "claude-yolo-oauth-token": {
      "config_dir": null,
      "minted": "2026-06-10T14:32:05-07:00"
    },
    "claude-yolo-oauth-token-a1b2c3d4": {
      "config_dir": "/Users/peter/.claude-work",
      "minted": "2026-05-01T09:12:44-07:00"
    }
  }
  ```

- Single writer: `_store_oauth_token` (both mint paths — the `setup-token`
  verb and the implicit launch mint — already funnel through it). On re-mint
  the entry is overwritten; print the *previous* mint timestamp so the user
  can hunt the now-orphaned token at claude.ai if they care.
- Readers reconcile against the keychain: an entry whose service no longer
  exists in the keychain (checked with `security find-generic-password -s …`
  *without* `-w` — attributes only, no secret read) is reported as stale, in
  the spirit of the dangling-projects.json-key warning. Malformed file →
  `sys.exit` naming the file, like `_read_projects_file`.
- Helpers modeled on the projects.json pair: `_read_tokens_file` /
  `_write_token_entry` (read-modify-write the whole file).

### 3. Expiry warning at launch (registry-independent)

Warn when the active config dir's token is about to expire, so it doesn't
silently 401 inside a container a year from now.

- Source of truth is the keychain itself, not the registry: keychain items
  carry creation/modification timestamps, and since we upsert with
  `add-generic-password -U`, **`mdat` = last mint time**. New helper
  `_keychain_mdat(service) -> datetime | None` parses the `"mdat"` attribute
  from `security find-generic-password -s <service>` (no `-w`); return None
  on any parse trouble (then skip the warning — it's advisory).
- Constants: `TOKEN_LIFETIME_DAYS = 365` (an assumption — the token string is
  opaque and the mint flow states no expiry; comment this), and
  `EXPIRY_WARN_DAYS = 7`.
- Check on oauth-token launches only, and only when the token came from the
  keychain (an env-supplied `CLAUDE_CODE_OAUTH_TOKEN` has unknowable expiry —
  skip). If `mdat + 365d < now + 7d`, print a stderr warning naming the
  config dir, the estimated expiry date, and the remedy: `yolo setup-token`
  to re-mint (plus the claude.ai link for cleaning up the old token).
- This check works for tokens minted before the registry existed, and can't
  drift: the timestamp lives and dies with the keychain entry.

### 4. `yolo tokens` — inventory verb

Terminal verb (no container), listing the registry as a table:

```
SERVICE                              CONFIG DIR            MINTED       EXPIRES~     STATUS
claude-yolo-oauth-token              (default ~/.claude)   2026-06-10   2027-06-10   ok
claude-yolo-oauth-token-a1b2c3d4     ~/.claude-work        2026-05-01   2027-05-01   ok
claude-yolo-oauth-token-99887766     ~/.claude-old         2026-01-15   2027-01-15   stale (not in keychain)
```

- `EXPIRES~` is minted + 365d, tilde because it's an estimate.
- `STATUS`: `ok` (keychain entry present), `stale` (registry entry with no
  keychain item), and append a note row or footnote when the keychain `mdat`
  disagrees materially with the registry `minted` (re-minted outside yolo).
- Footer: the claude.ai settings URL and one line on how to use MINTED to
  identify a token there.
- Dispatch with the other terminal verbs (`list`/`finish`/`setup-token`);
  reads only `tokens.json` + keychain attributes, so it must work even when
  no project entry matches (exempt from guardrails, like `list`).

### 5. `yolo forget-token` — local removal, honest messaging

Terminal verb; honours `--config-dir` (and a config-file `config-dir`), so it
targets the same service name a launch would read — dispatch it where
`setup-token` sits, after config-dir resolution.

- Does: `security delete-generic-password -s <service>`, remove the registry
  entry, done.
- Says (this wording is the point — key requirement #3):

  ```
  Forgotten: deleted the cached token for ~/.claude-work from the keychain
  (minted 2026-05-01). yolo will no longer use it.

  NOTE: the token itself is still valid server-side until ~2027-05-01.
  Anthropic provides no API or CLI to revoke it — the only revocation path
  is manual, at https://claude.ai/settings/claude-code — and in practice
  identifying one token there may be impossible (the list shows no usable
  metadata and accumulates entries from normal Claude Code usage; see
  claude-code issues #48373 and #59378). Revocation, when it works, may
  also lag by days (#43801). This is outside yolo's control.
  ```

- No keychain entry to delete → say so, and still remove a stale registry
  entry if present.
- Named `forget-token`, not `revoke-token`: the verb must not claim a power
  it doesn't have.

### 6. README: "Tokens & revocation" section + flip notes

- Document the new default: first run per config dir mints a 1-year token
  (interactive browser flow, consent prompt), stored in the macOS keychain;
  one token per config dir; `yolo tokens` / `yolo forget-token` /
  `yolo setup-token` lifecycle.
- State plainly: revocation is manual-only at
  <https://claude.ai/settings/claude-code>; link the support article
  (<https://support.claude.com/en/articles/10310342-how-do-i-log-out-of-all-active-sessions>)
  and the issues:
  [#48373](https://github.com/anthropics/claude-code/issues/48373) (no
  list/revoke CLI),
  [#59378](https://github.com/anthropics/claude-code/issues/59378) (token
  accumulation, no bulk revoke),
  [#43801](https://github.com/anthropics/claude-code/issues/43801)
  (revocation lag),
  [#34198](https://github.com/anthropics/claude-code/issues/34198) (logout is
  local-only).
- `setup-token` requires a Pro/Max/Team/Enterprise plan; users outside that
  set `auth: keychain` (one line on how).
- Keychain mode section: keep, but reframe as the opt-in legacy mode with an
  explicit warning about the rotating-refresh-token race for concurrent /
  overlapping sessions.

### 7. CHANGELOG + version

- Behavior change on upgrade: users with no `auth` key get the consent prompt
  and mint flow on their next interactive launch (scripted launches fail with
  guidance via the existing isatty gate — nothing mints silently). Call this
  out at the top of the 0.6.0 notes with the one-liner to opt out
  (`echo '{"auth": "keychain"}' > ~/.yolo.json` or per-project
  `yolo config --auth keychain`).
- Minor bump (0.5.0 → 0.6.0) via bump-my-version.

### 8. Tests

- `tests/conftest.py`: the `run_cli` fixture must now also stub
  `ensure_oauth_token` (return a dummy token) since default-mode launches hit
  it instead of `ensure_logged_in`/`extract_credentials`. Keep the old stubs
  for explicit `--auth keychain` tests.
- `test_cli.py`: update default-launch assertions (expect
  `-e CLAUDE_CODE_OAUTH_TOKEN=…`, no `.credentials.json` mount); keychain
  assertions move behind explicit `--auth keychain`.
- New `test_tokens.py` (or extend `test_config.py`): registry read/write and
  reconcile (stale entries), `tokens` and `forget-token` verb output
  (keychain `security` calls stubbed), `_keychain_mdat` parsing against a
  captured `security` output sample, expiry-warning threshold logic, consent
  prompt (declined → exit; `setup-token` verb skips it), env-var token skips
  both mint and expiry check.
- `CLAUDE.md`: update the auth section (default flipped, new verbs, registry,
  honest-revocation stance) — likely via the update-claude-md skill after
  implementation.

## Out of scope

- Any attempt at server-side revocation (no endpoint exists; revisit if
  #48373 ships — `forget-token` is the natural place to grow it).
- Detecting concurrent keychain-mode sessions (docker-label check discussed
  2026-06-10): superseded by the default flip; could be a follow-up hardening
  for explicit keychain users, but don't block on it.
- Migrating/cleaning the user's existing pile of claude.ai tokens — outside
  our control by definition.
