# Checklist /ticket — Definition of Done

Copie de la DoD de `docs/SCRUM.md` (source de vérité : en cas d'écart, `docs/SCRUM.md` prime).
À cocher avant l'étape 8 (carte « Livré »).

- [ ] Critères d'acceptation couverts par des tests automatisés.
- [ ] `make check` vert en local ; CI verte (ruff, mypy, pytest, gitleaks, pip-audit, build).
- [ ] PR relue (agent reviewer) si le déclencheur de `docs/SCRUM.md` s'applique, titre en Conventional Commits, merge squash.
- [ ] Déployé en prod par le pipeline, smoke test vert.
- [ ] Docs/ADR/contrats à jour ; `docs/MAP.md` régénéré (`make map`).
- [ ] Aucun secret, aucune donnée d'enfant dans le dépôt.
