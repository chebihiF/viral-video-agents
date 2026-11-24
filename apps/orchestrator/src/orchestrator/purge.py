"""Purge des runs anciens (#152) : supprime les dossiers de run plus vieux que N jours.

Un run = sous-dossier direct de la racine contenant un `run.json` conforme (schema_version 1.0,
run_id, etat). Âge = date de dernière modification de `run.json`. Aucun lien symbolique n'est suivi
(dossier de run, `run.json`) ; `shutil.rmtree` retire les liens internes sans les traverser.
Seuls `run_id` (validé par le contrat) et l'âge sont rendus : jamais de contenu de dictée.
Stdlib seule, importable en Python 3.9+.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
_ETATS = ("ok", "bloque", "erreur")
_RUN_JSON_MAX = 1_000_000
JOUR_S = 86400


@dataclass(frozen=True)
class RunAncien:
    run_id: str
    age_jours: int
    dossier: Path


def _lire_run(run_json: Path) -> tuple[str, float] | None:
    """(run_id, mtime) si `run_json` est un fichier régulier conforme, sinon None.

    Un seul `lstat` (pas de suivi de lien), lecture en `O_NOFOLLOW` ; jamais d'exception.
    """
    try:
        info = os.lstat(run_json)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _RUN_JSON_MAX:
            return None
        fd = os.open(run_json, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as f:
            run = json.loads(f.read(_RUN_JSON_MAX + 1))
    except (OSError, ValueError):  # FileNotFoundError (course) incluse ; ValueError : JSON, UTF-8
        return None
    if not isinstance(run, dict):
        return None
    if run.get("schema_version") != "1.0" or run.get("etat") not in _ETATS:
        return None
    rid = run.get("run_id")
    if isinstance(rid, str) and _RUN_ID.fullmatch(rid):
        return rid, info.st_mtime
    return None


def trouver_anciens(
    racine: Path, jours: int, *, maintenant: float | None = None
) -> list[RunAncien]:
    """Runs de `racine` plus vieux que `jours` jours, triés par dossier. `jours` >= 1.

    Lève OSError si `racine` est illisible.
    """
    if jours < 1:
        raise ValueError("jours doit être >= 1")
    instant = time.time() if maintenant is None else maintenant
    anciens: list[RunAncien] = []
    for dossier in sorted(racine.iterdir()):
        if dossier.is_symlink() or not dossier.is_dir():
            continue
        lu = _lire_run(dossier / "run.json")
        if lu is None:
            continue
        rid, mtime = lu
        age_s = instant - mtime
        if age_s > jours * JOUR_S:
            anciens.append(RunAncien(rid, int(age_s // JOUR_S), dossier))
    return anciens


def supprimer(run: RunAncien) -> None:
    """Supprime le dossier du run (liens symboliques internes retirés, jamais suivis)."""
    shutil.rmtree(run.dossier)
