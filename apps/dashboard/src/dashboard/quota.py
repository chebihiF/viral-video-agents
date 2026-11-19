"""Quota de tokens réglé par le parent (#100) : fenêtre glissante de 5 h et semaine.

Lu dans `VVA_QUOTA_5H_TOKENS` et `VVA_QUOTA_WEEK_TOKENS` (entier ASCII > 0). Jamais de
valeur par défaut : une variable absente ou invalide vaut `None`, soit « non configuré ».
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_5H = "VVA_QUOTA_5H_TOKENS"
ENV_WEEK = "VVA_QUOTA_WEEK_TOKENS"


@dataclass(frozen=True)
class Quota:
    five_hours: int | None
    week: int | None

    @property
    def configured(self) -> bool:
        return self.five_hours is not None or self.week is not None


def _tokens(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    value = int(raw)
    return value if value > 0 else None


def read_quota() -> Quota:
    """Ne lève jamais : une fenêtre non configurée vaut `None`."""
    return Quota(five_hours=_tokens(ENV_5H), week=_tokens(ENV_WEEK))
