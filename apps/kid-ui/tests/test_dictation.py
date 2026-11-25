"""Tests de la dictée au micro (#161) : POST /api/dictation, aucune trace, aucun tiers."""

from __future__ import annotations

import http.client
import json
import logging
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import server
import transcriber
from jsonschema import Draft202012Validator

STATIC = Path(server.STATIC_DIR)
APP = Path(server.__file__).resolve().parent
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "dictation.schema.json").read_text("utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA)
BOUNDARY = "----mimo-test-boundary"
TEXTE = "une vidéo de dragon"
AUDIO = b"\x1aE\xdf\xa3-audio-d-enfant-secret"


class Boom:
    """Transcriber en panne : son message d'erreur contient le contenu (ne doit jamais fuiter)."""

    def transcribe(self, audio: bytes, mime: str) -> str:
        raise RuntimeError(f"échec sur {audio!r}")


def multipart(
    audio: bytes | None, mime: str = "audio/webm;codecs=opus", field: str = "audio"
) -> bytes:
    if audio is None:
        return f"--{BOUNDARY}--\r\n".encode()
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="dictee.webm"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    )
    return head.encode() + audio + f"\r\n--{BOUNDARY}--\r\n".encode()


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def post(
        self,
        body: bytes,
        ctype: str | None = f"multipart/form-data; boundary={BOUNDARY}",
        path: str = "/api/dictation",
    ) -> tuple[int, dict[str, str], Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Type": ctype} if ctype else {}
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            raw = r.read()
            return r.status, dict(r.getheaders()), (json.loads(raw) if raw else None)
        finally:
            conn.close()


def serve(tr: transcriber.Transcriber | None) -> Iterator[Client]:
    httpd = server.make_server("127.0.0.1", 0, transcriber=tr)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Client(httpd.server_address[1])
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def mock() -> transcriber.MockTranscriber:
    return transcriber.MockTranscriber(TEXTE)


@pytest.fixture
def client(mock: transcriber.MockTranscriber) -> Iterator[Client]:
    yield from serve(mock)


# --- Scénario : dictée réussie ---
def test_dictee_reussie(client: Client, mock: transcriber.MockTranscriber) -> None:
    status, headers, body = client.post(multipart(AUDIO))
    assert status == 200
    assert body == {"text": TEXTE}
    assert VALIDATOR.is_valid(body)
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"
    assert mock.calls == 1


@pytest.mark.parametrize("mime", ["audio/webm", "audio/ogg;codecs=opus", "audio/mp4"])
def test_formats_acceptes(client: Client, mime: str) -> None:
    assert client.post(multipart(AUDIO, mime))[0] == 200


def test_le_transcriber_recoit_audio_et_type(client: Client) -> None:
    seen: list[tuple[bytes, str]] = []

    class Spy:
        def transcribe(self, audio: bytes, mime: str) -> str:
            seen.append((audio, mime))
            return "ok"

    for c in serve(Spy()):
        assert c.post(multipart(AUDIO, "audio/ogg;codecs=opus"))[0] == 200
    assert seen == [(AUDIO, "audio/ogg")]


# --- Scénario : audio trop gros ---
def test_audio_2_mo_refuse_sans_transcription(
    client: Client, mock: transcriber.MockTranscriber
) -> None:
    status, _, body = client.post(multipart(b"\0" * (2 * 1024 * 1024)))
    assert status == 413
    assert body == {"error": "audio_trop_gros"}
    assert VALIDATOR.is_valid(body)
    assert mock.calls == 0


def test_limite_exacte_1_mo(client: Client, mock: transcriber.MockTranscriber) -> None:
    assert client.post(multipart(b"\0" * transcriber.MAX_AUDIO_BYTES))[0] == 200
    assert client.post(multipart(b"\0" * (transcriber.MAX_AUDIO_BYTES + 1)))[0] == 413
    assert mock.calls == 1


# --- Erreurs : jamais d'écho du contenu ---
def test_type_non_supporte(client: Client, mock: transcriber.MockTranscriber) -> None:
    status, _, body = client.post(multipart(AUDIO, "text/html"))
    assert (status, body) == (415, {"error": "type_non_supporte"})
    assert mock.calls == 0


@pytest.mark.parametrize(
    ("body", "ctype", "code"),
    [
        (multipart(None), f"multipart/form-data; boundary={BOUNDARY}", "audio_manquant"),
        (
            multipart(AUDIO, field="autre"),
            f"multipart/form-data; boundary={BOUNDARY}",
            "audio_manquant",
        ),
        (multipart(b""), f"multipart/form-data; boundary={BOUNDARY}", "audio_vide"),
        (b"{}", "application/json", "requete_invalide"),
        (b"", None, "requete_invalide"),
    ],
)
def test_requetes_invalides(client: Client, body: bytes, ctype: str | None, code: str) -> None:
    status, _, resp = client.post(body, ctype)
    assert (status, resp) == (400, {"error": code})
    assert VALIDATOR.is_valid(resp)


def test_transcriber_en_panne_sans_echo() -> None:
    for c in serve(Boom()):
        status, _, body = c.post(multipart(AUDIO))
        assert (status, body) == (500, {"error": "transcription_echouee"})
        assert "secret" not in json.dumps(body)


def test_sans_transcriber_503() -> None:
    for c in serve(None):
        status, _, body = c.post(multipart(AUDIO))
        assert (status, body) == (503, {"error": "asr_indisponible"})
        assert VALIDATOR.is_valid(body)


def test_autre_chemin_en_post_404(client: Client) -> None:
    status, _, body = client.post(multipart(AUDIO), path="/api/autre")
    assert (status, body) == (404, {"error": "introuvable"})


# --- Scénario : aucune trace de l'enfant ---
def test_aucune_trace_logs_sortie_et_disque(
    client: Client,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    caplog.set_level(logging.DEBUG)
    assert client.post(multipart(AUDIO))[0] == 200
    assert client.post(multipart(b"\0" * (2 * 1024 * 1024)))[0] == 413
    out = capfd.readouterr()
    for trace in (caplog.text, out.out, out.err):
        assert TEXTE not in trace
        assert "secret" not in trace
        assert "audio" not in trace.lower()
    assert list(tmp_path.iterdir()) == []


def test_code_sans_ecriture_disque_ni_journal() -> None:
    for f in (APP / "transcriber.py", APP / "server.py"):
        src = f.read_text("utf-8")
        assert not re.search(
            r"\bopen\(|write_bytes|NamedTemporaryFile|import logging|print\(.*audio", src
        ), f.name


def test_mock_ne_conserve_pas_l_audio() -> None:
    m = transcriber.MockTranscriber(TEXTE)
    assert m.transcribe(AUDIO, "audio/webm") == TEXTE
    assert not any(isinstance(v, bytes | bytearray) for v in vars(m).values())


# --- Scénario : aucun service tiers (test statique) ---
def sources() -> list[Path]:
    return [*STATIC.glob("*.js"), *STATIC.glob("*.html"), APP / "transcriber.py", APP / "server.py"]


def test_aucun_service_de_reconnaissance_tiers() -> None:
    for f in sources():
        assert not re.search(r"speechrecognition", f.read_text("utf-8"), re.I), f.name


def test_aucune_url_externe() -> None:
    for f in sources():
        for url in re.findall(r"https?://[^\s\"')]+", f.read_text("utf-8")):
            ok = url.startswith("http://www.w3.org") or re.match(r"https://127\.0\.0\.1[:/]", url)
            assert ok, (f.name, url)


def test_dictation_js_local_et_sans_stockage() -> None:
    js = (STATIC / "dictation.js").read_text("utf-8")
    assert "MediaRecorder" in js
    assert '"/api/dictation"' in js
    assert not re.search(r"innerHTML|localStorage|sessionStorage|indexedDB|caches\.|\.href\s*=", js)
    assert "textContent" in js


def test_dictation_js_charge_par_la_coquille() -> None:
    assert '<script src="/dictation.js"></script>' in (STATIC / "index.html").read_text("utf-8")


def test_service_worker_ne_met_pas_l_audio_en_cache() -> None:
    sw = (STATIC / "sw.js").read_text("utf-8")
    assert "api/dictation" not in sw
    assert 'req.method !== "GET"' in sw


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_dictation_js_syntaxe_valide() -> None:
    r = subprocess.run(  # noqa: S603
        ["node", "--check", str(STATIC / "dictation.js")],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr


# --- Contrat contracts/dictation.schema.json ---
def test_schema_valide_et_refuse_le_superflu() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert VALIDATOR.is_valid({"text": "salut"})
    assert VALIDATOR.is_valid({"error": "audio_vide"})
    assert not VALIDATOR.is_valid({"text": "salut", "error": "x"})
    assert not VALIDATOR.is_valid({"text": "salut", "audio": "AAAA"})
    assert not VALIDATOR.is_valid({"error": "Message libre avec espaces"})
    assert not VALIDATOR.is_valid({})


def test_texte_tronque_au_maximum_du_contrat() -> None:
    for c in serve(transcriber.MockTranscriber("a" * 5000)):
        body = c.post(multipart(AUDIO))[2]
        assert VALIDATOR.is_valid(body)
        assert len(body["text"]) == 2000


# --- --mock-asr : démo seulement, défaut = 503 ---
def test_mock_asr_option_demo_seulement() -> None:
    assert server.make_transcriber(server.parse_args([])) is None
    mock = server.make_transcriber(server.parse_args(["--mock-asr"]))
    assert isinstance(mock, transcriber.MockTranscriber)


def test_readme_documente_mock_asr_comme_demo() -> None:
    readme = (APP / "README.md").read_text("utf-8")
    assert "--mock-asr" in readme
    assert "démo" in readme.lower()


# --- dictation.js : robustesse (tests statiques, pas de navigateur en CI) ---
def dictation_js() -> str:
    return (STATIC / "dictation.js").read_text("utf-8")


def test_js_libere_le_micro_si_mediarecorder_echoue() -> None:
    js = dictation_js()
    m = re.search(r"try\s*\{[^}]*new MediaRecorder[^}]*\}\s*catch[^{]*\{[^}]*getTracks\(\)", js)
    assert m, "new MediaRecorder doit être dans un try/catch qui coupe les pistes"


def test_js_garde_d_identite_d_ecran_apres_reponse() -> None:
    js = dictation_js()
    assert js.count("ecran === mon") >= 2  # envoi (réponse) et résultat


def test_js_ferme_l_ecoute_si_le_reseau_change() -> None:
    js = dictation_js()
    assert re.search(r'addEventListener\("online",\s*fermerEcoute', js)
    assert re.search(r'addEventListener\("offline"', js)
    assert "fermerEcoute();" in js.split('"offline"', 1)[1].split("});", 1)[0]


def test_js_type_audio_jamais_vide() -> None:
    js = dictation_js()
    assert re.search(r"recorder\.mimeType\s*\|\|\s*type\s*\|\|\s*\"audio/webm\"", js)
