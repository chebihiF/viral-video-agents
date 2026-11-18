"""deploy.sh + compose.yaml (#30) : clé de session stable, hash transmis, /login fermé par défaut.

Tout se passe dans un dossier temporaire : faux docker/curl dans le PATH, état dans tmp_path.
Aucun secret réel : la clé et le hash générés ne servent qu'au test.
"""

from __future__ import annotations

import os
import stat
import subprocess
from typing import Any
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
VERSION = "1.2.3"
FAKE_DOCKER = """#!/usr/bin/env bash
{
  echo "ARGS:$*"
  echo "SESSION_KEY:${DASHBOARD_SESSION_KEY-<unset>}"
  echo "PASSWORD_HASH:${DASHBOARD_PASSWORD_HASH-<unset>}"
} >> "$FAKE_LOG"
exit 0
"""
# Répond à /healthz (version) et à / (titre) avec la même ligne : suffit au smoke test.
FAKE_CURL = """#!/usr/bin/env bash
echo '{"version":"'"$FAKE_VERSION"'"} Agents Dashboard'
"""


def _fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, Path | dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake(bin_dir, "docker", FAKE_DOCKER)
    _fake(bin_dir, "curl", FAKE_CURL)
    state = tmp_path / "state"
    state.mkdir()
    (state / "proxy.env").write_text("DASHBOARD_HOST=dashboard.test\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "VVA_STATE_DIR": str(state),
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_VERSION": VERSION,
    }
    return {"state": state, "env": env, "log": tmp_path / "docker.log"}


def deploy(sb: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(DEPLOY / "deploy.sh"), "ghcr.io/example/vva-dashboard", VERSION],  # noqa: S607
        env=sb["env"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_session_key_created_600_and_kept_between_deploys(sandbox: dict[str, Any]) -> None:
    res = deploy(sandbox)
    assert res.returncode == 0, res.stdout + res.stderr
    key_file = sandbox["state"] / "session_key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    first = key_file.read_text().strip()
    assert len(first) == 64  # openssl rand -hex 32
    assert not (sandbox["state"] / "session_key.tmp").exists()

    res = deploy(sandbox)
    assert res.returncode == 0, res.stdout + res.stderr
    assert key_file.read_text().strip() == first


def test_no_hash_file_keeps_login_closed_at_deploy(sandbox: dict[str, Any]) -> None:
    res = deploy(sandbox)
    assert res.returncode == 0, res.stdout + res.stderr
    log = sandbox["log"].read_text()
    assert "PASSWORD_HASH:\n" in log  # vide => /login répond 503 côté appli
    assert "SESSION_KEY:<unset>" not in log


def test_hash_file_is_passed_to_container_env(sandbox: dict[str, Any]) -> None:
    encoded = "pbkdf2_sha256:600000:00112233:44556677"  # faux, jamais vérifié ici
    (sandbox["state"] / "dashboard_password_hash").write_text(encoded + "\n")
    res = deploy(sandbox)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"PASSWORD_HASH:{encoded}" in sandbox["log"].read_text()


def test_compose_passes_both_secrets_from_environment() -> None:
    text = COMPOSE.read_text()
    assert "DASHBOARD_SESSION_KEY: ${DASHBOARD_SESSION_KEY:-}" in text
    assert "DASHBOARD_PASSWORD_HASH: ${DASHBOARD_PASSWORD_HASH:-}" in text
