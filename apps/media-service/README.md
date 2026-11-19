# media-service

Montage FFmpeg des vidéos générées (VPS) : concat des clips, voix off AAC, sous-titres SRT incrustés ; voix off par plan avec Piper (CPU). Mixage musique et galerie : à venir ; sous-titres mot par mot (ASS) : module prêt, branchement au montage à venir.

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

## Voix off (Piper)

```python
from pathlib import Path
from media_service.tts import synthetiser

voices = synthetiser(
    [{"plan_id": "plan-1", "texte": "Bonjour.", "langue": "fr"}], Path("/data/jobs/42/voix")
)
voices[0].wav_path, voices[0].duree_s  # <plan_id>.wav et sa durée en secondes
```

- Contrat : [`contracts/tts-job.schema.json`](../../contracts/tts-job.schema.json) (`parse_job` valide un job complet ; sortie `$defs/TtsResult`).
- Configuration par variables d'environnement : `PIPER_BIN` (binaire Piper, MIT) et `PIPER_MODEL` (modèle de voix `.onnx`). Absente ou introuvable : `TtsError` nommant la variable.
- Piper visé : binaire C++ rhasspy/piper (MIT), flag `--output_file` ; `piper1-gpl` (GPL, flag `--output-file`) n'est pas supporté.
- Le texte passe par stdin de Piper : jamais dans les arguments, les logs ni les erreurs (qui nomment le plan). Tout ou rien : un échec supprime les WAV déjà écrits.
- `duree_s` = durée du WAV ; elle fixe celle du plan au montage. Choix définitif de la voix, darija, GPU : hors périmètre.

## Prérequis

FFmpeg et ffprobe système, compilés avec `libx264` et `libass` (filtre `subtitles`) et une police installée (fontconfig). Piper (voix off, `PIPER_BIN`/`PIPER_MODEL`). Aucune dépendance Python. Licence FFmpeg : LGPL, GPL avec libx264 (usage local, aucun service payant).

## Tests

`uv run pytest apps/media-service` : clips et voix synthétiques (`ffmpeg -f lavfi`), ignorés si ffmpeg est absent. `test_tts.py` utilise un faux Piper (WAV silencieux de durée connue), sans Piper réel.
