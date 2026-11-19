# media-service

Montage FFmpeg des vidéos générées (VPS) : concat des clips, voix off AAC, sous-titres SRT incrustés. Mixage musique et galerie : à venir ; sous-titres mot par mot (ASS) : module prêt, branchement au montage à venir.

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

## Sous-titres mot par mot (ASS)

```python
from pathlib import Path
from media_service.subtitles import ShotText, write_ass

write_ass([ShotText("un petit chat", 3.0), ShotText("il s'envole", 2.0)], Path("final.ass"))
```

- Calcul pur (stdlib, sans GPU) : un événement par mot, durée proportionnelle à la longueur du mot, plans mis bout à bout ; bornes au centième, contiguës, sans chevauchement.
- Fichier ASS UTF-8, 1080x1920, police `DejaVu Sans` (arabe + latin) ; mots conservés tels quels, seuls `{`, `}` et `\` sont neutralisés.
- Contrat : champ optionnel `subtitles_path` (`.ass`) dans `montage-job.schema.json`, exemple `examples/montage-job.subtitles.json`. Pas encore lu par `montage.py` (ticket ultérieur).

## Prérequis

FFmpeg et ffprobe système, compilés avec `libx264` et `libass` (filtre `subtitles`) et une police installée (fontconfig). Aucune dépendance Python. Licence FFmpeg : LGPL, GPL avec libx264 (usage local, aucun service payant).

## Tests

`uv run pytest apps/media-service` : clips et voix synthétiques (`ffmpeg -f lavfi`), ignorés si ffmpeg est absent.
