"""Galerie locale des vidéos produites (#151) : une page HTML statique, autonome, sans réseau.

Entrée : `DIR/<dossier>/run.json` (pipeline-run.schema.json). Ne sont rendus que `run_id`, l'état,
la durée (somme des étapes) et le lien relatif vers le MP4 des runs `ok` : jamais d'autre champ,
donc jamais de dictée ni de texte adouci. Tout texte est échappé ; aucun script, aucune URL
http(s). Aucun lien symbolique n'est suivi (dossier de run, `run.json`). Stdlib seule, Python 3.9+.
Spec visuelle : docs/design/galerie.md.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,48}")
_TERMINE_LE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_ETATS = {
    "ok": ("ok", "✔", "Terminé"),
    "bloque": ("bad", "▲", "Bloqué"),
    "erreur": ("bad", "▲", "Échec"),
}
_RUN_JSON_MAX = 1_000_000

_CSS = """\
:root{--color-bg:#f7f7f9;--color-surface:#fff;--color-text:#1b1b21;--color-muted:#4a4a57;
--color-border:#767684;--color-ok:#0b6b2e;--color-info:#0b5394;--color-danger:#a31515;
--color-focus:#1b1b21;--space-2:8px;--space-3:12px;--space-4:16px;
--radius:8px;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--color-bg:#14141a;--color-surface:#1e1e27;
--color-text:#ececf3;--color-muted:#b4b4c4;--color-border:#8a8a9c;--color-ok:#6fd48f;
--color-info:#7db8ff;--color-danger:#ff8f8f;--color-focus:#ffd84d}}
body{margin:0;padding:var(--space-4);background:var(--color-bg);color:var(--color-text);
font:15px/1.5 system-ui,sans-serif}
.runs{list-style:none;margin:0;padding:0;display:grid;gap:var(--space-4);
grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr))}
.run{background:var(--color-surface);border:1px solid var(--color-border);
border-radius:var(--radius);padding:var(--space-4)}
.run video{width:100%;max-width:100%;margin-top:var(--space-3);border-radius:var(--radius)}
code{font:14px ui-monospace,monospace}.meta,.dur{color:var(--color-muted)}
.state{font-weight:600}.ok{color:var(--color-ok)}.info{color:var(--color-info)}
.bad{color:var(--color-danger)}
:focus-visible{outline:3px solid var(--color-focus);outline-offset:3px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}"""


@dataclass(frozen=True)
class Entree:
    """Ce qui est affiché d'un run, et rien d'autre."""

    run_id: str
    etat: str
    duree_s: float | None
    video: Path | None  # chemin du MP4 (run `ok` seulement, chemin validé)
    termine_le: str


def formater_duree(secondes: float) -> tuple[str, str]:
    """(« 1 min 42 s », « PT1M42S ») ; arrondi à la seconde."""
    total = int(round(secondes))
    minutes, reste = divmod(total, 60)
    if minutes == 0:
        return f"{reste} s", f"PT{reste}S"
    return f"{minutes} min {reste} s", f"PT{minutes}M{reste}S"


def _lire_json(run_json: Path) -> object:
    """Contenu JSON de `run_json` (fichier régulier, sans suivre de lien) ; None si invalide."""
    try:
        info = os.lstat(run_json)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _RUN_JSON_MAX:
            return None
        fd = os.open(run_json, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as f:
            return json.loads(f.read(_RUN_JSON_MAX + 1))
    except (OSError, ValueError):
        return None


def _duree(run: dict[str, object]) -> float | None:
    etapes = run.get("etapes")
    if not isinstance(etapes, list) or not etapes:
        return None
    total = 0.0
    for etape in etapes:
        d = etape.get("duree_s") if isinstance(etape, dict) else None
        if isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d < 0:
            return None
        total += d
    return total


def _video_valide(chemin: object) -> str | None:
    """`video_path` s'il est relatif, sans `..`, sans schéma ni antislash, et finit par .mp4."""
    if not isinstance(chemin, str) or not chemin.endswith(".mp4"):
        return None
    if any(c in chemin for c in ("\\", ":", "\x00")) or chemin.startswith("/"):
        return None
    if any(seg in ("", ".", "..") for seg in chemin.split("/")):
        return None
    return chemin


def _entree(dossier: Path) -> Entree | None:
    run = _lire_json(dossier / "run.json")
    if not isinstance(run, dict) or run.get("schema_version") != "1.0":
        return None
    run_id, etat = run.get("run_id"), run.get("etat")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or etat not in _ETATS:
        return None
    video = _video_valide(run.get("video_path")) if etat == "ok" else None
    fin = run.get("termine_le")
    return Entree(
        run_id=run_id,
        etat=str(etat),
        duree_s=_duree(run),
        video=dossier / video if video else None,
        termine_le=fin if isinstance(fin, str) and _TERMINE_LE.fullmatch(fin) else "",
    )


def lister(racine: Path) -> list[Entree]:
    """Runs de `racine`, plus récent d'abord (sans date : en dernier). Lève OSError si illisible."""
    entrees: list[Entree] = []
    for dossier in sorted(racine.iterdir()):
        if dossier.is_symlink() or not dossier.is_dir():
            continue
        entree = _entree(dossier)
        if entree is not None:
            entrees.append(entree)
    entrees.sort(key=lambda e: e.termine_le, reverse=True)  # tri stable : nom de dossier à égalité
    return entrees


def _lien(video: Path, page: Path) -> str | None:
    """Lien relatif de `page` vers `video`, segments encodés ; None si impossible."""
    try:
        rel = os.path.relpath(os.path.abspath(video), os.path.abspath(page.parent))
    except ValueError:  # autre lecteur (Windows)
        return None
    return "/".join(quote(seg, safe="") for seg in Path(rel).parts)


def _carte(entree: Entree, page: Path) -> str:
    rid = html.escape(entree.run_id)
    classe, icone, mot = _ETATS[entree.etat]
    if entree.duree_s is None:
        duree = "—"
    else:
        texte, iso = formater_duree(entree.duree_s)
        duree = f'<time datetime="{iso}">{texte}</time>'
    lien = _lien(entree.video, page) if entree.video else None
    lecteur = (
        f'\n    <video controls preload="none" src="{html.escape(lien)}"></video>' if lien else ""
    )
    return (
        f'  <li><article class="run" aria-labelledby="r-{rid}">\n'
        f'    <h2 id="r-{rid}"><code>{rid}</code></h2>\n'
        f'    <p class="state {classe}"><span aria-hidden="true">{icone}</span> {mot}</p>\n'
        f'    <p class="dur">{duree}</p>{lecteur}\n'
        "  </article></li>"
    )


def rendre(entrees: list[Entree], page: Path) -> str:
    """HTML autonome de la galerie ; `page` sert à calculer les liens relatifs."""
    n = len(entrees)
    if entrees:
        cartes = "\n".join(_carte(e, page) for e in entrees)
        corps = f'<ul class="runs">\n{cartes}\n</ul>'
    else:
        corps = '<p class="empty" role="status">Aucune vidéo pour l\'instant.</p>'
    return (
        '<!doctype html><html lang="fr"><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="light dark"><title>Galerie des vidéos</title>\n'
        f"<style>\n{_CSS}\n</style>\n"
        f'<body><header><h1>Galerie des vidéos</h1><p class="meta">{n} run{"s" if n > 1 else ""}'
        f"</p></header>\n<main>\n{corps}\n</main></body></html>\n"
    )


def generer(racine: Path, out: Path) -> int:
    """Écrit la galerie dans `out` ; renvoie le nombre de runs listés. Lève OSError."""
    entrees = lister(racine)
    out.write_text(rendre(entrees, out), encoding="utf-8")
    return len(entrees)
