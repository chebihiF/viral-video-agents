# ADR-004 — Séparer l'usine d'agents (A) et l'application vidéo (B)

- Statut : brouillon — décision du propriétaire du produit le 2026-11-18, à accepter après relecture
- Liens : ADR-001 (architecture de B), ADR-002 (stack du dashboard), ADR-003 (mémoire des agents), #15, #16, #19, #21

## Contexte
Au 18/11, seuls 5 des 23 tickets livrés concernent le produit. Les autres ont servi à l'outillage
des agents. Le dépôt mélange deux briques :
- **A, l'usine d'agents** : rôles et règles des agents, skills, hooks, télémétrie, dashboard de
  pilotage, économie de tokens. A n'a pas besoin du PC GPU.
- **B, l'application de vidéos animées pour l'enfant** : le produit que A développe.

Symptômes : les contrats de A et de B partagent `contracts/` ; un test de B
(`test_contracts_storyboard.py`) se trouve dans `apps/dashboard` ; la CI et `release.yml` ne
déploient que A ; #15 et #16 sont bloqués parce qu'une règle interdisait de versionner des
fichiers Claude, alors que ces fichiers sont le code source de A.

## Options
1. **Deux dépôts** : un nouveau dépôt pour A (`chebihiF/agent-factory`), et
   `viral-video-agents` qui garde B. Frontière nette, A réutilisable dans d'autres projets,
   tableau de B centré sur le produit. Coût : une migration (2 à 3 tickets), deux CI, droit de
   push du bot à étendre.
2. Un dépôt, deux épics et un champ « Brique » dans le Project. Coût presque nul, mais aucune
   réutilisation et la dérive reste possible.
3. Un dépôt réorganisé en `factory/` et `product/`, extraction plus tard. Peu coûteux maintenant,
   mais deux migrations au lieu d'une.

## Décision
**Option 1 : deux dépôts.**

### Répartition des dossiers
| Va dans A (`chebihiF/agent-factory`) | Reste dans B (`viral-video-agents`) |
|---|---|
| `apps/dashboard`, `tools/telemetry` | `apps/gpu-worker`, `apps/media-service` |
| contrats `agent-event`, `ingest-api`, `token-usage`, `dashboard-routes`, `progress` | contrats `storyboard`, `classification`, `gpu-job`, `montage-job` et leurs exemples |
| `docs/design/`, ADR-002, ADR-003, `docs/SCRUM.md` | `prompts/`, `evals/`, `comfyui/`, `n8n/` |
| `infra/deploy`, `infra/runner`, `release.yml` | ADR-001, `docs/ARCHITECTURE.md` |
| scripts `pool`, `bootstrap_worktree`, `quiet`, `gen_map`, `set-dashboard-password`, `test_no_claude_tracked` | `apps/dashboard/tests/test_contracts_storyboard.py` (revient dans B) |
| agents, skills et hooks versionnés (voir plus bas) | |

Les fichiers communs et petits sont dupliqués, sans paquet partagé : `Makefile`, `pyproject.toml`,
`next_version.py`, les modèles d'issue et de PR.

L'historique git est conservé (`git filter-repo` sur une copie du dépôt). Les issues de A sont
déplacées avec `gh issue transfer`, ce qui garde leurs commentaires. #21 reste dans B comme
journal du produit ; A a son propre journal.

### Fichiers Claude
Le propriétaire accorde une **exception limitée au dépôt de A** : les agents, skills et hooks y
sont versionnés, hors d'un dossier `.claude/`, puisqu'ils forment le produit de A. Une commande
d'installation les copie dans le `.claude/` local de chaque projet consommateur. Dans B et dans
tout autre dépôt, la règle reste entière : aucun fichier Claude commité (garde-fou
`test_no_claude_tracked`). Cette exception débloque #15 et #16.

### CI, déploiement, dashboard
- **A** : la CI actuelle et `release.yml`. Le dashboard reste en prod à la même URL, sur le VPS,
  déployé depuis A. Le runner `vps-deploy` est réenregistré sur A. Le dashboard devient
  multi-projets : champ `project` ajouté au contrat `agent-event`, compatible avec les anciens
  événements.
- **B** : une CI de tests sans GPU (pytest, ruff, contrats, évals en `--dry-run`). Pas de
  déploiement tant que l'interface #36 n'existe pas.
- **Suivi** : un seul Project utilisateur couvre les deux dépôts, avec une vue par brique.

### Capacité et objectifs de A
- **80 % B / 20 % A** : au plus 1 ticket A sur 5 par sprint. La migration elle-même compte dans
  les 20 %.
- Objectifs de A (plafonds provisoires, ajustés après la première mesure de #19) :
  - ticket simple ≤ 150 k tokens, ticket `complex` ≤ 300 k ;
  - Opus ≤ 30 % des tokens d'un sprint ;
  - au plus 1 geste du propriétaire par ticket (la validation).

## Conséquences
- \+ Le tableau de B ne montre que le produit ; la dérive se voit et se plafonne.
- \+ A peut servir aux autres projets du propriétaire.
- \+ #15 et #16 sont débloqués dans A.
- − Une migration à faire (2 à 3 tickets dans les 20 % de A), deux CI à maintenir.
- − Deux journaux et deux ensembles de règles locales à garder cohérents.

## Suivi
- Fait le 2026-11-18 : dépôt public `chebihiF/agent-factory` créé (public : CI illimitée et protection
  de `main` gratuites), bot `infinitySoftMA` collaborateur en écriture.

## Attend l'utilisateur
- Mettre à jour les règles globales : exception « fichiers Claude commités autorisés dans
  `chebihiF/agent-factory` uniquement », et droit de push du bot étendu à ce dépôt (mêmes limites
  que pour `viral-video-agents`).
