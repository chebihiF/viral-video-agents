"""CLI orchestrateur (#138) : 4 scénarios Gherkin, mode mocks, run.json validé par le contrat."""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import test_steps_gpu
from fakes import env_faux_piper
from jsonschema import Draft202012Validator
from test_etapes import LIBRE, STORYBOARD
from test_steps_gpu import MP4, FauxComfy

from orchestrator.__main__ import main
from orchestrator.mocks import MOT_BLOQUE, etapes_factices

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
RUN = Draft202012Validator(json.loads((CONTRACTS / "pipeline-run.schema.json").read_text("utf-8")))
DICTEE = "Un hibou violet secret joue du tambour sur la lune."
serveurs = test_steps_gpu.serveurs  # fixture : faux serveurs HTTP sur 127.0.0.1


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


# --- Parcours réel (#245) : faux Ollama, Piper et ComfyUI derrière de vrais serveurs HTTP ---


class _FauxOllamaHttp:
    """Fausse API Ollama : classification, storyboard, déchargement (`keep_alive: 0`)."""

    def __init__(self, journal: list[str]) -> None:
        self.journal = journal
        self.reponses: list[Any] = [LIBRE, STORYBOARD]
        self.dechargements: list[Any] = []

    def repondre(self, methode: str, chemin: str, corps: bytes | None) -> tuple[int, bytes]:
        self.journal.append(f"ollama {methode} {chemin}")
        if chemin == "/api/generate":
            self.dechargements.append(json.loads(corps or b"{}"))
            return 200, b'{"done": true}'
        contenu = json.dumps(self.reponses.pop(0))
        return 200, json.dumps({"message": {"role": "assistant", "content": contenu}}).encode()


@dataclasses.dataclass
class _Reel:
    out: Path
    journal: list[str]
    comfy: FauxComfy
    ollama: _FauxOllamaHttp
    montages: list[Any]


@pytest.fixture
def reel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serveurs: Any
) -> Iterator[Callable[..., _Reel]]:
    def preparer(mode: str = "ok", cible: str | None = None) -> _Reel:
        journal: list[str] = []
        comfy = FauxComfy(journal, mode, cible)
        ollama = _FauxOllamaHttp(journal)
        env = env_faux_piper(tmp_path)
        env["OLLAMA_URL"] = serveurs(ollama.repondre).url
        env["COMFY_URL"] = serveurs(comfy.repondre).url
        for nom, valeur in env.items():
            monkeypatch.setenv(nom, valeur)
        montages: list[Any] = []

        def montage(job: Any) -> str:
            montages.append(job)
            Path(job["output_path"]).write_bytes(MP4)
            return str(job["output_path"])

        monkeypatch.setattr("orchestrator.etapes.montage_reel", lambda: montage)
        return _Reel(tmp_path / "o", journal, comfy, ollama, montages)

    yield preparer


def _run_reel(tmp_path: Path, reel: _Reel) -> tuple[int, dict[str, Any]]:
    code = main(["run", "--dictee-file", str(_dictee(tmp_path)), "--out", str(reel.out)])
    run = json.loads((reel.out / "run.json").read_text("utf-8"))
    RUN.validate(run)
    return code, run


def test_reel_dictee_vers_mp4_avec_faux_comfyui(
    tmp_path: Path, reel: Callable[..., _Reel], capsys: pytest.CaptureFixture[str]
) -> None:
    r = reel()
    code, run = _run_reel(tmp_path, r)
    assert (code, run["etat"], run["mode"]) == (0, "ok", "reel")
    [job] = r.montages
    dossier = r.out / run["run_id"]
    clips = [Path(p["clip_path"]) for p in job["plans"]]
    assert len(clips) == len(STORYBOARD["plans"])
    assert all(c.parent == dossier and c.read_bytes() == MP4 for c in clips)
    assert run["video_path"] == str(dossier / "final.mp4")
    sortie = capsys.readouterr()
    prompts = [p["image_prompt"] for p in STORYBOARD["plans"]]
    for texte in (run_texte(r.out), sortie.out, sortie.err):
        assert "hibou" not in texte
        assert not [p for p in prompts if p in texte]


def test_reel_memoire_graphique(tmp_path: Path, reel: Callable[..., _Reel]) -> None:
    r = reel()
    _run_reel(tmp_path, r)
    j = r.journal
    premiere_image = j.index("comfy POST /prompt")
    assert j.index("ollama POST /api/generate") < premiere_image
    images = len(STORYBOARD["plans"])
    assert r.ollama.dechargements == [{"model": "qwen3:8b", "keep_alive": 0}] * images
    free = j.index("comfy POST /free")
    assert j[:free].count("comfy POST /prompt") == images  # aucun clip avant /free
    assert j[free:].count("comfy POST /prompt") == images


@pytest.mark.parametrize(("cible", "etape"), [("SaveImage", "image"), ("SaveVideo", "clip")])
def test_reel_rendu_en_echec_sans_mp4_ni_fuite(
    tmp_path: Path,
    reel: Callable[..., _Reel],
    capsys: pytest.CaptureFixture[str],
    cible: str,
    etape: str,
) -> None:
    r = reel("echec", cible)
    code, run = _run_reel(tmp_path, r)
    sortie = capsys.readouterr()
    assert (code, run["etat"], run["etapes"][-1]["nom"]) == (1, "erreur", etape)
    assert r.montages == []
    assert not list(r.out.rglob("final.mp4"))
    prompts = [p[c] for p in STORYBOARD["plans"] for c in ("image_prompt", "motion_prompt")]
    for texte in (run_texte(r.out), sortie.out, sortie.err):
        assert not [p for p in prompts if p in texte]


def test_reel_nom_de_fichier_hostile_rien_hors_du_run(
    tmp_path: Path, reel: Callable[..., _Reel]
) -> None:
    r = reel("hostile", "SaveVideo")
    code, run = _run_reel(tmp_path, r)
    assert (code, run["etapes"][-1]["nom"]) == (1, "clip")
    assert not list(tmp_path.rglob("x.mp4"))
    assert not list(tmp_path.rglob("*.mp4"))


@pytest.mark.parametrize("url", [None, "http://192.168.1.14:8188", "http://comfy.test:8188"])
def test_reel_comfy_url_refusee_code_78_sans_reseau(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str | None,
) -> None:
    from orchestrator import steps_gpu

    ollama = _opener(monkeypatch, [])
    comfy = _FauxOpener([])
    monkeypatch.setattr(steps_gpu, "_OPENER", comfy)
    for nom, valeur in env_faux_piper(tmp_path).items():
        monkeypatch.setenv(nom, valeur)
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test:11434")
    if url is None:
        monkeypatch.delenv("COMFY_URL", raising=False)
    else:
        monkeypatch.setenv("COMFY_URL", url)
    out = tmp_path / "o"
    code = main(["run", "--dictee-file", str(_dictee(tmp_path)), "--out", str(out)])
    sortie = capsys.readouterr()
    assert code == 78
    assert "COMFY_URL" in sortie.err
    assert "hibou" not in sortie.out + sortie.err
    assert (ollama.appels, comfy.appels) == (0, 0)
    assert not out.exists()


def run_texte(out: Path) -> str:
    return "".join(p.read_text("utf-8", errors="ignore") for p in out.rglob("*.json"))
