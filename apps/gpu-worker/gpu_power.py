"""Alimentation du PC GPU (#37) : réveil à la demande, veille après inactivité.

Seule interface vers le PC : la commande `~/.local/bin/pc-gpu wake|sleep|status`, lancée via un
`run` injectable (liste d'arguments, shell=False, délai max 120 s). Les journaux et les erreurs
ne contiennent jamais de texte d'enfant (ni sortie de pc-gpu, ni message d'exception brut).
Bibliothèque standard uniquement, pour tourner tel quel sur le PC GPU ou le Mac relais.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PC_GPU = str(Path.home() / ".local" / "bin" / "pc-gpu")
TIMEOUT_S = 120
IDLE_MIN_S, IDLE_MAX_S = 300, 600
AWAKE, ASLEEP, BUSY = "awake", "asleep", "busy"
# début de la sortie de `pc-gpu status` (« eveille » / « endormi ou injoignable ») ou de ses variantes
_STATES = {"eveille": AWAKE, "éveillé": AWAKE, "awake": AWAKE, "endormi": ASLEEP,
           "asleep": ASLEEP, "occupe": BUSY, "occupé": BUSY, "busy": BUSY}  # fmt: skip

log = logging.getLogger("gpu_power")


class GpuUnavailable(RuntimeError):
    """Le PC GPU n'a pas pu être réveillé (message fixe, sans donnée d'enfant)."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str


Runner = Callable[[list[str], float], Result]


def subprocess_runner(args: list[str], timeout: float) -> Result:
    """Lance la commande sans shell ; TimeoutExpired / OSError remontent à l'appelant."""
    done = subprocess.run(  # noqa: S603 - liste d'arguments fixe, shell=False
        args, shell=False, capture_output=True, text=True, timeout=timeout, check=False
    )
    return Result(done.returncode, done.stdout)


class GpuPower:
    def __init__(
        self,
        run: Runner = subprocess_runner,
        clock: Callable[[], float] = time.monotonic,
        idle_s: int = IDLE_MAX_S,
    ) -> None:
        if not IDLE_MIN_S <= idle_s <= IDLE_MAX_S:
            raise ValueError(f"idle_s doit être entre {IDLE_MIN_S} et {IDLE_MAX_S} s")
        self._run = run
        self._clock = clock
        self._idle_s = idle_s
        self._running = 0
        self._sleeping = False  # veille décidée par tick() ; toute activité l'annule
        self._last_activity = clock()
        self._lock = threading.Lock()
        # sérialise ensure_awake() et la fin de tick() (dernier contrôle + sleep) : pas de
        # `status` lu pendant un `sleep` en cours (#223)
        self._power_lock = threading.Lock()

    def ensure_awake(self) -> None:
        """Réveille le PC s'il dort (idempotent) ; GpuUnavailable si impossible."""
        self._touch()  # avant le verrou : annule une veille pas encore engagée
        with self._power_lock:
            try:
                if self._status() in (AWAKE, BUSY):
                    return
                self._pc_gpu("wake", check=True)
                if self._status() not in (AWAKE, BUSY):
                    raise GpuUnavailable("PC GPU injoignable après réveil")
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("pc-gpu wake : %s", type(exc).__name__)
                raise GpuUnavailable("PC GPU injoignable") from None
            self._touch()

    def job_started(self) -> None:
        with self._lock:
            self._running += 1
            self._last_activity = self._clock()
            self._sleeping = False

    def job_finished(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)
            self._last_activity = self._clock()
            self._sleeping = False

    def tick(self) -> bool:
        """Endort le PC si rien ne tourne depuis idle_s ; vrai si `sleep` a réussi. Ne lève jamais."""
        with self._lock:
            if self._running > 0 or self._clock() - self._last_activity < self._idle_s:
                return False
            self._sleeping = True
        try:
            if self._status() != AWAKE:  # busy, endormi, ambigu : on ne touche à rien
                return self._abort_sleep()
            with self._power_lock:
                with self._lock:  # dernier contrôle : un job ou une dictée a-t-il annulé la veille ?
                    if not self._sleeping or self._running > 0:
                        self._sleeping = False
                        return False
                self._pc_gpu("sleep", check=True)
        except (OSError, subprocess.SubprocessError, GpuUnavailable) as exc:
            log.warning("pc-gpu status/sleep : %s", type(exc).__name__)
            return self._abort_sleep()
        with self._lock:
            self._sleeping = False
        log.info("PC GPU mis en veille")
        return True

    def _abort_sleep(self) -> bool:
        with self._lock:
            self._sleeping = False
        return False

    def _touch(self) -> None:
        with self._lock:
            self._last_activity = self._clock()
            self._sleeping = False

    def _pc_gpu(self, verb: str, check: bool = False) -> Result:
        res = self._run([PC_GPU, verb], TIMEOUT_S)
        if check and res.returncode != 0:
            log.warning("pc-gpu %s : code %s", verb, res.returncode)
            raise GpuUnavailable(f"pc-gpu {verb} : code {res.returncode}")
        return res

    def _status(self) -> str | None:
        """awake / asleep / busy ; None si la sortie est ambiguë ou la commande échoue."""
        try:
            res = self._pc_gpu("status")
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("pc-gpu status : %s", type(exc).__name__)
            return None
        words = res.stdout.split()
        state = _STATES.get(words[0].lower()) if words else None
        if res.returncode != 0 or state is None:
            log.warning("pc-gpu status : réponse illisible (code %s)", res.returncode)
            return None
        return state
