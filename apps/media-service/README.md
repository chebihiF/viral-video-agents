# media-service

Montage FFmpeg des vidéos générées (VPS) : concat des clips, voix off AAC, sous-titres SRT incrustés ; voix par réplique avec SILMA TTS (GPU, ADR-006 ; Piper abandonné). Mixage musique et galerie : à venir ; sous-titres mot par mot (ASS) : module prêt, branchement au montage à venir.

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
- Contrat : champ optionnel `subtitles_path` (`.ass`) dans `montage-job.schema.json`, exemple `examples/montage-job.subtitles.json`. Lu par `montage.py` : si `subtitles_path` est fourni, le filtre `ass` incruste l'ASS (le SRT reste écrit à côté) ; fichier absent : `MontageError` nommant le chemin ; sans le champ, SRT incrusté comme avant.

## Voix par réplique (SILMA TTS, #249)

```python
from pathlib import Path
from media_service.tts import synthetiser

voix = synthetiser(
    [{"replique_id": "r1", "voix_id": "chat", "texte": "مرحبا!", "langue": "ar"}],
    Path("/data/jobs/42/voix"),
)
voix[0].wav_path, voix[0].duree_s  # <replique_id>.wav et sa durée en secondes
```

- Contrat : [`contracts/tts-job.schema.json`](../../contracts/tts-job.schema.json) v2.0 (`parse_job` valide un job complet et refuse un job 1.0 ; sortie `$defs/TtsResult`, un élément par réplique dans l'ordre d'entrée). Une réplique de personnage et une voix off passent par la même interface.
- `verifier_configuration(env)` rend désormais une `Configuration` (`binaire`, `modeles`, `voix` : `voix_id` -> `Voix(audio, transcription)`) au lieu du couple Piper `(binaire, modèle)`.
- Configuration (variables d'environnement) ; absente ou introuvable : `TtsError` qui nomme la variable (et la voix), jamais un chemin :
  - `SILMA_BIN` : exécutable de l'enveloppe [`scripts/silma_cli.py`](scripts/silma_cli.py), lancé avec le Python de l'environnement où `silma-tts` est installé (PC GPU, hors CI).
  - `SILMA_MODEL_DIR` : poids locaux, annexes compris : `silma-tts/model.pt`, `silma-tts/vocab.txt`, `vocos-mel-24khz/{config.yaml,pytorch_model.bin}`, `catt/{encoder,decoder}.onnx` (vérifiés par l'enveloppe avant l'import).
  - `SILMA_VOIX` : fichier JSON hors dépôt, `{"<voix_id>": {"audio": "ref.wav", "transcription": "ref.txt"}}` (chemins relatifs au fichier ; audio de moins de 8 s ; transcription obligatoire, pas de transcription automatique). Aucune voix par défaut ; plusieurs voix admises.
- `voix_id` inconnu, transcription vide ou référence introuvable : échec avant tout lancement du moteur, sans repli sur une autre voix. `PIPER_BIN` et `PIPER_MODEL` ne sont plus lus.
- Le texte et la transcription passent en JSON sur stdin de l'enveloppe : jamais dans les arguments, les logs, les erreurs ni un fichier temporaire. Sorties du moteur jamais relayées. Délai borné (`DELAI_S`, 600 s par réplique, chargement du modèle compris) ; le moteur est arrêté avec son groupe de processus avant le retour. Tout ou rien : un échec supprime les WAV déjà écrits.
- Hors ligne : `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `HF_DATASETS_OFFLINE` forcés ; l'enveloppe refuse en plus toute connexion IPv4/IPv6 et résolution DNS dans son processus (code 4) et vérifie les fichiers locaux avant d'importer `silma_tts` (code 3). Codes de l'enveloppe : 0 ok, 2 entrée invalide, 3 fichier manquant, 4 réseau refusé, 5 échec du moteur.
- **Non validé sur le vrai moteur** : enveloppe testée avec une fausse bibliothèque ; qualité en darija non testée (la fiche SILMA annonce arabe standard et anglais). Installation et essai réel : ticket distinct (ADR-006).

## Prérequis

FFmpeg et ffprobe système, compilés avec `libx264` et `libass` (filtre `subtitles`) et une police installée (fontconfig). Voix : SILMA TTS dans un environnement séparé (`SILMA_BIN`, `SILMA_MODEL_DIR`, `SILMA_VOIX`) ; aucune dépendance de SILMA (torch…) dans ce dépôt. Aucune dépendance Python. Licence FFmpeg : LGPL, GPL avec libx264 (usage local, aucun service payant).

## Tests

`uv run pytest apps/media-service` : clips et voix synthétiques (`ffmpeg -f lavfi`), ignorés si ffmpeg est absent. `test_tts.py` utilise un faux moteur SILMA (WAV silencieux de durée connue) et une fausse bibliothèque `silma_tts` pour l'enveloppe, sans `silma-tts` installé ; les voix de référence sont des WAV générés à la volée.
