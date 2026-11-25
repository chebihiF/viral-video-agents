"""Lecture et téléchargement de la vidéo terminée (#163) : faux orchestrateur, aucun GPU."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import jobs
import pytest
import server
import video

STATIC = Path(server.STATIC_DIR)
MP4 = bytes(range(256)) * 8  # 2048 octets de « vidéo » de test
DICTEE = "Un chat bleu qui danse sur la lune"  # synthétique
UNKNOWN = "11111111-2222-3333-4444-555555555555"
SRC = f"/api/jobs/{UNKNOWN}/video"

# Faux CLI : écrit un MP4 de test dans --out/<run>/final.mp4 (comme le pipeline), sauf FAKE_NOVIDEO.
FAKE = """
import os, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--out") + 1])
sys.stdin.read()
if not os.environ.get("FAKE_NOVIDEO"):
    (out / "0123456789abcdef").mkdir(parents=True)
    (out / "0123456789abcdef" / "final.mp4").write_bytes(bytes(range(256)) * 8)
"""


@pytest.fixture
def store(tmp_path: Path) -> jobs.JobStore:
    fake = tmp_path / "fake_orchestrator.py"
    fake.write_text(FAKE, encoding="utf-8")
    return jobs.JobStore(tmp_path / "runs", command=(sys.executable, str(fake)))


@pytest.fixture
def client_ip(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ip = ["192.168.1.42"]
    monkeypatch.setattr(server.Handler, "client_ip", lambda self: ip[0])
    return ip


@pytest.fixture
def addr(store: jobs.JobStore, client_ip: list[str]) -> Iterator[tuple[str, int]]:
    httpd = server.make_server("127.0.0.1", 0, store=store)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "127.0.0.1", httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def done_job(store: jobs.JobStore) -> str:
    job_id = store.submit(DICTEE)
    deadline = time.monotonic() + 10
    while (store.status(job_id) or {}).get("state") not in ("done", "error"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return job_id


def get(
    addr: tuple[str, int], path: str, **headers: str
) -> tuple[int, http.client.HTTPMessage, bytes]:
    conn = http.client.HTTPConnection(*addr, timeout=5)
    try:
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        return r.status, r.msg, r.read()
    finally:
        conn.close()


# --- Scénario : lecture ---
def test_lecture_par_plage_206_video_mp4(addr: tuple[str, int], store: jobs.JobStore) -> None:
    job_id = done_job(store)
    status, h, body = get(addr, f"/api/jobs/{job_id}/video", Range="bytes=0-99")
    assert status == 206
    assert h["Content-Type"] == "video/mp4"
    assert h["Content-Range"] == f"bytes 0-99/{len(MP4)}"
    assert h["Content-Length"] == "100"
    assert h["Accept-Ranges"] == "bytes"
    assert h["Cache-Control"] == "no-store"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert body == MP4[:100]


@pytest.mark.parametrize(
    ("rng", "debut", "fin"),
    [("bytes=2000-", 2000, 2047), ("bytes=-48", 2000, 2047), ("bytes=2040-9999", 2040, 2047)],
)
def test_plages_ouvertes_ou_de_fin(
    addr: tuple[str, int], store: jobs.JobStore, rng: str, debut: int, fin: int
) -> None:
    job_id = done_job(store)
    status, h, body = get(addr, f"/api/jobs/{job_id}/video", Range=rng)
    assert status == 206
    assert h["Content-Range"] == f"bytes {debut}-{fin}/{len(MP4)}"
    assert body == MP4[debut : fin + 1]


def test_sans_range_le_fichier_entier_en_200(addr: tuple[str, int], store: jobs.JobStore) -> None:
    job_id = done_job(store)
    status, h, body = get(addr, f"/api/jobs/{job_id}/video")
    assert (status, body) == (200, MP4)
    assert h["Content-Type"] == "video/mp4"
    assert h["Accept-Ranges"] == "bytes"
    assert "attachment" not in (h["Content-Disposition"] or "")


def test_plage_hors_fichier_416(addr: tuple[str, int], store: jobs.JobStore) -> None:
    job_id = done_job(store)
    status, h, _ = get(addr, f"/api/jobs/{job_id}/video", Range="bytes=5000-6000")
    assert status == 416
    assert h["Content-Range"] == f"bytes */{len(MP4)}"


@pytest.mark.parametrize("rng", ["bytes=abc", "bytes=9-3", "items=0-9", "bytes=0-1,5-6"])
def test_plage_illisible_ignoree(addr: tuple[str, int], store: jobs.JobStore, rng: str) -> None:
    job_id = done_job(store)
    status, _, body = get(addr, f"/api/jobs/{job_id}/video", Range=rng)
    assert (status, body) == (200, MP4)


# --- Scénario : téléchargement ---
def test_telechargement_nom_neutre(addr: tuple[str, int], store: jobs.JobStore) -> None:
    job_id = done_job(store)
    status, h, body = get(addr, f"/api/jobs/{job_id}/video?download=1")
    assert (status, body) == (200, MP4)
    assert h["Content-Disposition"] == f'attachment; filename="{job_id}.mp4"'


# --- Scénario : traversée de chemin et jobs inconnus ---
@pytest.mark.parametrize(
    "ident",
    ["..%2Fetc%2Fpasswd", "../etc/passwd", "..", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", UNKNOWN,
     "0123456789abcdef", UNKNOWN.upper(), "x" * 5000],
)  # fmt: skip
def test_traversee_et_inconnu_404(addr: tuple[str, int], store: jobs.JobStore, ident: str) -> None:
    done_job(store)
    status, _, body = get(addr, f"/api/jobs/{ident}/video")
    assert status == 404
    assert b"passwd" not in body


def test_job_non_termine_404(addr: tuple[str, int], store: jobs.JobStore) -> None:
    store._jobs[UNKNOWN] = jobs.Status(state="running", step="montage")
    (store.runs_dir / UNKNOWN).mkdir(parents=True)
    (store.runs_dir / UNKNOWN / "final.mp4").write_bytes(MP4)
    assert get(addr, f"/api/jobs/{UNKNOWN}/video")[0] == 404


def test_job_done_sans_mp4_404(
    addr: tuple[str, int], store: jobs.JobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_NOVIDEO", "1")
    job_id = done_job(store)
    assert get(addr, f"/api/jobs/{job_id}/video")[0] == 404


def test_lien_symbolique_refuse(
    addr: tuple[str, int], store: jobs.JobStore, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"secret")
    store._jobs[UNKNOWN] = jobs.Status(state="done", step="montage")
    d = store.runs_dir / UNKNOWN
    d.mkdir(parents=True)
    (d / "final.mp4").symlink_to(secret)
    assert get(addr, f"/api/jobs/{UNKNOWN}/video")[0] == 404


# --- Scénario : hors réseau local ---
@pytest.mark.parametrize("ip", ["82.10.20.30", "", "pas-une-ip"])
def test_hors_reseau_403_sans_detail(
    addr: tuple[str, int], store: jobs.JobStore, client_ip: list[str], ip: str
) -> None:
    job_id = done_job(store)
    client_ip[0] = ip
    status, _, body = get(addr, f"/api/jobs/{job_id}/video")
    assert status == 403
    assert json.loads(body) == {"error": "accès refusé"}
    assert get(addr, f"/api/jobs/{job_id}/video?download=1", Range="bytes=0-9")[0] == 403
    assert get(addr, "/api/jobs/..%2Fx/video")[0] == 403  # la garde passe avant toute analyse


# --- Contrat : aucun nom ni journal ne porte la dictée ---
def test_ni_journal_ni_nom_ne_contient_la_dictee(
    addr: tuple[str, int], store: jobs.JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    job_id = done_job(store)
    _, h, _ = get(addr, f"/api/jobs/{job_id}/video?download=1", Range="bytes=0-9")
    assert DICTEE not in caplog.text
    assert DICTEE not in str(h)
    assert not any(DICTEE in str(p) for p in store.runs_dir.rglob("*"))


# --- Client : static/player.js ---
def test_player_js_lecteur_telechargement_et_nouvelle_video() -> None:
    src = (STATIC / "player.js").read_text(encoding="utf-8")
    motifs = ("mimo:video-prete", "controls:", "playsinline:", "?download=1", "download")
    motifs += ("/video", "Télécharger", "Nouvelle vidéo", "ecran-accueil", "encodeURIComponent")
    for motif in motifs:
        assert motif in src, motif
    assert "innerHTML" not in src
    assert not re.search(r"console\.|localStorage|sessionStorage|caches\.", src)


def test_index_charge_player_js_et_sw_inchange() -> None:
    assert '<script src="/player.js"></script>' in (STATIC / "index.html").read_text("utf-8")
    assert "player.js" not in (STATIC / "sw.js").read_text("utf-8")  # jamais mis en cache


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize(
    ("job_id", "attendu"),
    [
        (UNKNOWN, {"src": SRC, "dl": SRC + "?download=1"}),
        ("../etc/passwd", None),
        (None, None),
        (42, None),
    ],
)  # fmt: skip
def test_player_js_urls(job_id: object, attendu: dict[str, str] | None) -> None:
    script = f"const p = require({json.dumps(str(STATIC / 'player.js'))});" + (
        f"console.log(JSON.stringify(p.urls({json.dumps(job_id)})))"
    )
    out = subprocess.run(  # noqa: S603
        ["node", "-e", script],  # noqa: S607
        capture_output=True, text=True, check=True, timeout=10,
    )  # fmt: skip
    assert json.loads(out.stdout) == attendu


def test_sous_dossier_en_lien_symbolique_refuse(
    addr: tuple[str, int], store: jobs.JobStore, tmp_path: Path
) -> None:
    ailleurs = tmp_path / "ailleurs"
    ailleurs.mkdir()
    (ailleurs / "final.mp4").write_bytes(b"secret")
    store._jobs[UNKNOWN] = jobs.Status(state="done", step="montage")
    d = store.runs_dir / UNKNOWN
    d.mkdir(parents=True)
    (d / "run").symlink_to(ailleurs, target_is_directory=True)
    assert get(addr, f"/api/jobs/{UNKNOWN}/video")[0] == 404


def test_dossier_du_job_en_lien_symbolique_refuse(
    addr: tuple[str, int], store: jobs.JobStore, tmp_path: Path
) -> None:
    ailleurs = tmp_path / "ailleurs"
    ailleurs.mkdir()
    (ailleurs / "final.mp4").write_bytes(b"secret")
    store._jobs[UNKNOWN] = jobs.Status(state="done", step="montage")
    store.runs_dir.mkdir(parents=True)
    (store.runs_dir / UNKNOWN).symlink_to(ailleurs, target_is_directory=True)
    assert get(addr, f"/api/jobs/{UNKNOWN}/video")[0] == 404


def test_fichier_non_regulier_404(addr: tuple[str, int], store: jobs.JobStore) -> None:
    """Un tube nommé final.mp4 ne bloque jamais le serveur (O_NONBLOCK + S_ISREG)."""
    store._jobs[UNKNOWN] = jobs.Status(state="done", step="montage")
    d = store.runs_dir / UNKNOWN
    d.mkdir(parents=True)
    os.mkfifo(d / "final.mp4")
    assert get(addr, f"/api/jobs/{UNKNOWN}/video")[0] == 404


def test_send_refuse_un_non_regulier_meme_si_find_a_ete_contourne(tmp_path: Path) -> None:
    fifo = tmp_path / "final.mp4"
    os.mkfifo(fifo)
    reponses: list[int] = []

    class H:
        def send_json(self, code: int, _payload: object) -> None:
            reponses.append(code)

    video.send(H(), fifo, UNKNOWN, download=False)
    assert reponses == [404]


@pytest.mark.parametrize(
    "rng", ["bytes=" + "9" * 40 + "-", "bytes=-" + "9" * 40, "bytes=0-" + "9" * 25]
)
def test_plage_aux_nombres_demesures(rng: str) -> None:
    assert video.parse_range(rng, 2048) is None


def test_erreur_ssl_ou_os_pendant_l_envoi_ferme_sans_lever(tmp_path: Path) -> None:
    mp4 = tmp_path / "final.mp4"
    mp4.write_bytes(MP4)

    class W:
        def write(self, _b: bytes) -> None:
            raise ssl.SSLError("boom")

    class H:
        wfile = W()
        close_connection = False
        headers: dict[str, str] = {}  # noqa: RUF012

        def send_response(self, _c: int) -> None: ...
        def send_header(self, _k: str, _v: str) -> None: ...
        def end_headers(self) -> None: ...

    h = H()
    video.send(h, mp4, UNKNOWN, download=False)
    assert h.close_connection is True
