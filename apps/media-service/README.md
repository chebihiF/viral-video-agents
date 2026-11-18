# media-service

Montage FFmpeg des vidéos générées (VPS) : concat des clips, voix off AAC, sous-titres SRT incrustés. Mixage musique, mot par mot et galerie : à venir.

## Usage

```python
from media_service.montage import parse_job, run_montage

result = run_montage(parse_job(job_dict))  # job conforme à contracts/montage-job.schema.json
result.video_path, result.srt_path  # MP4 + SRT à côté (même nom, extension .srt)
```

- Entrée : [`contracts/montage-job.schema.json`](../../contracts/montage-job.schema.json) (exemple dans `contracts/examples/`).
- Durée d'un plan = durée de sa voix (ffprobe) ; clip bouclé s'il est plus court, coupé s'il est plus long.
- Sortie : MP4 H.264 + AAC, 1080x1920 (recadrage plein cadre), 24 i/s ; un cue SRT par plan.
- Écriture atomique : fichiers temporaires dans le dossier de sortie puis `rename`. Clip/audio manquant ou FFmpeg en échec : `MontageError` nommant le plan, aucun fichier laissé.

## Prérequis

FFmpeg et ffprobe système, compilés avec `libx264` et `libass` (filtre `subtitles`) et une police installée (fontconfig). Aucune dépendance Python. Licence FFmpeg : LGPL, GPL avec libx264 (usage local, aucun service payant).

## Tests

`uv run pytest apps/media-service` : clips et voix synthétiques (`ffmpeg -f lavfi`), ignorés si ffmpeg est absent.
