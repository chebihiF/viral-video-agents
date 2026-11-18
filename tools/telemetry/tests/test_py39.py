"""Les scripts lancés par les hooks tournent sous le python3 du Mac (3.9) (#59)."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TELEMETRY = Path(__file__).resolve().parents[1]
SCRIPTS = ["hook_emit.py", "ship.py", "pretool_gate.py"]


def py39_interpreter() -> str | None:
    """Un Python < 3.10 présent sur la machine (le Mac), sinon None."""
    for name in ("python3.9", "/usr/bin/python3"):
        path = shutil.which(name)
        if path is None:
            continue
        out = subprocess.run(  # noqa: S603 - interpréteur local, sans entrée utilisateur
            [path, "-c", "import sys; print(sys.version_info < (3, 10))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.stdout.strip() == "True":
            return path
    return None


@pytest.mark.parametrize("script", SCRIPTS)
def test_syntax_is_python_39(script: str) -> None:
    source = (TELEMETRY / script).read_text(encoding="utf-8")
    ast.parse(source, filename=script, feature_version=(3, 9))


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_api_newer_than_39(script: str) -> None:
    """`datetime.UTC` (3.11) et `zip(strict=)` (3.10) cassent le hook sous 3.9."""
    tree = ast.parse((TELEMETRY / script).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            assert "UTC" not in [a.name for a in node.names]
        if isinstance(node, ast.Attribute) and node.attr == "UTC":
            pytest.fail("datetime.UTC n'existe pas en 3.9 : utiliser timezone.utc")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "zip"
        ):
            assert "strict" not in [k.arg for k in node.keywords]


def test_hook_emit_writes_a_line_under_real_python_39(tmp_path: Path) -> None:
    interpreter = py39_interpreter()
    if interpreter is None:
        pytest.skip("aucun Python < 3.10 sur cette machine (couvert par les tests AST)")
    target = tmp_path / "events.jsonl"
    env = {**os.environ, "VVA_TELEMETRY_FILE": str(target)}
    hook = {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash"}
    proc = subprocess.run(  # noqa: S603 - interpréteur local, script du dépôt
        [interpreter, str(TELEMETRY / "hook_emit.py")],
        input=json.dumps(hook),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0
    line = json.loads(target.read_text(encoding="utf-8"))
    assert line["hook_event_name"] == "PostToolUse"
    assert line["ts"].endswith("+00:00")


def test_ship_imports_under_real_python_39() -> None:
    interpreter = py39_interpreter()
    if interpreter is None:
        pytest.skip("aucun Python < 3.10 sur cette machine (couvert par les tests AST)")
    proc = subprocess.run(  # noqa: S603 - interpréteur local, script du dépôt
        [interpreter, "-c", "import ship"],
        capture_output=True,
        text=True,
        cwd=TELEMETRY,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert sys.version_info >= (3, 12)  # le test lui-même tourne sous 3.12
