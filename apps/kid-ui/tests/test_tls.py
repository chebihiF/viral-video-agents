"""HTTPS local de l'appli enfant (#160) : démarrage TLS, refus du HTTP nu, écoute locale."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest
import server

OPENSSL = shutil.which("openssl") or "openssl"
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl absent")


@pytest.fixture(scope="module")
def pems(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Certificat auto-signé jetable (SAN 127.0.0.1), généré dans tmp, jamais dans le dépôt."""
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(  # noqa: S603
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=kid-ui-test", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


def test_demarrage_https_repond_200(pems: tuple[Path, Path]) -> None:
    cert, key = pems
    httpd = server.make_server("127.0.0.1", 0, tls_cert=str(cert), tls_key=str(key))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context(cafile=str(cert))  # la « CA locale » = le certificat
        url = f"https://127.0.0.1:{httpd.server_address[1]}/"
        with urllib.request.urlopen(url, context=ctx, timeout=5) as r:  # noqa: S310
            assert r.status == 200
            assert b"<html" in r.read().lower()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_https_refuse_le_http_nu(pems: tuple[Path, Path]) -> None:
    cert, key = pems
    httpd = server.make_server("127.0.0.1", 0, tls_cert=str(cert), tls_key=str(key))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        with pytest.raises(OSError):  # noqa: PT011
            urllib.request.urlopen(url, timeout=5)  # noqa: S310
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_refus_http_sans_insecure_dev(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        server.main(["--port", "0"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "refus de démarrer en HTTP" in err
    assert "--tls-cert" in err
    assert "--insecure-dev" in err


def test_refus_si_un_seul_fichier_tls(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        server.main(["--tls-cert", "cert.pem"])
    assert e.value.code == 2
    assert "vont ensemble" in capsys.readouterr().err


def test_refus_si_certificat_introuvable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--tls-cert", str(tmp_path / "a.pem"), "--tls-key", str(tmp_path / "b.pem")]
    with pytest.raises(SystemExit) as e:
        server.main(args)
    assert e.value.code == 2
    assert "introuvable" in capsys.readouterr().err


def test_insecure_dev_autorise_le_http() -> None:
    assert server.validate(server.parse_args(["--insecure-dev"])) is None


def test_ecoute_locale_par_defaut_et_host_configurable() -> None:
    a = server.parse_args(["--insecure-dev"])
    assert a.host == "127.0.0.1"
    assert server.parse_args(["--host", "192.168.1.20"]).host == "192.168.1.20"


def test_connexion_muette_ne_bloque_pas_le_serveur(pems: tuple[Path, Path]) -> None:
    cert, key = pems
    httpd = server.make_server("127.0.0.1", 0, tls_cert=str(cert), tls_key=str(key))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    mute = socket.create_connection(("127.0.0.1", port))  # se connecte, ne dit rien
    try:
        ctx = ssl.create_default_context(cafile=str(cert))
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/", context=ctx, timeout=5) as r:  # noqa: S310
            assert r.status == 200
    finally:
        mute.close()
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.168.1.20", "192.168.0.5"])
def test_host_permis(host: str) -> None:
    assert server.validate(server.parse_args(["--insecure-dev", "--host", host])) is None


HOSTS_REFUSES = [
    "0.0.0.0",  # noqa: S104
    "::",
    "8.8.8.8",
    "10.0.0.2",
    "172.16.0.1",
    "192.169.1.1",
    "kid.local",
]


@pytest.mark.parametrize("host", HOSTS_REFUSES)
def test_host_refuse(host: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        server.main(["--insecure-dev", "--host", host])
    assert e.value.code == 2
    assert "--host" in capsys.readouterr().err
