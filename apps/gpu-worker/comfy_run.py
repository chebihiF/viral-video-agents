#!/usr/bin/env python3
"""Lance un workflow ComfyUI (format API) et attend le résultat.

Usage : comfy_run.py WORKFLOW.json "prompt" [--seed N] [--out DOSSIER] [--url http://127.0.0.1:8188]

Le workflow peut contenir les marqueurs {{PROMPT}} ; le seed du premier KSampler est remplacé.
Bibliothèque standard uniquement, pour tourner tel quel sur le PC GPU.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def http_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def free_ollama_vram(ollama_url: str = "http://127.0.0.1:11434") -> None:
    """Décharge les modèles Ollama pour libérer la VRAM avant une génération."""
    try:
        for m in http_json(f"{ollama_url}/api/ps").get("models", []):
            http_json(f"{ollama_url}/api/generate", {"model": m["name"], "keep_alive": 0})
    except Exception:
        pass  # Ollama absent ou arrêté : rien à libérer


def run(workflow_path: str, prompt: str, seed: int, out_dir: str, url: str, image: str = "",
        audio: str = "", length: int = 0) -> list[Path]:
    raw = Path(workflow_path).read_text().replace("{{PROMPT}}", json.dumps(prompt)[1:-1])
    raw = raw.replace("{{IMAGE}}", image)  # nom d'un fichier présent dans ComfyUI/input
    raw = raw.replace("{{AUDIO}}", audio)  # idem, pour les workflows audio
    wf_tmp = json.loads(raw)
    if length:
        for node in wf_tmp.values():
            if "length" in node["inputs"] and isinstance(node["inputs"]["length"], int) and node["class_type"] != "ImageFromBatch":
                node["inputs"]["length"] = length
    raw = json.dumps(wf_tmp)
    wf = json.loads(raw)
    for node in wf.values():
        if node["class_type"] == "KSampler":
            node["inputs"]["seed"] = seed
            break

    free_ollama_vram()
    client_id = uuid.uuid4().hex
    prompt_id = http_json(f"{url}/prompt", {"prompt": wf, "client_id": client_id})["prompt_id"]

    start = time.time()
    while True:
        hist = http_json(f"{url}/history/{prompt_id}").get(prompt_id)
        if hist and hist.get("status", {}).get("completed"):
            break
        if hist and hist.get("status", {}).get("status_str") == "error":
            raise RuntimeError(json.dumps(hist["status"], indent=2)[:2000])
        time.sleep(1)
    elapsed = time.time() - start

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for node_out in hist["outputs"].values():
        items = [i for v in node_out.values() if isinstance(v, list) for i in v if isinstance(i, dict) and "filename" in i]
        for item in items:
            q = urllib.parse.urlencode({k: item[k] for k in ("filename", "subfolder", "type")})
            dest = out / item["filename"]
            with urllib.request.urlopen(f"{url}/view?{q}") as r:
                dest.write_bytes(r.read())
            saved.append(dest)
    print(f"Terminé en {elapsed:.1f} s -> {', '.join(map(str, saved))}")
    return saved


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("workflow")
    p.add_argument("prompt")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="out")
    p.add_argument("--url", default="http://127.0.0.1:8188")
    p.add_argument("--image", default="", help="image de départ (fichier dans ComfyUI/input)")
    p.add_argument("--audio", default="", help="fichier audio (dans ComfyUI/input)")
    p.add_argument("--length", type=int, default=0, help="nombre d'images (4n+1), remplace celui du workflow")
    a = p.parse_args()
    run(a.workflow, a.prompt, a.seed, a.out, a.url, a.image, a.audio, a.length)
