"""CLI orchestrateur (#138) : 4 scénarios Gherkin, mode mocks, run.json validé par le contrat."""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orchestrator.__main__ import main
from orchestrator.mocks import MOT_BLOQUE, etapes_factices

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
RUN = Draft202012Validator(json.loads((CONTRACTS / "pipeline-run.schema.json").read_text("utf-8")))
DICTEE = "Un hibou violet secret joue du tambour sur la lune."


def _lancer(tmp_path: Path, dictee: str, *extra: str) -> tuple[int, Path]:
    fichier = tmp_path / "dictee.txt"
    fichier.write_text(dictee, encoding="utf-8")
    out = tmp_path / "out"
    code = main(["run", "--mocks", "--dictee-file", str(fichier), "--out", str(out), *extra])
    return code, out / "run.json"


def test_parcours_simule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, run_json = _lancer(tmp_path, DICTEE)
    run: dict[str, Any] = json.loads(run_json.read_text("utf-8"))
    RUN.validate(run)
    assert code == 0
    assert run["etat"] == "ok"
    assert [e["nom"] for e in run["etapes"]][-1] == "montage"


def test_contenu_bloque(tmp_path: Path) -> None:
    code, run_json = _lancer(tmp_path, f"{DICTEE} {MOT_BLOQUE}")
    run = json.loads(run_json.read_text("utf-8"))
    RUN.validate(run)
    assert code == 2
    assert run["etat"] == "bloque"
    assert [e["nom"] for e in run["etapes"]] == ["classification"]
    assert "video_path" not in run


def test_storyboard_bloque_code_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = etapes_factices()
    refus = dataclasses.replace(
        base, storyboard=lambda texte: {**base.storyboard(texte), "classification": "bloque"}
    )
    monkeypatch.setattr("orchestrator.__main__.etapes_factices", lambda: refus)
    code, run_json = _lancer(tmp_path, DICTEE)
    run = json.loads(run_json.read_text("utf-8"))
    RUN.validate(run)
    assert code == 2
    assert run["etat"] == "bloque"
    assert [e["nom"] for e in run["etapes"]] == ["classification", "storyboard"]
    assert "video_path" not in run


@pytest.mark.parametrize("dictee", ["", "  \n\t ", "a" * 2001])
def test_dictee_invalide(tmp_path: Path, dictee: str) -> None:
    code, run_json = _lancer(tmp_path, dictee)
    assert code == 64
    assert not run_json.exists()
    assert not run_json.parent.exists()


def test_dictee_2000_caracteres_acceptee(tmp_path: Path) -> None:
    code, _ = _lancer(tmp_path, "a" * 2000)
    assert code == 0


def test_dictee_non_utf8_refusee(tmp_path: Path) -> None:
    fichier = tmp_path / "d.txt"
    fichier.write_bytes(b"\xff\xfe\xfa")
    code = main(["run", "--mocks", "--dictee-file", str(fichier), "--out", str(tmp_path / "o")])
    assert code == 64
    assert not (tmp_path / "o").exists()


def test_pas_de_fuite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    code, run_json = _lancer(tmp_path, DICTEE)
    sortie = capsys.readouterr()
    assert code == 0
    for texte in (run_json.read_text("utf-8"), sortie.out, sortie.err, caplog.text):
        assert "hibou" not in texte
        assert "secret" not in texte
    assert not any("hibou" in p.read_text("utf-8") for p in tmp_path.rglob("*.json"))


def test_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(DICTEE))
    out = tmp_path / "out"
    assert main(["run", "--mocks", "--dictee-file", "-", "--out", str(out)]) == 0
    RUN.validate(json.loads((out / "run.json").read_text("utf-8")))


def test_sans_mocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for variable in ("OLLAMA_URL", "PIPER_BIN", "PIPER_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    fichier = tmp_path / "d.txt"
    fichier.write_text(DICTEE, encoding="utf-8")
    code = main(["run", "--dictee-file", str(fichier), "--out", str(tmp_path / "o")])
    assert code == 78
    assert "configuration absente" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_arguments_invalides_code_64() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["run", "--mocks"])
    assert exc.value.code == 64


def test_python_m_orchestrator(tmp_path: Path) -> None:
    fichier = tmp_path / "d.txt"
    fichier.write_text(DICTEE, encoding="utf-8")
    cmd = [sys.executable, "-m", "orchestrator", "run", "--mocks"]
    cmd += ["--dictee-file", str(fichier), "--out", str(tmp_path / "o")]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    assert res.returncode == 0
    assert "hibou" not in res.stdout + res.stderr
    RUN.validate(json.loads((tmp_path / "o" / "run.json").read_text("utf-8")))


# --- #164 : mode dans run.json et journal des durées --------------------------------------------


def _lignes(out: Path) -> list[dict[str, Any]]:
    texte = (out / "durees.jsonl").read_text("utf-8")
    return [json.loads(ligne) for ligne in texte.splitlines()]


def test_run_mocks_ecrit_mode_et_une_ligne_de_durees(tmp_path: Path) -> None:
    code, run_json = _lancer(tmp_path, DICTEE)
    run = json.loads(run_json.read_text("utf-8"))
    assert code == 0
    assert run["mode"] == "mocks"
    [ligne] = _lignes(run_json.parent)
    assert ligne["run_id"] == run["run_id"]
    assert ligne["mode"] == "mocks"
    assert ligne["termine_le"] == run["termine_le"]
    assert [e["nom"] for e in ligne["etapes"]][-1] == "montage"
    assert all(e["duree_s"] >= 0 for e in ligne["etapes"])


def test_les_runs_s_ajoutent_au_journal(tmp_path: Path) -> None:
    _lancer(tmp_path, DICTEE)
    _, run_json = _lancer(tmp_path, f"{DICTEE} {MOT_BLOQUE}")
    assert [ligne["etat"] for ligne in _lignes(run_json.parent)] == ["ok", "bloque"]


def test_run_en_erreur_au_clip_journalise_les_etapes_executees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def clip_en_panne(job: object) -> str:
        raise RuntimeError(DICTEE)

    panne = dataclasses.replace(etapes_factices(), clip=clip_en_panne)
    monkeypatch.setattr("orchestrator.__main__.etapes_factices", lambda: panne)
    code, run_json = _lancer(tmp_path, DICTEE)
    [ligne] = _lignes(run_json.parent)
    assert code == 1
    assert ligne["etat"] == "erreur"
    assert [e["nom"] for e in ligne["etapes"]] == ["classification", "storyboard", "voix"] + [
        "image",
        "clip",
    ]
    assert ligne["etapes"][-1]["statut"] == "erreur"
    assert all(isinstance(e["duree_s"], (float, int)) for e in ligne["etapes"])


def test_journal_sans_aucun_texte_de_dictee(tmp_path: Path) -> None:
    _, run_json = _lancer(tmp_path, DICTEE)
    journal = (run_json.parent / "durees.jsonl").read_text("utf-8")
    assert not [mot for mot in DICTEE.rstrip(".").split() if len(mot) > 3 and mot in journal]
    assert str(tmp_path) not in journal


# --- Parcours réel (#150) : sans --mocks, échec fermé si la configuration manque ---


class _FauxOpener:
    """Remplace l'ouvreur urllib d'Ollama : compte les appels, réponses préenregistrées."""

    def __init__(self, reponses: list[Any]) -> None:
        self.reponses = reponses
        self.appels = 0

    def open(self, requete: object, timeout: float = 0) -> _FausseReponse:
        self.appels += 1
        if not self.reponses:
            raise AssertionError("appel réseau inattendu")
        contenu = self.reponses.pop(0)
        corps = json.dumps({"message": {"content": json.dumps(contenu)}}).encode()
        return _FausseReponse(corps)


class _FausseReponse:
    def __init__(self, corps: bytes) -> None:
        self.corps = corps

    def __enter__(self) -> _FausseReponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self.corps


def _dictee(tmp_path: Path) -> Path:
    fichier = tmp_path / "d.txt"
    fichier.write_text(DICTEE, encoding="utf-8")
    return fichier


def _opener(monkeypatch: pytest.MonkeyPatch, reponses: list[Any]) -> _FauxOpener:
    from orchestrator import steps_llm

    faux = _FauxOpener(reponses)
    monkeypatch.setattr(steps_llm, "_OPENER", faux)
    return faux


def test_reel_sans_piper_echoue_ferme_sans_reseau(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    faux = _opener(monkeypatch, [])
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test:11434")
    monkeypatch.delenv("PIPER_BIN", raising=False)
    out = tmp_path / "o"
    code = main(["run", "--dictee-file", str(_dictee(tmp_path)), "--out", str(out)])
    sortie = capsys.readouterr()
    assert code == 78
    assert "PIPER_BIN" in sortie.err
    assert "hibou" not in sortie.out + sortie.err
    assert faux.appels == 0
    assert not out.exists()


def test_reel_sans_ollama_echoue_ferme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    code = main(["run", "--dictee-file", str(_dictee(tmp_path)), "--out", str(tmp_path / "o")])
    assert code == 78
    assert "OLLAMA_URL" in capsys.readouterr().err


def test_reel_image_non_branchee_run_en_erreur(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_etapes import FAKE_PIPER, LIBRE, STORYBOARD

    binaire = tmp_path / "piper"
    binaire.write_text(FAKE_PIPER, encoding="utf-8")
    binaire.chmod(0o755)
    modele = tmp_path / "voix.onnx"
    modele.write_bytes(b"model")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test:11434")
    monkeypatch.setenv("PIPER_BIN", str(binaire))
    monkeypatch.setenv("PIPER_MODEL", str(modele))
    faux = _opener(monkeypatch, [LIBRE, STORYBOARD])
    out = tmp_path / "o"
    code = main(["run", "--dictee-file", str(_dictee(tmp_path)), "--out", str(out)])
    run = json.loads((out / "run.json").read_text("utf-8"))
    RUN.validate(run)
    sortie = capsys.readouterr()
    assert code == 1
    assert faux.appels == 2
    assert (run["etat"], run["etapes"][-1]) == ("erreur", {**run["etapes"][-1], "nom": "image"})
    assert not list(out.rglob("*.mp4"))
    assert "hibou" not in run_texte(out) + sortie.out + sortie.err


def run_texte(out: Path) -> str:
    return "".join(p.read_text("utf-8", errors="ignore") for p in out.rglob("*.json"))
