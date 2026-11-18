"""Connexion au dashboard (contrat : contracts/dashboard-routes.md).

Mot de passe unique (hash PBKDF2-SHA256) et clé de session lus de l'environnement ;
session = cookie signé HMAC-SHA256 portant une date d'expiration. Stdlib uniquement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

PASSWORD_HASH_ENV = "DASHBOARD_PASSWORD_HASH"  # noqa: S105 - nom de variable, pas un secret
SESSION_KEY_ENV = "DASHBOARD_SESSION_KEY"
COOKIE_NAME = "vva_session"
SESSION_TTL_S = 8 * 3600
MIN_SESSION_KEY_LEN = 32
MAX_FORM_BYTES = 4096
ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
MAX_ITERATIONS = 10_000_000
NEUTRAL_MESSAGE = "Identifiants invalides."

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Encode `pbkdf2_sha256:<itérations>:<sel hex>:<hash hex>` (sans `$`, sûr en .env)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{ALGORITHM}:{iterations}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Vrai si `password` correspond ; tout hash mal formé ne correspond jamais."""
    try:
        algorithm, raw_iterations, salt_hex, digest_hex = encoded.split(":")
        iterations = int(raw_iterations)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        return False
    if algorithm != ALGORITHM or not 0 < iterations <= MAX_ITERATIONS or not expected:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest, expected)


def _signature(key: str, expires_at: int) -> str:
    mac = hmac.new(key.encode(), f"v1.{expires_at}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def sign_session(key: str, *, expires_at: int) -> str:
    return f"{expires_at}.{_signature(key, expires_at)}"


def verify_session(key: str, token: str, *, now: float | None = None) -> bool:
    raw_expiry, _, signature = token.partition(".")
    if not (raw_expiry.isascii() and raw_expiry.isdigit()) or len(raw_expiry) > 12:
        return False
    expires_at = int(raw_expiry)
    if not hmac.compare_digest(signature.encode(), _signature(key, expires_at).encode()):
        return False
    return (time.time() if now is None else now) < expires_at


class RateLimiter:
    """Fenêtre glissante : au plus `max_attempts` tentatives par `window_s` secondes."""

    def __init__(
        self, max_attempts: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_attempts, self.window_s, self.clock = max_attempts, window_s, clock
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self.clock()
            while self._hits and now - self._hits[0] >= self.window_s:
                self._hits.popleft()
            if len(self._hits) >= self.max_attempts:
                return False
            self._hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Compte unique : la limite est globale (une limite par IP se contourne en changeant d'IP).
LOGIN_LIMITER = RateLimiter(max_attempts=5, window_s=60)


def _settings() -> tuple[str, str] | None:
    """(hash, clé) si la connexion est configurée, sinon None (connexion fermée)."""
    encoded = os.environ.get(PASSWORD_HASH_ENV, "")
    key = os.environ.get(SESSION_KEY_ENV, "")
    if not encoded or len(key) < MIN_SESSION_KEY_LEN:
        return None
    return encoded, key


def is_authenticated(request: Request) -> bool:
    settings = _settings()
    token = request.cookies.get(COOKIE_NAME, "")
    return settings is not None and bool(token) and verify_session(settings[1], token)


def _login_page(request: Request, status: int, message: str | None = None) -> HTMLResponse:
    response = TEMPLATES.TemplateResponse(
        request, "login.html", {"message": message}, status_code=status
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def _read_password(request: Request) -> str | None:
    """Champ `password` d'un formulaire urlencodé borné ; None si trop gros."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            return None
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return fields.get("password", [""])[0]


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return _login_page(request, 200)


@router.post("/login", response_model=None)
async def login(request: Request) -> Response:
    settings = _settings()
    if settings is None:
        return _login_page(request, 503, "Connexion indisponible : service non configuré.")
    if not LOGIN_LIMITER.allow():
        response = _login_page(request, 429, "Trop de tentatives. Réessayez dans une minute.")
        response.headers["Retry-After"] = str(int(LOGIN_LIMITER.window_s))
        return response
    password = await _read_password(request)
    if password is None:
        return _login_page(request, 413, NEUTRAL_MESSAGE)
    encoded, key = settings
    if not await run_in_threadpool(verify_password, password, encoded):
        return _login_page(request, 401, NEUTRAL_MESSAGE)
    return _open_session(key)


def _open_session(key: str) -> RedirectResponse:
    response = RedirectResponse("/activity", status_code=303)
    token = sign_session(key, expires_at=int(time.time()) + SESSION_TTL_S)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_S,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response
