"""Persistance SQLite des événements d'agents (stdlib `sqlite3`)."""

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
)
EventRow = tuple[str | None, ...]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    hook_event_name TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    agent_type      TEXT,
    agent_id        TEXT,
    tool_name       TEXT,
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_agent_id ON events (agent_id);
"""

_INSERT = (
    f"INSERT OR IGNORE INTO events ({', '.join(EVENT_COLUMNS)}) "  # noqa: S608 - colonnes constantes
    f"VALUES ({', '.join('?' for _ in EVENT_COLUMNS)})"
)


BUSY_TIMEOUT_S = 5.0
_initialized: set[Path] = set()
_init_lock = threading.Lock()


def db_path() -> Path:
    return Path(os.environ.get("VVA_DB_PATH") or DEFAULT_DB_PATH)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)


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


def insert_events(conn: sqlite3.Connection, rows: Sequence[EventRow]) -> int:
    """Insère un lot de façon atomique ; ignore les `event_id` connus. Renvoie le nb inséré."""
    with conn:
        before = conn.total_changes
        conn.executemany(_INSERT, rows)
        return conn.total_changes - before
