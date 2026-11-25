"""Durée restante estimée (#165) : médianes mesurées, valeur par défaut, aucune dictée."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import eta
import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "contracts" / "kid-job.schema.json").read_text(encoding="utf-8"))
PROGRESS = Draft202012Validator({**SCHEMA, "$ref": "#/$defs/Progress"})
STEPS = ("storyboard", "voix", "images", "montage")
DEFAUT_TOTAL = sum(eta.DEFAUT_S.values())


def ligne(
    n: int = 0,
    *,
    mode: str = "reel",
    etat: str = "ok",
    images: float = 100,
    storyboard: float = 10,
    voix: float = 20,
    montage: float = 40,
) -> dict[str, Any]:
    """Ligne de durees.jsonl ; « images » = image + clip (moitié chacun)."""
    etapes = [
        ("classification", 1),
        ("storyboard", storyboard),
        ("voix", voix),
        ("image", images / 2),
        ("clip", images / 2),
        ("montage", montage),
    ]
    return {
        "run_id": f"r{n}",
        "mode": mode,
        "etat": etat,
        "termine_le": f"2026-11-{1 + n:02d}T10:00:00Z",
        "etapes": [{"nom": k, "statut": "ok", "duree_s": float(d)} for k, d in etapes],
    }


# --- Scénario : estimation à partir de la médiane ---
def test_mediane_et_pas_moyenne() -> None:
    lignes = [ligne(i, images=d) for i, d in enumerate((100, 120, 130, 140, 400))]
    assert eta.medianes(lignes)["images"] == 130  # la moyenne serait 178
    # l'étape « images » vient de démarrer : 130 s + le montage encore à faire
    r = eta.estimer(lignes, "images", 0)
    assert r["restant_s"] == 130 + 40


def test_somme_des_etapes_restantes_et_reste_de_l_etape_courante() -> None:
    lignes = [ligne(i) for i in range(3)]  # storyboard 11 (classification + storyboard)
    assert eta.medianes(lignes) == {"storyboard": 11, "voix": 20, "images": 100, "montage": 40}
    assert eta.estimer(lignes, "storyboard", 0)["restant_s"] == 171
    assert eta.estimer(lignes, "voix", 5)["restant_s"] == 15 + 100 + 40
    assert eta.estimer(lignes, "images", 30)["restant_s"] == 70 + 40


def test_ne_garde_que_les_runs_reels_termines() -> None:
    lignes = [ligne(i, images=100) for i in range(3)]
    lignes += [ligne(5, mode="mocks", images=1), ligne(6, etat="erreur", images=9999)]
    lignes += [ligne(7, etat="bloque", images=9999)]
    assert eta.medianes(lignes)["images"] == 100


def test_seuls_les_dix_derniers_runs_comptent() -> None:
    anciens = [ligne(i, images=1000) for i in range(5)]
    recents = [ligne(10 + i, images=100) for i in range(10)]
    assert eta.medianes(anciens + recents)["images"] == 100
    assert eta.medianes(recents + anciens)["images"] == 100  # l'ordre du fichier ne compte pas


# --- Scénario : valeur par défaut sans mesure ---
@pytest.mark.parametrize(
    "lignes",
    [[], [ligne(mode="mocks")], [ligne(etat="erreur")], [{}], ["x", 3, None, [1]]],
    ids=["vide", "mocks", "erreur", "objet-vide", "n-importe-quoi"],
)
def test_sans_mesure_valeur_par_defaut(lignes: list[Any]) -> None:
    assert eta.medianes(lignes) == eta.DEFAUT_S
    r = eta.estimer(lignes, "storyboard", 0)
    assert r == {"pct": 0, "restant_s": DEFAUT_TOTAL, "bientot": False}
    assert PROGRESS.is_valid(r)


def test_etape_sans_mesure_garde_son_defaut() -> None:
    sans_montage = ligne()
    sans_montage["etapes"] = [e for e in sans_montage["etapes"] if e["nom"] != "montage"]
    assert eta.medianes([sans_montage]) == eta.DEFAUT_S  # run incomplet : ignoré


@pytest.mark.parametrize("duree", [True, "12", None, -3, math.nan, math.inf, 1e9])
def test_durees_invalides_ignorees(duree: Any) -> None:
    mauvais = ligne()
    mauvais["etapes"][3]["duree_s"] = duree
    assert eta.medianes([mauvais]) == eta.DEFAUT_S


def test_lignes_mal_formees_ne_cassent_rien() -> None:
    bancal = [{"mode": "reel", "etat": "ok", "etapes": "x"}, {"etapes": [1, None]}]
    bancal += [{"mode": "reel", "etat": "ok", "termine_le": 5, "etapes": [{"nom": []}]}]
    assert eta.estimer(bancal + [ligne()], "images", 0)["restant_s"] is not None


# --- Scénario : la barre avance, ne dépasse pas 99 avant la fin ---
def test_pct_croit_avec_le_temps_et_les_etapes() -> None:
    lignes = [ligne(i) for i in range(3)]
    suite = [
        eta.estimer(lignes, e, t)["pct"]
        for e, t in [("storyboard", 0), ("storyboard", 5), ("voix", 0), ("voix", 10),
                     ("images", 0), ("images", 50), ("montage", 0), ("montage", 39)]
    ]  # fmt: skip
    assert suite == sorted(suite)
    assert suite[0] == 0
    assert 0 < suite[-1] <= 99


def test_pct_ne_depasse_jamais_99_meme_en_depassement() -> None:
    r = eta.estimer([ligne()], "montage", 10_000)
    assert r["pct"] == 99
    assert r["restant_s"] == 0


# --- Scénario : bientôt prête ---
def test_bientot_sous_30_secondes() -> None:
    lignes = [ligne(i, montage=40) for i in range(3)]
    assert eta.estimer(lignes, "montage", 0)["bientot"] is False  # 40 s restantes
    r = eta.estimer(lignes, "montage", 11)  # 29 s
    assert r["bientot"] is True
    assert r["restant_s"] == 29
    assert eta.estimer(lignes, "montage", 10)["bientot"] is False  # 30 s : pas encore


def test_depassement_jamais_de_temps_negatif() -> None:
    lignes = [ligne(i) for i in range(3)]
    r = eta.estimer(lignes, "montage", 500)
    assert r["restant_s"] == 0
    assert r["bientot"] is True
    r = eta.estimer(lignes, "images", 500)  # dépasse l'étape, le montage reste à faire
    assert r["restant_s"] == 40
    assert r["restant_s"] >= 0


@pytest.mark.parametrize("ecoule", [-5, math.nan, math.inf, -math.inf])
def test_temps_ecoule_invalide_vaut_zero_ou_borne(ecoule: float) -> None:
    r = eta.estimer([ligne()], "images", ecoule)
    assert PROGRESS.is_valid(r)
    assert r["restant_s"] is not None
    assert r["restant_s"] >= 0


def test_etape_inconnue_pas_de_faux_chiffre() -> None:
    r = eta.estimer([ligne()], "classification", 0)
    assert r == {"pct": 0, "restant_s": None, "bientot": False}
    assert PROGRESS.is_valid(r)


# --- Scénario : aucun texte de dictée ---
def test_sortie_sans_autre_champ_que_le_contrat() -> None:
    r = eta.estimer([ligne()], "voix", 3)
    assert set(r) == {"pct", "restant_s", "bientot"}
    assert all(type(v) in (int, bool) for v in r.values())
    assert PROGRESS.is_valid(r)


# --- Lecture du journal ---
def ecrire(runs: Path, nom: str, contenu: str) -> Path:
    d = runs / nom
    d.mkdir(parents=True)
    f = d / "durees.jsonl"
    f.write_text(contenu, encoding="utf-8")
    return f


def test_lire_durees_agrege_les_runs(tmp_path: Path) -> None:
    ecrire(tmp_path, "a", json.dumps(ligne(1)) + "\n" + json.dumps(ligne(2)) + "\n")
    ecrire(tmp_path, "b", json.dumps(ligne(3)) + "\n")
    assert sorted(x["run_id"] for x in eta.lire_durees(tmp_path)) == ["r1", "r2", "r3"]


def test_lire_durees_dossier_absent_ou_vide(tmp_path: Path) -> None:
    assert eta.lire_durees(tmp_path / "absent") == []
    assert eta.lire_durees(tmp_path) == []
    ecrire(tmp_path, "vide", "")
    assert eta.lire_durees(tmp_path) == []


def test_lire_durees_saute_le_bruit(tmp_path: Path) -> None:
    ecrire(tmp_path, "a", "pas du json\n\n[1]\n" + json.dumps(ligne(1)) + "\n\xff{")
    assert [x["run_id"] for x in eta.lire_durees(tmp_path)] == ["r1"]
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "durees.jsonl").write_bytes(b"\xff\xfe\x00{")
    assert [x["run_id"] for x in eta.lire_durees(tmp_path)] == ["r1"]


def test_lire_durees_ignore_liens_et_fichiers_enormes(tmp_path: Path) -> None:
    secret = tmp_path / "secret.jsonl"
    secret.write_text(json.dumps(ligne(9)) + "\n", encoding="utf-8")
    (tmp_path / "lien").mkdir()
    (tmp_path / "lien" / "durees.jsonl").symlink_to(secret)
    ecrire(tmp_path, "gros", json.dumps(ligne(8)) + "\n" + " " * (eta.MAX_OCTETS + 1))
    assert eta.lire_durees(tmp_path) == []


def test_lire_durees_en_lecture_seule(tmp_path: Path) -> None:
    f = ecrire(tmp_path, "a", json.dumps(ligne(1)) + "\n")
    avant = f.read_bytes()
    eta.lire_durees(tmp_path)
    assert f.read_bytes() == avant
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a"]
