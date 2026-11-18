"""set-dashboard-password.sh (#30) : hash vérifiable, mot de passe jamais écrit ni affiché."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
from dashboard.auth import verify_password

SCRIPT = Path(__file__).resolve().parent / "set-dashboard-password.sh"
PASSWORD = "Tr0ub4dor-&-3-chevaux"  # noqa: S105 - valeur de test jetable
# Faux ssh : journalise ses arguments, exécute la commande distante localement (stdin transmis).
FAKE_SSH = """#!/usr/bin/env bash
printf '%s\\n' "$@" >> "$FAKE_SSH_LOG"
eval "${@: -1}"
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    ssh = tmp_path / "fake-ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    return {
        **os.environ,
        "VVA_SSH": str(ssh),
        "VVA_SSH_KEY": str(tmp_path / "key"),
        "VVA_SSH_TARGET": "test@localhost",
        "VVA_REMOTE_FILE": str(tmp_path / "remote" / "dashboard_password_hash"),
        "VVA_REMOTE_OWNER": str(os.getuid()),
        "FAKE_SSH_LOG": str(tmp_path / "ssh.log"),
    }


def run(env: dict[str, str], stdin: str) -> subprocess.CompletedProcess[str]:
    Path(env["VVA_REMOTE_FILE"]).parent.mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_hash_is_written_600_and_verifiable(env: dict[str, str]) -> None:
    res = run(env, f"{PASSWORD}\n{PASSWORD}\n")
    assert res.returncode == 0, res.stderr
    target = Path(env["VVA_REMOTE_FILE"])
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    encoded = target.read_text().strip()
    assert encoded.startswith("pbkdf2_sha256:600000:")
    assert verify_password(PASSWORD, encoded)
    assert not verify_password(PASSWORD + "x", encoded)
    assert not list(target.parent.glob("*.tmp"))


def test_password_appears_nowhere(env: dict[str, str], tmp_path: Path) -> None:
    res = run(env, f"{PASSWORD}\n{PASSWORD}\n")
    assert res.returncode == 0, res.stderr
    assert PASSWORD not in res.stdout + res.stderr
    leaks = [p for p in tmp_path.rglob("*") if p.is_file() and PASSWORD.encode() in p.read_bytes()]
    assert leaks == []
    log = Path(env["FAKE_SSH_LOG"]).read_text()
    assert "-i" in log and "IdentitiesOnly=yes" in log and "test@localhost" in log


def test_two_hashes_differ_by_salt(env: dict[str, str]) -> None:
    run(env, f"{PASSWORD}\n{PASSWORD}\n")
    first = Path(env["VVA_REMOTE_FILE"]).read_text()
    run(env, f"{PASSWORD}\n{PASSWORD}\n")
    assert Path(env["VVA_REMOTE_FILE"]).read_text() != first


@pytest.mark.parametrize(
    ("stdin", "why"),
    [(f"{PASSWORD}\nautre-chose-123\n", "diffèrent"), ("court\ncourt\n", "trop court")],
)
def test_refuses_mismatch_or_short_without_touching_target(
    env: dict[str, str], stdin: str, why: str
) -> None:
    res = run(env, stdin)
    assert res.returncode != 0
    assert why in res.stderr
    assert PASSWORD not in res.stdout + res.stderr
    assert not Path(env["VVA_REMOTE_FILE"]).exists()
    assert not Path(env["FAKE_SSH_LOG"]).exists()


def test_ssh_failure_is_reported(env: dict[str, str], tmp_path: Path) -> None:
    failing = tmp_path / "fail-ssh"
    failing.write_text("#!/usr/bin/env bash\nexit 255\n")
    failing.chmod(0o755)
    res = run({**env, "VVA_SSH": str(failing)}, f"{PASSWORD}\n{PASSWORD}\n")
    assert res.returncode == 255
    assert "enregistré" not in res.stderr
