"""Skill versionné /ticket : boucle ticket -> Livré (#15). Lecture des fichiers seulement."""

from __future__ import annotations

import re
from pathlib import Path

import handoff as ho
import pytest

AGENTS = Path(__file__).resolve().parents[1]
REPO = AGENTS.parents[1]
SKILL_DIR = AGENTS / "skills" / "ticket"
SKILL = SKILL_DIR / "SKILL.md"
CHECKLIST = SKILL_DIR / "checklist.md"
DOC = REPO / "docs" / "agents.md"
STEPS = [
    "stop-agents",
    "En cours",
    "make check",
    "Closes #",
    "infinitySoftMA",
    "Revue",
    "reviewer",
    "--squash",
    "Livré",
    "15 lignes",
    "feat/<n>-<slug>",
]
SCENARIOS = ["Boucle nominale", "Ticket bloqué", "Échecs répétés"]


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "frontmatter YAML absent"
    pairs = (line.split(":", 1) for line in match.group(1).splitlines() if line.strip())
    return {key.strip(): value.strip() for key, value in pairs}


def test_frontmatter_names_the_skill() -> None:
    meta = _frontmatter(SKILL.read_text(encoding="utf-8"))
    assert meta["name"] == "ticket"
    assert meta.get("description")


@pytest.mark.parametrize("step", STEPS)
def test_skill_covers_contract_step(step: str) -> None:
    assert step in SKILL.read_text(encoding="utf-8")


def test_skill_numbers_eight_steps_in_order() -> None:
    text = SKILL.read_text(encoding="utf-8")
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", text, re.MULTILINE)]
    assert numbers[:8] == list(range(1, 9))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_gherkin_scenario_is_traced(scenario: str) -> None:
    assert scenario in SKILL.read_text(encoding="utf-8")


def test_repeated_failures_stop_without_merge() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "2 échecs" in text
    assert "aucun merge" in text


def test_checklist_lists_definition_of_done() -> None:
    items = re.findall(r"^- \[ \] .+", CHECKLIST.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(items) >= 5


def test_agents_doc_references_skill() -> None:
    assert "skills/ticket/SKILL.md" in DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [SKILL, CHECKLIST])
def test_no_machine_path_nor_secret(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "/Users/" not in text
    assert "/home/" not in text
    assert not ho.SECRETS.search(text)
