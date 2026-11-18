"""pool.sh : un ticket = une branche sur un poste libre du pool (#14)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "pool.sh"
MAKEFILE = 'bootstrap:\n\ttouch "$$BOOT_MARK"\n'
FAKE_GH = """#!/usr/bin/env bash
# Faux gh : issue view -> $FAKE_GH/issue.json ; pr list --head B -> $FAKE_GH/pr-<B>.json
set -euo pipefail
if [ "$1 $2" = "issue view" ]; then cat "$FAKE_GH/issue.json"; exit 0; fi
[ ! -e "$FAKE_GH/pr-fail" ] || { echo "gh: erreur réseau" >&2; exit 1; }
while [ $# -gt 0 ]; do [ "$1" = "--head" ] && head="$2"; shift; done
f="$FAKE_GH/pr-${head//\\//_}.json"
if [ -f "$f" ]; then cat "$f"; else echo '[]'; fi
"""


@dataclass
class Pool:
    root: Path
    main: Path
    postes: list[Path]
    env: dict[str, str]

    def git(self, cwd: Path, *args: str) -> str:
        res = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()

    def ticket(self, title: str, labels: list[str], status: str = "Prêt") -> None:
        data = {
            "title": title,
            "labels": [{"name": n} for n in labels],
            "projectItems": [{"status": {"name": status}, "title": "Scrum"}],
        }
        (self.root / "gh" / "issue.json").write_text(json.dumps(data))

    def pr(self, poste: Path, state: str) -> None:
        branch = self.git(poste, "branch", "--show-current")
        data = [{"state": state, "headRefOid": self.git(poste, "rev-parse", "HEAD")}]
        (self.root / "gh" / f"pr-{branch.replace('/', '_')}.json").write_text(json.dumps(data))

    def commit(self, poste: Path, name: str, push: bool) -> None:
        (poste / name).write_text(name)
        self.git(poste, "add", name)
        self.git(poste, "commit", "-q", "-m", name)
        if push:
            self.git(poste, "push", "-q", "origin", "HEAD")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["bash", str(SCRIPT), *args],  # noqa: S607
            cwd=self.main,
            env=self.env,
            capture_output=True,
            text=True,
        )


@pytest.fixture
def pool(tmp_path: Path) -> Pool:
    (tmp_path / "gh").mkdir()
    gh = tmp_path / "gh" / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    env = {
        **os.environ,
        "GH": str(gh),
        "FAKE_GH": str(tmp_path / "gh"),
        "BOOT_MARK": str(tmp_path / "boot"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    env.pop("MAKEFLAGS", None)
    main = tmp_path / "main"
    p = Pool(tmp_path, main, [], env)
    p.git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    p.git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), "main")
    (main / "Makefile").write_text(MAKEFILE)
    p.git(main, "add", "Makefile")
    p.git(main, "commit", "-q", "-m", "init")
    p.git(main, "push", "-q", "origin", "HEAD:main")
    for i in (1, 2, 3):
        wt = main / ".claude" / "worktrees" / f"p{i}"
        p.git(main, "worktree", "add", "-q", "--no-track", "-b", f"feat/{i}-x", str(wt), "main")
        p.postes.append(wt)
    p.ticket("TS: Session neuve par ticket", ["type:TS"])
    return p


def occuper(pool: Pool, poste: Path) -> None:
    pool.commit(poste, "wip", push=True)
    pool.pr(poste, "OPEN")


def test_poste_libre_bascule_sur_branche_neuve(pool: Pool) -> None:
    libre = pool.postes[1]
    occuper(pool, pool.postes[0])
    pool.commit(libre, "fait", push=True)
    pool.pr(libre, "MERGED")
    occuper(pool, pool.postes[2])

    res = pool.run("42")

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().splitlines()[-1] == str(libre)
    assert pool.git(libre, "branch", "--show-current") == "feat/42-session-neuve-par-ticket"
    assert pool.git(libre, "rev-parse", "HEAD") == pool.git(pool.main, "rev-parse", "origin/main")
    with pytest.raises(subprocess.CalledProcessError):
        pool.git(libre, "rev-parse", "--abbrev-ref", "@{u}")
    assert (pool.root / "boot").exists()


def test_type_bug_donne_une_branche_fix(pool: Pool) -> None:
    pool.ticket("Corrige l'écran Activité", ["type:bug"])
    pool.pr(pool.postes[0], "CLOSED")

    res = pool.run("7")

    assert res.returncode == 0, res.stderr
    assert pool.git(pool.postes[0], "branch", "--show-current") == "fix/7-corrige-l-ecran-activite"


@pytest.mark.parametrize(
    ("labels", "status"), [(["stop-agents"], "Prêt"), (["type:TS"], "En cours")]
)
def test_ticket_interdit_aucune_branche(pool: Pool, labels: list[str], status: str) -> None:
    pool.ticket("Interdit", labels, status)

    res = pool.run("42")

    assert res.returncode != 0
    assert len(res.stderr.strip().splitlines()) == 1
    assert pool.git(pool.main, "branch", "--list", "*42*") == ""
    assert not (pool.root / "boot").exists()


def test_aucun_poste_libre_liste_les_occupes(pool: Pool) -> None:
    for poste in pool.postes:
        occuper(pool, poste)

    res = pool.run("42")

    assert res.returncode != 0
    for poste in pool.postes:
        assert str(poste) in res.stderr
    assert pool.git(pool.main, "branch", "--list", "*42*") == ""


def test_travail_non_pousse_pas_libre(pool: Pool) -> None:
    for poste in pool.postes:
        pool.commit(poste, "fait", push=True)
        pool.pr(poste, "MERGED")
    pool.commit(pool.postes[0], "local", push=False)
    (pool.postes[1] / "brouillon").write_text("x")
    pool.git(pool.postes[2], "switch", "-q", "--no-track", "-c", "feat/9-sans-upstream")
    pool.commit(pool.postes[2], "local", push=False)

    res = pool.run("42")

    assert res.returncode != 0
    for poste in pool.postes:
        assert str(poste) in res.stderr
    assert (pool.postes[1] / "brouillon").exists()


def test_poste_sans_pr_deja_dans_main_est_libre(pool: Pool) -> None:
    for poste in pool.postes[:2]:
        occuper(pool, poste)
    (pool.postes[2] / ".claude").mkdir()
    (pool.postes[2] / ".claude" / "local").write_text("x")
    (pool.postes[2] / "CLAUDE.md").write_text("x")

    res = pool.run("42")

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().splitlines()[-1] == str(pool.postes[2])


def test_dry_run_ne_change_rien(pool: Pool) -> None:
    res = pool.run("--dry-run", "42")

    assert res.returncode == 0, res.stderr
    assert str(pool.postes[0]) in res.stdout
    assert pool.git(pool.postes[0], "branch", "--show-current") == "feat/1-x"
    assert not (pool.root / "boot").exists()


def test_etat_pr_inconnu_rend_le_poste_occupe(pool: Pool) -> None:
    (pool.root / "gh" / "pr-fail").write_text("")

    res = pool.run("42")

    assert res.returncode != 0
    assert res.stderr.count("état PR inconnu") == 3
    assert pool.git(pool.main, "branch", "--list", "*42*") == ""


def test_plus_de_trois_postes_refus(pool: Pool) -> None:
    wt = pool.main / ".claude" / "worktrees" / "p4"
    pool.git(pool.main, "worktree", "add", "-q", "--no-track", "-b", "feat/4-x", str(wt), "main")

    res = pool.run("42")

    assert res.returncode != 0
    assert len(res.stderr.strip().splitlines()) == 1
    assert pool.git(pool.main, "branch", "--list", "*42*") == ""


def test_verrou_refuse_une_execution_concurrente(pool: Pool) -> None:
    verrou = pool.main / ".git" / "vva-pool.lock"
    verrou.mkdir()

    res = pool.run("42")

    assert res.returncode != 0
    assert len(res.stderr.strip().splitlines()) == 1
    assert pool.git(pool.main, "branch", "--list", "*42*") == ""
    verrou.rmdir()
    assert pool.run("42").returncode == 0
    assert not verrou.exists()


def test_slug_vide_devient_ticket(pool: Pool) -> None:
    pool.ticket("TS: !!!", [])

    res = pool.run("5")

    assert res.returncode == 0, res.stderr
    assert pool.git(pool.postes[0], "branch", "--show-current") == "feat/5-ticket"


@pytest.mark.parametrize("n", ["", "1;touch pwned", "abc"])
def test_make_ticket_refuse_n_non_numerique(tmp_path: Path, n: str) -> None:
    racine = SCRIPT.parents[2]
    res = subprocess.run(  # noqa: S603
        ["make", "-C", str(racine), "ticket", f"N={n}"],  # noqa: S607
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert res.returncode != 0
    assert "usage" in res.stderr
    assert not (racine / "pwned").exists()
