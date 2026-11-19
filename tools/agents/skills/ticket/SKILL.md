---
name: ticket
description: Boucle complète d'un ticket GitHub, de l'issue « Prêt » au merge squash et à la carte « Livré », sans intervention du parent. Usage /ticket <n>.
---
# /ticket <n>

Exécuté par le tech lead depuis un poste (worktree) déjà existant ; la création du poste est
hors périmètre (#14). Entrée : le numéro `n` de l'issue. Les commandes `gh` visent le dépôt
courant. Économie de tokens : ne lire que le ticket et les fichiers qu'il cite.

## Contrat (8 étapes)

1. **Lire le ticket** : `gh issue view <n>`. Label `stop-agents` → arrêt immédiat : rien n'est
   modifié (ni carte, ni branche, ni commentaire), l'arrêt est signalé dans le rapport. Même
   arrêt si la DoR de `docs/SCRUM.md` n'est pas remplie.
2. **Carte « En cours »** : `gh project item-edit` (champ Status du Project du dépôt). Branche
   `feat/<n>-<slug>` (`fix/` pour un bug) : `git fetch -q origin && git switch --no-track -c feat/<n>-<slug> origin/main`.
3. **Implémenter** en déléguant à l'agent du ticket (brief : n° d'issue, fichiers, critère de
   fin ; modèle donné par `tools/agents/bin/token-report.sh route <agent> --ticket <n> --label <label>`).
   Seuls les fichiers listés par le ticket sont touchés ; tout écart est signalé, jamais improvisé.
4. **`make check` vert**. `git add <fichiers>` avant `make map` (la carte lit l'index git).
   Au plus **2 échecs** : au deuxième échec, arrêt ; la carte reste « En cours », ni push, ni PR,
   aucun merge ; la cause (première erreur de `make check`) est rapportée et l'échec noté
   (`token-report.sh fail <n> <agent>`).
5. **PR** : commit en Conventional Commits (jamais de mention d'assistant), push de
   `feat/<n>-<slug>` par le compte bot `infinitySoftMA`, puis
   `GH_TOKEN=$(gh auth token -u infinitySoftMA) gh pr create -B main -t "<titre CC>" -b "Closes #<n> …"`.
6. **Carte « Revue »**, agent `reviewer` sur le diff (`git diff origin/main...`) selon le
   déclencheur de `docs/SCRUM.md`, puis `qa` si parcours complet. Changements demandés →
   retour à l'étape 3 (2 tours max, puis ticket bloqué : label `blocked`, ligne « Attend l'utilisateur : <question> » dans le ticket et dans #21, poste libéré, ticket Prêt suivant).
7. **Merge squash** quand les checks sont verts : `gh pr merge <url> --auto --squash`. PR
   `BEHIND` → `GH_TOKEN=$(gh auth token -u infinitySoftMA) gh pr update-branch <pr>`.
8. **Carte « Livré »** après le merge, une fois la DoD de `checklist.md` cochée.

Sortie : rapport de **15 lignes** au plus (fait, fichiers, preuves, risques, prochaine action).

## Critères d'acceptation (traçabilité)

| Scénario | Étapes | Preuve attendue |
|---|---|---|
| Boucle nominale | 1 à 8 | PR `Closes #<n>` par `infinitySoftMA` depuis `feat/<n>-<slug>` ; carte En cours → Revue → Livré après le merge squash ; rapport ≤ 15 lignes |
| Ticket bloqué | 1 | label `stop-agents` : rien n'est modifié, arrêt signalé |
| Échecs répétés | 4 | 2 échecs de `make check` : carte « En cours », aucun merge, cause rapportée |

## Interdits

Jamais de push sur `main`, de `--force`, de suppression de branche distante ni de tag à la
main. Aucun secret ni donnée d'enfant dans le dépôt, la PR ou le rapport. 0 € : aucun service payant.
