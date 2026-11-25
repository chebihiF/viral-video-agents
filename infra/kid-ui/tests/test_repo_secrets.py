# ruff: noqa: S603
"""Scénarios 1 et 4 de #160 : certs/ ignoré, aucune clé ni CA suivie, noms hostiles refusés."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra" / "kid-ui" / "make-cert.sh"
GIT = shutil.which("git") or "git"
pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="hors dépôt git")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT, "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )  # noqa: S603


@pytest.mark.parametrize("f", ["cert.pem", "key.pem", "rootCA.pem", "rootCA-key.pem"])
def test_certs_ignores_par_git(f: str) -> None:
    assert _git("check-ignore", "-q", f"infra/kid-ui/certs/{f}").returncode == 0


def test_aucune_cle_ni_ca_suivie() -> None:
    suivis = _git("ls-files").stdout.splitlines()
    bad = [
        p for p in suivis if p.lower().endswith((".pem", ".key", ".p12", ".crt")) or "/certs/" in p
    ]
    assert bad == []
    for p in suivis:
        f = ROOT / p
        if f.is_file() and f.stat().st_size < 1_000_000:
            assert b"PRIVATE KEY-----" not in f.read_bytes() or p.endswith(".py"), p


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks absent")
def test_gitleaks_vert() -> None:
    r = subprocess.run(  # noqa: S603
        [shutil.which("gitleaks") or "gitleaks", "dir", str(ROOT), "--no-banner", "--redact"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize(
    "name", ["", "مرحبا.local", "kid local", "-x", "a/b", "ünï.local", "kid\n.local"]
)
def test_noms_hostiles_refuses(name: str) -> None:
    r = subprocess.run(
        [shutil.which("bash") or "bash", str(SCRIPT), "--dry-run", name],
        capture_output=True,
        text=True,
        check=False,
    )  # noqa: S603
    assert r.returncode == 2, r.stdout
