# Plan: Labels for forwarded ports

Goal: let a port spec carry an optional human-readable label (`web=8000`), so
that with several forwarded ports the one to open can be picked — from the
`browse` verb or the `wip` dashboard's `b` action — by **either** the
in-container port number **or** the label.

---

## Current state (what already works)

Multiple forwarded ports are already supported end to end; this plan does not
add that, it adds naming on top of it:

- `--port [HOST:]CONTAINER` is repeatable, and `ports` is a list/concat config
  key (`_CONCAT_DESTS`, `yolo.py:1593`). Specs are parsed by `_parse_port_spec`
  (`yolo.py:2346`) and merged/deduped by container port in `_resolve_ports`
  (`yolo.py:2365`), lowest-precedence first, insertion order kept (first
  configured = `browse`'s default).

- Each launch publishes `-p 127.0.0.1:{host or 0}:{container}`
  (`launch_container`, `yolo.py:4904-4909`) and stamps the forwarded container
  ports into the **`yolo.ports` docker label** as a comma-joined list
  (`yolo.py:5087-5092`). That label — not config — is what browse reads back.

- `yolo browse [TOPIC] [--port N]` (`do_browse`, `yolo.py:8289`;
  `browse_session`, `yolo.py:8327`) reads `_forwarded_ports`
  (`yolo.py:8321`), defaults to the first, `--port N` selects another (bare
  container port only; validated in the browse dispatch, `yolo.py:8715-8727`,
  using the `cli_ports` explicit-CLI capture at `yolo.py:8533-8537`).

- The `wip` dashboard's `b` action (`_wip_browse`, `yolo.py:7566`) prompts
  "Which port? 8000, 3000:" when there's more than one and accepts only a
  digit string.

- `config --add-port` / `--remove-port` edit the stored list keyed by container
  port via `_port_container` (`yolo.py:2380`, edit logic `yolo.py:2612-2625`);
  specs are validated at store time (`yolo.py:2553-2554`).

The pain point: with two or more ports, `8000` vs `3000` is a memory test.
Nothing names what each port *is*.

## Design decisions

### 1. Spec syntax: `[NAME=][HOST:]CONTAINER`

Examples: `web=8000`, `api=3000`, `web=9000:8000` (label + host pin), plain
`8000` (unlabeled, unchanged).

Why `NAME=` prefix rather than a trailing `:LABEL` segment:

- **Zero ambiguity, zero migration.** Existing specs contain only digits and
  `:`; splitting once on `=` cleanly peels the label off and hands the
  remainder to the existing `[HOST:]CONTAINER` parser unchanged. A trailing
  `:LABEL` would overload the `:` separator and complicate `rpartition`-based
  parsing (`8000:80` = pin, `8000:web` = label — readable but fragile).

- It reads naturally ("web is 8000") and the same syntax can be reused
  verbatim in the `yolo.ports` docker label (see decision 3).

**Label validation** (in `_parse_port_spec`, so config store-time validation at
`yolo.py:2553` catches bad labels too): must match `[A-Za-z][A-Za-z0-9_-]*`.
That rules out the problem characters by construction — all-digits (would be
ambiguous with a port number when selecting), `,` (the docker-label join
character), `:` and `=` (spec separators). Case-sensitive, exact match.

### 2. Labels are always optional

The user note said "or at least when there's more than one" — resolve that as:
**never required**. Container port numbers remain valid selectors everywhere,
so an unlabeled multi-port setup keeps working exactly as today (numeric
prompt, `--port 3000`). Requiring labels would break every existing multi-port
config for no gain. Labels are pure added convenience.

### 3. Storage: extend the `yolo.ports` label with the same syntax

Stamp labeled entries as `web=8000,3000` (labeled ports as `name=port`,
unlabeled as bare port), replacing today's `8000,3000`. One label, one syntax,
one parser.

Considered and rejected: a second `yolo.port-names` docker label to keep
`yolo.ports` format-stable for old binaries reading new containers. Not worth
it — the binary that launched a session is in practice the one that browses
it, containers are ephemeral, and old-yolo-reads-new-container only breaks
when labels are actually used. New yolo reading an old container's bare-port
label works by construction (bare entries are the unlabeled form).

`_forwarded_ports` (`yolo.py:8321`) changes from `list[int]` to a list of
`(label | None, container_port)` pairs (or a small dict); callers are
`browse_session` and `_wip_browse` only.

### 4. Merge semantics: label rides the spec, container port stays the key

`_resolve_ports` (`yolo.py:2365`) stays keyed by container port; the label is
just part of the value, so a higher layer's spec for the same container port
replaces the lower layer's **wholesale** — including dropping or renaming the
label (consistent with how a `HOST:` pin is replaced today).

New validation after the merge: two *different* container ports carrying the
same label → `sys.exit` with both specs named. (Same label on the same
container port across layers is fine — later wins, that's the point.)

### 5. Selection: one token, number-or-label, accepted everywhere a port is picked

A "port selection" becomes: digits → container port; otherwise → label
(matched against the session's forwarded labels). Applied in all three places:

- **`yolo browse --port web`** — the browse dispatch (`yolo.py:8715-8727`)
  currently requires the single `--port` value to be a bare container port.
  Relax it: bare digits or a bare label are a selection; anything with `:` or
  `=` stays an error ("pass the container port or its label"). `select` flows
  into `browse_session` as `int | str`.

  (Deliberately *not* adding `yolo browse web` as a positional — the optional
  positional after `browse` is already the TOPIC, and overloading it would
  make `yolo browse web` ambiguous between "topic web" and "port web".)

- **`browse_session(cid, select=…)`** (`yolo.py:8327`) — resolve a str select
  against the labels from the `yolo.ports` label, an int against the ports;
  the existing `YoloError` for an unknown selection now lists ports *with*
  their labels: `forwarded: web (8000), api (3000)`.

- **`_wip_browse`** (`yolo.py:7566`) — prompt becomes
  `Which port? web (8000), api (3000): ` (bare `8000` shown for unlabeled
  entries) and accepts either form. Empty still cancels; unknown token still
  reports `not a forwarded port: {choice}`.

### 6. Config editing: `--remove-port` also matches by label

- `--add-port web=8000` keeps its replace-by-container-port semantics
  (`yolo.py:2619-2623`), so it's also how you *rename or attach* a label to an
  already-listed port. `_port_container` (`yolo.py:2380`) must learn to strip
  a `NAME=` prefix (staying lenient/no-validation, per its docstring).

- `--remove-port` accepts a container port (any `NAME=`/`HOST:` decoration on
  the stored spec ignored, as today) **or** a label (matches the stored spec
  carrying that label).

### 7. Surfacing labels (discoverability)

- **Container system prompt** — the forwarded-ports line in
  `build_claude_args` (`yolo.py:4160-4168`) includes labels:
  `Container port(s) 8000 (web), 3000 (api) are forwarded…`, so Claude knows
  which service belongs on which port.

- **`ps` / `wip` PORTS column** — `_ps_rows` (`yolo.py:6370`) and `_wip_ps`
  (`yolo.py:6773`) already query docker ps with a format string; add
  `yolo.ports` to the queried labels and have `_condense_ports`
  (`yolo.py:6302`) take the label map so mapped pairs render as
  `web:55001->8000` when labeled. Small, self-contained; makes labels visible
  in the same view you pick them from.

- **`config` interactive editor hint** — the `ports` hint string
  (`yolo.py:7923`) becomes `[NAME=][HOST:]CONTAINER`.

## Implementation steps

1. **Parsing** — extend `_parse_port_spec` to return `(label, host, container)`
   (split once on `=`, validate the label regex, delegate the rest); update
   `_resolve_ports` to carry the label, keep container-port keying, and add
   the duplicate-label check; teach `_port_container` to strip a `NAME=`
   prefix. Update the two `--port` help strings (`yolo.py:3518-3535`,
   `yolo.py:3815-3825`) and the config hint (`yolo.py:7923`).

2. **Launch side** — thread the 3-tuples through `launch_container`: `-p`
   publishing ignores the label; the `yolo.ports` label stamp
   (`yolo.py:5087-5092`) emits `[name=]port` entries; `build_claude_args`'s
   prompt line includes labels.

3. **Read-back side** — `_forwarded_ports` parses `[name=]port` entries
   (backward compatible with bare-port labels from old sessions);
   `browse_session` takes `select: int | str | None` and resolves labels;
   browse dispatch in `main` accepts a digits-or-label `--port` value;
   `_wip_browse` shows `label (port)` and accepts both.

4. **Config verb** — `--remove-port` matches by label as well as container
   port (error message mentions both forms).

5. **ps/wip PORTS column** — add `yolo.ports` to the docker-ps format queries,
   pass the parsed label map into `_condense_ports`, render `web:HOST->CONT`.

6. **Docs** — README port-forwarding section (`Forwarded ports` around
   README.md:444-481: spec syntax, browse-by-label example, `--remove-port`
   by label) and the `ports` config-key section; ARCHITECTURE.md `--port`
   bullet (~line 319-330) and the `yolo.ports` label format; CHANGELOG
   `## Unreleased` entry.

## Tests (`tests/test_ports.py`, per the coverage map)

- `_parse_port_spec`: `web=8000`, `web=9000:8000` parse; rejected labels —
  all-digits (`80=8000`), bad chars (`a,b=80`, `a=b=80`, `=8000`), empty.

- `_resolve_ports`: label carried through; higher layer replacing a labeled
  spec drops/renames the label; duplicate label on two container ports exits.

- Launch: `--port web=8000 --port 3000` stamps `yolo.ports=web=8000,3000` and
  still publishes both `-p 127.0.0.1:0:…` args; system prompt line mentions
  `8000 (web)`.

- Browse: `--port web` selects the labeled port; `--port 3000` (numeric) still
  works alongside labels; unknown label errors listing `web (8000), …`;
  reading an old-style bare `yolo.ports` label still works.

- Config: `--add-port web=8000` replaces a stored bare `8000` (label attach);
  `--remove-port web` removes the labeled entry; `--remove-port` unknown label
  errors.

- wip: `b` prompt shows labels and accepts a label (wherever the existing
  `_wip_browse` prompt test lives — check the wip/dashboard test file).

- ps/wip: PORTS column renders `web:HOST->CONT` for a labeled mapping.

## Explicitly out of scope

- Making labels mandatory (even with >1 port) — see decision 2.
- A `yolo browse LABEL` positional (ambiguous with TOPIC) — see decision 5.
- Any change to loopback-only binding, host-port assignment, or the
  launch-time-only nature of port mappings.
