#!/usr/bin/env python3
"""Service de jobs image -> clip (Wan 2.2) appelé par n8n (contrat : contracts/gpu-job.schema.json).

Usage : worker.py [--port 8765] [--comfy-url http://127.0.0.1:8188] [--workflow ...] [--out DOSSIER]

Écoute 127.0.0.1 uniquement ; un seul job GPU à la fois ; idempotence par job_id.
Les journaux ne contiennent que le job_id (jamais de prompt ni de chemin : données d'enfant).
Hors réseau local (lan_guard, adresse du socket seule) : réponse « deferred », rien n'est lu ni mis en file.
Bibliothèque standard uniquement, pour tourner tel quel sur le PC GPU.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import comfy_run
import lan_guard

HOST = "127.0.0.1"
MAX_BODY = 64 * 1024
FRAMES_MIN, FRAMES_MAX = 5, 121
LENGTH_RE = re.compile(r"[0-9]{1,9}")
INTERRUPTED = "Interrupted: worker redémarré"
JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
IMAGE_RE = re.compile(r"(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}")
FIELDS = {"schema_version", "job_id", "type", "image_path", "motion_prompt", "frames"}
WORKFLOW = Path(__file__).resolve().parents[2] / "comfyui" / "workflows" / "wan22_5b_i2v_api.json"

log = logging.getLogger("gpu_worker")


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    image_path: str
    motion_prompt: str
    frames: int


ClipRunner = Callable[[JobRequest], str]


def _field_errors(p: dict[str, Any]) -> list[str]:
    def is_str(v: object, rx: re.Pattern[str] | None = None, max_len: int = 2000) -> bool:
        ok = isinstance(v, str) and 0 < len(v) <= max_len
        return ok and (rx is None or rx.fullmatch(str(v)) is not None)

    f = p.get("frames")
    checks = {
        "schema_version": p.get("schema_version") == "1.0",
        "type": p.get("type") == "image_to_clip",
        "job_id": is_str(p.get("job_id"), JOB_ID_RE),
        "image_path": is_str(p.get("image_path"), IMAGE_RE),
        "motion_prompt": is_str(p.get("motion_prompt")),
        "frames": type(f) is int and FRAMES_MIN <= f <= FRAMES_MAX and (f - 1) % 4 == 0,
    }
    return [f"{name} : invalide" for name, ok in checks.items() if not ok]


def validate(payload: object) -> tuple[JobRequest | None, list[str]]:
    """Vérifie un job selon contracts/gpu-job.schema.json (sans dépendance)."""
    if not isinstance(payload, dict):
        return None, ["corps : objet JSON attendu"]
    errors = [f"{k} : propriété inconnue" for k in sorted(set(payload) - FIELDS)]
    errors += _field_errors(payload)
    if errors:
        return None, errors
    req = JobRequest(
        payload["job_id"], payload["image_path"], payload["motion_prompt"], payload["frames"]
    )
    return req, []


class JobQueue:
    """File à un seul exécutant : un job GPU à la fois, dans l'ordre d'arrivée.

    L'état de chaque job est persisté dans out_root/<job_id>/state.json et relu à la demande :
    l'idempotence survit au redémarrage ; un job resté queued/running est vu comme interrompu.
    """

    def __init__(self, runner: ClipRunner, out_root: Path) -> None:
        self._runner = runner
        self._out_root = out_root
        self._jobs: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._pending: queue.Queue[JobRequest | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="gpu-job", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._pending.put(None)
        self._thread.join(timeout)

    def submit(self, req: JobRequest) -> dict[str, str]:
        """Met le job en file ; un job_id connu renvoie son état, sauf failed qui est relancé."""
        with self._lock:
            known = self._known(req.job_id)
            if known is not None and known["status"] != "failed":
                return dict(known)
            job = self._store(req.job_id, "queued")
            self._pending.put(req)
        log.info("job %s queued", req.job_id)
        return job

    def get(self, job_id: str) -> dict[str, str] | None:
        if JOB_ID_RE.fullmatch(job_id) is None:
            return None
        with self._lock:
            job = self._known(job_id)
            return dict(job) if job is not None else None

    def _known(self, job_id: str) -> dict[str, str] | None:
        """État en mémoire, sinon relu sur disque (verrou tenu par l'appelant)."""
        job = self._jobs.get(job_id) or self._read_state(job_id)
        if job is not None:
            self._jobs[job_id] = job
        return job

    def _state_file(self, job_id: str) -> Path:
        return self._out_root / job_id / "state.json"

    def _read_state(self, job_id: str) -> dict[str, str] | None:
        try:
            data = json.loads(self._state_file(job_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("job_id") != job_id:
            return None
        if data.get("status") not in ("done", "failed"):  # processus précédent interrompu
            return {"job_id": job_id, "status": "failed", "error": INTERRUPTED}
        return {k: str(v) for k, v in data.items()}

    def _store(self, job_id: str, status: str, **extra: str) -> dict[str, str]:
        """Enregistre l'état en mémoire et sur disque (écriture atomique)."""
        job = {"job_id": job_id, "status": status, **extra}
        self._jobs[job_id] = job
        path = self._state_file(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job), encoding="utf-8")
        os.replace(tmp, path)
        return dict(job)

    def _set(self, job_id: str, status: str, **extra: str) -> None:
        with self._lock:
            self._store(job_id, status, **extra)
        log.info("job %s %s", job_id, status)

    def _loop(self) -> None:
        while (req := self._pending.get()) is not None:
            self._set(req.job_id, "running")
            try:
                self._set(req.job_id, "done", output_path=self._runner(req))
            except Exception as exc:  # le worker survit à tout échec de rendu
                # jamais le texte brut de ComfyUI (il peut citer le prompt)
                self._set(req.job_id, "failed", error=f"{type(exc).__name__}: échec du rendu ComfyUI")


class ComfyRunner:
    """Rendu réel via comfy_run (ComfyUI sur le PC GPU)."""

    def __init__(self, url: str, workflow: Path, out_root: Path) -> None:
        self.url, self.workflow, self.out_root = url, workflow, out_root

    def __call__(self, req: JobRequest) -> str:
        out_dir = self.out_root / req.job_id
        saved = comfy_run.run(
            str(self.workflow),
            req.motion_prompt,
            42,
            str(out_dir),
            self.url,
            image=req.image_path,
            length=req.frames,
        )
        clips = [p for p in saved if p.suffix == ".mp4"]
        if not clips:
            raise RuntimeError("ComfyUI n'a produit aucun .mp4")
        return str(clips[0])


def _handler(jobs: JobQueue, networks: tuple[str, ...]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            """Pas de journal d'accès : seul le job_id est journalisé, par JobQueue."""

        def _reply(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if self.path != "/jobs":
                return self._reply(404, {"error": "route inconnue"})
            if not lan_guard.is_local(self.client_address[0], networks):
                return self._reply(
                    200, {"status": "deferred", "message": lan_guard.MSG_HORS_RESEAU}
                )
            raw = self.headers.get("Content-Length") or "0"
            if LENGTH_RE.fullmatch(raw) is None:
                return self._reply(422, {"errors": ["Content-Length : invalide"]})
            length = int(raw)
            if length > MAX_BODY:
                return self._reply(422, {"errors": ["corps : trop volumineux"]})
            try:
                payload = json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                return self._reply(422, {"errors": ["corps : JSON invalide"]})
            req, errors = validate(payload)
            if req is None:
                return self._reply(422, {"errors": errors})
            self._reply(202, jobs.submit(req))

        def do_GET(self) -> None:
            job_id = self.path.removeprefix("/jobs/")
            job = jobs.get(job_id) if self.path.startswith("/jobs/") else None
            if job is None:
                return self._reply(404, {"error": "job inconnu"})
            self._reply(200, job)

    return Handler


def make_server(
    jobs: JobQueue, port: int = 8765, networks: tuple[str, ...] = lan_guard.LAN_NETWORKS
) -> ThreadingHTTPServer:
    """Serveur HTTP lié à 127.0.0.1 uniquement (WireGuard viendra plus tard)."""
    return ThreadingHTTPServer((HOST, port), _handler(jobs, networks))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    p.add_argument("--workflow", type=Path, default=WORKFLOW)
    p.add_argument("--out", type=Path, default=Path("out/clips"))
    p.add_argument(
        "--lan", nargs="+", default=list(lan_guard.LAN_NETWORKS), help="réseaux autorisés (CIDR)"
    )
    return p


def main() -> None:
    a = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = a.out.resolve()
    jobs = JobQueue(ComfyRunner(a.comfy_url, a.workflow, out), out)
    jobs.start()
    server = make_server(jobs, a.port, tuple(a.lan))
    log.info("gpu-worker sur %s:%s", HOST, a.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        jobs.stop()


if __name__ == "__main__":
    main()
