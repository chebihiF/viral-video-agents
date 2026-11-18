# evals

Évaluations reproductibles des modèles (darija, storyboard, classification).

## Storyboard (`storyboard_eval.py`)

Mesure le taux de sorties JSON conformes à `contracts/storyboard.schema.json` pour le prompt
`prompts/storyboard.v1.md` (Qwen3 8B via Ollama, température 0, seed 42, stdlib uniquement).

```bash
# CI / sans GPU : rejoue evals/cases/storyboard.recorded.jsonl, aucun appel réseau
python3 evals/storyboard_eval.py --dry-run

# Réel (PC GPU : `~/.local/bin/pc-gpu wake` d'abord ; Ollama sur http://127.0.0.1:11434)
python3 evals/storyboard_eval.py --record /tmp/storyboard.recorded.jsonl
```

- Code de sortie : 0 si taux >= 90 %, 1 sinon (cas en échec listés), 2 si configuration invalide.
- Le rapport ne contient ni durée ni date : deux exécutions donnent le même texte. Les durées par
  cas du mode réel vont sur stderr (relever aussi la VRAM avec `nvidia-smi` pendant l'éval).
- Cas : `evals/cases/storyboard.jsonl` (12 dictées synthétiques : darija latin/arabe, français,
  mélange ; aucune donnée d'enfant). Tests : `pytest evals` (jamais d'appel Ollama).
- Validation : sous-ensemble JSON Schema (type, required, properties, items, enum,
  minItems/maxItems, minLength/maxLength, additionalProperties). Tout autre mot-clé fait échouer
  l'éval (code 2) plutôt que d'être ignoré.
- Avant/après : après un changement de prompt ou de modèle, relancer en réel et reporter le taux
  dans la PR.

### Schéma, enregistrements et empreinte

- Mode réel : charge `contracts/storyboard.schema.json` ; absent -> code 2 avec message clair.
- `--dry-run` : utilise TOUJOURS `evals/cases/storyboard.test.schema.json` (fourni avec les
  enregistrements), jamais le vrai contrat ; `--schema <fichier>` force un autre schéma.
- Chaque ligne de `storyboard.recorded.jsonl` porte `schema_sha256` (sha256 court du schéma
  canonique). Si l'empreinte diffère du schéma chargé, `--dry-run` sort en code 2 :
  « ré-enregistrer avec --record » (pas de vert trompeur).
- Le schéma est vérifié en entier avant l'éval (properties, items, additionalProperties) : tout
  mot-clé non géré -> code 2 ; `additionalProperties` ne peut être qu'un booléen.
- `--threshold` doit être dans [0, 1], sinon code 2.

### En attente du ticket #31 (contrat)

`contracts/storyboard.schema.json` n'est pas encore mergé : le mode réel échoue proprement tant
qu'il est absent. Au merge de #31 : réaligner `prompts/storyboard.v1.md` sur ses champs
(nouvelle version si les champs changent), lancer l'éval réelle avec
`--record evals/cases/storyboard.recorded.jsonl`, puis lancer `--dry-run --schema
contracts/storyboard.schema.json` et retirer le schéma de test (les réponses actuelles sont
synthétiques, dont une volontairement non conforme : `mix-12`).

`darija_storyboard.py` est conservé comme référence (non modifié).
