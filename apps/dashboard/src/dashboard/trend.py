"""Tendance de consommation (#96) : tokens par jour sur 7 jours UTC et marge avant le plafond.

Pas de coût en € : aucune grille de prix locale n'existe dans le dépôt, donc rien n'est estimé
(0 € : aucune API de prix). Données : `token_usage` seulement, aucune nouvelle source.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from dashboard.tokens import thousands

DAYS = 7
# ts est en ISO 8601 UTC (otlp.py) : ses 10 premiers caractères sont le jour.
_QUERY = "SELECT substr(ts, 1, 10), SUM(count) FROM token_usage WHERE ts >= ? GROUP BY 1"


@dataclass(frozen=True)
class Day:
    day: str  # AAAA-MM-JJ (UTC)
    tokens: int
    partial: bool  # jour en cours, incomplet
    change: str | None  # variation vs la veille, en toutes lettres ; None sans veille affichée
    delta: int | None  # variation en %, None si la veille est à 0 ou absente


def _change(tokens: int, previous: int | None) -> tuple[str | None, int | None]:
    if previous is None:
        return None, None
    if previous == 0:
        return ("0 %", 0) if tokens == 0 else ("nouveau (veille sans token)", None)
    diff = tokens - previous
    magnitude = (abs(diff) * 100 + previous // 2) // previous  # arrondi au plus proche
    percent = magnitude if diff >= 0 else -magnitude
    return f"{percent:+d} %" if percent else "0 %", percent


def daily_usage(conn: sqlite3.Connection, now: datetime) -> list[Day]:
    """7 jours UTC, du plus ancien au jour en cours ; jour sans token = 0."""
    today = now.date()
    first = today - timedelta(days=DAYS - 1)
    since = datetime.combine(first, time.min, tzinfo=now.tzinfo).isoformat(timespec="microseconds")
    found = {day: int(total) for day, total in conn.execute(_QUERY, (since,))}
    days: list[Day] = []
    previous: int | None = None
    for offset in range(DAYS):
        day = (first + timedelta(days=offset)).isoformat()
        tokens = found.get(day, 0)
        change, delta = _change(tokens, previous)
        days.append(Day(day, tokens, offset == DAYS - 1, change, delta))
        previous = tokens
    return days


def cap_text(cap: int, used: int) -> str:
    """Marge avant le plafond de pause (fenêtre de 24 h glissantes, comme les contrôles)."""
    detail = f"{thousands(used)} sur {thousands(cap)} tokens, 24 dernières heures"
    if used >= cap:
        return f"plafond atteint : les agents sont mis en pause ({detail})"
    return f"marge restante : {thousands(cap - used)} tokens avant le plafond ({detail})"
