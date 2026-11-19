"""Persistance SQLite des événements d'agents et des tokens (stdlib `sqlite3`)."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = "vva.db"
EVENT_COLUMNS = (
    "event_id",
    "ts",
    "hook_event_name",
    "session_id",
    "agent_type",
    "agent_id",
    "tool_name",
    "ticket",
)
EventRow = tuple[str | int | None, ...]
TOKEN_COLUMNS = ("point_id", "ts", "session_id", "agent_id", "model", "type", "count")
TokenRow = tuple[str, str, str, str | None, str, str, int]
ControlRow = tuple[bool, str | None, int, str]
DEFAULT_CAP_TOKENS = 500_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    hook_event_name TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    agent_type      TEXT,
    agent_id        TEXT,
    tool_name       TEXT,
    ticket          INTEGER,
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_agent_id ON events (agent_id);
CREATE TABLE IF NOT EXISTS token_usage (
    point_id    TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    agent_id    TEXT,
    model       TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('input', 'output', 'cacheRead', 'cacheCreation')),
    count       INTEGER NOT NULL CHECK (count >= 0),
    -- même format que ts (isoformat microsecondes, +00:00)
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000+00:00', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage (ts);
-- une seule ligne (id = 1) : pause des agents et plafond de tokens sur 24 h (#8)
CREATE TABLE IF NOT EXISTS controls (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    reason     TEXT CHECK (reason IN ('manuel', 'plafond')),
    cap_tokens INTEGER NOT NULL DEFAULT 500000 CHECK (cap_tokens > 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((paused = 0 AND reason IS NULL) OR (paused = 1 AND reason IS NOT NULL))
);
INSERT OR IGNORE INTO controls (id) VALUES (1);
"""


def _insert_sql(table: str, columns: Sequence[str]) -> str:
    return (
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "  # noqa: S608 - noms constants
        f"VALUES ({', '.join('?' for _ in columns)})"
    )


_INSERT = _insert_sql("events", EVENT_COLUMNS)
_INSERT_TOKENS = _insert_sql("token_usage", TOKEN_COLUMNS)


BUSY_TIMEOUT_S = 5.0
_initialized: set[Path] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    return Path(os.environ.get("VVA_DB_PATH") or DEFAULT_DB_PATH)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _add_ticket_column(conn)


def _add_ticket_column(conn: sqlite3.Connection) -> None:
    """Migration idempotente (#100) : bases créées avant la colonne `events.ticket`."""
    if any(row[1] == "ticket" for row in conn.execute("PRAGMA table_info(events)")):
        return
    try:
        with conn:
            conn.execute("ALTER TABLE events ADD COLUMN ticket INTEGER")
    except sqlite3.OperationalError as exc:  # autre processus plus rapide
        if "duplicate column" not in str(exc):
            raise


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Crée le schéma au premier accès à un fichier donné (une seule fois par processus)."""
    with _init_lock:
        if path not in _initialized:
            init_schema(conn)
            _initialized.add(path)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Ouvre la base désignée par `VVA_DB_PATH` et garantit le schéma."""
    path = db_path().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
    try:
        _ensure_schema(conn, path)
        yield conn
    finally:
        conn.close()


def _insert_many(conn: sqlite3.Connection, sql: str, rows: Sequence[Sequence[object]]) -> int:
    with conn:
        before = conn.total_changes
        conn.executemany(sql, rows)
        return conn.total_changes - before


def insert_events(conn: sqlite3.Connection, rows: Sequence[EventRow]) -> int:
    """Insère un lot de façon atomique ; ignore les `event_id` connus. Renvoie le nb inséré."""
    return _insert_many(conn, _INSERT, rows)


def insert_token_usage(conn: sqlite3.Connection, rows: Sequence[TokenRow]) -> int:
    """Insère un lot de façon atomique ; ignore les `point_id` connus. Renvoie le nb inséré."""
    return _insert_many(conn, _INSERT_TOKENS, rows)


_TOUCH = "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def read_controls(conn: sqlite3.Connection) -> ControlRow:
    """(en pause, motif, plafond, mis à jour) ; la ligne unique est créée avec le schéma."""
    paused, reason, cap, updated_at = conn.execute(
        "SELECT paused, reason, cap_tokens, updated_at FROM controls WHERE id = 1"
    ).fetchone()
    return bool(paused), reason, int(cap), str(updated_at)


def pause(conn: sqlite3.Connection, reason: str) -> None:
    """Met en pause ; sans effet si déjà en pause (le premier motif est conservé)."""
    with conn:
        conn.execute(
            f"UPDATE controls SET paused = 1, reason = ?, {_TOUCH} "  # noqa: S608 - constante
            "WHERE id = 1 AND paused = 0",
            (reason,),
        )


def resume(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            f"UPDATE controls SET paused = 0, reason = NULL, {_TOUCH} "  # noqa: S608 - constante
            "WHERE id = 1 AND paused = 1"
        )


def set_cap(conn: sqlite3.Connection, cap_tokens: int) -> None:
    with conn:
        conn.execute(
            f"UPDATE controls SET cap_tokens = ?, {_TOUCH} WHERE id = 1",  # noqa: S608 - constante
            (cap_tokens,),
        )
