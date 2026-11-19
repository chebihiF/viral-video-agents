"""Tokens en pourcentage du quota (#103) : 5 h, semaine, week-end, par agent et par ticket.

Fenêtres glissantes : 5 h = maintenant - 5 h ; semaine = 7 jours ; week-end = part de la semaine
tombée un samedi ou un dimanche (UTC). Un pourcentage n'existe que si le quota de sa fenêtre est
configuré (`quota.py`) : sinon tokens bruts et message d'aide, jamais de valeur inventée.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from dashboard.quota import ENV_5H, ENV_WEEK, Quota
from dashboard.tokens import MAIN_THREAD_LABEL, thousands

UNKNOWN_TICKET = "ticket inconnu"
WARN_PERCENT = 70
DANGER_PERCENT = 90
LEVEL_LABELS = {"ok": "Normal", "warn": "Attention", "danger": "Critique"}
_FIVE_HOURS = timedelta(hours=5)
_WEEK = timedelta(days=7)

_TOTALS = (
    "SELECT COALESCE(SUM(CASE WHEN ts >= ? THEN count END), 0), COALESCE(SUM(count), 0), "
    "COALESCE(SUM(CASE WHEN strftime('%w', ts) IN ('0', '6') THEN count END), 0) "
    "FROM token_usage WHERE ts >= ?"
)
_GROUPED = (
    "SELECT {col}, SUM(CASE WHEN ts >= ? THEN count ELSE 0 END), SUM(count) "
    "FROM token_usage WHERE ts >= ? GROUP BY {col}"
)
# Ticket d'une session : celui de son événement le plus récent qui en porte un.
_SESSION_TICKETS = (
    "SELECT session_id, ticket, MAX(ts) FROM events WHERE ticket IS NOT NULL GROUP BY session_id"
)


@dataclass(frozen=True)
class Gauge:
    used: int
    limit: int | None
    percent: int | None
    level: str | None  # "ok" | "warn" | "danger" ; None sans quota
    level_label: str | None  # « Normal » | « Attention » | « Critique »
    text: str
    help: str | None


@dataclass(frozen=True)
class Cell:
    tokens: int
    percent: int | None
    level: str | None


@dataclass(frozen=True)
class Row:
    label: str
    last_5h: Cell
    last_7d: Cell


@dataclass(frozen=True)
class Margin:
    """Tokens restants avant le plafond d'une fenêtre (0 si déjà dépassé)."""

    name: str
    remaining: int
    percent: int


@dataclass(frozen=True)
class QuotaView:
    configured: bool
    five_hours: Gauge
    week: Gauge
    weekend: Gauge
    margins: list[Margin]
    closest: Margin | None
    by_agent: list[Row]
    by_ticket: list[Row]


def _percent(used: int, limit: int) -> int:
    return (used * 100 + limit // 2) // limit


def _level(used: int, limit: int) -> str:
    if used * 100 < WARN_PERCENT * limit:
        return "ok"
    return "warn" if used * 100 < DANGER_PERCENT * limit else "danger"


def gauge(used: int, limit: int | None, quota_label: str, raw_label: str, env: str) -> Gauge:
    """`quota_label` : « des 5 dernières heures » ; `raw_label` : « sur les 5 dernières heures »."""
    if limit is None:
        text = f"{thousands(used)} tokens {raw_label}"
        return Gauge(used, None, None, None, None, text, f"quota non configuré : renseigner {env}")
    percent = _percent(used, limit)
    detail = f"({thousands(used)} sur {thousands(limit)} tokens)"
    level = _level(used, limit)
    return Gauge(
        used,
        limit,
        percent,
        level,
        LEVEL_LABELS[level],
        f"{percent} % du quota {quota_label} {detail}",
        None,
    )


def margins(five_hours: Gauge, week: Gauge) -> list[Margin]:
    """Marge des fenêtres à quota. Le week-end (même plafond, tokens inclus dans la semaine)
    n'est jamais plus proche du plafond que la semaine : il n'a pas de ligne propre."""
    named = (("Fenêtre de 5 heures", five_hours), ("Semaine", week))
    return [
        Margin(name, max(g.limit - g.used, 0), max(100 - g.percent, 0))
        for name, g in named
        if g.limit is not None and g.percent is not None
    ]


def closest_to_limit(items: list[Margin]) -> Margin | None:
    """Marge relative la plus faible ; à égalité, la première fenêtre."""
    return min(items, key=lambda m: m.percent, default=None)


def _cell(tokens: int, limit: int | None) -> Cell:
    if limit is None:
        return Cell(tokens, None, None)
    return Cell(tokens, _percent(tokens, limit), _level(tokens, limit))


def _rows(totals: dict[str, tuple[int, int]], quota: Quota) -> list[Row]:
    rows = [
        Row(label, _cell(h5, quota.five_hours), _cell(d7, quota.week))
        for label, (h5, d7) in totals.items()
    ]
    return sorted(rows, key=lambda r: (-r.last_5h.tokens, -r.last_7d.tokens, r.label))


def _iso(now: datetime, delta: timedelta) -> str:
    return (now - delta).isoformat(timespec="microseconds")


def _by_agent(conn: sqlite3.Connection, params: tuple[str, str]) -> dict[str, tuple[int, int]]:
    sql = _GROUPED.format(col="agent_id")
    return {
        MAIN_THREAD_LABEL if agent is None else agent: (int(h5), int(d7))
        for agent, h5, d7 in conn.execute(sql, params)
    }


def _by_ticket(conn: sqlite3.Connection, params: tuple[str, str]) -> dict[str, tuple[int, int]]:
    tickets = {session: ticket for session, ticket, _ in conn.execute(_SESSION_TICKETS)}
    totals: dict[str, tuple[int, int]] = {}
    for session, h5, d7 in conn.execute(_GROUPED.format(col="session_id"), params):
        ticket = tickets.get(session)
        label = UNKNOWN_TICKET if ticket is None else f"#{ticket}"
        prev = totals.get(label, (0, 0))
        totals[label] = (prev[0] + int(h5), prev[1] + int(d7))
    return totals


def build(conn: sqlite3.Connection, now: datetime, quota: Quota) -> QuotaView:
    params = (_iso(now, _FIVE_HOURS), _iso(now, _WEEK))
    h5, d7, weekend = conn.execute(_TOTALS, params).fetchone()
    five_hours = gauge(
        int(h5), quota.five_hours, "des 5 dernières heures", "sur les 5 dernières heures", ENV_5H
    )
    week = gauge(
        int(d7),
        quota.week,
        "de la semaine (7 derniers jours)",
        "sur les 7 derniers jours",
        ENV_WEEK,
    )
    weekend_gauge = gauge(
        int(weekend),
        quota.week,
        "de la semaine, consommés le week-end (samedi et dimanche)",
        "consommés le week-end (samedi et dimanche) sur les 7 derniers jours",
        ENV_WEEK,
    )
    window_margins = margins(five_hours, week)
    return QuotaView(
        configured=quota.configured,
        five_hours=five_hours,
        week=week,
        weekend=weekend_gauge,
        margins=window_margins,
        closest=closest_to_limit(window_margins),
        by_agent=_rows(_by_agent(conn, params), quota),
        by_ticket=_rows(_by_ticket(conn, params), quota),
    )
