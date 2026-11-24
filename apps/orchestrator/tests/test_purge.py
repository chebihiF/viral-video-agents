"""Purge des runs anciens (#152) : dry-run, suppression, sécurité, usage. Dossiers synthétiques."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from orchestrator.__main__ import main

JOUR = 86400
DICTEE = "Un hibou violet secret joue du tambour"


def _run(racine: Path, nom: str, age_jours: float, run_id: str | None = None) -> Path:
    dossier = racine / nom
    (dossier / "voix").mkdir(parents=True)
    run_json = dossier / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": run_id or nom,
                "etat": "ok",
                "etapes": [{"nom": "montage", "statut": "ok", "duree_s": 0.1}],
                "video_path": "final.mp4",
            }
        ),
        encoding="utf-8",
    )
    for fichier in ("final.mp4", "final.ass", "voix/p1.wav"):
        (dossier / fichier).write_bytes(b"x")
    instant = time.time() - age_jours * JOUR
    os.utime(run_json, (instant, instant))
    return dossier


def _purge(racine: Path, jours: str, *extra: str) -> int:
    return main(["purge", "--runs", str(racine), "--older-than-days", jours, *extra])


def test_dry_run_par_defaut(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    assert _purge(tmp_path, "7") == 0
    out = capsys.readouterr().out
    assert "vieux" in out
    assert ancien.is_dir()
    assert (ancien / "final.mp4").exists()


def test_dry_run_explicite_sans_effet(tmp_path: Path) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    assert _purge(tmp_path, "7", "--dry-run") == 0
    assert ancien.is_dir()


def test_apply_supprime_ancien_garde_recent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    recent = _run(tmp_path, "neuf", 2)
    assert _purge(tmp_path, "7", "--apply") == 0
    out = capsys.readouterr().out
    assert "vieux" in out
    assert "neuf" not in out
    assert not ancien.exists()
    assert (recent / "run.json").is_file()
    assert (recent / "final.mp4").is_file()


def test_apply_et_dry_run_incompatibles(tmp_path: Path) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    assert _purge(tmp_path, "7", "--apply", "--dry-run") == 2
    assert ancien.is_dir()


def test_dossier_sans_run_json_ignore(tmp_path: Path) -> None:
    sans = tmp_path / "pas-un-run"
    sans.mkdir()
    (sans / "final.mp4").write_bytes(b"x")
    os.utime(sans, (0, 0))
    assert _purge(tmp_path, "7", "--apply") == 0
    assert (sans / "final.mp4").exists()


def test_run_json_invalide_ignore(tmp_path: Path) -> None:
    for nom, contenu in (("json-casse", "{"), ("hors-contrat", '{"run_id": "x"}'), ("liste", "[]")):
        dossier = tmp_path / nom
        dossier.mkdir()
        (dossier / "run.json").write_text(contenu, encoding="utf-8")
        os.utime(dossier / "run.json", (0, 0))
    assert _purge(tmp_path, "7", "--apply") == 0
    for nom in ("json-casse", "hors-contrat", "liste"):
        assert (tmp_path / nom / "run.json").exists()


def test_lien_symbolique_ignore(tmp_path: Path) -> None:
    dehors = tmp_path / "dehors"
    _run(dehors, "cible", 30)
    racine = tmp_path / "runs"
    racine.mkdir()
    (racine / "lien").symlink_to(dehors / "cible", target_is_directory=True)
    assert _purge(racine, "7", "--apply") == 0
    assert (dehors / "cible" / "run.json").is_file()
    assert (dehors / "cible" / "final.mp4").is_file()
    assert (racine / "lien").is_symlink()


def test_run_json_lien_symbolique_ignore(tmp_path: Path) -> None:
    dehors = _run(tmp_path / "dehors", "source", 30)
    racine = tmp_path / "runs"
    faux = racine / "faux"
    faux.mkdir(parents=True)
    (faux / "run.json").symlink_to(dehors / "run.json")
    assert _purge(racine, "7", "--apply") == 0
    assert faux.is_dir()
    assert (dehors / "run.json").is_file()


def test_lien_interne_au_run_non_suivi(tmp_path: Path) -> None:
    dehors = tmp_path / "dehors"
    dehors.mkdir()
    (dehors / "garde.txt").write_text("x", encoding="utf-8")
    racine = tmp_path / "runs"
    ancien = _run(racine, "vieux", 10)
    (ancien / "sortie").symlink_to(dehors, target_is_directory=True)
    assert _purge(racine, "7", "--apply") == 0
    assert not ancien.exists()
    assert (dehors / "garde.txt").read_text("utf-8") == "x"


@pytest.mark.parametrize("jours", ["0", "-3", "abc", "1.5"])
def test_older_than_days_invalide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], jours: str
) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    try:
        code = _purge(tmp_path, jours, "--apply")
    except SystemExit as sortie:  # argparse : valeur non entière
        code = int(str(sortie.code))
    assert code == 2
    assert capsys.readouterr().err.strip()
    assert ancien.is_dir()


def test_dossier_runs_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _purge(tmp_path / "absent", "7") == 2
    assert "absent" in capsys.readouterr().err


def test_aucune_dictee_imprimee(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    (ancien / "dictee.txt").write_text(DICTEE, encoding="utf-8")
    assert _purge(tmp_path, "7", "--apply") == 0
    sortie = capsys.readouterr()
    assert DICTEE not in sortie.out + sortie.err
    assert sortie.out.strip() == "vieux age_jours=10 supprime"


def test_run_id_affiche_est_celui_du_contrat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "dossier", 10, run_id="abc123")
    assert _purge(tmp_path, "7") == 0
    assert capsys.readouterr().out.strip() == "abc123 age_jours=10 a_supprimer"


def test_dossier_runs_lien_symbolique(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reel = tmp_path / "reel"
    ancien = _run(reel, "vieux", 10)
    lien = tmp_path / "lien"
    lien.symlink_to(reel, target_is_directory=True)
    assert _purge(lien, "7", "--apply") == 2
    assert capsys.readouterr().err.strip()
    assert ancien.is_dir()


def test_dossier_runs_illisible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ancien = _run(tmp_path, "vieux", 10)

    def _refus(self: Path) -> Iterator[Path]:
        raise PermissionError("secret-chemin")

    monkeypatch.setattr(Path, "iterdir", _refus)
    assert _purge(tmp_path, "7", "--apply") == 2
    err = capsys.readouterr().err
    assert err.strip()
    assert "secret-chemin" not in err
    assert ancien.is_dir()


def test_apply_refuse_sans_rmtree_resistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False, raising=False)
    assert _purge(tmp_path, "7", "--apply") == 2
    assert ancien.is_dir()
    assert _purge(tmp_path, "7") == 0  # le dry-run reste possible


def test_echec_suppression_code_1_sans_fuite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "vieux", 10)
    recent = _run(tmp_path, "recent", 1)

    def _echec(*_a: object, **_k: object) -> None:
        raise PermissionError("secret-chemin")

    _echec.avoids_symlink_attacks = True  # type: ignore[attr-defined]
    monkeypatch.setattr("orchestrator.purge.shutil.rmtree", _echec)
    assert _purge(tmp_path, "7", "--apply") == 1
    captured = capsys.readouterr()
    assert "vieux" in captured.out
    assert "secret-chemin" not in captured.out + captured.err
    assert recent.is_dir()


def test_un_seul_lstat_par_run_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(tmp_path, "vieux", 10)
    appels: list[str] = []
    reel = os.lstat

    def _compte(chemin: object, *a: object, **k: object) -> os.stat_result:
        if str(chemin).endswith("run.json"):
            appels.append(str(chemin))
        return reel(chemin, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr("orchestrator.purge.os.lstat", _compte)
    assert _purge(tmp_path, "7") == 0
    assert len(appels) == 1


def test_older_than_days_non_numerique_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ancien = _run(tmp_path, "vieux", 10)
    with pytest.raises(SystemExit) as sortie:
        _purge(tmp_path, "abc", "--apply")
    assert sortie.value.code == 2
    assert capsys.readouterr().err.strip()
    assert ancien.is_dir()
