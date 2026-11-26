"""TTS SILMA par réplique (#249) : faux moteur (WAV silencieux de durée connue), aucun texte exposé.

Aucun fichier audio dans le dépôt : voix de référence et WAV sont générés à la volée.
"""

import json
import logging
import os
import stat
import sys
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from media_service import tts
from media_service.tts import (
    SCHEMA_VERSION,
    Configuration,
    TtsError,
    parse_job,
    synthetiser,
    verifier_configuration,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
PRENOM = "Secret-Prenom-Enfant"  # noqa: S105 - marqueur de fuite

# Faux moteur : argv et entrée (JSON sur stdin) consignés dans <binaire>.log, une ligne par appel ;
# WAV silencieux de 0,25 s par mot du texte. Le texte lui-même n'est jamais consigné.
FAUX_MOTEUR = f"""#!{sys.executable}
import json, os, sys, wave
from pathlib import Path
args = sys.argv[1:]
entree = json.loads(sys.stdin.read())
ligne = {{
    "argv": args,
    "mots": len(entree["texte"].split()),
    "transcription": entree["transcription"],
    "hors_ligne": os.environ.get("HF_HUB_OFFLINE"),
    "env": sorted(os.environ),
}}
with open(Path(sys.argv[0]).with_suffix(".log"), "a", encoding="utf-8") as f:
    f.write(json.dumps(ligne) + "\\n")
out = args[args.index("--sortie") + 1]
rate = 24000
with wave.open(out, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(b"\\0\\0" * (rate // 4) * ligne["mots"])
"""


def _wav(path: Path, secondes: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * secondes))
    return path


def _executable(path: Path, contenu: str) -> Path:
    path.write_text(contenu, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _voix(dossier: Path, *noms: str) -> Path:
    """Fichier SILMA_VOIX : une référence synthétique et sa transcription par voix."""
    dossier.mkdir(exist_ok=True)
    voix = {}
    for nom in noms:
        (dossier / f"{nom}.txt").write_text(f"transcription de {nom}", encoding="utf-8")
        voix[nom] = {"audio": str(_wav(dossier / f"{nom}.wav")), "transcription": f"{nom}.txt"}
    fichier = dossier / "voix.json"
    fichier.write_text(json.dumps(voix), encoding="utf-8")
    return fichier


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    modeles = tmp_path / "modeles"
    modeles.mkdir()
    return {
        "SILMA_BIN": str(_executable(tmp_path / "silma", FAUX_MOTEUR)),
        "SILMA_MODEL_DIR": str(modeles),
        "SILMA_VOIX": str(_voix(tmp_path / "voix", "chat", "hibou")),
    }


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "sortie"
    path.mkdir()
    return path


def _journal(env: dict[str, str]) -> list[dict[str, Any]]:
    log = Path(env["SILMA_BIN"]).with_suffix(".log")
    if not log.exists():
        return []
    return [json.loads(ligne) for ligne in log.read_text("utf-8").splitlines()]


def _repliques() -> list[dict[str, str]]:
    return [
        {"replique_id": "r1", "voix_id": "chat", "texte": "un deux trois quatre", "langue": "ar"},
        {"replique_id": "r2", "voix_id": "hibou", "texte": f"{PRENOM} dit bonjour", "langue": "ar"},
        {"replique_id": "r3", "voix_id": "chat", "texte": "au revoir", "langue": "ar"},
    ]


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _arg(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


# Scénario: une voix choisie par réplique


def test_chaque_replique_avec_la_reference_de_son_voix_id(
    env: dict[str, str], out: Path, tmp_path: Path
) -> None:
    results = synthetiser(_repliques(), out, env=env)
    appels = _journal(env)
    references = [Path(_arg(a["argv"], "--reference")).name for a in appels]
    assert references == ["chat.wav", "hibou.wav", "chat.wav"]
    assert [a["transcription"] for a in appels] == [
        "transcription de chat",
        "transcription de hibou",
        "transcription de chat",
    ]
    assert all(Path(_arg(a["argv"], "--reference")).is_absolute() for a in appels)
    assert {_arg(a["argv"], "--modeles") for a in appels} == {env["SILMA_MODEL_DIR"]}
    assert [r.replique_id for r in results] == ["r1", "r2", "r3"]


def test_un_wav_et_sa_duree_par_replique(env: dict[str, str], out: Path) -> None:
    results = synthetiser(_repliques(), out, env=env)
    assert [r.wav_path for r in results] == [out / "r1.wav", out / "r2.wav", out / "r3.wav"]
    for r in results:
        assert r.duree_s == pytest.approx(_wav_seconds(r.wav_path))
    assert [r.duree_s for r in results] == [
        pytest.approx(1.0),
        pytest.approx(0.75),
        pytest.approx(0.5),
    ]
    assert sorted(p.name for p in out.iterdir()) == ["r1.wav", "r2.wav", "r3.wav"]


def test_sortie_conforme_a_tts_result(env: dict[str, str], out: Path) -> None:
    results = synthetiser(_repliques(), out, env=env)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repliques": [
            {"replique_id": r.replique_id, "wav_path": str(r.wav_path), "duree_s": r.duree_s}
            for r in results
        ],
    }
    schema = json.loads((CONTRACTS / "tts-job.schema.json").read_text("utf-8"))
    Draft202012Validator({"$ref": "#/$defs/TtsResult", "$defs": schema["$defs"]}).validate(payload)


def test_mode_hors_ligne_force_pour_le_moteur(env: dict[str, str], out: Path) -> None:
    synthetiser(_repliques()[:1], out, env=env)
    assert [a["hors_ligne"] for a in _journal(env)] == ["1"]


def test_environnement_du_moteur_en_liste_blanche(
    env: dict[str, str], out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_TOKEN", "jeton-secret")
    monkeypatch.setenv("SILMA_VOIX", env["SILMA_VOIX"])
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    synthetiser(_repliques()[:1], out, env=env)
    [appel] = _journal(env)
    vues = set(appel["env"])
    assert {"PATH", "CUDA_VISIBLE_DEVICES", "LC_ALL", *tts.HORS_LIGNE} <= vues
    assert not vues & {"OLLAMA_TOKEN", "SILMA_VOIX"}
    assert not [nom for nom in vues if nom.startswith(("SILMA_", "OLLAMA_", "COMFY_"))]


# Scénario: voix inconnue


def test_voix_inconnue_echoue_sans_lancer_le_moteur(env: dict[str, str], out: Path) -> None:
    repliques = _repliques()
    repliques[2]["voix_id"] = "renard"
    with pytest.raises(TtsError, match="r3.*renard"):
        synthetiser(repliques, out, env=env)
    assert _journal(env) == []
    assert list(out.iterdir()) == []


def test_transcription_vide_echoue_sans_lancer_le_moteur(
    env: dict[str, str], out: Path, tmp_path: Path
) -> None:
    (tmp_path / "voix" / "hibou.txt").write_text(" \n", encoding="utf-8")
    with pytest.raises(TtsError, match="hibou"):
        synthetiser(_repliques(), out, env=env)
    assert _journal(env) == []


# Scénario: configuration absente ou invalide


def test_verifier_configuration_nominale(env: dict[str, str], tmp_path: Path) -> None:
    config = verifier_configuration(env)
    assert isinstance(config, Configuration)
    assert config.binaire == env["SILMA_BIN"] and Path(config.binaire).is_absolute()
    assert config.modeles == env["SILMA_MODEL_DIR"]
    assert sorted(config.voix) == ["chat", "hibou"]
    assert config.voix["hibou"].audio == tmp_path / "voix" / "hibou.wav"
    assert config.voix["hibou"].transcription == tmp_path / "voix" / "hibou.txt"


def _absent(env: dict[str, str], tmp_path: Path, variable: str) -> None:
    del env[variable]


def _introuvable(env: dict[str, str], tmp_path: Path, variable: str) -> None:
    env[variable] = str(tmp_path / "absent")


def _voix_json(contenu: Any) -> Any:
    def ecrire(env: dict[str, str], tmp_path: Path, variable: str) -> None:
        Path(env["SILMA_VOIX"]).write_text(json.dumps(contenu), encoding="utf-8")

    return ecrire


def _reference_supprimee(nom: str) -> Any:
    def supprimer(env: dict[str, str], tmp_path: Path, variable: str) -> None:
        (tmp_path / "voix" / nom).unlink()

    return supprimer


CONFIGURATIONS = [
    ("SILMA_BIN", _absent),
    ("SILMA_BIN", _introuvable),
    ("SILMA_MODEL_DIR", _absent),
    ("SILMA_MODEL_DIR", _introuvable),
    ("SILMA_VOIX", _absent),
    ("SILMA_VOIX", _introuvable),
    ("SILMA_VOIX", _voix_json({})),
    ("SILMA_VOIX", _voix_json([])),
    ("SILMA_VOIX", _voix_json({"../x": {"audio": "a.wav", "transcription": "a.txt"}})),
    ("SILMA_VOIX", _voix_json({"chat": {"audio": "chat.wav"}})),
    ("SILMA_VOIX", _voix_json({"chat": {"audio": 1, "transcription": "chat.txt"}})),
    ("SILMA_VOIX", _reference_supprimee("hibou.wav")),
    ("SILMA_VOIX", _reference_supprimee("chat.txt")),
]


@pytest.mark.parametrize(("variable", "casser"), CONFIGURATIONS)
def test_configuration_invalide_nomme_la_variable_sans_chemin(
    env: dict[str, str], out: Path, tmp_path: Path, variable: str, casser: Any
) -> None:
    origine = dict(env)
    casser(env, tmp_path, variable)
    with pytest.raises(TtsError, match=variable) as err:
        verifier_configuration(env)
    assert str(tmp_path) not in str(err.value) and "/" not in str(err.value)
    with pytest.raises(TtsError, match=variable):
        synthetiser(_repliques(), out, env=env)
    assert _journal(origine) == []
    assert list(out.iterdir()) == []


def test_voix_json_illisible(env: dict[str, str]) -> None:
    Path(env["SILMA_VOIX"]).write_text("{pas du json", encoding="utf-8")
    with pytest.raises(TtsError, match="SILMA_VOIX"):
        verifier_configuration(env)


def test_piper_ne_suffit_plus(tmp_path: Path) -> None:
    piper = _executable(tmp_path / "piper", "#!/bin/sh\n")
    with pytest.raises(TtsError, match="SILMA_BIN"):
        verifier_configuration({"PIPER_BIN": str(piper), "PIPER_MODEL": str(piper)})


# Scénario: texte jamais exposé


def test_texte_jamais_expose_meme_si_le_moteur_le_recopie(
    env: dict[str, str], out: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bavard = (
        FAUX_MOTEUR.replace('"mots": len(entree["texte"].split()),', '"mots": 0,')
        + "sys.stdout.write(entree['texte'])\nsys.stderr.write(entree['texte'])\nsys.exit(4)\n"
    )
    env["SILMA_BIN"] = str(_executable(tmp_path / "silma", bavard))
    with caplog.at_level(logging.DEBUG), pytest.raises(TtsError, match="r1") as err:
        synthetiser([_repliques()[1] | {"replique_id": "r1"}], out, env=env)
    [appel] = _journal(env)
    assert PRENOM not in json.dumps(appel["argv"])
    assert PRENOM not in str(err.value) and PRENOM not in repr(err.value)
    assert err.value.__cause__ is None
    assert PRENOM not in caplog.text
    restes = [p for p in tmp_path.rglob("*") if p.is_file() and PRENOM.encode() in p.read_bytes()]
    assert restes == []
    assert list(out.iterdir()) == []


def test_texte_vide_nomme_la_replique(env: dict[str, str], out: Path) -> None:
    repliques = _repliques()
    repliques[1]["texte"] = " \n "
    with pytest.raises(TtsError, match="r2") as err:
        synthetiser(repliques, out, env=env)
    assert PRENOM not in str(err.value)
    assert _journal(env) == []


# Scénario: échec au milieu des répliques


def _moteur_qui_flanche(tmp_path: Path, fin: str) -> str:
    """Faux moteur : la 1re réplique réussit, la 2e exécute `fin`."""
    compteur = tmp_path / "appels"
    corps = FAUX_MOTEUR + (
        f"c = Path({str(compteur)!r})\n"
        "n = int(c.read_text()) if c.exists() else 0\n"
        "c.write_text(str(n + 1))\n"
        f"if n >= 1:\n{fin}"
    )
    return str(_executable(tmp_path / "silma", corps))


def test_echec_a_la_deuxieme_replique_aucun_wav(
    env: dict[str, str], out: Path, tmp_path: Path
) -> None:
    env["SILMA_BIN"] = _moteur_qui_flanche(tmp_path, "    sys.exit(3)\n")
    with pytest.raises(TtsError, match="r2") as err:
        synthetiser(_repliques(), out, env=env)
    assert (tmp_path / "appels").read_text() == "2"
    assert PRENOM not in str(err.value)
    assert list(out.iterdir()) == []


def test_delai_depasse_a_la_deuxieme_replique_moteur_arrete(
    env: dict[str, str], out: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = tmp_path / "pid"
    fin = (
        "    import subprocess, time\n"
        "    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"    Path({str(pid)!r}).write_text(str(p.pid))\n"
        "    time.sleep(60)\n"
    )
    env["SILMA_BIN"] = _moteur_qui_flanche(tmp_path, fin)
    monkeypatch.setattr(tts, "DELAI_S", 1.5)
    debut = time.monotonic()
    with pytest.raises(TtsError, match="r2.*délai"):
        synthetiser(_repliques(), out, env=env)
    assert time.monotonic() - debut < 20
    assert list(out.iterdir()) == []
    _attendre_disparition(int(pid.read_text()))


def _attendre_disparition(pid: int) -> None:
    """Le processus lancé par le moteur est arrêté avec lui (le GPU est libre pour l'image)."""
    fin = time.monotonic() + 5
    while time.monotonic() < fin:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("processus du moteur toujours vivant")


def test_erreur_systeme_a_l_ecriture_nomme_la_replique(
    env: dict[str, str], out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any) -> None:
        raise PermissionError("refusé")

    monkeypatch.setattr("media_service.tts.os.replace", boom)
    with pytest.raises(TtsError, match="r1"):
        synthetiser(_repliques(), out, env=env)
    assert list(out.iterdir()) == []


def test_sortie_sans_wav_valide(env: dict[str, str], out: Path, tmp_path: Path) -> None:
    lazy = f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
    env["SILMA_BIN"] = str(_executable(tmp_path / "silma", lazy))
    with pytest.raises(TtsError, match="r1"):
        synthetiser(_repliques(), out, env=env)
    assert list(out.iterdir()) == []


def test_replique_id_en_double_ou_dangereux(env: dict[str, str], out: Path) -> None:
    repliques = _repliques()
    repliques[1]["replique_id"] = "r1"
    with pytest.raises(TtsError, match="r1"):
        synthetiser(repliques, out, env=env)
    repliques[1]["replique_id"] = "../evil"
    with pytest.raises(TtsError, match="replique_id"):
        synthetiser(repliques, out, env=env)
    assert _journal(env) == []


def test_dossier_de_sortie_absent(env: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(TtsError, match="dossier"):
        synthetiser(_repliques(), tmp_path / "absent", env=env)


# --- parse_job et contrat ------------------------------------------------------------------------


def _exemple() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((CONTRACTS / "examples/tts-job.json").read_text("utf-8"))
    return data


def test_exemple_du_contrat_accepte_par_le_parseur() -> None:
    assert SCHEMA_VERSION == "2.0"
    job = parse_job(_exemple())
    assert [r.replique_id for r in job.repliques] == ["r1", "r2", "r3"]
    assert [r.voix_id for r in job.repliques] == ["chat", "hibou", "chat"]
    assert job.output_dir == Path("/data/jobs/42/voix")


def test_synthetiser_accepte_les_repliques_de_parse_job(env: dict[str, str], out: Path) -> None:
    job = parse_job({"schema_version": "2.0", "output_dir": str(out), "repliques": _repliques()})
    results = synthetiser(job.repliques, job.output_dir, env=env)
    assert [r.replique_id for r in results] == ["r1", "r2", "r3"]


def test_job_1_0_refuse() -> None:
    job = {
        "schema_version": "1.0",
        "output_dir": "/data/jobs/42/voix",
        "plans": [{"plan_id": "plan-1", "texte": "Bonjour.", "langue": "fr"}],
    }
    with pytest.raises(TtsError):
        parse_job(job)


@pytest.mark.parametrize(
    ("cle", "valeur"),
    [
        ("voix_id", "../voix"),
        ("voix_id", ""),
        ("replique_id", "a" * 65),
        ("texte", "bon\u0000jour"),
        ("langue", "Darija"),
        ("reference", "/home/parent/voix.wav"),
    ],
)
def test_replique_hors_contrat_refusee(cle: str, valeur: str) -> None:
    job = _exemple()
    job["repliques"][0][cle] = valeur
    with pytest.raises(TtsError):
        parse_job(job)


# --- Enveloppe côté moteur (scripts/silma_cli.py) avec une fausse bibliothèque ------------------

ENVELOPPE = Path(__file__).resolve().parents[1] / "scripts" / "silma_cli.py"

# Fausse bibliothèque `silma_tts` (+ `catt_tashkeel`) : consigne ses appels dans FAUX_JOURNAL ;
# si FAUX_PORT est défini, tente une connexion dès l'import et dans `cached_path`, comme une
# bibliothèque qui irait chercher ses poids en ligne.
FAUX_API = """
import json, os, socket, wave
from pathlib import Path

def _noter(**champs):
    with open(os.environ["FAUX_JOURNAL"], "a", encoding="utf-8") as f:
        f.write(json.dumps(champs) + "\\n")

def _reseau():
    port = os.environ.get("FAUX_PORT")
    if port:
        _noter(evenement="tentative_reseau")
        socket.create_connection(("127.0.0.1", int(port)), timeout=2).close()

_reseau()

def cached_path(url, cache_dir=None):
    _reseau()
    return url

class SilmaTTS:
    def __init__(self, vocoder_local_path=None, **options):
        from silma_tts.infer import utils_infer
        modele = cached_path("hf://silma-ai/silma-tts/model.pt")
        vocab = cached_path("hf://silma-ai/silma-tts/vocab.txt")
        tashkeel = utils_infer.tashkeel_model
        _noter(evenement="init", modele=str(modele), vocab=str(vocab),
               vocodeur=vocoder_local_path, tashkeel=getattr(tashkeel, "chemins", None))

    def infer(self, ref_file, ref_text, gen_text, file_wave=None, **options):
        print("Input text with tashkeel  : " + gen_text, flush=True)  # comme la vraie
        _noter(evenement="infer", ref_file=ref_file, ref_text=ref_text,
               mots=len(gen_text.split()))
        with wave.open(file_wave, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\\0\\0" * 6000 * len(gen_text.split()))
        return None, 24000, None
"""

FAUX_CATT = """
class CATTEncoderOnly:
    def __init__(self, encoder_path=None, decoder_path=None, **options):
        self.chemins = [encoder_path, decoder_path]
"""

FICHIERS_MODELES = (
    "silma-tts/model.pt",
    "silma-tts/vocab.txt",
    "vocos-mel-24khz/config.yaml",
    "vocos-mel-24khz/pytorch_model.bin",
    "catt/encoder.onnx",
    "catt/decoder.onnx",
)


@pytest.fixture
def bibliotheque(tmp_path: Path) -> Path:
    racine = tmp_path / "site"
    for module, contenu in {
        "silma_tts/__init__.py": "",
        "silma_tts/api.py": FAUX_API,
        "silma_tts/infer/__init__.py": "",
        "silma_tts/infer/utils_infer.py": "tashkeel_model = None\n",
        "catt_tashkeel/__init__.py": FAUX_CATT,
    }.items():
        (racine / module).parent.mkdir(parents=True, exist_ok=True)
        (racine / module).write_text(contenu, encoding="utf-8")
    return racine


@pytest.fixture
def modeles(tmp_path: Path) -> Path:
    racine = tmp_path / "poids"
    for nom in FICHIERS_MODELES:
        (racine / nom).parent.mkdir(parents=True, exist_ok=True)
        (racine / nom).write_bytes(b"poids factices")
    return racine


@pytest.fixture
def ecoute() -> Any:
    """Socket en écoute sur la boucle locale : toute connexion tentée y resterait en attente."""
    import socket

    serveur = socket.create_server(("127.0.0.1", 0))
    serveur.setblocking(False)
    yield serveur
    serveur.close()


def _aucune_connexion(serveur: Any) -> bool:
    try:
        connexion, _ = serveur.accept()
    except BlockingIOError:
        return True
    connexion.close()
    return False


def _enveloppe(
    tmp_path: Path, bibliotheque: Path, modeles: Path, port: int | None = None
) -> tuple[Any, list[dict[str, Any]]]:
    import subprocess

    journal = tmp_path / "journal.jsonl"
    reference = _wav(tmp_path / "reference.wav")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HF_", "TRANSFORMERS_"))}
    env |= {"PYTHONPATH": str(bibliotheque), "FAUX_JOURNAL": str(journal)}
    if port is not None:
        env["FAUX_PORT"] = str(port)
    cmd = [sys.executable, str(ENVELOPPE), "--modeles", str(modeles)]
    cmd += ["--reference", str(reference), "--sortie", str(tmp_path / "r1.wav")]
    entree = json.dumps({"texte": f"{PRENOM} vole loin", "transcription": "bonjour à tous"})
    fini = subprocess.run(  # noqa: S603
        cmd, input=entree.encode(), capture_output=True, env=env, timeout=60, check=False
    )
    lignes = journal.read_text("utf-8").splitlines() if journal.exists() else []
    return fini, [json.loads(ligne) for ligne in lignes]


def test_enveloppe_nominale_fichiers_locaux_seulement(
    tmp_path: Path, bibliotheque: Path, modeles: Path
) -> None:
    fini, journal = _enveloppe(tmp_path, bibliotheque, modeles)
    assert fini.returncode == 0, fini.stderr
    init, infer = journal
    assert init["modele"] == str(modeles / "silma-tts/model.pt")
    assert init["vocab"] == str(modeles / "silma-tts/vocab.txt")
    assert init["vocodeur"] == str(modeles / "vocos-mel-24khz")
    assert init["tashkeel"] == [str(modeles / f"catt/{n}.onnx") for n in ("encoder", "decoder")]
    assert infer["ref_file"] == str(tmp_path / "reference.wav")
    assert infer["ref_text"] == "bonjour à tous"
    assert infer["mots"] == 3
    assert _wav_seconds(tmp_path / "r1.wav") == pytest.approx(0.75)
    assert PRENOM.encode() not in fini.stdout + fini.stderr  # sorties de la bibliothèque tues


# Scénario: aucun téléchargement à l'exécution


@pytest.mark.parametrize("manquant", FICHIERS_MODELES)
def test_enveloppe_fichier_annexe_manquant_sans_tentative_reseau(
    tmp_path: Path, bibliotheque: Path, modeles: Path, ecoute: Any, manquant: str
) -> None:
    (modeles / manquant).unlink()
    fini, journal = _enveloppe(tmp_path, bibliotheque, modeles, ecoute.getsockname()[1])
    assert fini.returncode == 3
    assert manquant in fini.stderr.decode()
    assert str(tmp_path) not in fini.stderr.decode()
    assert journal == []  # bibliothèque jamais importée
    assert _aucune_connexion(ecoute)
    assert not (tmp_path / "r1.wav").exists()


def test_enveloppe_refuse_le_reseau_meme_si_la_bibliotheque_essaie(
    tmp_path: Path, bibliotheque: Path, modeles: Path, ecoute: Any
) -> None:
    fini, journal = _enveloppe(tmp_path, bibliotheque, modeles, ecoute.getsockname()[1])
    assert fini.returncode == 4
    assert "réseau" in fini.stderr.decode()
    assert {"evenement": "tentative_reseau"} in journal  # la bibliothèque a essayé…
    assert _aucune_connexion(ecoute)  # … sans qu'aucune connexion ne parte
    assert PRENOM.encode() not in fini.stdout + fini.stderr


@pytest.mark.parametrize("entree", [b"", b"{}", b'{"texte": " ", "transcription": "x"}', b"\xff"])
def test_enveloppe_entree_invalide(
    tmp_path: Path, bibliotheque: Path, modeles: Path, entree: bytes
) -> None:
    import subprocess

    cmd = [sys.executable, str(ENVELOPPE), "--modeles", str(modeles)]
    cmd += ["--reference", str(_wav(tmp_path / "ref.wav")), "--sortie", str(tmp_path / "o.wav")]
    env = os.environ | {"PYTHONPATH": str(bibliotheque), "FAUX_JOURNAL": str(tmp_path / "j")}
    fini = subprocess.run(cmd, input=entree, capture_output=True, env=env, timeout=60, check=False)  # noqa: S603
    assert fini.returncode == 2
    assert not (tmp_path / "j").exists()


# --- Adaptateur + vraie enveloppe, par le lanceur SILMA_BIN documenté (#249) --------------------


def test_adaptateur_et_enveloppe_avec_darija_en_lettres_arabes_en_locale_c(
    env: dict[str, str],
    out: Path,
    tmp_path: Path,
    bibliotheque: Path,
    modeles: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shlex

    journal = tmp_path / "journal.jsonl"
    lanceur = (
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(bibliotheque))}\n"
        f"export FAUX_JOURNAL={shlex.quote(str(journal))}\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(ENVELOPPE))} "$@"\n'
    )
    env["SILMA_BIN"] = str(_executable(tmp_path / "silma-lanceur", lanceur))
    env["SILMA_MODEL_DIR"] = str(modeles)
    transcription = "السلام عليكم، لاباس عليك؟"
    (Path(env["SILMA_VOIX"]).parent / "chat.txt").write_text(transcription, encoding="utf-8")
    monkeypatch.setenv("LC_ALL", "C")
    replique = {
        "replique_id": "r1",
        "voix_id": "chat",
        "texte": "المشّ كيجري فالجردة 🐈",
        "langue": "ar",
    }
    [resultat] = synthetiser([replique], out, env=env)
    _, infer = [json.loads(ligne) for ligne in journal.read_text("utf-8").splitlines()]
    assert infer["ref_text"] == transcription
    assert infer["mots"] == 4
    assert resultat.duree_s == pytest.approx(1.0)
    assert list(out.iterdir()) == [out / "r1.wav"]
