# Routes du dashboard

Pages HTML servies au parent (épique #2, ticket #5). L'ingestion est décrite dans
[`ingest-api.md`](ingest-api.md). Alignement code / contrat vérifié par
`apps/dashboard/tests/test_auth.py::test_contract_documents_routes_env_and_cookie`.

## Configuration (variables d'environnement, jamais dans le dépôt)

| Variable | Contenu |
|---|---|
| `DASHBOARD_PASSWORD_HASH` | hash du mot de passe : `pbkdf2_sha256:<itérations>:<sel hex>:<hash hex>` (PBKDF2-HMAC-SHA256, stdlib ; 600 000 itérations par défaut) |
| `DASHBOARD_SESSION_KEY` | clé HMAC de signature des sessions, **32 caractères minimum** (ex. `python -c "import secrets; print(secrets.token_urlsafe(48))"`) |

Le séparateur `:` (pas de `$`) évite toute interpolation dans un `.env` ou un shell.
Générer le hash sans l'écrire dans l'historique du shell :

```sh
uv run python -c "import getpass; from dashboard.auth import hash_password; print(hash_password(getpass.getpass()))"
```

Variable absente, vide, ou clé trop courte : la connexion est **fermée** (503).
Changer `DASHBOARD_SESSION_KEY` invalide toutes les sessions ouvertes.

## Session

Cookie `vva_session` = `<expiration epoch>.<HMAC-SHA256 base64url>`, signé avec
`DASHBOARD_SESSION_KEY`, vérifié en temps constant. Attributs : `HttpOnly`, `Secure`,
`SameSite=Lax`, `Path=/`, durée 8 h (`Max-Age` et expiration signée). Aucune donnée
personnelle dans le cookie. Le cookie `Secure` impose HTTPS (Traefik en prod).

## Routes

| Route | Accès | Réponses |
|---|---|---|
| `GET /healthz` | public | 200 `{"status": "ok", "version": "<APP_VERSION>"}` — aucune donnée d'événement |
| `GET /login` | public | 200 formulaire (champ `password`) |
| `POST /login` | public, limité | voir ci-dessous |
| `GET /activity` | session | 200 page ; 303 vers `/login` sans session valide (aucune lecture de la base) ; 503 si la base est illisible |
| `GET /static/tokens.css` | public | feuille de style du design system |

### `POST /login`

Corps `application/x-www-form-urlencoded`, champ `password`, 4 Ko maximum.

| Code | Cas |
|---|---|
| 303 | bon mot de passe : pose `vva_session`, redirige vers `/activity` |
| 401 | mot de passe faux, vide ou absent — message neutre « Identifiants invalides. » |
| 413 | corps > 4 Ko |
| 429 | plus de **5 tentatives par minute** (fenêtre glissante, globale : compte unique), en-tête `Retry-After: 60` |
| 503 | connexion non configurée |

L'ordre des contrôles est : configuration, limite, taille, mot de passe. Une tentative
compte dans la limite qu'elle réussisse ou non.

### `GET /activity`

Lit la table `events` (voir `ingest-api.md`) : les **100 derniers** événements, triés par
`ts` décroissant. Deux vues (spec `docs/design/activity.md`) :

- tableau « Agents » : dernier événement de chaque couple (`agent_type`, `agent_id`) ;
- flux d'événements (`<ol aria-live="polite">`) : heure (UTC), `agent_type`, `agent_id`,
  `hook_event_name`, `tool_name` ; champ absent affiché « — ».

États : vide (« Aucun agent n'a encore travaillé »), erreur (bandeau `role="alert"`,
« Réessayer »). Réponses en `Cache-Control: no-store`. Hors périmètre : temps réel,
pause, filtres, multi-comptes.

Limites connues : la limite de 5 essais/minute est globale (compte unique : un tiers peut verrouiller la connexion une minute) et en mémoire (un seul worker ; remise à zéro au redémarrage). Pas de déconnexion : seule la rotation de `DASHBOARD_SESSION_KEY` révoque les sessions (ticket de suivi).
