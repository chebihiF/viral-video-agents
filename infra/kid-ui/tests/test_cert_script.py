"""Passerelle pytest vers test_cert_script.sh (#160) : make check couvre le script de certificat."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SH = Path(__file__).resolve().parent / "test_cert_script.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash absent")
def test_make_cert_script() -> None:
    r = subprocess.run(["bash", str(SH)], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert r.returncode == 0, r.stdout + r.stderr
