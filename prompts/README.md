# prompts

Prompts système versionnés (conteur, storyboard, classification).

Convention : `<nom>.v<N>.md`, en-tête `---` (prompt, version, modèle cible, sortie attendue),
puis le texte du prompt. Toute modification = nouveau fichier `v<N+1>` (ou changement mesuré) et
éval avant/après dans `evals/` ; ne jamais éditer une version déjà évaluée sans relancer l'éval.

| Prompt | Version | Modèle | Contrat | Éval |
|---|---|---|---|---|
| `storyboard.v1.md` | v1 | qwen3:8b | `contracts/storyboard.schema.json` (#31) | `evals/storyboard_eval.py` |

Notes de version :
- `storyboard.v1` : champs repris de `evals/darija_storyboard.py` (compris_fr, recap_darija,
  plans[image_prompt, motion_prompt, narration_fr]). À aligner sur `contracts/storyboard.schema.json`
  (ticket #31) dès son merge, puis passer en v2 si les champs changent.
