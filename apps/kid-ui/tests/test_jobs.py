"""Envoi de la dictée et suivi du statut (#162) : faux orchestrateur, aucun GPU, aucun réseau."""

from __future__ import annotations

import http.client
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

import eta
import gpu_power
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


def call(url: str, body: bytes | None = None, **headers: str) -> tuple[int, Any]:
    method = "POST" if body is not None else "GET"
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - URL locale
    req.add_header("Content-Type", "application/json")
    for nom, valeur in headers.items():
        req.add_header(nom.replace("_", "-"), valeur)
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
    base: str, store: jobs.JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    caplog.set_level(logging.INFO, logger="jobs")
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    job_id = post(base, {"text": DICTEE})[1]["job_id"]
    wait_state(store, job_id, "running")
    time.sleep(0.2)  # le faux CLI a lu la dictée et signalé la classification
    status, body = call(f"{base}/api/jobs/{job_id}")
    assert (status, body["state"], body["step"]) == (200, "running", "storyboard")
    assert list(validator("Status").iter_errors(body)) == []
    gate.touch()
    wait_state(store, job_id, "done")
    status, body = call(f"{base}/api/jobs/{job_id}")
    done = {"pct": 100, "restant_s": 0, "bientot": False}
    assert (status, body) == (200, {"state": "done", "step": "montage", "progress": done})
    assert list(validator("Status").iter_errors(body)) == []
    etats = re.findall(rf"job={job_id} etat=(\w+)", caplog.text)
    assert etats == ["queued", "running", "done"]


def test_un_seul_job_a_la_fois_409(
    base: str, store: jobs.JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Path
) -> None:
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    premier = post(base, {"text": DICTEE})[1]["job_id"]
    status, body = post(base, {"text": DICTEE})
    assert status == 409
    assert DICTEE not in json.dumps(body)
    gate.touch()
    wait_state(store, premier, "done")
    assert post(base, {"text": DICTEE})[0] == 202
    assert len(spied(spy)) <= 2


def test_jobs_bornes_les_plus_anciens_termines_purges(tmp_path: Path, spy: Path) -> None:
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE, encoding="utf-8")
    store = jobs.JobStore(tmp_path / "runs", command=(sys.executable, str(fake)), max_jobs=2)
    ids = []
    for _ in range(3):
        ids.append(store.submit(DICTEE))
        wait_state(store, ids[-1], "done")
    assert store.status(ids[0]) is None
    assert [store.status(i) is not None for i in ids[1:]] == [True, True]
    assert jobs.MAX_JOBS == 50


def test_delai_depasse_tue_le_processus_et_donne_error(
    tmp_path: Path, spy: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="jobs")
    monkeypatch.setenv("FAKE_GATE", str(tmp_path / "jamais"))
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE, encoding="utf-8")
    store = jobs.JobStore(tmp_path / "runs", command=(sys.executable, str(fake)), timeout_s=0.5)
    job_id = store.submit(DICTEE)
    assert wait_state(store, job_id, "done", "error")["state"] == "error"
    assert f"job={job_id} erreur=delai" in caplog.text
    assert jobs.JOB_TIMEOUT_S == 30 * 60


def test_dictee_bloquee_error_sans_reessai(
    base: str, store: jobs.JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CODE", "2")  # code de sortie « bloque » du CLI
    job_id = post(base, {"text": DICTEE})[1]["job_id"]
    wait_state(store, job_id, "error")
    status, body = call(f"{base}/api/jobs/{job_id}")
    assert status == 200
    assert body["state"] == "error" and body["blocked"] is True
    assert list(validator("Status").iter_errors(body)) == []


# --- CSRF et corps de requête ---
def test_csrf_content_type_json_exige(base: str, spy: Path) -> None:
    data = json.dumps({"text": DICTEE}).encode()
    assert call(base + "/api/jobs", data, Content_Type="text/plain")[0] == 403
    assert spied(spy) == []


def test_csrf_origin_etrangere_refusee(base: str, spy: Path) -> None:
    data = json.dumps({"text": DICTEE}).encode()
    assert call(base + "/api/jobs", data, Origin="https://evil.example")[0] == 403
    assert spied(spy) == []


def test_csrf_origin_du_meme_hote_acceptee(base: str) -> None:
    data = json.dumps({"text": DICTEE}).encode()
    assert call(base + "/api/jobs", data, Origin=base)[0] == 202


@pytest.mark.parametrize("longueur", ["-1", "abc", ""])
def test_content_length_invalide_400(base: str, spy: Path, longueur: str) -> None:
    hote, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(hote, int(port), timeout=5)
    conn.putrequest("POST", "/api/jobs")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", longueur)
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()
    assert spied(spy) == []


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
def test_echec_ferme_code_ex_config_du_cli(
    base: str, store: jobs.JobStore, spy: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    caplog.set_level(logging.INFO, logger="jobs")
    monkeypatch.setenv("FAKE_CODE", "78")  # EX_CONFIG : configuration absente (#150)
    job_id = post(base, {"text": DICTEE})[1]["job_id"]
    wait_state(store, job_id, "error")
    assert f"job={job_id} etat=error code=78" in caplog.text
    assert "blocked" not in call(f"{base}/api/jobs/{job_id}")[1]
    (vu,) = spied(spy)  # un seul appel : aucun repli sur les mocks
    assert "--mocks" not in vu["argv"]


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
                  '"zzz"', "Réessayer", '"disabled"', "Autre idée", "Mimo dort un peu",
                  MSG, "2000"):  # fmt: skip
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
        ({"state": "error", "step": "voix"}, {"ecran": "erreur", "bloque": False}),
        (
            {"state": "error", "step": "storyboard", "blocked": True},
            {"ecran": "erreur", "bloque": True},
        ),
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


# --- #165 : progress (barre et durée restante) ---
def _journal(runs: Path, images: float, n: int = 3) -> None:
    """Journal d'un run précédent (n runs réels identiques) : images = image + clip."""
    d = runs / "ancien"
    d.mkdir(parents=True, exist_ok=True)
    etapes = [("classification", 1), ("storyboard", 9), ("voix", 20), ("image", images / 2),
              ("clip", images / 2), ("montage", 40)]  # fmt: skip
    lignes = [
        {
            "run_id": f"r{i}",
            "mode": "reel",
            "etat": "ok",
            "termine_le": f"2026-11-0{i + 1}T10:00:00Z",
            "etapes": [{"nom": k, "statut": "ok", "duree_s": float(v)} for k, v in etapes],
        }
        for i in range(n)
    ]
    (d / "durees.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lignes), encoding="utf-8")


@pytest.fixture
def horloge(tmp_path: Path, spy: Path) -> tuple[jobs.JobStore, list[float]]:
    fake = tmp_path / "fake_orchestrator.py"
    fake.write_text(FAKE, encoding="utf-8")
    t = [0.0]
    runs = tmp_path / "runs"
    _journal(runs, images=130)
    return jobs.JobStore(runs, command=(sys.executable, str(fake)), clock=lambda: t[0]), t


def test_progress_d_apres_les_durees_mesurees(
    horloge: tuple[jobs.JobStore, list[float]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, t = horloge
    monkeypatch.setenv("FAKE_GATE", str(tmp_path / "gate"))
    job_id = store.submit(DICTEE)
    wait_state(store, job_id, "running")
    time.sleep(0.2)  # classification reçue : l'étape affichée reste « storyboard »
    lignes = eta.lire_durees(tmp_path / "runs")
    t[0] = 4
    s = store.status(job_id)
    assert s is not None
    assert s["progress"] == eta.estimer(lignes, "storyboard", 4)
    assert s["progress"]["restant_s"] == 10 - 4 + 20 + 130 + 40
    assert list(validator("Status").iter_errors(s)) == []
    assert DICTEE not in json.dumps(s)
    (tmp_path / "gate").touch()
    wait_state(store, job_id, "done")


def test_la_barre_ne_recule_jamais(
    horloge: tuple[jobs.JobStore, list[float]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, t = horloge
    monkeypatch.setenv("FAKE_GATE", str(tmp_path / "gate"))
    job_id = store.submit(DICTEE)
    wait_state(store, job_id, "running")
    time.sleep(0.2)
    pcts = []
    for instant in (0, 8, 3, 9, 1):  # estimation qui baisserait (horloge qui recule)
        t[0] = instant
        s = store.status(job_id)
        assert s is not None
        pcts.append(s["progress"]["pct"])
    assert pcts == sorted(pcts)
    assert pcts[-1] > pcts[0]
    (tmp_path / "gate").touch()
    wait_state(store, job_id, "done")


def test_progress_sans_journal_valeur_par_defaut(
    store: jobs.JobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GATE", str(tmp_path / "gate"))
    job_id = store.submit(DICTEE)
    s = store.status(job_id)
    assert s is not None
    assert s["progress"] == eta.estimer([], "storyboard", 0) | {"pct": s["progress"]["pct"]}
    assert s["progress"]["restant_s"] is not None
    assert list(validator("Status").iter_errors(s)) == []
    (tmp_path / "gate").touch()
    wait_state(store, job_id, "done")


def test_progress_100_a_done_et_absent_en_erreur(
    store: jobs.JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = store.submit(DICTEE)
    done = wait_state(store, job_id, "done")
    assert done["progress"] == {"pct": 100, "restant_s": 0, "bientot": False}
    monkeypatch.setenv("FAKE_CODE", "1")
    err = wait_state(store, store.submit(DICTEE), "error")
    assert "progress" not in err
    assert list(validator("Status").iter_errors(err)) == []


def test_progress_ne_modifie_pas_le_journal(
    horloge: tuple[jobs.JobStore, list[float]], tmp_path: Path
) -> None:
    store, _ = horloge
    f = tmp_path / "runs" / "ancien" / "durees.jsonl"
    avant = f.read_bytes()
    wait_state(store, store.submit(DICTEE), "done")
    assert f.read_bytes() == avant


def test_purge_oublie_aussi_le_suivi(tmp_path: Path, spy: Path) -> None:
    fake = tmp_path / "fake_orchestrator.py"
    fake.write_text(FAKE, encoding="utf-8")
    petit = jobs.JobStore(tmp_path / "runs", command=(sys.executable, str(fake)), max_jobs=2)
    for _ in range(4):
        wait_state(petit, petit.submit(DICTEE), "done")
    assert len(petit._suivi) == len(petit._jobs) <= 2  # noqa: SLF001


# --- #165 : écran (JS) ---
def _node(script: str) -> Any:
    out = subprocess.run(  # noqa: S603
        ["node", "-e", f"const s = require({json.dumps(str(STATIC / 'status.js'))});" + script],  # noqa: S607
        capture_output=True, text=True, check=True, timeout=10,
    )  # fmt: skip
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node absent")


@needs_node
@pytest.mark.parametrize(
    ("progress", "attendu"),
    [
        ({"pct": 40, "restant_s": 200, "bientot": False}, {"minutes": 3}),
        ({"pct": 40, "restant_s": 90, "bientot": False}, {"minutes": 2}),
        ({"pct": 40, "restant_s": 30, "bientot": False}, {"minutes": 1}),  # arrondi, jamais 0 min
        ({"pct": 90, "restant_s": 29, "bientot": False}, {"bientot": True}),
        ({"pct": 90, "restant_s": 0, "bientot": True}, {"bientot": True}),  # dépassement
        ({"pct": 90, "restant_s": 500, "bientot": True}, {"bientot": True}),
        ({"pct": 0, "restant_s": None, "bientot": False}, None),  # inconnue : pas de faux chiffre
        ({"pct": 0, "restant_s": -5, "bientot": False}, None),  # jamais de temps négatif
        (None, None),
    ],
)
def test_status_js_duree_restante(progress: object, attendu: object) -> None:
    assert _node(f"console.log(JSON.stringify(s.reste({json.dumps(progress)})))") == attendu


@needs_node
def test_status_js_la_barre_ne_recule_jamais_et_vue_porte_progress() -> None:
    assert _node("console.log(JSON.stringify([s.avancer(60, 45), s.avancer(60, 61)]))") == [60, 61]
    rep = {"state": "running", "step": "images",
           "progress": {"pct": 55, "restant_s": 100, "bientot": False}}  # fmt: skip
    v = _node(f"console.log(JSON.stringify(s.vue({json.dumps(rep)})))")
    assert (v["pct"], v["reste"]) == (55, {"minutes": 2})
    v = _node('console.log(JSON.stringify(s.vue({state: "done", step: "montage"})))')
    assert (v["ecran"], v["pct"]) == ("prete", 100)
    v = _node(
        'console.log(JSON.stringify(s.vue({state: "running", step: "voix", progress: {pct: 1e9}})))'
    )
    assert v["pct"] == 100  # borné


# Mini DOM : de quoi exécuter le rendu réel de status.js sans navigateur.
DOM = r"""
const toks = (n) => String(n.className || "").split(/\s+/).filter(Boolean);
class N {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.className = "";
    this.style = {}; this.hidden = false; this.text = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.children.push(c)); }
  replaceChildren(...cs) { this.children = []; this.append(...cs); }
  addEventListener() {}
  cloneNode() { return new N(this.tag); }
  set textContent(t) { this.children = []; this.text = String(t); }
  get textContent() { return (this.text || "") + this.children.map((c) => c.textContent).join(""); }
  all() { return this.children.flatMap((c) => [c, ...(c.all ? c.all() : [])]); }
  match(sel) { return sel[0] === "." ? toks(this).includes(sel.slice(1)) : this.tag === sel; }
  querySelector(sel) { return this.all().find((n) => n.match && n.match(sel)) || null; }
  querySelectorAll(sel) { return this.all().filter((n) => n.match && n.match(sel)); }
}
const body = new N("body");
global.document = {
  body, documentElement: { dir: "" }, dir: "",
  createElement: (t) => new N(t), createElementNS: (_, t) => new N(t),
  createTextNode: (t) => { const n = new N("#text"); n.text = t; return n; },
  querySelector: () => null, querySelectorAll: (s) => body.querySelectorAll(s),
  getElementById: () => null,
};
global.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
document.dispatchEvent = () => {};
const reponses = [];
const timers = [];
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(reponses.shift()) });
global.setTimeout = (f) => { timers.push(f); return timers.length; };
global.clearTimeout = () => {};
const flush = () => new Promise((r) => setImmediate(r));
async function voir(vus) {
  await flush(); await flush(); vus.push(etat());
  const t = timers.shift(); if (t) t();
}
function etat() {
  const seg = body.querySelector(".kid-seg");
  const eta = body.querySelector(".eta");
  return { now: seg && seg.attrs["aria-valuenow"], text: seg && seg.attrs["aria-valuetext"],
    chip: eta ? eta.textContent : null, role: eta && eta.attrs.role,
    on: seg ? seg.children.filter((i) => i.className === "on").length : null,
    h1: body.querySelector("h1").textContent };
}
"""


@needs_node
def test_status_js_rendu_de_la_barre_et_de_la_duree() -> None:
    script = (
        DOM
        + """
(async () => {
  const p = (pct, restant_s, bientot) => ({ state: "running", step: "images",
    progress: { pct, restant_s, bientot } });
  reponses.push({ job_id: "j" }, p(60, 200, false), p(45, 100, false), p(70, 10, true),
    { state: "done", step: "montage", progress: { pct: 100, restant_s: 0, bientot: false } });
  const vus = [];
  s.envoyer("une dictee secrete");
  await voir(vus);  // accepté : barre à 0, pas de chip
  await voir(vus);  // 60 %, environ 3 min
  await voir(vus);  // l'estimation baisse (45) : la barre reste à 60
  await voir(vus);  // bientôt prête
  await voir(vus);  // prête : 100 %
  console.log(JSON.stringify(vus));
})();
"""
    )
    vus = _node(script)
    assert [(v["now"], v["chip"]) for v in vus] == [
        ("0", ""), ("60", "Encore environ 3 min"), ("60", "Encore environ 2 min"),
        ("70", "Bientôt prête"), ("100", None),
    ]  # fmt: skip
    assert vus[1]["role"] == "status"
    assert vus[1]["on"] == 2 and vus[3]["on"] == 2  # 60 % : 2 segments pleins sur 4
    assert "environ 3 minutes" in vus[1]["text"]
    assert vus[4]["h1"] == "Mimo" and vus[4]["on"] == 4
    assert all("secrete" not in json.dumps(v) for v in vus)  # la dictée n'est jamais affichée


# --- #223 : réveil et veille du PC GPU (pc-gpu simulé, aucun appel réel) ---


class FakePcGpu:
    """pc-gpu simulé : note chaque verbe et le nombre de jobs déjà lancés par le faux CLI."""

    def __init__(self, spy: Path, etat: str = "endormi", wake_code: int = 0) -> None:
        self.spy, self.etat, self.wake_code = spy, etat, wake_code
        self.gate: threading.Event | None = None  # si posé, `wake` attend son ouverture
        self.appels: list[tuple[str, int]] = []

    def __call__(self, args: list[str], timeout: float) -> gpu_power.Result:
        verbe = args[-1]
        assert args == [gpu_power.PC_GPU, verbe]
        self.appels.append((verbe, len(list(self.spy.glob("*.json")))))
        if verbe == "wake":
            if self.gate is not None:
                self.gate.wait(10)
            self.etat = "eveille" if self.wake_code == 0 else self.etat
            return gpu_power.Result(self.wake_code, "")
        if verbe == "sleep":
            self.etat = "endormi"
            return gpu_power.Result(0, "")
        return gpu_power.Result(0, self.etat + "\n")

    def verbes(self) -> list[str]:
        return [v for v, _ in self.appels]


def store_alimente(tmp_path: Path, power: jobs.Power) -> jobs.JobStore:
    fake = tmp_path / "fake_orchestrator.py"
    fake.write_text(FAKE, encoding="utf-8")
    return jobs.JobStore(tmp_path / "runs", command=(sys.executable, str(fake)), power=power)


def attendre(cond: Any) -> None:
    deadline = time.monotonic() + 10
    while not cond():
        assert time.monotonic() < deadline, "condition non atteinte"
        time.sleep(0.01)


def test_reveil_avant_job(tmp_path: Path, spy: Path) -> None:
    pc = FakePcGpu(spy, etat="endormi")
    st = store_alimente(tmp_path, gpu_power.GpuPower(run=pc))
    job_id = st.submit(DICTEE)
    wait_state(st, job_id, "done")
    assert pc.appels == [("status", 0), ("wake", 0), ("status", 0)]  # tout avant le lancement
    assert len(spied(spy)) == 1


def test_j_allume_le_pc_expose_pendant_le_reveil(tmp_path: Path, spy: Path) -> None:
    pc = FakePcGpu(spy, etat="endormi")
    pc.gate = threading.Event()
    st = store_alimente(tmp_path, gpu_power.GpuPower(run=pc))
    job_id = st.submit(DICTEE)
    attendre(lambda: "wake" in pc.verbes())
    s = st.status(job_id)
    assert s is not None and s["state"] == "queued" and s.get("waking") is True
    validator("Status").validate(s)
    pc.gate.set()
    fin = wait_state(st, job_id, "done")
    assert "waking" not in fin


def test_pc_injoignable_job_en_erreur_sans_texte_d_enfant(
    tmp_path: Path, spy: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    pc = FakePcGpu(spy, etat="endormi", wake_code=1)
    st = store_alimente(tmp_path, gpu_power.GpuPower(run=pc))
    job_id = st.submit(DICTEE)
    s = wait_state(st, job_id, "error")
    validator("Status").validate(s)
    assert "blocked" not in s and "waking" not in s
    assert spied(spy) == []  # le CLI n'est jamais lancé
    assert f"job={job_id} erreur=GpuUnavailable" in caplog.text
    assert DICTEE not in caplog.text


def test_veille_apres_inactivite_une_seule_fois(spy: Path) -> None:
    t = [0.0]
    pc = FakePcGpu(spy, etat="eveille")
    veille = jobs.Veille(gpu_power.GpuPower(run=pc, clock=lambda: t[0], idle_s=300), period_s=0.01)
    veille.start()
    time.sleep(0.05)
    assert pc.appels == []  # idle_s pas écoulé : pas même un status
    t[0] = 301
    attendre(lambda: "sleep" in pc.verbes())
    time.sleep(0.1)  # plusieurs tick() de plus
    veille.stop()
    assert pc.verbes().count("sleep") == 1
    assert not veille.is_alive()


def test_job_en_cours_tick_n_endort_pas(
    tmp_path: Path, spy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    t = [0.0]
    pc = FakePcGpu(spy, etat="eveille")
    power = gpu_power.GpuPower(run=pc, clock=lambda: t[0], idle_s=300)
    st = store_alimente(tmp_path, power)
    job_id = st.submit(DICTEE)
    wait_state(st, job_id, "running")
    t[0] = 10_000
    assert power.tick() is False
    gate.touch()
    wait_state(st, job_id, "done")
    assert power.tick() is False  # job_finished a relancé le compte à rebours
    assert "sleep" not in pc.verbes()
    t[0] = 10_301
    assert power.tick() is True


def test_alimentation_branchee_seulement_sur_option() -> None:
    assert server.make_power(server.parse_args([])) is None
    assert isinstance(server.make_power(server.parse_args(["--gpu-power"])), gpu_power.GpuPower)
