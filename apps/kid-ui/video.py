"""Service de la vidéo terminée (#163) : MP4 du job, avec `Range` et téléchargement optionnel.

Le chemin vient du seul `job_id` (UUID validé) : jamais d'un nom fourni par le client. Seul un
fichier régulier `final.mp4` (lien symbolique exclu), directement dans le dossier du job ou dans
son unique sous-dossier de run, est servi. Aucun nom ni journal ne porte la dictée.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

CHUNK = 64 * 1024
FILE_NAME = "final.mp4"
JOB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_RANGE = re.compile(r"bytes=(\d{0,19})-(\d{0,19})")


def _kind(path: Path, expected: Callable[[int], bool]) -> bool:
    """Vrai si `path` lui-même (lien non suivi) est du type attendu : lien symbolique => faux."""
    try:
        return expected(os.lstat(path).st_mode)
    except OSError:
        return False


def find_mp4(runs_dir: Path, job_id: str) -> Path | None:
    """MP4 du job, ou None. `job_id` doit être un UUID minuscule : sinon rien n'est cherché.
    Aucun lien symbolique n'est suivi : ni le dossier du job, ni un sous-dossier, ni le fichier."""
    if not JOB_ID.fullmatch(job_id):
        return None
    base = runs_dir / job_id
    if not _kind(base, stat.S_ISDIR):
        return None
    try:
        subs = sorted(p for p in base.iterdir() if _kind(p, stat.S_ISDIR))
    except OSError:
        return None
    candidates = [base / FILE_NAME, *(p / FILE_NAME for p in subs)]
    return next((c for c in candidates if _kind(c, stat.S_ISREG)), None)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """Plage (début, fin) incluse ; None = pas de plage exploitable (fichier entier) ;
    "unsatisfiable" si la plage est hors fichier. Une seule plage `bytes=` est comprise."""
    m = _RANGE.fullmatch(header.strip()) if header else None
    if m is None or (m[1] == "" and m[2] == ""):
        return None
    if m[1] == "":  # suffixe : les n derniers octets
        n = int(m[2])
        return "unsatisfiable" if n == 0 or size == 0 else (max(0, size - n), size - 1)
    start = int(m[1])
    end = int(m[2]) if m[2] else size - 1
    if m[2] and end < start:
        return None  # plage invalide : ignorée (RFC 9110)
    return "unsatisfiable" if start >= size else (start, min(end, size - 1))


def send(h: Any, mp4: Path, job_id: str, *, download: bool) -> None:
    """Répond 200/206/416 pour le gestionnaire `h` (Handler) ; 404 si le fichier a disparu."""
    try:
        fd = os.open(mp4, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # jamais bloqué par un tube
    except OSError:
        h.send_json(404, {"error": "vidéo inconnue"})
        return
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            h.send_json(404, {"error": "vidéo inconnue"})
            return
        size = info.st_size
        rng = parse_range(h.headers.get("Range"), size)
        h.send_response(416 if rng == "unsatisfiable" else 206 if rng else 200)
        start, end = rng if isinstance(rng, tuple) else (0, size - 1)
        h.send_header("Content-Type", "video/mp4")
        h.send_header("Accept-Ranges", "bytes")
        h.send_header("Cache-Control", "no-store")
        kind = "attachment" if download else "inline"
        h.send_header("Content-Disposition", f'{kind}; filename="{job_id}.mp4"')
        if rng == "unsatisfiable":
            h.send_header("Content-Range", f"bytes */{size}")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        if isinstance(rng, tuple):
            h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        h.send_header("Content-Length", str(end - start + 1))
        h.end_headers()
        copy_range(h, f, start, end - start + 1)


def copy_range(h: Any, f: Any, start: int, length: int) -> None:
    f.seek(start)
    try:
        while length > 0:
            chunk = f.read(min(CHUNK, length))
            if not chunk:
                break
            h.wfile.write(chunk)
            length -= len(chunk)
    except OSError:  # lecteur parti ou plage changée (BrokenPipe, Reset, délai, ssl.SSLError)
        h.close_connection = True
