#!/usr/bin/env python3
"""Hook Claude Code PreToolUse : bloque les outils tant que les agents sont en pause (#8).

Contrat : contracts/control-api.md. Interroge `GET /api/v1/control` (jeton Bearer) ;
`paused=true` -> message sur stderr et code de sortie 2 (l'outil n'est pas exécuté).
Échec sûr (fail-open) : serveur injoignable, 4xx/5xx ou réponse invalide -> dernier état en
cache s'il a au plus 5 min, sinon on laisse passer. Chaque repli est journalisé (jamais le
jeton, l'URL, ni le contenu du hook : stdin n'est même pas lu). Stdlib uniquement.

Variables : VVA_CONTROL_URL (URL complète, https ou http sur localhost), VVA_INGEST_TOKEN,
VVA_GATE_CACHE et VVA_GATE_LOG (facultatifs).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from types import FrameType
from typing import IO
from urllib.parse import urlsplit

EXIT_ALLOW, EXIT_BLOCK = 0, 2
CACHE_TTL_S = 300
TIMEOUT_S = 2.0
DEADLINE_S = 4.0
MAX_RESPONSE_BYTES = 4096
MAX_LOG_BYTES = 256 * 1024
KNOWN_REASONS = frozenset({"manuel", "plafond"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
STATE_DIR = Path(".claude") / "telemetry"


@dataclass(frozen=True)
class State:
    paused: bool
    reason: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Aucune redirection suivie : le jeton ne part jamais vers une autre URL."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _state_path(variable: str, name: str) -> Path:
    configured = os.environ.get(variable)
    if configured:
        return Path(configured)
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / STATE_DIR / name


def log(message: str) -> None:
    """Une ligne `<date UTC> <code>` ; le code est un mot-clé fixe, jamais une donnée d'entrée."""
    try:
        path = _state_path("VVA_GATE_LOG", "gate.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def url_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS


def parse_state(raw: bytes) -> State | None:
    try:
        return state_from(json.loads(raw))
    except ValueError:
        return None


def state_from(data: object) -> State | None:
    """`paused` booléen strict et `reason` texte ou null ; sinon inexploitable."""
    if not isinstance(data, dict) or not isinstance(data.get("paused"), bool):
        return None
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None
    return State(data["paused"], reason or None)


def fetch(url: str, token: str) -> State | None:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    try:
        with _OPENER.open(request, timeout=TIMEOUT_S) as response:
            if response.status != 200:
                log(f"fetch-failed status={response.status}")
                return None
            state = parse_state(response.read(MAX_RESPONSE_BYTES))
    except urllib.error.HTTPError as exc:
        log(f"fetch-failed status={exc.code}")
        return None
    except (OSError, ValueError) as exc:
        log(f"fetch-failed {type(exc).__name__}")
        return None
    if state is None:
        log("fetch-failed invalid-response")
    return state


def load_cache(now: float) -> State | None:
    """Dernier état connu s'il a au plus CACHE_TTL_S (et n'est pas daté du futur)."""
    try:
        data = json.loads(_state_path("VVA_GATE_CACHE", "gate-cache.json").read_bytes())
        age = now - float(data["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    state = state_from(data)
    return state if state is not None and 0 <= age <= CACHE_TTL_S else None


def save_cache(state: State, now: float) -> None:
    path = _state_path("VVA_GATE_CACHE", "gate-cache.json")
    payload = {"paused": state.paused, "reason": state.reason, "fetched_at": now}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gate-")  # 0600
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"cache-write-failed {type(exc).__name__}")


def block_message(state: State) -> str:
    reason = state.reason if state.reason in KNOWN_REASONS else "inconnu"
    return (
        f"Agents en pause (motif : {reason}). L'outil n'a pas été exécuté. "
        "Reprenez depuis la page Contrôles du dashboard ; les agents repartent au prochain appel."
    )


def current_state(now: float) -> State | None:
    """État frais, sinon cache valide, sinon None (on laisse passer)."""
    url = os.environ.get("VVA_CONTROL_URL", "")
    token = os.environ.get("VVA_INGEST_TOKEN", "")
    if not token or not url_allowed(url):
        log("config missing-or-unsafe fail-open")
        return None
    state = fetch(url, token)
    if state is not None:
        save_cache(state, now)
        return state
    cached = load_cache(now)
    if cached is None:
        log("fail-open no-valid-cache")
        return None
    log(f"cache-used paused={cached.paused}")
    return cached


def _bail(_signum: int, _frame: FrameType | None) -> None:
    """Délai global dépassé : cache valide s'il est en pause (code 2), sinon on laisse passer."""
    log("fail-open deadline")
    try:
        cached = load_cache(time.time())
    except BaseException:  # noqa: BLE001 - jamais d'échec ici : on laisse passer
        cached = None
    if cached is not None and cached.paused:
        log("blocked")
        os.write(sys.stderr.fileno(), (block_message(cached) + "\n").encode())
        os._exit(EXIT_BLOCK)
    os._exit(EXIT_ALLOW)


def main() -> int:
    try:
        signal.signal(signal.SIGALRM, _bail)
        signal.setitimer(signal.ITIMER_REAL, DEADLINE_S)
        state = current_state(time.time())
    except BaseException:  # noqa: BLE001 - un bug du hook ne doit jamais bloquer le travail
        log("fail-open internal-error")
        return EXIT_ALLOW
    if state is not None and state.paused:
        log("blocked")
        print(block_message(state), file=sys.stderr)
        return EXIT_BLOCK
    return EXIT_ALLOW


if __name__ == "__main__":
    sys.exit(main())
