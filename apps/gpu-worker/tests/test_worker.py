"""gpu-worker (#33) : jobs image -> clip, ComfyUI simulé (aucun appel réseau vers le PC GPU)."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import comfy_run
import pytest
import worker
from jsonschema import Draft202012Validator

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
TIMEOUT = 5.0


def _load(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


class FakeComfy:
    """Simule ComfyUI : compte les rendus ; `gate` permet de tenir un job en cours."""

    def __init__(self, block: bool = False, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.gate = threading.Event()
        self.started = threading.Event()
        self.fail = fail
        if not block:
            self.gate.set()

    def __call__(self, req: worker.JobRequest) -> str:
        self.calls.append(req.job_id)
        self.started.set()
        assert self.gate.wait(TIMEOUT), "gate jamais ouverte"
        if self.fail:
            raise RuntimeError("ComfyUI en erreur : brut")
        return f"/out/{req.job_id}/clip.mp4"


Server = Callable[[FakeComfy], str]


@pytest.fixture
def serve(tmp_path: Path) -> Iterator[Server]:
    """Démarre un worker ; chaque appel simule un redémarrage sur le même dossier out/."""
    started: list[tuple[worker.JobQueue, Any]] = []

    def _serve(comfy: FakeComfy) -> str:
        jobs = worker.JobQueue(comfy, tmp_path)
        jobs.start()
        server = worker.make_server(jobs, port=0, networks=("127.0.0.0/8",))
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        started.append((jobs, server))
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    yield _serve
    for jobs, server in started:
        server.shutdown()
        server.server_close()
        jobs.stop(TIMEOUT)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    data: dict[str, Any] = _load("gpu-job.schema.json")
    Draft202012Validator.check_schema(data)
    return data


def _job(job_id: str = "story-1-plan-1", **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = _load("examples/gpu-job.valid.json")
    data["job_id"] = job_id
    data.update(over)
    return data


def _post(base: str, payload: object) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        f"{base}/jobs",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _get(base: str, job_id: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{base}/jobs/{job_id}", timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def _wait_status(base: str, job_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        body = _get(base, job_id)[1]
        if body.get("status") == status:
            return body
        time.sleep(0.01)
    raise AssertionError(f"{job_id} jamais {status}")


# --- Contrat contracts/gpu-job.schema.json ---


def test_schema_is_versioned_draft_2020_12(schema: dict[str, Any]) -> None:
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "schema_version" in schema["required"]
    assert schema["additionalProperties"] is False


def test_examples_match_schema_and_worker(schema: dict[str, Any]) -> None:
    v = Draft202012Validator(schema)
    valid, invalid = _load("examples/gpu-job.valid.json"), _load("examples/gpu-job.invalid.json")
    assert list(v.iter_errors(valid)) == [] and worker.validate(valid)[1] == []
    assert not v.is_valid(invalid) and worker.validate(invalid)[1]


def test_worker_frames_rule_is_the_schema_enum(schema: dict[str, Any]) -> None:
    allowed = [f for f in range(0, 200) if not worker.validate(_job(frames=f))[1]]
    assert allowed == schema["properties"]["frames"]["enum"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_path", "../secret.png"),
        ("image_path", "/etc/x.png"),
        ("image_path", 'a".png'),
        ("job_id", "a/b"),
        ("type", "text_to_image"),
        ("schema_version", "2.0"),
        ("motion_prompt", ""),
        ("frames", True),
        ("frames", "49"),
        ("extra", "x"),
    ],
)
def test_worker_and_schema_refuse_the_same_inputs(
    schema: dict[str, Any], field: str, value: object
) -> None:
    payload = _job(**{field: value})
    assert not Draft202012Validator(schema).is_valid(payload)
    assert worker.validate(payload)[1]


# --- Scénarios Gherkin ---


def test_job_nominal_goes_from_queued_to_done(
    serve: Server, schema: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="gpu_worker")
    comfy = FakeComfy(block=True)
    base = serve(comfy)
    status_v = Draft202012Validator(schema["$defs"]["JobStatus"])

    code, body = _post(base, _job())
    assert (code, body["status"]) == (202, "queued")
    comfy.gate.set()
    done = _wait_status(base, "story-1-plan-1", "done")

    assert done["output_path"] == "/out/story-1-plan-1/clip.mp4"
    assert list(status_v.iter_errors(body)) == [] and list(status_v.iter_errors(done)) == []
    assert "story-1-plan-1" in caplog.text
    assert _job()["motion_prompt"] not in caplog.text
    assert _job()["image_path"] not in caplog.text


def test_same_job_id_twice_renders_one_clip(serve: Server) -> None:
    comfy = FakeComfy()
    base = serve(comfy)
    assert _post(base, _job())[0] == 202
    assert _post(base, _job())[0] == 202
    _wait_status(base, "story-1-plan-1", "done")
    assert _post(base, _job())[1]["status"] == "done"
    assert comfy.calls == ["story-1-plan-1"]


def test_frames_not_4n_plus_1_is_422_and_nothing_runs(serve: Server) -> None:
    comfy = FakeComfy()
    base = serve(comfy)
    code, body = _post(base, _job(frames=48))
    assert code == 422
    assert any("frames" in e for e in body["errors"])
    assert _get(base, "story-1-plan-1")[0] == 404
    assert comfy.calls == []


def test_second_job_stays_queued_until_first_ends(serve: Server) -> None:
    comfy = FakeComfy(block=True)
    base = serve(comfy)
    _post(base, _job("a"))
    _post(base, _job("b"))
    assert comfy.started.wait(TIMEOUT)
    _wait_status(base, "a", "running")
    time.sleep(0.05)
    assert _get(base, "b")[1]["status"] == "queued"
    assert comfy.calls == ["a"]
    comfy.gate.set()
    _wait_status(base, "b", "done")
    assert comfy.calls == ["a", "b"]


# --- Bords ---


def test_comfy_error_is_generic_and_failed_job_is_retried(serve: Server) -> None:
    comfy = FakeComfy(fail=True)
    base = serve(comfy)
    _post(base, _job())
    failed = _wait_status(base, "story-1-plan-1", "failed")
    assert failed["error"] == "RuntimeError: échec du rendu ComfyUI"
    comfy.fail = False
    assert _post(base, _job())[1]["status"] == "queued"
    _wait_status(base, "story-1-plan-1", "done")
    assert comfy.calls == ["story-1-plan-1", "story-1-plan-1"]


def test_idempotence_survives_restart(
    serve: Server, tmp_path: Path, schema: dict[str, Any]
) -> None:
    first = FakeComfy()
    base = serve(first)
    _post(base, _job())
    _wait_status(base, "story-1-plan-1", "done")
    state = json.loads((tmp_path / "story-1-plan-1" / "state.json").read_text())
    assert list(Draft202012Validator(schema["$defs"]["JobStatus"]).iter_errors(state)) == []

    again = FakeComfy()
    base = serve(again)
    assert _get(base, "story-1-plan-1")[1]["status"] == "done"
    assert _post(base, _job())[1]["status"] == "done"
    assert again.calls == []


def test_job_interrupted_by_restart_is_failed_then_retried(
    serve: Server, tmp_path: Path
) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "state.json").write_text('{"job_id": "x", "status": "running"}')
    comfy = FakeComfy()
    base = serve(comfy)
    interrupted = {"job_id": "x", "status": "failed", "error": "Interrupted: worker redémarré"}
    assert _get(base, "x")[1] == interrupted
    _post(base, _job("x"))
    _wait_status(base, "x", "done")
    assert comfy.calls == ["x"]


@pytest.mark.parametrize("length", ["abc", "-1"])
def test_bad_content_length_is_422(serve: Server, length: str) -> None:
    host, port = serve(FakeComfy()).removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=TIMEOUT)
    try:
        conn.putrequest("POST", "/jobs")
        conn.putheader("Content-Length", length)
        conn.endheaders()
        assert conn.getresponse().status == 422
    finally:
        conn.close()


def test_unknown_job_and_bad_json(serve: Server) -> None:
    base = serve(FakeComfy())
    assert _get(base, "inconnu")[0] == 404
    req = urllib.request.Request(f"{base}/jobs", data=b"{pas du json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=TIMEOUT)
    assert err.value.code == 422


def test_server_listens_on_loopback_only() -> None:
    jobs = worker.JobQueue(FakeComfy(), Path("unused"))
    server = worker.make_server(jobs, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_comfy_runner_maps_job_to_comfy_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(
        workflow: str,
        prompt: str,
        seed: int,
        out_dir: str,
        url: str,
        image: str = "",
        audio: str = "",
        length: int = 0,
    ) -> list[Path]:
        if prompt == "png":
            return [Path(out_dir) / "frame.png"]
        seen.update(workflow=workflow, prompt=prompt, out=out_dir, url=url, image=image, n=length)
        return [Path(out_dir) / "clip.mp4"]

    monkeypatch.setattr(worker.comfy_run, "run", fake_run)
    runner = worker.ComfyRunner("http://comfy.test", Path("wf.json"), Path("/o"))
    req = worker.validate(_job())[0]
    assert req is not None
    assert runner(req) == "/o/story-1-plan-1/clip.mp4"
    assert seen == {
        "workflow": "wf.json",
        "prompt": _job()["motion_prompt"],
        "out": "/o/story-1-plan-1",
        "url": "http://comfy.test",
        "image": _job()["image_path"],
        "n": 49,
    }
    with pytest.raises(RuntimeError):
        runner(worker.JobRequest("j", "a.png", "png", 49))


def _fake_comfy_http(history: dict[str, Any]) -> Callable[..., dict[str, Any]]:
    def fake(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"prompt_id": "p"} if url.endswith("/prompt") else history

    return fake


def _workflow(tmp_path: Path) -> str:
    wf = tmp_path / "wf.json"
    wf.write_text('{"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}')
    return str(wf)


def test_comfy_run_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comfy_run, "http_json", _fake_comfy_http({}))
    monkeypatch.setattr(comfy_run.time, "sleep", lambda _s: None)
    assert comfy_run.DEFAULT_TIMEOUT_S == 15 * 60
    with pytest.raises(TimeoutError):
        comfy_run.run(_workflow(tmp_path), "p", 1, str(tmp_path / "o"), "http://c", timeout=0)


class _Body:
    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return b"mp4"


def test_comfy_run_prints_nothing_and_keeps_file_names_inside_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = {"filename": "../evil.mp4", "subfolder": "", "type": "output"}
    hist = {"p": {"status": {"completed": True}, "outputs": {"12": {"videos": [item]}}}}
    monkeypatch.setattr(comfy_run, "http_json", _fake_comfy_http(hist))
    monkeypatch.setattr(comfy_run.urllib.request, "urlopen", lambda *_a, **_k: _Body())
    saved = comfy_run.run(_workflow(tmp_path), "secret", 1, str(tmp_path / "o"), "http://c")
    assert saved == [tmp_path / "o" / "evil.mp4"]
    assert capsys.readouterr().out == ""
