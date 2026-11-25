"""Envoi de la dictée et suivi du statut (#162) : faux orchestrateur, aucun GPU, aucun réseau."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jobs
import pytest
import server
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
STATIC = Path(server.STATIC_DIR)
SCHEMA = json.loads((ROOT / "contracts" / "kid-job.schema.json").read_text(encoding="utf-8"))
DICTEE = "Un chat bleu qui danse sur la lune"  # synthétique
MSG = "Je te génère la vidéo une fois à la maison."

# Faux CLI orchestrateur : note argv et stdin dans FAKE_SPY, recrache la dictée sur ses sorties
# (pour prouver qu'elle n'est pas relayée), attend FAKE_GATE si posé, sort avec FAKE_CODE.
FAKE = """
import json, os, pathlib, sys, time
spy = pathlib.Path(os.environ["FAKE_SPY"])
texte = sys.stdin.read()
n = len(list(spy.glob("*.json")))
(spy / f"{n}.json").write_text(json.dumps({
    "argv": sys.argv[1:], "stdin": texte,
    "dans_env": any(texte in v for v in os.environ.values()),
}))
print(texte, flush=True)
print("echo:", texte, file=sys.stderr, flush=True)
print("run=r etape=classification statut=ok duree_s=0.1", file=sys.stderr, flush=True)
gate = os.environ.get("FAKE_GATE")
while gate and not pathlib.Path(gate).exists():
    time.sleep(0.02)
code = int(os.environ.get("FAKE_CODE", "0"))
etapes = ["storyboard", "voix", "image", "clip", "montage"]
for nom in etapes if code == 0 else etapes[:1]:
    print(f"run=r etape={nom} statut=ok duree_s=0.1", file=sys.stderr, flush=True)
if code:
    print("run=r etape=voix statut=erreur type=X", file=sys.stderr, flush=True)
sys.exit(code)
"""


def validator(defn: str) -> Draft202012Validator:
    return Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{defn}"})


@pytest.fixture
def spy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "spy"
    d.mkdir()
    monkeypatch.setenv("FAKE_SPY", str(d))
    return d


@pytest.fixture
def store(tmp_path: Path, spy: Path) -> jobs.JobStore:
    fake = tmp_path / "fake_orchestrator.py"
    fake.write_text(FAKE, encoding="utf-8")
    return jobs.JobStore(tmp_path / "runs", command=(sys.executable, str(fake)))


@pytest.fixture
def client_ip(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    ip = ["192.168.1.42"]
    monkeypatch.setattr(server.Handler, "client_ip", lambda self: ip[0])
    yield ip


@pytest.fixture
def base(store: jobs.JobStore, client_ip: list[str]) -> Iterator[str]:
    httpd = server.make_server("127.0.0.1", 0, store=store)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url: str, body: bytes | None = None) -> tuple[int, Any]:
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - URL locale
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - URL locale de test
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def post(base: str, payload: object) -> tuple[int, Any]:
    return call(base + "/api/jobs", json.dumps(payload).encode())


def wait_state(store: jobs.JobStore, job_id: str, *states: str) -> jobs.Status:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        s = store.status(job_id)
        if s is not None and s["state"] in states:
            return s
        time.sleep(0.02)
    raise AssertionError(f"état {states} non atteint : {store.status(job_id)}")


def spied(spy: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(spy.glob("*.json"))]


# --- Scénario : soumission depuis la maison ---
def test_soumission_depuis_la_maison(base: str, store: jobs.JobStore, spy: Path) -> None:
    status, body = post(base, {"text": DICTEE})
    assert status == 202
    assert list(validator("Accepted").iter_errors(body)) == []
    uuid.UUID(body["job_id"])
    wait_state(store, body["job_id"], "done")
    (vu,) = spied(spy)
    assert vu["stdin"] == DICTEE  # texte reçu par stdin
    assert not any(DICTEE in a for a in vu["argv"])  # jamais en argument
    assert vu["dans_env"] is False  # jamais en variable d'environnement
    assert vu["argv"][:3] == ["run", "--dictee-file", "-"]


def test_texte_de_500_caracteres_accepte(base: str) -> None:
    assert post(base, {"text": "a" * 500})[0] == 202


@pytest.mark.parametrize(
    "payload",
    [{}, {"text": ""}, {"text": "   "}, {"text": "a" * 501}, {"text": 3}, [DICTEE],
     {"text": DICTEE, "autre": 1}],
)  # fmt: skip
def test_texte_invalide_refuse_sans_le_citer(base: str, spy: Path, payload: object) -> None:
    status, body = post(base, payload)
    assert status == 400
    assert DICTEE not in json.dumps(body)
    assert spied(spy) == []


def test_corps_non_json_ou_trop_gros_refuse(base: str, spy: Path) -> None:
    assert call(base + "/api/jobs", b"pas du json")[0] == 400
    assert call(base + "/api/jobs", b"x" * (server.BODY_MAX + 1))[0] == 413
    assert spied(spy) == []


# --- Scénario : hors de la maison ---
def test_hors_de_la_maison(base: str, client_ip: list[str], spy: Path) -> None:
    client_ip[0] = "82.10.20.30"
    status, body = post(base, {"text": DICTEE})
    assert status == 200
    assert body == {"status": "deferred", "message": MSG}
    assert list(validator("Deferred").iter_errors(body)) == []
    time.sleep(0.2)
    assert spied(spy) == []  # aucun job lancé


def test_suivi_hors_de_la_maison_differe(base: str, client_ip: list[str]) -> None:
    client_ip[0] = "82.10.20.30"
    assert call(f"{base}/api/jobs/{uuid.uuid4()}") == (200, {"status": "deferred", "message": MSG})


# --- Scénario : suivi du statut ---
def test_suivi_queued_running_done(
    base: str, store: jobs.JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    premier = post(base, {"text": DICTEE})[1]["job_id"]
    second = post(base, {"text": DICTEE})[1]["job_id"]
    wait_state(store, premier, "running")
    time.sleep(0.2)  # le faux CLI a lu la dictée et signalé la classification
    assert call(f"{base}/api/jobs/{premier}") == (200, {"state": "running", "step": "storyboard"})
    status, body = call(f"{base}/api/jobs/{second}")
    assert (status, body["state"]) == (200, "queued")  # un seul parcours à la fois
    gate.touch()
    for job_id in (premier, second):
        wait_state(store, job_id, "done")
        status, body = call(f"{base}/api/jobs/{job_id}")
        assert (status, body) == (200, {"state": "done", "step": "montage"})
        assert list(validator("Status").iter_errors(body)) == []


def test_echec_du_parcours_donne_error(
    base: str, store: jobs.JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODE", "1")
    job_id = post(base, {"text": DICTEE})[1]["job_id"]
    wait_state(store, job_id, "error")
    assert call(f"{base}/api/jobs/{job_id}") == (200, {"state": "error", "step": "voix"})


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "pas-un-id", "../../etc/passwd"])
def test_job_inconnu_404(base: str, job_id: str) -> None:
    assert call(f"{base}/api/jobs/{job_id}")[0] == 404


def test_pas_de_post_hors_api(base: str) -> None:
    assert call(base + "/index.html", b"{}")[0] == 404


# --- Scénario : aucune donnée d'enfant dans les logs ---
def test_aucune_donnee_enfant_dans_les_logs(
    base: str, store: jobs.JobStore, caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    caplog.set_level(logging.DEBUG)
    job_id = post(base, {"text": DICTEE})[1]["job_id"]
    wait_state(store, job_id, "done")
    _, body = call(f"{base}/api/jobs/{job_id}")
    out, err = capfd.readouterr()
    assert DICTEE not in caplog.text + out + err + json.dumps(body)
    assert job_id in caplog.text  # seul job_id est journalisé


# --- Scénario : échec fermé ---
def test_echec_ferme_configuration_absente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OLLAMA_URL", "PIPER_BIN", "PIPER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    store = jobs.JobStore(tmp_path / "runs")  # vrai CLI orchestrateur, sans --mocks
    job_id = store.submit(DICTEE)
    assert wait_state(store, job_id, "done", "error")["state"] == "error"
    assert not (tmp_path / "runs" / job_id / "run.json").exists()  # aucun parcours simulé


def test_jamais_de_mocks_dans_la_commande(tmp_path: Path) -> None:
    argv = jobs.JobStore(tmp_path).argv(tmp_path / "x")
    assert argv[:3] == [sys.executable, "-m", "orchestrator"]
    assert "--mocks" not in argv


def test_commande_introuvable_donne_error(tmp_path: Path) -> None:
    store = jobs.JobStore(tmp_path / "runs", command=(str(tmp_path / "absent"),))
    job_id = store.submit(DICTEE)
    assert wait_state(store, job_id, "done", "error")["state"] == "error"


# --- Scénario : écran conforme (états Envoi, Vidéo en cours, Hors maison, Erreur) ---
def _status_js() -> str:
    return (STATIC / "status.js").read_text(encoding="utf-8")


def test_status_js_charge_et_servi(base: str) -> None:
    assert '<script src="/status.js"></script>' in (STATIC / "index.html").read_text()
    with urllib.request.urlopen(base + "/status.js", timeout=5) as r:  # noqa: S310
        assert r.headers["Content-Type"].startswith("text/javascript")


def test_status_js_structure_de_la_maquette() -> None:
    src = _status_js()
    for motif in ('"note warn"', '"status"', '"note bad"', '"alert"', '"kid-seg"',
                  '"progressbar"', '"film"', '"aria-current"', '"say"', '"home-scene"',
                  '"zzz"', "Réessayer", "Mimo dort un peu", MSG, "2000"):  # fmt: skip
        assert motif in src, motif
    assert "innerHTML" not in src  # tout passe par textContent
    assert not re.search(r"console\.|localStorage|sessionStorage", src)  # dictée jamais gardée


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize(
    ("rep", "attendu"),
    [
        (None, {"ecran": "envoi"}),
        ({"job_id": "x"}, {"ecran": "en-cours", "etape": 0}),
        ({"state": "queued", "step": "storyboard"}, {"ecran": "en-cours", "etape": 0}),
        ({"state": "running", "step": "voix"}, {"ecran": "en-cours", "etape": 1}),
        ({"state": "running", "step": "images"}, {"ecran": "en-cours", "etape": 2}),
        ({"state": "running", "step": "montage"}, {"ecran": "en-cours", "etape": 3}),
        ({"state": "done", "step": "montage"}, {"ecran": "prete"}),
        ({"state": "error", "step": "voix"}, {"ecran": "erreur"}),
        ({"status": "deferred", "message": MSG}, {"ecran": "hors-maison"}),
        ({"state": "inconnu"}, {"ecran": "erreur"}),
    ],
)
def test_status_js_vue_par_etat(rep: object, attendu: dict[str, object]) -> None:
    script = f"const s = require({json.dumps(str(STATIC / 'status.js'))});" + (
        f"console.log(JSON.stringify(s.vue({json.dumps(rep)})))"
    )
    out = subprocess.run(  # noqa: S603
        ["node", "-e", script],  # noqa: S607
        capture_output=True, text=True, check=True, timeout=10,
    )  # fmt: skip
    vue = json.loads(out.stdout)
    assert {k: vue[k] for k in attendu} == attendu
