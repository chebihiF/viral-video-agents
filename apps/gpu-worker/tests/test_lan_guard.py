"""lan_guard (#158) : le PC GPU n'est joignable que depuis le réseau local (aucun appel réel à pc-gpu)."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import lan_guard
import pytest
import worker

TIMEOUT = 5.0
MESSAGE = "Je te génère la vidéo une fois à la maison."
DICTEE = "dictée-secrète-de-l-enfant"
HOME = ("127.0.0.0/8",)  # le client des tests est 127.0.0.1 : on le déclare « maison » ou non
ELSEWHERE = ("192.168.1.0/24",)


class FakeComfy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, req: worker.JobRequest) -> str:
        self.calls.append(req.job_id)
        return f"/out/{req.job_id}/clip.mp4"


@pytest.fixture
def pc_gpu_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Mock de pc-gpu : tout lancement de processus est enregistré (et ne part pas)."""
    calls: list[object] = []

    def fake(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("pc-gpu ne doit jamais être appelé")

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(subprocess, "Popen", fake)
    return calls


def _serve(tmp_path: Path, networks: tuple[str, ...]) -> Iterator[tuple[str, FakeComfy]]:
    comfy = FakeComfy()
    jobs = worker.JobQueue(comfy, tmp_path)
    jobs.start()
    server = worker.make_server(jobs, port=0, networks=networks)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", comfy
    server.shutdown()
    server.server_close()
    jobs.stop(TIMEOUT)


@pytest.fixture
def home(tmp_path: Path) -> Iterator[tuple[str, FakeComfy]]:
    yield from _serve(tmp_path, HOME)


@pytest.fixture
def away(tmp_path: Path) -> Iterator[tuple[str, FakeComfy]]:
    yield from _serve(tmp_path, ELSEWHERE)


def _post(base: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    payload = {
        "schema_version": "1.0",
        "job_id": "story-1-plan-1",
        "type": "image_to_clip",
        "image_path": "img/a.png",
        "motion_prompt": DICTEE,
        "frames": 5,
    }
    req = urllib.request.Request(
        f"{base}/jobs",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


# --- is_local ---


@pytest.mark.parametrize("ip", ["192.168.1.42", "192.168.1.1", "192.168.1.254"])
def test_home_addresses_are_local(ip: str) -> None:
    assert lan_guard.is_local(ip) is True


@pytest.mark.parametrize(
    "ip",
    ["82.10.20.30", "192.168.2.1", "10.0.0.1", "", "abc", "::1", "999.1.1.1", "192.168.1.42/24", " ", "fe80::1"],
)
def test_other_or_invalid_addresses_are_not_local(ip: str) -> None:
    assert lan_guard.is_local(ip) is False


@pytest.mark.parametrize("ip", [None, 42, b"192.168.1.42"])
def test_absent_or_non_string_ip_is_not_local(ip: Any) -> None:
    assert lan_guard.is_local(ip) is False


def test_ipv6_in_configured_list_is_local() -> None:
    assert lan_guard.is_local("::1", networks=("::1/128",)) is True
    assert lan_guard.is_local("::1", networks=("192.168.1.0/24",)) is False


def test_ipv4_mapped_ipv6_is_matched_as_ipv4() -> None:
    assert lan_guard.is_local("::ffff:192.168.1.42") is True
    assert lan_guard.is_local("::ffff:82.10.20.30") is False


def test_bad_network_in_config_fails_closed() -> None:
    assert lan_guard.is_local("192.168.1.42", networks=("pas-un-réseau",)) is False


def test_message_is_exact() -> None:
    assert lan_guard.MSG_HORS_RESEAU == MESSAGE


# --- Soumission d'un job ---


def test_client_at_home_job_is_accepted_and_queued(home: tuple[str, FakeComfy]) -> None:
    status, body = _post(home[0])
    assert status == 202
    assert body["status"] in {"queued", "running", "done"}


def test_client_outside_gets_deferred_and_nothing_runs(
    away: tuple[str, FakeComfy], tmp_path: Path, pc_gpu_calls: list[object]
) -> None:
    status, body = _post(away[0])
    assert (status, body) == (200, {"status": "deferred", "message": MESSAGE})
    assert away[1].calls == []
    assert pc_gpu_calls == []
    assert list(tmp_path.iterdir()) == []  # aucun job créé ni stocké


def test_forged_x_forwarded_for_is_ignored(away: tuple[str, FakeComfy]) -> None:
    status, body = _post(away[0], {"X-Forwarded-For": "192.168.1.5", "Forwarded": "for=192.168.1.5"})
    assert (status, body["status"]) == (200, "deferred")
    assert away[1].calls == []


def test_deferred_reply_and_logs_never_contain_the_dictation(
    away: tuple[str, FakeComfy], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _, body = _post(away[0])
    assert DICTEE not in json.dumps(body)
    assert DICTEE not in caplog.text
