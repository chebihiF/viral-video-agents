#!/usr/bin/env python3
"""Expéditeur : JSONL des hooks -> POST /api/v1/events par lots de 100 au plus (#4).

Contrat : contracts/ingest-api.md. Configuration par l'environnement :
VVA_INGEST_URL (https, ou http sur localhost), VVA_INGEST_TOKEN (Bearer),
VVA_TELEMETRY_FILE (facultatif).

Fiabilité : un curseur (inode + offset en octets, `<fichier>.offset`) n'avance
qu'après un 200/202. Réseau ou 5xx : on garde la file et on réessaie au prochain
passage. 3xx (jamais suivie) et 4xx hors 413/422 : configuration à corriger, file
gardée. 413/422 sur un lot : renvoi événement par événement ; les refusés sont
isolés dans `<fichier>.rejected`. Le serveur déduplique par event_id.
Stdlib uniquement.
"""

from __future__ import annotations

import enum
import fcntl
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from hook_emit import target_file

EXIT_OK, EXIT_RETRY, EXIT_CONFIG = 0, 1, 2
BATCH_MAX = 100
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
REJECTABLE = frozenset({413, 422})


class Outcome(enum.Enum):
    OK = enum.auto()
    RETRY = enum.auto()
    CONFIG = enum.auto()
    REJECTED = enum.auto()


@dataclass(frozen=True)
class Config:
    events_file: Path
    url: str
    token: str
    timeout: float = 10.0


@dataclass(frozen=True)
class Line:
    end: int
    raw: bytes


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


def cursor_path(events_file: Path) -> Path:
    return events_file.with_name(events_file.name + ".offset")


def rejected_path(events_file: Path) -> Path:
    return events_file.with_name(events_file.name + ".rejected")


def lock_path(events_file: Path) -> Path:
    return events_file.with_name(events_file.name + ".lock")


def url_allowed(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS


def open_private(path: Path, flags: int) -> int:
    """Descripteur sur un fichier d'état en 0600, même s'il existait déjà."""
    fd = os.open(path, flags | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    return fd


@dataclass(frozen=True)
class Cursor:
    events_file: Path
    ino: int

    def advance(self, offset: int) -> None:
        path = cursor_path(self.events_file)
        tmp = path.with_suffix(".tmp")
        fd = open_private(tmp, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            json.dump({"ino": self.ino, "offset": offset}, fh)
        os.replace(tmp, path)


def stored_offset(events_file: Path, ino: int, size: int) -> int:
    """Offset mémorisé, ou 0 si le fichier a été remplacé (inode) ou tronqué."""
    try:
        state = json.loads(cursor_path(events_file).read_text(encoding="ascii"))
        offset = state["offset"] if state["ino"] == ino else 0
    except (OSError, ValueError, TypeError, KeyError):
        return 0
    return offset if isinstance(offset, int) and 0 <= offset <= size else 0


def complete_lines(data: bytes, offset: int) -> list[Line]:
    """Lignes complètes ; une ligne en cours d'écriture (sans \\n) est ignorée."""
    lines: list[Line] = []
    position = offset
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break
        position += len(raw)
        lines.append(Line(end=position, raw=raw))
    return lines


def read_pending(events_file: Path) -> tuple[Cursor, list[Line]]:
    with events_file.open("rb") as fh:
        st = os.fstat(fh.fileno())
        offset = stored_offset(events_file, st.st_ino, st.st_size)
        fh.seek(offset)
        data = fh.read()
    return Cursor(events_file, st.st_ino), complete_lines(data, offset)


def parse(line: Line) -> Any | None:
    try:
        return json.loads(line.raw)
    except ValueError:
        return None


def reject(cfg: Config, line: Line, reason: str) -> None:
    fd = open_private(rejected_path(cfg.events_file), os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "ab") as fh:
        fh.write(line.raw)
    print(f"ship: 1 ligne isolée ({reason})", file=sys.stderr)


def classify(status: int) -> Outcome:
    if status in (200, 202):
        return Outcome.OK
    if status in REJECTABLE:
        return Outcome.REJECTED
    if 300 <= status < 500:
        return Outcome.CONFIG
    return Outcome.RETRY


def post(cfg: Config, events: list[Any]) -> Outcome:
    request = urllib.request.Request(  # noqa: S310 - URL vérifiée par url_allowed()
        cfg.url,
        data=json.dumps(events).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=cfg.timeout) as resp:
            return classify(resp.status)
    except urllib.error.HTTPError as exc:
        return classify(exc.code)
    except (OSError, ValueError):
        return Outcome.RETRY


def next_batch(cfg: Config, cursor: Cursor, lines: list[Line]) -> tuple[list[Line], list[Any]]:
    """Lot contigu de lignes JSON valides ; une ligne illisible en tête est isolée."""
    batch: list[Line] = []
    events: list[Any] = []
    for line in lines[:BATCH_MAX]:
        event = parse(line)
        if event is None:
            if not batch:
                reject(cfg, line, "JSON illisible")
                cursor.advance(line.end)
                batch.append(line)
            break
        batch.append(line)
        events.append(event)
    return batch, events


def send_one_by_one(cfg: Config, cursor: Cursor, batch: list[Line], events: list[Any]) -> Outcome:
    for line, event in zip(batch, events, strict=True):
        outcome = post(cfg, [event])
        if outcome is Outcome.REJECTED:
            reject(cfg, line, "refusé par le serveur")
        elif outcome is not Outcome.OK:
            return outcome
        cursor.advance(line.end)
    return Outcome.OK


def drain(cfg: Config) -> Outcome:
    cursor, lines = read_pending(cfg.events_file)
    while lines:
        batch, events = next_batch(cfg, cursor, lines)
        lines = lines[len(batch) :]
        if not events:
            continue
        outcome = post(cfg, events)
        if outcome is Outcome.REJECTED:
            outcome = send_one_by_one(cfg, cursor, batch, events)
        elif outcome is Outcome.OK:
            cursor.advance(batch[-1].end)
        if outcome is not Outcome.OK:
            return outcome
    return Outcome.OK


@contextmanager
def exclusive(events_file: Path) -> Iterator[bool]:
    """Verrou non bloquant : un seul expéditeur à la fois par fichier."""
    fd = open_private(lock_path(events_file), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def run(cfg: Config) -> int:
    if not url_allowed(cfg.url):
        print(
            "ship: VVA_INGEST_URL doit être en https (http : localhost seulement)", file=sys.stderr
        )
        return EXIT_CONFIG
    if not cfg.events_file.is_file():
        return EXIT_OK
    with exclusive(cfg.events_file) as acquired:
        if not acquired:
            return EXIT_OK
        outcome = drain(cfg)
    if outcome is Outcome.CONFIG:
        print("ship: réponse 3xx/4xx (URL ou jeton à corriger), file conservée", file=sys.stderr)
        return EXIT_CONFIG
    if outcome is Outcome.RETRY:
        print("ship: serveur injoignable ou en erreur, file conservée", file=sys.stderr)
        return EXIT_RETRY
    return EXIT_OK


def main() -> int:
    url = os.environ.get("VVA_INGEST_URL", "")
    token = os.environ.get("VVA_INGEST_TOKEN", "")
    if not url or not token:
        print("ship: VVA_INGEST_URL et VVA_INGEST_TOKEN sont requis", file=sys.stderr)
        return EXIT_CONFIG
    return run(Config(events_file=target_file(), url=url, token=token))


if __name__ == "__main__":
    sys.exit(main())
