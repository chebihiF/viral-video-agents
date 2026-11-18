# Contrôles : pause des agents et plafond de tokens (#8)

Le parent met les agents en pause (ou fixe un plafond de tokens) depuis le dashboard ; le hook
`PreToolUse` du Mac lit cet état avant chaque outil. Alignement code / contrat vérifié par
`apps/dashboard/tests/test_controls.py` et `tools/telemetry/tests/test_gate.py`.

## État

Une seule ligne SQLite (table `controls`, voir `db.py`), créée avec le schéma.

| Champ | Type | Sens |
|---|---|---|
| `paused` | booléen | `true` : les agents ne doivent exécuter aucun outil |
| `reason` | `"manuel"`, `"plafond"` ou `null` | motif de la pause (`null` si `paused=false`) ; le premier motif est conservé |
| `cap_tokens` | entier ≥ 1 | plafond, en tokens sur 24 h glissantes, tous agents (défaut **500 000**) |
| `tokens_24h` | entier | total des tokens des dernières 24 h (même source que `/tokens`, [`token-usage.md`](token-usage.md)) |
| `updated_at` | texte ISO 8601 UTC | dernier changement d'état |

**Plafond** : évalué à chaque lecture de l'état (page ou API). Si `tokens_24h >= cap_tokens` et
que les agents tournent, l'état passe à `paused=true`, `reason="plafond"` et la page affiche le
bandeau « Plafond atteint ». La reprise est **refusée (409)** tant que le plafond est atteint :
relever le plafond (ou attendre que la fenêtre de 24 h se vide), puis reprendre. Abaisser le plafond
sous la consommation met en pause. Hors périmètre : plafond par agent, notifications.

## API pour le hook : `GET /api/v1/control`

`Authorization: Bearer <INGEST_TOKEN>` (même jeton que l'ingestion, [`ingest-api.md`](ingest-api.md) :
comparaison en temps constant, 503 si non configuré). La session de navigateur n'ouvre pas cette route.

| Code | Cas | Corps |
|---|---|---|
| 200 | état courant, `Cache-Control: no-store` | `{"paused": false, "reason": null, "cap_tokens": 500000, "tokens_24h": 0, "updated_at": "..."}` |
| 401 | jeton absent ou faux | `{"detail": "..."}` |
| 503 | jeton non configuré, ou base illisible | `{"detail": "..."}` |

## Pages (session) : `GET /controls`, `POST /controls/{pause,resume,cap}`

| Route | Réponses |
|---|---|
| `GET /controls` | 200 page (état, bandeau, formulaires) ; 303 vers `/login` sans session ; 503 si la base est illisible (**fail-closed** : aucune action possible) |
| `POST /controls/pause` | 303 vers `/controls` ; pause manuelle (idempotente) |
| `POST /controls/resume` | 303 ; **409** si le plafond est encore atteint |
| `POST /controls/cap` | 303 ; champ `cap_tokens` entier ASCII entre 1 et 10^12, sinon **422** |

Tous les POST : `application/x-www-form-urlencoded`, 4 Ko maximum (413). Ordre des contrôles :
session (303 vers `/login`, aucune lecture de la base), taille, **jeton CSRF** (403), base (503).

**CSRF** : champ caché `csrf` = `base64url(HMAC-SHA256(DASHBOARD_SESSION_KEY, "csrf.v1." + cookie de session))`.
Lié à la session (un jeton d'une autre session est refusé), comparé en temps constant
(`hmac.compare_digest`), sans stockage serveur. S'ajoute au cookie `SameSite=Lax`
([`dashboard-routes.md`](dashboard-routes.md)).

## Hook `PreToolUse` : `tools/telemetry/pretool_gate.py`

Stdlib uniquement. Ne lit pas stdin (aucun prompt ni argument d'outil ne le traverse).

- `paused=true` : message sur stderr (« Agents en pause (motif : …) »), sortie en **code 2** ;
  Claude Code n'exécute pas l'outil et affiche le message. Sinon code 0, sans sortie.
- **Repli sûr (fail-open)** : serveur injoignable, délai (2 s par requête, 4 s au total), code ≠ 200
  (dont 401 et 503), JSON invalide, redirection (jamais suivie) ou configuration absente/refusée :
  le hook utilise le dernier état en cache s'il a **au plus 300 s** (5 min), sinon il **laisse passer**
  (code 0). Le délai global de 4 s applique la même règle (cache en pause : code 2). Choix assumé : une panne du VPS ne doit pas figer le travail ; la pause n'est donc garantie
  que si le VPS répond ou que le cache est frais. Un cache daté du futur est ignoré.
- Journal (une ligne `date code`, jamais jeton, URL, outil ni contenu) : `fetch-failed`, `cache-used`,
  `fail-open`, `blocked`. Fichiers d'état en `0600`, journal tourné à 256 Ko.

| Variable | Rôle | Défaut |
|---|---|---|
| `VVA_CONTROL_URL` | URL complète de `/api/v1/control` : `https`, ou `http` sur localhost seulement | requis |
| `VVA_INGEST_TOKEN` | jeton Bearer (= `INGEST_TOKEN` du VPS) | requis |
| `VVA_GATE_CACHE` | cache du dernier état (`paused`, `reason`, `fetched_at`) | `$CLAUDE_PROJECT_DIR/.claude/telemetry/gate-cache.json` |
| `VVA_GATE_LOG` | journal | `$CLAUDE_PROJECT_DIR/.claude/telemetry/gate.log` |

### Câblage (réglages locaux, jamais versionnés)

À placer dans **votre** `.claude/settings.local.json` (ou `~/.claude/settings.json`) ; le jeton et l'URL
restent dans l'environnement local :

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "timeout": 5,
        "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/pretool_gate.py\"" }] }
    ]
  }
}
```
