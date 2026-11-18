"""Totaux de tokens par agent et par modèle, sur 24 h et 7 jours glissants."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

MAIN_THREAD_LABEL = "(fil principal)"
_GROUPS = {"agent": "agent_id", "model": "model"}
_QUERY = (
    "SELECT {col}, SUM(CASE WHEN ts >= ? THEN count ELSE 0 END), SUM(count) "
    "FROM token_usage WHERE ts >= ? GROUP BY {col} ORDER BY 3 DESC, 1"
)


@dataclass(frozen=True)
class Total:
    label: str
    last_24h: int
    last_7d: int


@dataclass(frozen=True)
class Report:
    by_agent: list[Total]
    by_model: list[Total]


def _cutoff(now: datetime, delta: timedelta) -> str:
    return (now - delta).isoformat(timespec="microseconds")


def _group(conn: sqlite3.Connection, group: str, now: datetime) -> list[Total]:
    sql = _QUERY.format(col=_GROUPS[group])
    params = (_cutoff(now, timedelta(days=1)), _cutoff(now, timedelta(days=7)))
    return [
        Total(MAIN_THREAD_LABEL if label is None else label, int(day), int(week))
        for label, day, week in conn.execute(sql, params)
    ]


def totals(conn: sqlite3.Connection, now: datetime) -> Report:
    """Agent absent (`agent_id` NULL) : regroupé sous « (fil principal) »."""
    return Report(by_agent=_group(conn, "agent", now), by_model=_group(conn, "model", now))


def thousands(value: int) -> str:
    """1234567 -> « 1 234 567 » (espace fine insécable)."""
    return f"{value:,}".replace(",", " ")
