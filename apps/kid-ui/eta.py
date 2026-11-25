"""Durée restante estimée d'une vidéo (#165), d'après les durées mesurées (#164).

`estimer` est pure : elle reçoit les lignes de `durees.jsonl` (pipeline-run $defs/LigneDurees,
ni dictée ni texte) et rend `{pct, restant_s, bientot}` (kid-job $defs/Progress). Estimation =
médiane par étape (robuste aux runs aberrants) sur les N derniers runs réels terminés ; sans
mesure, `DEFAUT_S`. Jamais d'exception sur des données invalides : la ligne est ignorée.
`lire_durees` est la seule fonction qui touche au disque, en lecture seule.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, TypedDict

# Étapes affichées à l'enfant, dans l'ordre réel du parcours (orchestrator.pipeline).
ETAPES = ("storyboard", "voix", "images", "montage")
# Étape du pipeline -> étape affichée (la classification est comptée avec l'histoire).
_VERS_AFFICHEE = {
    "classification": "storyboard",
    "storyboard": "storyboard",
    "voix": "voix",
    "image": "images",
    "clip": "images",
    "montage": "montage",
}
# Durées (s) utilisées tant qu'aucun run réel n'a été mesuré. Ordre de grandeur posé a priori,
# sans mesure : à resserrer dès que `durees.jsonl` contient de vrais runs (le calcul les préfère).
DEFAUT_S: dict[str, float] = {"storyboard": 30.0, "voix": 30.0, "images": 300.0, "montage": 60.0}
N_RUNS = 10  # runs réels terminés pris en compte (les plus récents)
BIENTOT_S = 30  # en dessous : « bientôt prête » plutôt qu'un compte à rebours
PCT_MAX_EN_COURS = 99  # 100 % seulement quand le job est terminé (décidé par jobs.py)
DUREE_MAX_S = 24 * 3600  # une durée d'étape au-delà est une valeur aberrante, ignorée
MAX_OCTETS = 1_000_000  # un journal plus gros est ignoré (fichier anormal)
MAX_FICHIERS = 200  # journaux lus, les plus récemment modifiés d'abord
JOURNAL = "durees.jsonl"


class Progress(TypedDict):
    pct: int
    restant_s: int | None
    bientot: bool


def _duree_ok(d: object) -> bool:
    if isinstance(d, bool) or not isinstance(d, int | float):
        return False
    return math.isfinite(d) and 0 <= d <= DUREE_MAX_S


def _par_etape(ligne: object) -> dict[str, float] | None:
    """Durée par étape affichée d'un run réel terminé et complet ; None sinon."""
    if not isinstance(ligne, dict) or ligne.get("mode") != "reel" or ligne.get("etat") != "ok":
        return None
    etapes = ligne.get("etapes")
    if not isinstance(etapes, list):
        return None
    total: dict[str, float] = {}
    for e in etapes:
        if not isinstance(e, dict) or e.get("statut") != "ok":
            return None
        cible = _VERS_AFFICHEE.get(e.get("nom"))  # type: ignore[arg-type]
        if cible is None or not _duree_ok(e.get("duree_s")):
            return None
        total[cible] = total.get(cible, 0.0) + float(e["duree_s"])
    return total if set(total) == set(ETAPES) else None


def _recent(ligne: dict[str, Any]) -> str:
    t = ligne.get("termine_le")
    return t if isinstance(t, str) else ""


def medianes(lignes: list[Any]) -> dict[str, float]:
    """Médiane par étape affichée sur les N derniers runs réels terminés ; défaut sinon."""
    valides = [(_recent(x), p) for x in lignes if (p := _par_etape(x)) is not None]
    valides.sort(key=lambda v: v[0])
    runs = [p for _, p in valides[-N_RUNS:]]
    return {e: statistics.median(r[e] for r in runs) if runs else DEFAUT_S[e] for e in ETAPES}


def _ecoule(secondes: float) -> float:
    if math.isnan(secondes):
        return 0.0
    return min(max(0.0, secondes), float(DUREE_MAX_S))


def estimer(lignes: list[Any], etape_courante: str, ecoule_etape_s: float) -> Progress:
    """Avancement estimé ; `restant_s` vaut None si l'étape courante est inconnue."""
    if etape_courante not in ETAPES:
        return Progress(pct=0, restant_s=None, bientot=False)
    med = medianes(lignes)
    i = ETAPES.index(etape_courante)
    ecoule = _ecoule(ecoule_etape_s)
    reste_etape = max(0.0, med[etape_courante] - ecoule)
    restant = reste_etape + sum(med[e] for e in ETAPES[i + 1 :])
    fait = sum(med[e] for e in ETAPES[:i]) + min(ecoule, med[etape_courante])
    pct = min(PCT_MAX_EN_COURS, int(100 * fait / sum(med.values())))
    restant_s = max(0, round(restant))
    return Progress(pct=pct, restant_s=restant_s, bientot=restant_s < BIENTOT_S)


def _journaux(runs_dir: Path) -> list[Path]:
    try:
        trouves = [
            f
            for f in runs_dir.glob(f"*/{JOURNAL}")
            if not f.is_symlink() and not f.parent.is_symlink() and f.is_file()
        ]
        trouves.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return trouves[:MAX_FICHIERS]


def lire_durees(runs_dir: Path) -> list[dict[str, Any]]:
    """Lignes valides des journaux `<runs_dir>/*/durees.jsonl` (lecture seule, tolérante)."""
    lignes: list[dict[str, Any]] = []
    for f in _journaux(runs_dir):
        try:
            if f.stat().st_size > MAX_OCTETS:
                continue
            texte = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for brut in texte.splitlines():
            try:
                x = json.loads(brut)
            except (ValueError, RecursionError):
                continue
            if isinstance(x, dict):
                lignes.append(x)
    return lignes
