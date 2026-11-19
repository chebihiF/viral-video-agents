"""make telemetry-check : les hooks locaux émettent-ils vers l'ingestion ? (#170)"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import check_hooks
import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "telemetry" / "check_hooks.py"
EXAMPLE = ROOT / "docs" / "examples" / "hooks-settings.example.json"
SECRET = "jeton-ultra-secret-123"  # noqa: S105 - valeur factice de test
ENV_OK = {"VVA_INGEST_URL": "https://exemple.invalid/api/v1/events", "VVA_INGEST_TOKEN": SECRET}


def settings_file(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def hook(command: str, matcher: str | None = None) -> dict[str, Any]:
    group: dict[str, Any] = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        group["matcher"] = matcher
    return group


def run_cli(settings: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("VVA_")}
    return subprocess.run(  # noqa: S603 - interpréteur courant, script du dépôt
        [sys.executable, str(SCRIPT), "--settings", str(settings), *args],
        capture_output=True,
        text=True,
        env={**base, **env},
        cwd=ROOT,
        timeout=20,
        check=False,
    )


def example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def test_ancien_log_event_echoue_avec_correctif(tmp_path: Path) -> None:
    old = {
        "hooks": {
            "PostToolUse": [hook('"$CLAUDE_PROJECT_DIR"/.claude/hooks/log_event.sh', "*")],
            "Stop": [hook("quick_tests.sh")],
        }
    }
    proc = run_cli(settings_file(tmp_path, old), ENV_OK)
    assert proc.returncode != 0
    assert "log_event.sh" in proc.stderr
    assert "hooks-settings.example.json" in proc.stderr
    assert SECRET not in proc.stdout + proc.stderr


@pytest.mark.parametrize("missing", ["VVA_INGEST_URL", "VVA_INGEST_TOKEN"])
def test_variable_absente_echoue_sans_afficher_le_jeton(tmp_path: Path, missing: str) -> None:
    env = {k: v for k, v in ENV_OK.items() if k != missing}
    proc = run_cli(settings_file(tmp_path, example()), env)
    assert proc.returncode != 0
    assert missing in proc.stderr
    assert SECRET not in proc.stdout + proc.stderr


def test_variables_dans_le_bloc_env_des_reglages_comptent(tmp_path: Path) -> None:
    data = example()
    data["env"] = dict(ENV_OK)
    proc = run_cli(settings_file(tmp_path, data), {})
    assert proc.returncode == 0, proc.stderr
    assert SECRET not in proc.stdout + proc.stderr


def test_hooks_conformes_et_variables_presentes_passent(tmp_path: Path) -> None:
    proc = run_cli(settings_file(tmp_path, example()), ENV_OK)
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout.lower()
    assert SECRET not in proc.stdout + proc.stderr


def test_evenement_factice_valide_contre_le_schema() -> None:
    assert check_hooks.synthetic_event_errors() == []


def test_reglages_absents_ou_invalides_echouent(tmp_path: Path) -> None:
    assert run_cli(tmp_path / "absent.json", ENV_OK).returncode != 0
    bad = tmp_path / "bad.json"
    bad.write_text("{pas du json", encoding="utf-8")
    assert run_cli(bad, ENV_OK).returncode != 0


def test_ship_manquant_signale(tmp_path: Path) -> None:
    data = example()
    del data["hooks"]["Stop"]
    proc = run_cli(settings_file(tmp_path, data), ENV_OK)
    assert proc.returncode != 0
    assert "ship.py" in proc.stderr


def test_hook_emit_manquant_sur_echec_d_outil_signale(tmp_path: Path) -> None:
    data = example()
    del data["hooks"]["PostToolUseFailure"]
    proc = run_cli(settings_file(tmp_path, data), ENV_OK)
    assert proc.returncode != 0
    assert "PostToolUseFailure" in proc.stderr


def test_gate_absent_est_un_avertissement_pas_un_echec(tmp_path: Path) -> None:
    data = example()
    del data["hooks"]["PreToolUse"]
    proc = run_cli(settings_file(tmp_path, data), ENV_OK)
    assert proc.returncode == 0
    assert "pretool_gate.py" in proc.stdout


def test_modele_json_valide_sans_secret() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    data = json.loads(text)
    commands = [
        h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]
    ]
    assert any("hook_emit.py" in c for c in commands)
    assert any("ship.py" in c for c in commands)
    assert any("pretool_gate.py" in c for c in commands)
    assert not any("log_event.sh" in c for c in commands)
    assert "Bearer" not in text
    assert "https://" not in text


def test_lecture_seule_des_reglages(tmp_path: Path) -> None:
    path = settings_file(tmp_path, example())
    before = path.stat().st_mtime_ns, path.read_bytes()
    run_cli(path, ENV_OK)
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before
