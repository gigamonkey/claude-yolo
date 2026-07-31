"""Port forwarding: the --port/`ports` axis, the yolo.ports label, and `browse`.

Launch-side tests drive `cy.main()` through the `run_cli` fixture and assert on
the assembled `docker run` argv; `browse` tests stub the docker queries
(`running_container_for`, `_container_label`, `_docker_port`) and the `_open_url`
seam, asserting on the printed URL and what would have been opened.
"""

import json

import pytest


@pytest.fixture
def dirs(tmp_path):
    """A fresh (home, work) pair of real directories."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    return home, work


def claude_args(cy, argv):
    """The args after the image name — i.e. what's passed to `claude`."""
    i = next(i for i, a in enumerate(argv) if a.startswith(cy.DOCKER_IMAGE_REPO + ":"))
    return argv[i + 1 :]


def system_prompt(cy, argv):
    args = claude_args(cy, argv)
    return args[args.index("--append-system-prompt") + 1]


# --- spec parsing -----------------------------------------------------------


def test_parse_port_spec_bare_and_pinned(cy):
    assert cy._parse_port_spec("8000") == (None, None, 8000)
    assert cy._parse_port_spec("9000:8000") == (None, 9000, 8000)


def test_parse_port_spec_labeled(cy):
    assert cy._parse_port_spec("web=8000") == ("web", None, 8000)
    assert cy._parse_port_spec("web-2=9000:8000") == ("web-2", 9000, 8000)


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        "0",
        "65536",
        ":8000",
        "8000:",
        "1.2.3.4:80",
        "a:80",
        "80=8000",  # an all-digits label would be ambiguous with a port number
        "=8000",
        "a b=8000",
        "web=",
        "a=b=8000",
    ],
)
def test_parse_port_spec_rejects_malformed(cy, bad):
    with pytest.raises(SystemExit, match="port"):
        cy._parse_port_spec(bad)


def test_resolve_ports_dedupes_by_container_port_later_wins(cy):
    # same container port across layers: the later (higher-precedence) spec wins,
    # but the first occurrence keeps its position (it stays browse's default)
    assert cy._resolve_ports(["8000", "3000", "9000:8000"]) == [
        (None, 9000, 8000),
        (None, None, 3000),
    ]


def test_resolve_ports_later_spec_replaces_label_too(cy):
    # the higher layer's spec wins wholesale: a bare re-spec drops the label
    assert cy._resolve_ports(["web=8000", "8000"]) == [(None, None, 8000)]
    assert cy._resolve_ports(["8000", "web=9000:8000"]) == [("web", 9000, 8000)]


def test_resolve_ports_rejects_duplicate_label_across_ports(cy):
    with pytest.raises(SystemExit, match="label 'web'"):
        cy._resolve_ports(["web=8000", "web=3000"])


# --- launch assembly --------------------------------------------------------


def test_no_ports_by_default(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli([], home=home, cwd=work)
    assert flag_values(argv, "-p") == []
    assert not any(lbl.startswith("yolo.ports=") for lbl in flag_values(argv, "--label"))
    assert "forwarded to the host" not in system_prompt(cy, argv)


def test_port_publishes_loopback_dynamic_and_stamps_label(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--port", "8000"], home=home, cwd=work)
    assert flag_values(argv, "-p") == ["127.0.0.1:0:8000"]
    assert "yolo.ports=8000" in flag_values(argv, "--label")
    # the prompt tells Claude the port is forwarded and to bind 0.0.0.0
    prompt = system_prompt(cy, argv)
    assert "8000" in prompt and "0.0.0.0" in prompt and "yolo browse" in prompt


def test_port_host_pin(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--port", "8000:8000"], home=home, cwd=work)
    assert flag_values(argv, "-p") == ["127.0.0.1:8000:8000"]


def test_labeled_port_stamps_label_and_names_it_in_prompt(cy, run_cli, flag_values, dirs):
    home, work = dirs
    argv = run_cli(["--port", "web=8000", "--port", "3000"], home=home, cwd=work)
    # the label never reaches the -p publishing, only the yolo.ports label
    assert flag_values(argv, "-p") == ["127.0.0.1:0:8000", "127.0.0.1:0:3000"]
    assert "yolo.ports=web=8000,3000" in flag_values(argv, "--label")
    assert "8000 (web), 3000" in system_prompt(cy, argv)


def test_malformed_port_spec_exits(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="port"):
        run_cli(["--port", "nope"], home=home, cwd=work)


def test_ports_concatenate_across_layers_and_cli_wins_per_container_port(
    cy, run_cli, flag_values, dirs
):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"ports": "8000"}))
    (home / ".claude-yolo").mkdir()
    (home / ".claude-yolo" / "projects.json").write_text(
        json.dumps({str(work): {"ports": ["3000"]}})
    )
    argv = run_cli(["--port", "9000:3000"], home=home, cwd=work)
    # global 8000 stays dynamic; the CLI's pin overrides the project's bare 3000
    assert flag_values(argv, "-p") == ["127.0.0.1:0:8000", "127.0.0.1:9000:3000"]
    assert "yolo.ports=8000,3000" in flag_values(argv, "--label")


def test_malformed_port_spec_does_not_break_terminal_verbs(cy, run_cli, dirs, capsys):
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"ports": "nope"}))
    run_cli(["tokens"], home=home, cwd=work)  # resolved only on launch paths
    assert "No tokens recorded" in capsys.readouterr().out


# --- config verb ------------------------------------------------------------


def entry(home, work):
    projects = json.loads((home / ".claude-yolo" / "projects.json").read_text())
    e = next(v for v in projects.values() if v.get("dir") == str(work))
    return {k: v for k, v in e.items() if k != "dir"}


def test_config_persists_ports(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "8000", "--port", "9000:3000"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["8000", "9000:3000"]}


def test_config_rejects_malformed_port(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="port"):
        run_cli(["config", "--port", "nope"], home=home, cwd=work)


def test_config_add_port_appends(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "8000"], home=home, cwd=work)
    run_cli(["config", "--add-port", "3000"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["8000", "3000"]}


def test_config_add_port_updates_pin_for_same_container_port(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "8000"], home=home, cwd=work)
    run_cli(["config", "--add-port", "9000:8000"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["9000:8000"]}


def test_config_remove_port_ignores_host_pin(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "9000:8000", "--port", "3000"], home=home, cwd=work)
    run_cli(["config", "--remove-port", "8000"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["3000"]}


def test_config_add_port_attaches_label_to_listed_port(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "8000"], home=home, cwd=work)
    run_cli(["config", "--add-port", "web=8000"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["web=8000"]}


def test_config_remove_port_by_label(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "web=8000", "--port", "3000"], home=home, cwd=work)
    run_cli(["config", "--remove-port", "web"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["3000"]}


def test_config_remove_labeled_port_by_number(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "web=8000", "--port", "3000"], home=home, cwd=work)
    run_cli(["config", "--remove-port", "8000"], home=home, cwd=work)
    assert entry(home, work) == {"ports": ["3000"]}


def test_launch_rejects_duplicate_labels(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="label 'web'"):
        run_cli(["--port", "web=8000", "--port", "web=3000"], home=home, cwd=work)


def test_config_remove_absent_port_errors(cy, run_cli, dirs):
    home, work = dirs
    run_cli(["config", "--port", "8000"], home=home, cwd=work)
    with pytest.raises(SystemExit, match="--remove-port"):
        run_cli(["config", "--remove-port", "3000"], home=home, cwd=work)


def test_config_port_conflicts_with_add_port(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="--add-port"):
        run_cli(["config", "--port", "8000", "--add-port", "3000"], home=home, cwd=work)


def test_add_port_only_applies_to_config(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="--add-port"):
        run_cli(["--add-port", "8000"], home=home, cwd=work)


# --- the browse verb --------------------------------------------------------


@pytest.fixture
def browse_env(cy, monkeypatch):
    """Stub the docker queries behind `browse`; returns the opened-URL capture."""
    opened = []
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: "abc123")
    monkeypatch.setattr(cy, "_container_label", lambda cid, key: "8000,3000")
    monkeypatch.setattr(cy, "_docker_port", lambda cid, port: {8000: 55001, 3000: 55002}[port])
    monkeypatch.setattr(cy, "_open_url", lambda url: opened.append(url))
    return opened


def test_browse_opens_first_forwarded_port(cy, run_cli, dirs, browse_env, capsys):
    home, work = dirs
    run_cli(["browse"], home=home, cwd=work)
    assert "http://127.0.0.1:55001/" in capsys.readouterr().out
    assert browse_env == ["http://127.0.0.1:55001/"]


def test_browse_port_selects_another(cy, run_cli, dirs, browse_env, capsys):
    home, work = dirs
    run_cli(["browse", "--port", "3000"], home=home, cwd=work)
    assert browse_env == ["http://127.0.0.1:55002/"]


def test_browse_print_skips_open(cy, run_cli, dirs, browse_env, capsys):
    home, work = dirs
    run_cli(["browse", "--print"], home=home, cwd=work)
    assert "http://127.0.0.1:55001/" in capsys.readouterr().out
    assert browse_env == []


def test_browse_config_ports_are_not_a_selection(cy, run_cli, dirs, browse_env):
    # a config-supplied `ports` list must not masquerade as an explicit --port
    home, work = dirs
    (home / ".yolo.json").write_text(json.dumps({"ports": ["3000"]}))
    run_cli(["browse"], home=home, cwd=work)
    assert browse_env == ["http://127.0.0.1:55001/"]  # still the label's first port


def test_browse_unforwarded_port_errors(cy, run_cli, dirs, browse_env):
    home, work = dirs
    with pytest.raises(SystemExit, match="isn't forwarded"):
        run_cli(["browse", "--port", "9999"], home=home, cwd=work)


def test_browse_rejects_multiple_or_pinned_port_selections(cy, run_cli, dirs, browse_env):
    home, work = dirs
    with pytest.raises(SystemExit, match="at most one"):
        run_cli(["browse", "--port", "8000", "--port", "3000"], home=home, cwd=work)
    with pytest.raises(SystemExit, match="container.*port or the label"):
        run_cli(["browse", "--port", "9000:8000"], home=home, cwd=work)


@pytest.fixture
def labeled_browse_env(cy, browse_env, monkeypatch):
    """browse_env, but the session's yolo.ports label carries `web=8000`."""
    monkeypatch.setattr(cy, "_container_label", lambda cid, key: "web=8000,3000")
    return browse_env


def test_browse_selects_by_label(cy, run_cli, dirs, labeled_browse_env):
    home, work = dirs
    run_cli(["browse", "--port", "web"], home=home, cwd=work)
    assert labeled_browse_env == ["http://127.0.0.1:55001/"]


def test_browse_labeled_port_still_selects_by_number(cy, run_cli, dirs, labeled_browse_env):
    home, work = dirs
    run_cli(["browse", "--port", "8000"], home=home, cwd=work)
    assert labeled_browse_env == ["http://127.0.0.1:55001/"]


def test_browse_unknown_label_errors_listing_forwarded(cy, run_cli, dirs, labeled_browse_env):
    home, work = dirs
    with pytest.raises(SystemExit, match=r"labeled 'api'.*web \(8000\), 3000"):
        run_cli(["browse", "--port", "api"], home=home, cwd=work)


def test_browse_without_running_container_errors(cy, run_cli, dirs, browse_env, monkeypatch):
    home, work = dirs
    monkeypatch.setattr(cy, "running_container_for", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="no yolo session running"):
        run_cli(["browse"], home=home, cwd=work)


def test_browse_without_forwarded_ports_errors(cy, run_cli, dirs, browse_env, monkeypatch):
    home, work = dirs
    monkeypatch.setattr(cy, "_container_label", lambda cid, key: "")
    with pytest.raises(SystemExit, match="without any forwarded ports"):
        run_cli(["browse"], home=home, cwd=work)


def test_browse_topic_queries_by_worktree_label(cy, run_cli, dirs, browse_env, monkeypatch):
    home, work = dirs
    queries = {}
    monkeypatch.setattr(cy, "_worktree_dir", lambda topic, home: (work / "wt", work, "the-slug"))
    monkeypatch.setattr(
        cy,
        "running_container_for",
        lambda slug, topic=None, *, cwd=None: queries.update(slug=slug, topic=topic) or "abc123",
    )
    run_cli(["browse", "fix-auth"], home=home, cwd=work)
    assert queries == {"slug": "the-slug", "topic": "fix-auth"}


def test_print_only_applies_to_browse(cy, run_cli, dirs):
    home, work = dirs
    with pytest.raises(SystemExit, match="--print"):
        run_cli(["start", "--print"], home=home, cwd=work)


# --- the wip dashboard's port picker ----------------------------------------


class _Term:
    """A stub of the dashboard terminal: canned prompt_line answer, prompt capture."""

    def __init__(self, answer=""):
        self.answer = answer
        self.prompts = []

    def prompt_line(self, prompt):
        self.prompts.append(prompt)
        return self.answer


@pytest.fixture
def wip_browse_env(cy, monkeypatch):
    """Stub the docker queries behind _wip_browse; returns the opened-URL capture."""
    opened = []
    monkeypatch.setattr(cy, "_container_label", lambda cid, key: "web=8000,3000")
    monkeypatch.setattr(cy, "_docker_port", lambda cid, port: {8000: 55001, 3000: 55002}[port])
    monkeypatch.setattr(cy, "_open_url", lambda url: opened.append(url))
    return opened


def test_wip_browse_prompt_shows_labels_and_accepts_one(cy, wip_browse_env):
    term = _Term("web")
    assert "55001" in cy._wip_browse({"cid": "abc123"}, term)
    assert term.prompts == ["Which port? web (8000), 3000: "]
    assert wip_browse_env == ["http://127.0.0.1:55001/"]


def test_wip_browse_still_accepts_a_port_number(cy, wip_browse_env):
    assert "55002" in cy._wip_browse({"cid": "abc123"}, _Term("3000"))
    assert wip_browse_env == ["http://127.0.0.1:55002/"]


def test_wip_browse_rejects_unknown_choice(cy, wip_browse_env):
    assert cy._wip_browse({"cid": "abc123"}, _Term("api")) == "not a forwarded port: api"
    assert wip_browse_env == []


# --- the ps PORTS column ----------------------------------------------------


def test_condense_ports_prefixes_labeled_mappings(cy):
    raw = "127.0.0.1:55001->8000/tcp, 127.0.0.1:55002->3000/tcp"
    assert cy._condense_ports(raw, "web=8000,3000") == "web:55001->8000,55002->3000"
    # an old-style all-bare label leaves the rendering unchanged
    assert cy._condense_ports(raw, "8000,3000") == "55001->8000,55002->3000"


# --- the docker port query --------------------------------------------------


def test_docker_port_parses_first_ipv4_line(cy, monkeypatch):
    import subprocess

    out = "[::1]:55009\n127.0.0.1:55001\n"
    monkeypatch.setattr(
        cy.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, out, ""),
    )
    assert cy._docker_port("abc123", 8000) == 55001


def test_docker_port_no_mapping_exits(cy, monkeypatch):
    import subprocess

    monkeypatch.setattr(
        cy.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""),
    )
    with pytest.raises(cy.YoloError, match="no host mapping"):
        cy._docker_port("abc123", 8000)
