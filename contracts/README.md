# contracts

Schémas JSON versionnés échangés entre services de l'application vidéo (storyboard, classification, jobs, runs).
Les contrats de l'usine d'agents (`agent-event`, `ingest-api`, `dashboard-routes`…) vivent dans `chebihiF/agent-factory` (ADR-004, #68).

| Contrat | Rôle |
|---|---|
| [`storyboard.schema.json`](storyboard.schema.json) | Storyboard du conteur (v1.0), entrée du pipeline ; exemples dans [`examples/`](examples/) |
| [`classification.schema.json`](classification.schema.json) | Sortie de la classification de sécurité d'une dictée (`libre` / `adouci` / `bloque`), jamais la dictée elle-même ; exemples dans [`examples/`](examples/) |
| [`gpu-job.schema.json`](gpu-job.schema.json) | Job image -> clip n8n vers gpu-worker (v1.0) et son état (`$defs/JobStatus`) ; exemples dans [`examples/`](examples/) |
| [`montage-job.schema.json`](montage-job.schema.json) | Entrée du media-service (v1.0) : plans clip + voix + narration, sortie MP4, `subtitles_path` ASS optionnel ; exemples dans [`examples/`](examples/) |
| [`tts-job.schema.json`](tts-job.schema.json) | Entrée du TTS Piper du media-service (v1.0) : texte de voix off par plan ; sortie `$defs/TtsResult` (WAV + durée par plan) ; exemple dans [`examples/`](examples/) |
| [`pipeline-run.schema.json`](pipeline-run.schema.json) | État d'un run de l'orchestrateur (v1.0, #53) : `etat` ok/bloque/erreur, étapes (nom, statut, durée), `video_path` si ok ; aucun texte de dictée ; exemple dans [`examples/`](examples/) |
