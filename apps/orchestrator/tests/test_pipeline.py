"""Orchestrateur (#53) : 5 scénarios Gherkin avec étapes factices, sortie validée par le contrat."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orchestrator.pipeline import ETAPES, Etapes, lancer

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
DICTEE = "Dictée factice de test : un hibou violet joue du tambour sur la lune."
ORDRE = list(ETAPES)


def _load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validator(name: str) -> Draft202012Validator:
    schema = _load(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


RUN = _validator("pipeline-run.schema.json")
GPU_JOB = _validator("gpu-job.schema.json")
TTS_JOB = _validator("tts-job.schema.json")
MONTAGE_JOB = _validator("montage-job.schema.json")


class Fakes:
    """Étapes factices : enregistrent l'ordre des appels et valident les jobs reçus."""

    def __init__(self, classe: str = "libre", echec: str | None = None) -> None:
        self.classification: dict[str, Any] = _load(f"examples/classification.{classe}.json")
        self.echec = echec
        self.appels: list[str] = []
        self.textes_storyboard: list[str] = []
        self.job_ids: list[str] = []
        self.sortie_image: Any = None  # remplace la sortie de image si non None
        self.sortie_clip: Any = None
        self.ordre_wavs = 1  # -1 : WAV rendus dans l'ordre inverse
        self.wav_ids: list[str] | None = None  # plan_id rendus par voix (sinon ceux du job)
        self.montage_job: Mapping[str, Any] = {}

    def _appel(self, nom: str) -> None:
        self.appels.append(nom)
        if nom == self.echec:
            raise RuntimeError(f"échec factice sur {DICTEE}")

    def classifier(self, dictee: str) -> Mapping[str, Any]:
        self._appel("classification")
        return self.classification

    def storyboard(self, texte: str) -> Mapping[str, Any]:
        self._appel("storyboard")
        self.textes_storyboard.append(texte)
        data: dict[str, Any] = _load("examples/storyboard.valid.json")
        return data

    def voix(self, job: Mapping[str, Any]) -> Mapping[str, Any]:
        self._appel("voix")
        TTS_JOB.validate(job)
        plans = [
            {
                "plan_id": p["plan_id"],
                "wav_path": f"{job['output_dir']}/{p['plan_id']}.wav",
                "duree_s": 2.5,
            }
            for p in job["plans"]
        ]
        if self.wav_ids is not None:
            plans = [dict(p, plan_id=i) for p, i in zip(plans, self.wav_ids, strict=True)]
        return {"schema_version": "1.0", "plans": plans[:: self.ordre_wavs]}

    def image(self, job_id: str, image_prompt: str) -> str:
        self._appel("image")
        self.job_ids.append(job_id)
        return f"{job_id}.png" if self.sortie_image is None else self.sortie_image

    def clip(self, job: Mapping[str, Any]) -> str:
        self._appel("clip")
        GPU_JOB.validate(job)
        self.job_ids.append(job["job_id"])
        return f"/data/clips/{job['job_id']}.mp4" if self.sortie_clip is None else self.sortie_clip

    def montage(self, job: Mapping[str, Any]) -> str:
        self._appel("montage")
        MONTAGE_JOB.validate(job)
        self.montage_job = job
        return str(job["output_path"])

    def etapes(self) -> Etapes:
        return Etapes(
            self.classifier, self.storyboard, self.voix, self.image, self.clip, self.montage
        )


def _noms(run: Mapping[str, Any]) -> list[str]:
    return [e["nom"] for e in run["etapes"]]


def test_exemple_valide_le_contrat() -> None:
    RUN.validate(_load("examples/pipeline-run.ok.json"))


@pytest.mark.parametrize(
    "changement",
    [
        {"video_path": None},
        {"etat": "bloque"},
        {"etat": "erreur"},
        {"dictee": DICTEE},
        {"run_id": "x" * 49},
    ],
)
def test_contrat_refuse_un_etat_incoherent(changement: dict[str, Any]) -> None:
    run = _load("examples/pipeline-run.ok.json")
    run.update(changement)
    run = {k: v for k, v in run.items() if v is not None}
    assert not RUN.is_valid(run)


def test_parcours_nominal_dans_l_ordre_et_ok() -> None:
    fakes = Fakes("libre")
    run = lancer(DICTEE, fakes.etapes(), run_id="run-1", workdir="/data/jobs")
    RUN.validate(run)
    assert run["etat"] == "ok"
    assert run["video_path"] == "/data/jobs/run-1/final.mp4"
    assert _noms(run) == ORDRE
    assert all(e["statut"] == "ok" for e in run["etapes"])
    assert fakes.appels == ["classification", "storyboard", "voix"] + ["image"] * 2 + [
        "clip"
    ] * 2 + ["montage"]
    assert fakes.textes_storyboard == [DICTEE]


def test_contenu_bloque_aucune_generation() -> None:
    fakes = Fakes("bloque")
    run = lancer(DICTEE, fakes.etapes(), run_id="run-2")
    RUN.validate(run)
    assert run["etat"] == "bloque"
    assert fakes.appels == ["classification"]
    assert "video_path" not in run


def test_contenu_adouci_storyboard_recoit_le_texte_adouci() -> None:
    fakes = Fakes("adouci")
    run = lancer(DICTEE, fakes.etapes(), run_id="run-3")
    RUN.validate(run)
    assert run["etat"] == "ok"
    assert fakes.textes_storyboard == [fakes.classification["texte_adouci"]]


@pytest.mark.parametrize("etape", ORDRE)
def test_echec_d_etape_nomme_l_etape_et_arrete(etape: str) -> None:
    fakes = Fakes("libre", echec=etape)
    run = lancer(DICTEE, fakes.etapes(), run_id="run-4")
    RUN.validate(run)
    assert run["etat"] == "erreur"
    assert run["etapes"][-1] == {
        "nom": etape,
        "statut": "erreur",
        "duree_s": pytest.approx(0, abs=1),
    }
    assert _noms(run) == ORDRE[: ORDRE.index(etape) + 1]
    assert fakes.appels[-1] == etape
    assert "video_path" not in run


def test_classification_inconnue_est_une_erreur_sans_generation() -> None:
    fakes = Fakes("libre")
    fakes.classification = {"classe": "peut-etre"}
    run = lancer(DICTEE, fakes.etapes(), run_id="run-5")
    assert run["etat"] == "erreur"
    assert fakes.appels == ["classification"]


@pytest.mark.parametrize(
    ("classe", "echec"), [("libre", None), ("adouci", "clip"), ("bloque", None)]
)
def test_aucun_texte_de_dictee_dans_l_etat_ni_les_logs(
    classe: str, echec: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    fakes = Fakes(classe, echec=echec)
    with caplog.at_level(logging.DEBUG):
        run = lancer(DICTEE, fakes.etapes(), run_id="run-6")
    secrets = [DICTEE, "hibou", fakes.classification.get("texte_adouci", DICTEE)]
    traces = json.dumps(run, ensure_ascii=False) + caplog.text
    assert caplog.records, "le pipeline doit journaliser ses étapes"
    assert not [s for s in secrets if s in traces]


class DetecteurChevauchement:
    """Étapes GPU factices : comptent les appels simultanés, attendent un éventuel concurrent."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._deux = threading.Event()
        self.actifs = 0
        self.max_actifs = 0

    @contextmanager
    def _gpu(self) -> Iterator[None]:
        with self._mutex:
            self.actifs += 1
            self.max_actifs = max(self.max_actifs, self.actifs)
            if self.actifs >= 2:
                self._deux.set()
        try:
            self._deux.wait(timeout=0.05)  # laisse à un concurrent le temps d'entrer
            yield
        finally:
            with self._mutex:
                self.actifs -= 1

    def etapes(self, fakes: Fakes) -> Etapes:
        def image(job_id: str, prompt: str) -> str:
            with self._gpu():
                return fakes.image(job_id, prompt)

        def clip(job: Mapping[str, Any]) -> str:
            with self._gpu():
                return fakes.clip(job)

        return Etapes(fakes.classifier, fakes.storyboard, fakes.voix, image, clip, fakes.montage)


def _deux_runs_concurrents(
    detecteur: DetecteurChevauchement, **kwargs: Any
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []

    def go(n: int) -> None:
        runs.append(lancer(DICTEE, detecteur.etapes(Fakes()), run_id=f"run-gpu-{n}", **kwargs))

    threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return runs


def test_gpu_en_serie_deux_etapes_gpu_ne_se_chevauchent_jamais() -> None:
    detecteur = DetecteurChevauchement()
    runs = _deux_runs_concurrents(detecteur)
    assert [r["etat"] for r in runs] == ["ok", "ok"]
    assert detecteur.max_actifs == 1


def test_le_detecteur_voit_un_chevauchement_sans_verrou() -> None:
    detecteur = DetecteurChevauchement()
    _deux_runs_concurrents(detecteur, gpu_lock=nullcontext())
    assert detecteur.max_actifs == 2


def test_job_ids_gpu_distincts_entre_image_et_clip() -> None:
    fakes = Fakes()
    lancer(DICTEE, fakes.etapes(), run_id="run-7")
    assert fakes.job_ids == ["run-7-img-1", "run-7-img-2", "run-7-clip-1", "run-7-clip-2"]


class VerrouCompteur:
    def __init__(self) -> None:
        self.prises = 0

    def __enter__(self) -> None:
        self.prises += 1

    def __exit__(self, *exc: object) -> None:
        return None


def test_verrou_gpu_pris_une_fois_par_phase() -> None:
    verrou = VerrouCompteur()
    run = lancer(DICTEE, Fakes().etapes(), run_id="run-8", gpu_lock=verrou)
    assert run["etat"] == "ok"
    assert verrou.prises == 2


@pytest.mark.parametrize(
    ("etape", "sortie"),
    [
        ("image", "../evasion.png"),
        ("image", "/etc/passwd"),
        ("image", 42),
        ("image", ""),
        ("clip", "/data/../etc/clip.mp4"),
        ("clip", "  "),
        ("clip", "a\x00b.mp4"),
        ("clip", 3.5),
    ],
)
def test_sortie_gpu_hors_contrat_est_une_erreur(etape: str, sortie: Any) -> None:
    fakes = Fakes()
    setattr(fakes, f"sortie_{etape}", sortie)
    run = lancer(DICTEE, fakes.etapes(), run_id="run-9")
    RUN.validate(run)
    assert run["etat"] == "erreur"
    assert run["etapes"][-1]["nom"] == etape
    assert "montage" not in fakes.appels


def test_wavs_associes_par_plan_id() -> None:
    fakes = Fakes()
    fakes.ordre_wavs = -1
    run = lancer(DICTEE, fakes.etapes(), run_id="run-10", workdir="/w")
    assert run["etat"] == "ok"
    audios = [p["audio_path"] for p in fakes.montage_job["plans"]]
    assert audios == ["/w/run-10/voix/plan-1.wav", "/w/run-10/voix/plan-2.wav"]


@pytest.mark.parametrize("ids", [["plan-1", "plan-1"], ["plan-1", "plan-9"]])
def test_wavs_sans_correspondance_est_une_erreur(ids: list[str]) -> None:
    fakes = Fakes()
    fakes.wav_ids = ids
    run = lancer(DICTEE, fakes.etapes(), run_id="run-11")
    assert run["etat"] == "erreur"
    assert run["etapes"][-1]["nom"] == "voix"
    assert "image" not in fakes.appels
