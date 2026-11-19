"""Interactions (#102) : qui délègue à qui, fil chronologique groupé par ticket."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, db, delegations, interactions
from dashboard.main import app

PASSWORD = "mot-de-passe-de-test"  # noqa: S105 - valeur factice de test
NOW = datetime(2026, 11, 19, 15, 0, 0, tzinfo=UTC)


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vva.db"
    monkeypatch.setenv("VVA_DB_PATH", str(path))
    monkeypatch.setenv(auth.PASSWORD_HASH_ENV, auth.hash_password(PASSWORD, iterations=1_000))
    monkeypatch.setenv(auth.SESSION_KEY_ENV, "s" * 48)
    auth.LOGIN_LIMITER.reset()
    return path


@pytest.fixture
def client(db_file: Path) -> TestClient:
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    assert c.post("/login", data={"password": PASSWORD}).status_code == 303
    return c


_counter = iter(range(10_000))


def put(
    at: datetime,
    hook: str,
    *,
    tool: str | None = None,
    agent_type: str | None = None,
    agent_id: str | None = None,
    ticket: object = None,
    session: str = "sess-1",
) -> None:
    with db.connect() as conn, conn:
        names = "event_id, ts, hook_event_name, session_id, agent_type, agent_id, tool_name, ticket"
        values: list[object] = [
            f"evt-{next(_counter)}",
            at.isoformat(),
            hook,
            session,
            agent_type,
            agent_id,
            tool,
            ticket,
        ]
        marks = ", ".join("?" * len(values))
        conn.execute(f"INSERT INTO events ({names}) VALUES ({marks})", values)  # noqa: S608


def delegate(
    at: datetime,
    to: str,
    *,
    ticket: object = 93,
    tool: str = "Task",
    by: tuple[str, str] | None = None,
    session: str = "sess-1",
    stop_after: timedelta | None = None,
) -> None:
    """Un lancement : outil de délégation (fil principal ou agent `by`), puis SubagentStart."""
    agent_type, agent_id = by if by else (None, None)
    put(at, "PreToolUse", tool=tool, agent_type=agent_type, agent_id=agent_id, ticket=ticket,
        session=session)  # fmt: skip
    started = at + timedelta(seconds=1)
    put(started, "SubagentStart", agent_type=to, agent_id=f"id-{to}", ticket=ticket,
        session=session)  # fmt: skip
    if stop_after is not None:
        put(started + stop_after, "SubagentStop", agent_type=to, agent_id=f"id-{to}",
            ticket=ticket, session=session)  # fmt: skip


def groups_at(now: datetime = NOW) -> list[delegations.TicketGroup]:
    with db.connect() as conn:
        return delegations.load_groups(conn, now)


def lines(groups: list[delegations.TicketGroup]) -> list[str]:
    return [row.line for group in groups for row in group.rows]


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def at(hh: int, mm: int) -> datetime:
    return NOW.replace(hour=hh, minute=mm)


# Scénario : le tech lead lance dev-backend puis qa sur le ticket 93
def test_thread_is_chronological_with_time_and_ticket(db_file: Path) -> None:
    delegate(at(14, 2), "dev-backend")
    delegate(at(14, 40), "qa")
    assert lines(groups_at()) == [
        "14:02 tech lead → dev-backend (ticket #93)",
        "14:40 tech lead → qa (ticket #93)",
    ]


def test_page_shows_the_thread(client: TestClient) -> None:
    now = datetime.now(UTC)
    delegate(now - timedelta(minutes=50), "dev-backend")
    delegate(now - timedelta(minutes=10), "qa")
    html = text_of(client.get("/interactions").text)
    first = (now - timedelta(minutes=50)).strftime("%H:%M")
    assert f"{first} tech lead → dev-backend (ticket #93)" in html
    assert (now - timedelta(minutes=10)).strftime("%H:%M") + " tech lead → qa (ticket #93)" in html


# Scénario : un sous-agent terminé affiche sa durée
def test_finished_subagent_shows_its_duration(db_file: Path) -> None:
    delegate(at(14, 2), "dev-backend", stop_after=timedelta(minutes=38))
    [row] = groups_at()[0].rows
    assert row.duration == "a duré 38 min"


def test_running_subagent_has_no_duration(db_file: Path) -> None:
    delegate(at(14, 2), "dev-backend")
    assert groups_at()[0].rows[0].duration == "en cours"


def test_duration_is_shown_beside_the_arrow(client: TestClient) -> None:
    now = datetime.now(UTC)
    delegate(now - timedelta(hours=1), "dev-backend", stop_after=timedelta(minutes=12), ticket=None)
    assert "dev-backend (ticket inconnu) a duré 12 min" in text_of(client.get("/interactions").text)


# Scénario : deux tickets, groupés, le plus récent en haut
def test_groups_by_ticket_most_recent_first(db_file: Path) -> None:
    delegate(at(9, 0), "dev-backend", ticket=90)
    delegate(at(14, 0), "dev-backend", ticket=93)
    delegate(at(14, 30), "qa", ticket=93)
    delegate(at(10, 0), "qa", ticket=90)
    groups = groups_at()
    assert [g.ticket for g in groups] == [93, 90]
    assert [r.line for r in groups[1].rows] == [
        "09:00 tech lead → dev-backend (ticket #90)",
        "10:00 tech lead → qa (ticket #90)",
    ]


# Scénario : délégation sans ticket
def test_without_ticket_the_ticket_is_unknown(client: TestClient) -> None:
    now = datetime.now(UTC)
    delegate(now - timedelta(minutes=5), "dev-backend", ticket=None)
    response = client.get("/interactions")
    assert response.status_code == 200
    assert "→ dev-backend (ticket inconnu)" in text_of(response.text)


@pytest.mark.parametrize("raw", [None, "", "#93", "93a", 93.7, float("inf")])
def test_null_or_malformed_ticket_is_unknown(db_file: Path, raw: object) -> None:
    delegate(at(14, 2), "dev-backend", ticket=raw)
    [group] = groups_at()
    assert group.ticket is None
    assert group.rows[0].line == "14:02 tech lead → dev-backend (ticket inconnu)"


def test_ticket_of_the_start_event_is_used_when_the_tool_event_has_none(db_file: Path) -> None:
    put(at(14, 2), "PreToolUse", tool="Agent", ticket=None)
    put(at(14, 2) + timedelta(seconds=1), "SubagentStart", agent_type="qa", agent_id="a", ticket=7)
    assert groups_at()[0].ticket == 7


def test_ticket_zero_is_not_replaced_by_the_start_ticket(db_file: Path) -> None:
    put(at(14, 2), "PreToolUse", tool="Task", ticket=0)
    put(at(14, 2) + timedelta(seconds=1), "SubagentStart", agent_type="qa", agent_id="a", ticket=7)
    assert groups_at()[0].ticket == 0


# Appariement
def test_agent_tool_name_also_delegates(db_file: Path) -> None:
    delegate(at(14, 2), "qa", tool="Agent")
    assert len(lines(groups_at())) == 1


def test_other_tools_are_not_delegations(db_file: Path) -> None:
    delegate(at(14, 2), "qa", tool="Bash")
    assert groups_at() == []


def test_post_tool_events_are_not_delegations(db_file: Path) -> None:
    put(at(14, 2), "PostToolUse", tool="Task")
    put(at(14, 2) + timedelta(seconds=1), "SubagentStart", agent_type="qa", agent_id="a")
    assert groups_at() == []


def test_subagent_start_without_tool_event_is_not_a_delegation(db_file: Path) -> None:
    put(at(14, 2), "SubagentStart", agent_type="qa", agent_id="a")
    assert groups_at() == []


def test_tool_event_without_start_is_not_a_delegation(db_file: Path) -> None:
    put(at(14, 2), "PreToolUse", tool="Task")
    assert groups_at() == []


def test_stale_tool_event_is_not_paired_with_a_later_start(db_file: Path) -> None:
    put(at(14, 2), "PreToolUse", tool="Task")
    put(at(14, 2) + delegations.PAIR_WINDOW + timedelta(seconds=1), "SubagentStart",
        agent_type="qa", agent_id="a")  # fmt: skip
    assert groups_at() == []


def test_parallel_delegations_pair_in_order(db_file: Path) -> None:
    put(at(14, 0), "PreToolUse", tool="Task", ticket=None)
    put(at(14, 0) + timedelta(seconds=1), "PreToolUse", tool="Task", ticket=None)
    put(at(14, 0) + timedelta(seconds=2), "SubagentStart", agent_type="qa", agent_id="a")
    put(at(14, 0) + timedelta(seconds=3), "SubagentStart", agent_type="reviewer", agent_id="b")
    assert [r.delegatee for r in groups_at()[0].rows] == ["qa", "reviewer"]


def test_sessions_do_not_mix(db_file: Path) -> None:
    put(at(14, 0), "PreToolUse", tool="Task", session="s1")
    put(at(14, 0) + timedelta(seconds=1), "SubagentStart", agent_type="qa", agent_id="a",
        session="s2")  # fmt: skip
    assert groups_at() == []


def test_subagent_delegating_is_named_as_delegator(db_file: Path) -> None:
    delegate(at(14, 2), "scout", by=("dev-backend", "id-dev"), ticket=None)
    assert lines(groups_at()) == ["14:02 dev-backend → scout (ticket inconnu)"]


def test_duration_pairs_stop_by_agent_id(db_file: Path) -> None:
    delegate(at(14, 0), "qa", stop_after=timedelta(minutes=3))
    delegate(at(14, 30), "qa")  # même id relancé : le Stop précédent ne le concerne pas
    rows = groups_at()[0].rows
    assert [r.duration for r in rows] == ["a duré 3 min", "en cours"]


def test_events_older_than_the_window_are_ignored(db_file: Path) -> None:
    delegate(NOW - delegations.HISTORY_WINDOW - timedelta(hours=1), "ancien")
    delegate(NOW - timedelta(hours=1), "recent")
    assert [r.delegatee for g in groups_at() for r in g.rows] == ["recent"]


def test_rows_are_capped_to_the_most_recent(db_file: Path) -> None:
    for i in range(delegations.MAX_ROWS + 5):
        delegate(NOW - timedelta(hours=10) + timedelta(minutes=i), f"agent-{i}")
    rows = [r for g in groups_at() for r in g.rows]
    assert len(rows) == delegations.MAX_ROWS
    assert rows[-1].delegatee == f"agent-{delegations.MAX_ROWS + 4}"


# Échappement HTML
def test_agent_names_are_escaped(client: TestClient) -> None:
    now = datetime.now(UTC)
    delegate(now - timedelta(minutes=5), "<script>alert(1)</script>")
    html = client.get("/interactions").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# Contrat de la route
def test_requires_session(db_file: Path) -> None:
    anonymous = TestClient(app, base_url="https://testserver", follow_redirects=False)
    response = anonymous.get("/interactions")
    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_empty_database_shows_only_the_existing_empty_state(client: TestClient) -> None:
    response = client.get("/interactions")
    html = text_of(response.text)
    assert response.status_code == 200
    assert "Aucune interaction pour l'instant" in html
    assert "Délégations" not in html


def test_sessions_without_delegation_say_so(client: TestClient) -> None:
    put(datetime.now(UTC), "PreToolUse", tool="Bash")
    assert "Aucune délégation récente" in text_of(client.get("/interactions").text)


def test_unreadable_database_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_a: object, **_k: object) -> list[object]:
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(delegations, "load_groups", broken)
    response = client.get("/interactions")
    assert response.status_code == 503
    assert 'role="alert"' in response.text


def test_no_prompt_content_is_exposed() -> None:
    fields = set(delegations.Delegation.__dataclass_fields__)
    assert not {"prompt", "tool_input", "description"} & fields


def test_interactions_module_calls_the_loader() -> None:
    assert interactions.delegations is delegations
