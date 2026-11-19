# contracts

Schémas JSON versionnés échangés entre services (storyboard, jobs, événements d agents).

| Contrat | Rôle |
|---|---|
| [`storyboard.schema.json`](storyboard.schema.json) | Storyboard du conteur (v1.0), entrée du pipeline ; exemples dans [`examples/`](examples/) |
| [`gpu-job.schema.json`](gpu-job.schema.json) | Job image -> clip n8n vers gpu-worker (v1.0) et son état (`$defs/JobStatus`) ; exemples dans [`examples/`](examples/) |
| [`montage-job.schema.json`](montage-job.schema.json) | Entrée du media-service (v1.0) : plans clip + voix + narration, sortie MP4, `subtitles_path` ASS optionnel ; exemples dans [`examples/`](examples/) |
| [`tts-job.schema.json`](tts-job.schema.json) | Entrée du TTS Piper du media-service (v1.0) : texte de voix off par plan ; sortie `$defs/TtsResult` (WAV + durée par plan) ; exemple dans [`examples/`](examples/) |
