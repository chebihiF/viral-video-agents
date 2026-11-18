# gpu-worker

Scripts exécutés sur le PC GPU (ComfyUI, Ollama, TTS). Bibliothèque standard uniquement.

- `comfy_run.py` : lance un workflow ComfyUI via son API (CLI, réutilisé par le worker).
- `worker.py` : service de jobs image -> clip (Wan 2.2) appelé par n8n ; contrat
  [`contracts/gpu-job.schema.json`](../../contracts/gpu-job.schema.json).

## worker.py

```sh
~/.local/bin/pc-gpu wake        # d'abord ; ne jamais mettre le PC en veille
python3 apps/gpu-worker/worker.py --port 8765 --comfy-url http://127.0.0.1:8188
```

- Écoute `127.0.0.1` uniquement (WireGuard plus tard, hors périmètre).
- `POST /jobs` : job valide -> `202` + état ; invalide (dont `frames` hors 4n+1) -> `422`, rien n'est lancé.
- `GET /jobs/{job_id}` -> `200` + `queued|running|done|failed` (`output_path` ou `error`) ; inconnu -> `404`.
- Idempotence : un `job_id` déjà connu renvoie son état sans relancer de rendu ; un job `failed`
  renvoyé est remis en file (reprise).
- Persistance : l'état de chaque job est écrit dans `--out/<job_id>/state.json` (forme
  `$defs/JobStatus`) et relu à la demande, donc l'idempotence survit au redémarrage ; un job
  resté `queued`/`running` lors d'un arrêt est vu `failed` (`Interrupted`) et peut être renvoyé.
- Timeout ComfyUI : 15 min par rendu (`comfy_run.DEFAULT_TIMEOUT_S`, paramètre `timeout`) -> `failed`.
- `error` : type d'exception + message générique, jamais le texte brut de ComfyUI.
- Le rendu doit produire un `.mp4`, sinon le job est `failed`.
- Un seul job GPU à la fois (carte 8 Go) ; Ollama est déchargé avant chaque rendu ComfyUI.
- `image_path` est relatif à `ComfyUI/input` ; le clip est écrit dans `--out/<job_id>/`.
- Journaux et sortie : `job_id` seulement (aucun prompt ni chemin : données d'enfant) ;
  `comfy_run.run` n'écrit plus rien sur stdout (le résumé n'est affiché qu'en CLI).

## Tests

`apps/gpu-worker/tests/test_worker.py` simule ComfyUI (runner injectable) : aucun appel au PC GPU.
Le test réel (1 clip de 2 s, `frames: 49`, généré sur le PC) est manuel, fait par le parent.
