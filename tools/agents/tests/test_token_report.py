"""Mesure des tokens par ticket et routage des modèles (#19).

Base SQLite au schéma réel du dashboard (`dashboard.db.init_schema`), journal local factice.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from collections.abc import Sequence
from pathlib import Path

import jsonschema  # type: ignore[import-untyped]  # pas de stubs (dépendance de dev)
import pytest

import token_report as tr
from dashboard.db import init_schema

AGENTS = Path(__file__).resolve().parents[1]
REPO = AGENTS.parents[1]
ROUTING = AGENTS / "config" / "model-routing.yml"
SCRIPT = AGENTS / "bin" / "token-report.sh"
SECRET = "Voici mon prompt : lis /Users/x/secret.txt"  # noqa: S105 - faux contenu


def _db(path: Path, points: Sequence[tuple[str, str | None, str, int]]) -> Path:
    """points : (session, agent, modèle, tokens), tous datés du 18/11 10:00 UTC."""
    conn = sqlite3.connect(path)
    init_schema(conn)
    with conn:
        conn.executemany(
            "INSERT INTO token_usage (point_id, ts, session_id, agent_id, model, type, count)"
            " VALUES (?, ?, ?, ?, ?, 'output', ?)",
            [
                (f"p{i}", "2026-11-18T10:00:00.000001+00:00", s, a, m, n)
                for i, (s, a, m, n) in enumerate(points)
            ],
        )
    conn.close()
    return path


def _events(path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executemany(
            "INSERT INTO events (event_id, ts, hook_event_name, session_id) VALUES (?, ?, ?, ?)",
            [(f"e{i}", "2026-11-18T10:00:00+00:00", h, s) for i, (s, h) in enumerate(rows)],
        )
    conn.close()


def _journal(path: Path, links: dict[str, int]) -> Path:
    for session, ticket in links.items():
        tr.append_record(path, tr.link_record(session, ticket, "2026-11-18T09:00:00+00:00"))
    return path


def _run(*args: str) -> str:
    return "\n".join(tr.run(list(args)))


@pytest.fixture
def routing() -> tr.Routing:
    return tr.load_routing(ROUTING)


def _two_tickets(tmp_path: Path) -> tuple[Path, Path]:
    db = _db(
        tmp_path / "vva.db",
        [
            ("s42", "dev-backend", "claude-sonnet-5-5", 700),
            ("s42", "scout", "claude-haiku-5-5", 200),
            ("s42", None, "claude-opus-5-5", 100),
            ("s43", "dev-backend", "claude-sonnet-5-5", 300),
            ("s-libre", "qa", "claude-sonnet-5-5", 50),
        ],
    )
    return db, _journal(tmp_path / "journal.jsonl", {"s42": 42, "s43": 43})


# Scénario : rapport par ticket


def test_report_shows_tokens_per_ticket_and_agent(tmp_path: Path) -> None:
    db, journal = _two_tickets(tmp_path)
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    lines = out.splitlines()
    assert len(lines) <= 15
    assert "#42 1 000 : dev-backend 700, scout 200, (fil principal) 100" in lines
    assert "#43 300 : dev-backend 300" in lines
    assert "sans ticket 50 : qa 50" in lines
    assert any(line.startswith("Modèles :") and "claude-sonnet-5-5 1 050" in line for line in lines)


def test_report_stays_within_15_lines_with_many_tickets(tmp_path: Path) -> None:
    points = [(f"s{n}", "dev-backend", "claude-sonnet-5-5", n * 10) for n in range(1, 40)]
    db = _db(tmp_path / "vva.db", points)
    journal = _journal(tmp_path / "j.jsonl", {f"s{n}": n for n in range(1, 40)})
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    assert len(out.splitlines()) <= 15
    assert "autres tickets" in out


def test_report_flags_tickets_above_twice_the_median(tmp_path: Path) -> None:
    db = _db(
        tmp_path / "vva.db",
        [("a", "qa", "claude-sonnet-5-5", 100), ("b", "qa", "claude-sonnet-5-5", 120)]
        + [("c", "qa", "claude-sonnet-5-5", 900)],
    )
    journal = _journal(tmp_path / "j.jsonl", {"a": 1, "b": 2, "c": 3})
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    assert "> 2× médiane (120) : #3 (900)" in out


def test_report_gives_tool_failure_ratio_per_ticket(tmp_path: Path) -> None:
    db, journal = _two_tickets(tmp_path)
    _events(db, [("s42", "PostToolUse")] * 3 + [("s42", "PostToolUseFailure")])
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    assert "Échecs d'outils : #42 1/4 (25 %)" in out


def test_session_switching_branch_is_split_by_time(tmp_path: Path) -> None:
    conn = sqlite3.connect(_db(tmp_path / "vva.db", []))
    with conn:
        conn.executemany(
            "INSERT INTO token_usage (point_id, ts, session_id, agent_id, model, type, count)"
            " VALUES (?, ?, 's', 'qa', 'claude-sonnet-5-5', 'input', ?)",
            [("p1", "2026-11-18T10:00:00+00:00", 10), ("p2", "2026-11-18T12:00:00+00:00", 20)],
        )
    conn.close()
    journal = tmp_path / "j.jsonl"
    tr.append_record(journal, tr.link_record("s", 7, "2026-11-18T09:00:00+00:00"))
    tr.append_record(journal, tr.link_record("s", 8, "2026-11-18T11:00:00+00:00"))
    out = _run("report", "--db", str(tmp_path / "vva.db"), "--journal", str(journal))
    assert "#7 10 : qa 10" in out
    assert "#8 20 : qa 20" in out


def test_missing_database_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="base introuvable"):
        _run("report", "--db", str(tmp_path / "absente.db"))


# Rattachement session -> ticket


@pytest.mark.parametrize(
    ("branch", "ticket"),
    [("feat/19-mesure-tokens", 19), ("fix/59-py39", 59), ("main", None), ("feat/x-1", None)],
)
def test_ticket_of_branch(branch: str, ticket: int | None) -> None:
    assert tr.ticket_of_branch(branch) == ticket


def test_link_reads_hook_json_and_records_the_branch_ticket(tmp_path: Path) -> None:
    repo = tmp_path / "poste"
    subprocess.run(["git", "init", "-q", "-b", "feat/42-x", str(repo)], check=True)  # noqa: S603, S607
    journal = tmp_path / "j.jsonl"
    hook = json.dumps({"session_id": "s1", "cwd": str(repo), "prompt": SECRET})
    tr.link(hook, journal)
    tr.link(hook, journal)  # même ticket : pas de doublon
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [(r["kind"], r["session_id"], r["ticket"]) for r in records] == [("link", "s1", 42)]
    assert SECRET not in journal.read_text()


def test_link_never_fails(tmp_path: Path) -> None:
    tr.link("pas du json", tmp_path / "j.jsonl")
    tr.link(json.dumps({"session_id": "s", "cwd": str(tmp_path / "absent")}), tmp_path / "j")
    assert not (tmp_path / "j.jsonl").exists()


# Routage


def test_sonnet_is_the_default(routing: tr.Routing) -> None:
    assert tr.route(routing, "dev-backend") == ("claude-sonnet-5-5", None)


def test_scout_uses_haiku(routing: tr.Routing) -> None:
    assert tr.route(routing, "scout")[0] == "claude-haiku-5-5"
    assert tr.route(routing, "scout", labels=["complex"], failures=5)[0] == "claude-haiku-5-5"


def test_complex_label_promotes_to_opus(routing: tr.Routing) -> None:
    assert tr.route(routing, "dev-backend", labels=["complex"]) == ("claude-opus-5-5", "complex")


def test_two_failures_promote_to_opus(routing: tr.Routing) -> None:
    assert tr.route(routing, "dev-backend", failures=1)[0] == "claude-sonnet-5-5"
    assert tr.route(routing, "dev-backend", failures=2) == ("claude-opus-5-5", "2 échecs")


# Scénario : escalade


def test_two_sonnet_failures_then_relaunch_uses_opus_and_report_says_so(tmp_path: Path) -> None:
    db, journal = _two_tickets(tmp_path)
    args = ["--journal", str(journal), "--routing", str(ROUTING)]
    _run("fail", "42", "dev-backend", *args)
    _run("fail", "42", "dev-backend", *args)
    assert _run("route", "dev-backend", "--ticket", "42", *args) == "claude-opus-5-5"
    out = _run("report", "--db", str(db), *args)
    assert "Promotions Opus : #42 dev-backend (2 échecs)" in out


def test_unrecorded_opus_use_is_reported(tmp_path: Path) -> None:
    db = _db(tmp_path / "vva.db", [("s", "qa", "claude-opus-5-5", 10)])
    journal = _journal(tmp_path / "j.jsonl", {"s": 5})
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    assert "Promotions Opus : #5 qa (non tracée)" in out


# Scénario : aucune donnée sensible


def test_report_contains_no_prompt_nor_session_id(tmp_path: Path) -> None:
    db = _db(tmp_path / "vva.db", [("sess-secrete-123", SECRET, "claude-sonnet-5-5", 10)])
    journal = _journal(tmp_path / "j.jsonl", {"sess-secrete-123": 9})
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    assert SECRET not in out
    assert "sess-secrete-123" not in out
    assert "#9 10 : ? 10" in out


# Contrats et compatibilité


def test_journal_records_match_the_contract(tmp_path: Path) -> None:
    schema = json.loads((REPO / "contracts" / "agents-journal.schema.json").read_text())
    journal = tmp_path / "j.jsonl"
    _run("fail", "42", "qa", "--journal", str(journal), "--routing", str(ROUTING))
    _run("route", "qa", "--ticket", "42", "--label", "complex", "--journal", str(journal))
    tr.append_record(journal, tr.link_record("s", 42, "2026-11-18T09:00:00+00:00"))
    for line in journal.read_text().splitlines():
        jsonschema.validate(json.loads(line), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"kind": "link", "ts": "x", "session_id": "s", "ticket": 1}, schema)


def test_shell_wrapper_prints_the_report(tmp_path: Path) -> None:
    db, journal = _two_tickets(tmp_path)
    proc = subprocess.run(  # noqa: S603 - script du dépôt
        [str(SCRIPT), "--db", str(db), "--journal", str(journal)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "#42 1 000" in proc.stdout


def test_helper_is_python_39_compatible() -> None:
    source = (AGENTS / "token_report.py").read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "UTC", "datetime.UTC n'existe pas en 3.9"
        if isinstance(node, ast.Call):
            assert "strict" not in [k.arg for k in node.keywords]


def test_local_agents_follow_the_routing(routing: tr.Routing) -> None:
    """`.claude/agents/*.md` est local (jamais commité) : vérifié s'il existe, ignoré en CI."""
    files = sorted((REPO / ".claude" / "agents").glob("*.md"))
    if not files:
        pytest.skip("pas d'agents locaux sur ce poste")
    for path in files:
        front = path.read_text(encoding="utf-8").split("---")[1]
        fields = dict(line.split(":", 1) for line in front.strip().splitlines() if ":" in line)
        model = fields.get("model", "").strip()
        assert model == tr.route(routing, fields["name"].strip())[0], path.name


# Corrections de revue


def _lines(journal: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in journal.read_text().splitlines()]


def test_route_does_not_duplicate_an_identical_promotion(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    for _ in range(3):
        _run("route", "qa", "--ticket", "42", "--label", "complex", "--journal", str(journal))
    assert [r["kind"] for r in _lines(journal)] == ["promote"]


@pytest.mark.parametrize(
    "args",
    [
        ["fail", "0", "qa"],
        ["fail", "-3", "qa"],
        ["route", "qa", "--ticket", "0"],
        ["route", "qa", "--ticket", "1", "--label", "pas un label !"],
    ],
)
def test_ticket_and_label_are_validated(tmp_path: Path, args: list[str]) -> None:
    journal = tmp_path / "j.jsonl"
    with pytest.raises(SystemExit):
        _run(*args, "--journal", str(journal))
    assert not journal.exists()


def test_promotion_line_ignores_records_without_integer_ticket(tmp_path: Path) -> None:
    db = _db(tmp_path / "vva.db", [("s", "qa", "claude-sonnet-5-5", 10)])
    journal = _journal(tmp_path / "j.jsonl", {"s": 5})
    bad = {"kind": "promote", "ts": "2026-11-18T09:00:00+00:00", "agent": "qa"}
    tr.append_record(journal, {**bad, "ticket": "5", "model": "claude-opus-5-5", "reason": "x"})
    tr.append_record(journal, {**bad, "model": "claude-opus-5-5", "reason": "x"})
    out = _run("report", "--db", str(db), "--journal", str(journal), "--routing", str(ROUTING))
    assert "Promotions Opus : aucune" in out


def test_failures_are_counted_per_ticket_and_agent(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    _run("fail", "42", "qa", "--journal", str(journal))
    _run("fail", "42", "dev-backend", "--journal", str(journal))
    _run("fail", "43", "qa", "--journal", str(journal))
    assert _run("route", "qa", "--ticket", "42", "--journal", str(journal)) == "claude-sonnet-5-5"
    _run("fail", "42", "qa", "--journal", str(journal))
    assert _run("route", "qa", "--ticket", "42", "--journal", str(journal)) == "claude-opus-5-5"


def test_git_timeout_leaves_room_for_a_slow_disk() -> None:
    assert tr.GIT_TIMEOUT_S == 1.5
