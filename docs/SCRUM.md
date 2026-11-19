# Scrum — fonctionnement de l'équipe (humain + agents)

## Cadence
Sprints d'**une semaine** (champ d'itération « Sprint » du Project GitHub).
Colonnes : **Backlog → Prêt → En cours → Revue → Livré**.

## Types de tickets
- `type:US` — user story (valeur visible), critères d'acceptation **Gherkin**.
- `type:TS` — tâche technique (outillage, dette, infra).
- `type:bug` — anomalie.
- Priorité : `P0` (bloquant) … `P3` (un jour). `complex` : le dev passe sur Opus.
- `stop-agents` : aucun agent ne doit toucher ce ticket (ni le reprendre).

## Definition of Ready (passage en « Prêt »)
Un ticket est un **paquet de contexte autonome** : un agent doit pouvoir le traiter sans relire le dépôt.
- [ ] Objectif en une phrase et valeur pour l'utilisateur.
- [ ] **Fichiers à toucher** listés (et fichiers à ne pas toucher si utile).
- [ ] **Contrat** : schéma d'entrée/sortie, route, événement ou interface concernés (`contracts/`).
- [ ] **Critères d'acceptation** testables (Gherkin pour une US).
- [ ] Dépendances résolues, taille ≤ 1 jour d'agent ; sinon découper.
- [ ] Priorité et sprint renseignés ; `complex` posé si besoin.

## Definition of Done (passage en « Livré »)
- [ ] Critères d'acceptation couverts par des tests automatisés.
- [ ] `make check` vert en local ; CI verte (ruff, mypy, pytest, gitleaks, pip-audit, build).
- [ ] PR relue (agent reviewer) si le déclencheur ci-dessous s'applique, titre en Conventional Commits, merge squash.
- [ ] Déployé en prod par le pipeline, smoke test vert.
- [ ] Docs/ADR/contrats à jour ; `docs/MAP.md` régénéré (`make map`).
- [ ] Aucun secret, aucune donnée d'enfant dans le dépôt.
- [ ] Ticket touchant le dashboard : capture de la prod avec de vraies données (pas un jeu de test) jointe au ticket,
  mobile 360 px et bureau ; sinon le ticket reste en Revue. Les captures sont déposées dans le ticket ou #21,
  jamais dans le dépôt, et ne montrent aucune donnée d'enfant ni secret (masquer ou recadrer).

## Flux de travail
1. Branche `feat/<n°>-<slug>` (ou `fix/`, `chore/`) créée depuis `origin/main` sans suivi.
2. Commits en Conventional Commits (`feat(dashboard): …`, `fix: …`, `feat!: …`).
3. PR liée à l'issue (`Closes #n`) ; le titre de PR devient le commit squashé et fixe la version.
4. Merge squash automatique quand les checks sont verts ; `main` est protégée (aucun push direct).

## Parcours léger (petits tickets)
Éligible : ticket sans `complex` ni `needs-design`, ≤ 3 fichiers, aucune surface sécurité ni donnée d'enfant.
Parcours : dev (TDD, Sonnet) → CI. Pas de `product-owner`, de design ni de `qa` séparés : les critères
d'acceptation sont couverts par les tests du dev. Le parcours complet reste la règle pour `complex`.

### Déclenchement du reviewer
Le `reviewer` n'intervient que si le ticket est `complex`, **ou** si le diff dépasse 300 lignes,
**ou** s'il touche du code de sécurité ou un contrat (`contracts/`). Un seul tour, sur le diff seul
(`git diff origin/main...`), sans relire le dépôt. Hors de ces cas, la CI suffit.

### Plafonds de tokens
- Contexte par session : ~150 k tokens ; au-delà, passation (résumé dans l'épique #21) et nouvelle session (voir #83).
- Bilan par ticket (mesuré par `tools/agents/bin/token-report.sh`, voir `docs/agents.md`) :
  simple ≤ 150 k, `complex` ≤ 300 k ; part d'Opus ≤ 30 % des tokens du sprint.
- Un ticket qui dépasse son plafond est signalé dans le bilan de sprint.

### Contexte allégé
Lire des extraits (`grep -n`, `offset`/`limit`), jamais des fichiers entiers sans besoin ;
briefs de sous-agents courts (n° d'issue, fichiers, critère de fin) ; rapports ≤ 15 lignes.
