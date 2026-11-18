"""bootstrap_worktree.sh copie .claude/ sans worktrees/ ni telemetry/ et sans écraser."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "bootstrap_worktree.sh"


def test_bootstrap_exclut_worktrees_et_telemetry(tmp_path: Path) -> None:
    src, dst = tmp_path / "main", tmp_path / "wt"
    for d in (src, dst):
        subprocess.run(["git", "init", "-q", str(d)], check=True)  # noqa: S603, S607
    (src / ".claude" / "agents").mkdir(parents=True)
    (src / ".claude" / "agents" / "qa.md").write_text("qa\n")
    (src / ".claude" / "worktrees" / "autre").mkdir(parents=True)
    (src / ".claude" / "worktrees" / "autre" / "x").write_text("x\n")
    (src / ".claude" / "telemetry").mkdir()
    (src / ".claude" / "telemetry" / "t.json").write_text("{}\n")
    (src / "CLAUDE.md").write_text("regles\n")
    (dst / "CLAUDE.md").write_text("local\n")

    subprocess.run(["bash", str(SCRIPT), str(src)], cwd=dst, check=True)  # noqa: S603, S607

    assert (dst / ".claude" / "agents" / "qa.md").read_text() == "qa\n"
    assert not (dst / ".claude" / "worktrees").exists()
    assert not (dst / ".claude" / "telemetry").exists()
    assert (dst / "CLAUDE.md").read_text() == "local\n"
