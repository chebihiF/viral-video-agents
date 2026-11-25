"""Jobs vidéo de l'appli enfant (#162) : la dictée part au CLI orchestrateur (#150) par stdin.

Échec fermé : jamais `--mocks` ; configuration absente ou CLI introuvable => état `error`.
La dictée n'est ni journalisée, ni passée en argument, ni en variable d'environnement : seul
`job_id` est journalisé. De la sortie d'erreur du CLI, seules les lignes d'étape (expression
fermée) sont lues ; le reste est jeté. Un seul parcours à la fois (le PC GPU est unique).
Contrat : contracts/kid-job.schema.json.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, TypedDict

TEXT_MAX = 500
DEFAULT_COMMAND = (sys.executable, "-m", "orchestrator")

State = Literal["queued", "running", "done", "error", "deferred"]
Step = Literal["storyboard", "images", "voix", "montage"]

# Étape du pipeline (orchestrator.pipeline) -> étape affichée : en cours, puis après succès.
_EN_COURS: dict[str, Step] = {
    "classification": "storyboard",
    "storyboard": "storyboard",
    "voix": "voix",
    "image": "images",
    "clip": "images",
    "montage": "montage",
}
_APRES: dict[str, Step] = {
    "classification": "storyboard",
    "storyboard": "voix",
    "voix": "images",
    "image": "images",
    "clip": "montage",
    "montage": "montage",
}
_LIGNE = re.compile(r"\betape=(\w+) statut=(ok|erreur)\b")

log = logging.getLogger(__name__)


class Status(TypedDict):
    state: State
    step: Step


class InvalidText(ValueError):
    """Dictée refusée ; le message ne cite jamais le texte."""


def valid_text(text: object) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise InvalidText(f"texte requis, non vide, {TEXT_MAX} caractères au plus")
    return text


class JobStore:
    """Jobs en mémoire, exécutés un par un dans un fil dédié."""

    def __init__(self, runs_dir: Path, command: Sequence[str] = DEFAULT_COMMAND) -> None:
        self._runs = runs_dir
        self._command = tuple(command)
        self._lock = threading.Lock()
        self._jobs: dict[str, Status] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kid-job")

    def argv(self, out: Path) -> list[str]:
        """Ligne de commande du CLI : la dictée viendra de stdin (« - »), jamais d'ici."""
        return [*self._command, "run", "--dictee-file", "-", "--out", str(out)]

    def submit(self, text: object) -> str:
        dictee = valid_text(text)
        job_id = str(uuid.uuid4())
        self._set(job_id, Status(state="queued", step="storyboard"))
        self._pool.submit(self._run, job_id, dictee)
        return job_id

    def status(self, job_id: str) -> Status | None:
        with self._lock:
            s = self._jobs.get(job_id)
            return None if s is None else Status(state=s["state"], step=s["step"])

    def _set(self, job_id: str, status: Status) -> None:
        with self._lock:
            ancien = self._jobs.get(job_id)
            self._jobs[job_id] = status
        if ancien is None or ancien["state"] != status["state"]:
            log.info("job=%s etat=%s", job_id, status["state"])

    def _update(self, job_id: str, *, state: State | None = None, step: Step | None = None) -> None:
        with self._lock:
            s = self._jobs[job_id]
        self._set(job_id, Status(state=state or s["state"], step=step or s["step"]))

    def _run(self, job_id: str, dictee: str) -> None:
        try:
            code = self._spawn(job_id, dictee)
        except Exception as exc:  # CLI introuvable, dossier illisible… : échec fermé
            log.warning("job=%s erreur=%s", job_id, type(exc).__name__)
            code = -1
        self._update(job_id, state="done" if code == 0 else "error")

    def _spawn(self, job_id: str, dictee: str) -> int:
        self._runs.mkdir(mode=0o700, parents=True, exist_ok=True)
        proc = subprocess.Popen(  # noqa: S603 - liste fixe, pas de shell, dictée par stdin
            self.argv(self._runs / job_id),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdin is not None and proc.stderr is not None
        self._update(job_id, state="running")
        try:
            proc.stdin.write(dictee)  # ≤ 500 caractères : tient dans le tampon du tube
            proc.stdin.close()
        except BrokenPipeError:
            pass  # le CLI est sorti sans lire : son code de sortie dira l'échec
        for ligne in proc.stderr:
            self._progress(job_id, ligne)
        return proc.wait()

    def _progress(self, job_id: str, ligne: str) -> None:
        m = _LIGNE.search(ligne)
        if m is None or m[1] not in _EN_COURS:
            return
        table = _APRES if m[2] == "ok" else _EN_COURS
        self._update(job_id, step=table[m[1]])
