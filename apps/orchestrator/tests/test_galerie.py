"""Galerie locale (#151) : liste, sans texte d'enfant, état vide, hors ligne. Runs synthétiques."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from orchestrator.__main__ import main
from orchestrator.galerie import formater_duree

SECRET = "Un hibou violet secret joue du tambour"  # noqa: S105 (dictée factice)


def _run(
    racine: Path,
    nom: str,
    etat: str = "ok",
    *,
    termine_le: str | None = "2026-11-20T10:00:00Z",
    run_id: str | None = None,
    durees: tuple[float, ...] = (40.5, 61.9),
    **extra: Any,
) -> Path:
    dossier = racine / nom
    dossier.mkdir(parents=True)
    run: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id or nom,
        "etat": etat,
        "etapes": [{"nom": "montage", "statut": "ok", "duree_s": d} for d in durees],
    }
    if etat == "ok":
        run["video_path"] = "final.mp4"
    if termine_le:
        run["termine_le"] = termine_le
    run.update(extra)
    (dossier / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return dossier


def _galerie(racine: Path, out: Path | None = None) -> str:
    cible = out or racine.parent / "galerie.html"
    assert main(["galerie", "--runs", str(racine), "--out", str(cible)]) == 0
    return cible.read_text(encoding="utf-8")


def test_liste_trois_entrees_lecteur_pour_les_termines(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa", "ok")
    _run(runs, "bbb", "ok")
    _run(runs, "ccc", "erreur")
    page = _galerie(runs)
    assert page.count('<article class="run"') == 3
    assert page.count("<video ") == 2
    assert 'src="runs/aaa/final.mp4"' in page
    assert "runs/ccc" not in page
    assert "Terminé" in page
    assert "Échec" in page


def test_bloque_sans_lecteur(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "bbb", "bloque")
    page = _galerie(runs)
    assert "Bloqué" in page
    assert "<video" not in page


def test_lecteur_sobre(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa")
    page = _galerie(runs)
    assert 'controls preload="none"' in page
    assert "autoplay" not in page
    assert "<script" not in page


def test_champ_texte_inattendu_absent(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa", dictee=SECRET, texte_adouci=SECRET, message=SECRET)
    page = _galerie(runs)
    assert "hibou" not in page
    assert "secret" not in page


def test_etat_vide(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    page = _galerie(runs)
    assert '<p class="empty" role="status">Aucune vidéo pour l\'instant.</p>' in page
    assert "<h1>Galerie des vidéos</h1>" in page
    assert "0 run" in page
    assert "<ul" not in page


def test_hors_ligne(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa")
    _run(runs, "bbb", "erreur")
    page = _galerie(runs)
    assert not re.search(r"https?://", page)
    assert not re.search(r"<(script|link|img|iframe)\b", page)


def test_duree_somme_des_etapes(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa", durees=(40.5, 61.9))
    page = _galerie(runs)
    assert "1 min 42 s" in page
    assert 'datetime="PT1M42S"' in page


@pytest.mark.parametrize(
    ("secondes", "texte", "iso"),
    [
        (0, "0 s", "PT0S"),
        (42, "42 s", "PT42S"),
        (60, "1 min 0 s", "PT1M0S"),
        (3725, "62 min 5 s", "PT62M5S"),
    ],
)
def test_formater_duree(secondes: int, texte: str, iso: str) -> None:
    assert formater_duree(secondes) == (texte, iso)


def test_duree_inconnue_tiret(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa", "erreur", durees=())
    page = _galerie(runs)
    assert "<time" not in page
    assert "—" in page


def test_tri_plus_recent_d_abord(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "ancien", termine_le="2026-11-01T10:00:00Z")
    _run(runs, "recent", termine_le="2026-11-24T10:00:00Z")
    _run(runs, "sans-date", termine_le=None)
    page = _galerie(runs)
    assert page.index("recent") < page.index("ancien") < page.index("sans-date")


def test_echappement_html(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    dossier = _run(runs, "aaa")
    run = json.loads((dossier / "run.json").read_text(encoding="utf-8"))
    run["run_id"] = "<b>x</b>"
    (dossier / "run.json").write_text(json.dumps(run), encoding="utf-8")
    # run_id hors motif du contrat : entrée ignorée, jamais injectée
    assert "<b>x</b>" not in _galerie(runs)


def test_nom_de_dossier_echappe_et_encode(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "a b", "ok", run_id="aaa")
    page = _galerie(runs)
    assert 'src="runs/a%20b/final.mp4"' in page


@pytest.mark.parametrize(
    "video",
    [
        "../../etc/x.mp4",
        "/etc/x.mp4",
        "http://exemple.test/x.mp4",
        "a\\b.mp4",
        "ok.txt",
        "a/../../b.mp4",
    ],
)
def test_video_path_hostile_sans_lecteur(tmp_path: Path, video: str) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa", video_path=video)
    page = _galerie(runs)
    assert "<video" not in page
    assert "exemple.test" not in page
    assert "etc/x" not in page


def test_ignore_entrees_invalides(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa")
    (runs / "vide").mkdir()
    (runs / "casse").mkdir()
    (runs / "casse" / "run.json").write_text("{pas du json", encoding="utf-8")
    (runs / "fichier.txt").write_text("x", encoding="utf-8")
    page = _galerie(runs)
    assert page.count('<article class="run"') == 1
    assert "1 run" in page


def test_chemin_relatif_depuis_la_page(tmp_path: Path) -> None:
    runs = tmp_path / "donnees" / "runs"
    _run(runs, "aaa")
    out = tmp_path / "sortie" / "galerie.html"
    out.parent.mkdir()
    page = _galerie(runs, out)
    assert 'src="../donnees/runs/aaa/final.mp4"' in page


def test_dossier_absent_code_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["galerie", "--runs", str(tmp_path / "nope"), "--out", str(tmp_path / "g.html")])
    assert code == 2
    assert "invalide" in capsys.readouterr().err


def test_arguments_manquants_code_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["galerie"])
    assert exc.value.code == 2


def test_sortie_stdout_sans_texte(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = tmp_path / "runs"
    _run(runs, "aaa", dictee=SECRET)
    _galerie(runs)
    assert "hibou" not in capsys.readouterr().out
