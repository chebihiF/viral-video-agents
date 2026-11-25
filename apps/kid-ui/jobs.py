"""Jobs vidéo de l'appli enfant (#162) : la dictée part au CLI orchestrateur (#150) par stdin.

Échec fermé : jamais `--mocks` ; configuration absente ou CLI introuvable => état `error`.
La dictée n'est ni journalisée, ni passée en argument, ni en variable d'environnement : seul
`job_id` est journalisé. De la sortie d'erreur du CLI, seules les lignes d'étape (expression
fermée) sont lues ; le reste est jeté. Un seul job actif à la fois (le PC GPU est unique) :
une soumission pendant un job queued/running est refusée (Busy). Délai global JOB_TIMEOUT_S,
puis kill et état error. Dictée bloquée par la classification (code 2 du CLI) : error avec
`blocked`, à ne pas réessayer. Contrat : contracts/kid-job.schema.json.
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
from typing import Literal, NotRequired, TypedDict

TEXT_MAX = 500
MAX_JOBS = 50  # jobs gardés en mémoire ; au-delà, les plus anciens terminés sont oubliés
JOB_TIMEOUT_S = 30 * 60
EX_BLOQUE = 2  # code de sortie du CLI orchestrateur pour une dictée bloquée
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
    blocked: NotRequired[bool]


class InvalidText(ValueError):
    """Dictée refusée ; le message ne cite jamais le texte."""


class Busy(RuntimeError):
    """Un job est déjà en file ou en cours."""


def valid_text(text: object) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise InvalidText(f"texte requis, non vide, {TEXT_MAX} caractères au plus")
    return text


class JobStore:
    """Jobs en mémoire, exécutés un par un dans un fil dédié."""

    def __init__(
        self,
        runs_dir: Path,
        command: Sequence[str] = DEFAULT_COMMAND,
        *,
        max_jobs: int = MAX_JOBS,
        timeout_s: float = JOB_TIMEOUT_S,
    ) -> None:
        self._runs = runs_dir
        self._command = tuple(command)
        self._max = max_jobs
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._jobs: dict[str, Status] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kid-job")

    @property
    def runs_dir(self) -> Path:
        return self._runs

    def argv(self, out: Path) -> list[str]:
        """Ligne de commande du CLI : la dictée viendra de stdin (« - »), jamais d'ici."""
        return [*self._command, "run", "--dictee-file", "-", "--out", str(out)]

    def submit(self, text: object) -> str:
        dictee = valid_text(text)
        job_id = str(uuid.uuid4())
        with self._lock:
            if any(s["state"] in ("queued", "running") for s in self._jobs.values()):
                raise Busy("un job est déjà en cours")
            self._purge()
            self._jobs[job_id] = Status(state="queued", step="storyboard")
        log.info("job=%s etat=queued", job_id)
        self._pool.submit(self._run, job_id, dictee)
        return job_id

    def status(self, job_id: str) -> Status | None:
        with self._lock:
            s = self._jobs.get(job_id)
            return None if s is None else Status(**s)  # copie

    def _purge(self) -> None:
        """Oublie les plus anciens jobs terminés : au plus max_jobs - 1 restent (verrou tenu)."""
        finis = [k for k, s in self._jobs.items() if s["state"] in ("done", "error")]
        for k in finis[: max(0, len(self._jobs) - self._max + 1)]:
            del self._jobs[k]

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
        with self._lock:
            s = self._jobs[job_id]
            fin = Status(state="done" if code == 0 else "error", step=s["step"])
            if code == EX_BLOQUE:
                fin["blocked"] = True
            self._jobs[job_id] = fin
        log.info("job=%s etat=%s code=%d", job_id, fin["state"], code)

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
        tue = threading.Event()

        def tuer() -> None:
            tue.set()
            proc.kill()

        delai = threading.Timer(self._timeout, tuer)
        delai.daemon = True
        delai.start()
        self._update(job_id, state="running")
        try:
            proc.stdin.write(dictee)  # ≤ 500 caractères : tient dans le tampon du tube
            proc.stdin.close()
        except BrokenPipeError:
            pass  # le CLI est sorti sans lire : son code de sortie dira l'échec
        for ligne in proc.stderr:
            self._progress(job_id, ligne)
        code = proc.wait()
        delai.cancel()
        if tue.is_set():
            log.warning("job=%s erreur=delai", job_id)
            return -1
        return code

    def _progress(self, job_id: str, ligne: str) -> None:
        m = _LIGNE.search(ligne)
        if m is None or m[1] not in _EN_COURS:
            return
        table = _APRES if m[2] == "ok" else _EN_COURS
        self._update(job_id, step=table[m[1]])
