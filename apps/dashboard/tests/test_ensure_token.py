"""Le script de déploiement crée le jeton d'ingestion une seule fois, en 600."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "deploy" / "ensure-token.sh"


def _run(state: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [str(SCRIPT), str(state)], capture_output=True, text=True, check=True
    )


def test_creates_token_600_without_printing(tmp_path: Path) -> None:
    res = _run(tmp_path)
    token = (tmp_path / "ingest_token").read_text().strip()
    assert len(token) == 64
    assert stat.S_IMODE((tmp_path / "ingest_token").stat().st_mode) == 0o600
    assert token not in res.stdout + res.stderr


def test_idempotent_keeps_existing(tmp_path: Path) -> None:
    _run(tmp_path)
    first = (tmp_path / "ingest_token").read_text()
    _run(tmp_path)
    assert (tmp_path / "ingest_token").read_text() == first
