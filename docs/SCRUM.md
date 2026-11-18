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
- [ ] PR relue (agent reviewer), titre en Conventional Commits, merge squash.
- [ ] Déployé en prod par le pipeline, smoke test vert.
- [ ] Docs/ADR/contrats à jour ; `docs/MAP.md` régénéré (`make map`).
- [ ] Aucun secret, aucune donnée d'enfant dans le dépôt.

## Flux de travail
1. Branche `feat/<n°>-<slug>` (ou `fix/`, `chore/`) créée depuis `origin/main` sans suivi.
2. Commits en Conventional Commits (`feat(dashboard): …`, `fix: …`, `feat!: …`).
3. PR liée à l'issue (`Closes #n`) ; le titre de PR devient le commit squashé et fixe la version.
4. Merge squash automatique quand les checks sont verts ; `main` est protégée (aucun push direct).
