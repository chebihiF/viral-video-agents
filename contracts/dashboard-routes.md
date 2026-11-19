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
| `GET /` | session | 200 page « Accueil » (agents actifs ou en pause, dernier événement, tickets Prêt / En cours / Revue / Livré, tokens 24 h et 7 j, fil des délégations, badge si plafond atteint ; carte tickets « indisponible » si GitHub injoignable ; état vide sans événement) ; 303 vers `/login` sans session valide (aucune lecture de la base) ; 503 si la base est illisible ; jamais mis en cache. Aucun pourcentage sans légende : sans quota configuré, tokens bruts |
| `GET /login` | public | 200 formulaire (champ `password`) |
| `POST /login` | public, limité | voir ci-dessous |
| `GET /activity` | session | 200 page ; paramètres de requête optionnels `agent`, `statut`, `duree_min` (voir ci-dessous) ; 303 vers `/login` sans session valide (aucune lecture de la base) ; 503 si la base est illisible ; `no-store` |
| `GET /interactions` | session | 200 page ; aucun paramètre de requête ; 303 vers `/login` sans session valide (aucune lecture de la base) ; 503 si la base est illisible ; `no-store` |
| `GET /tokens` | session | 200 page (jauges en % du quota 5 h / semaine / week-end avec légende, par agent et par ticket ; sans quota : tokens bruts et message d'aide, jamais de % ; totaux par agent et modèle, 24 h / 7 j) ; 303 vers `/login` sans session valide ; 503 si la base est illisible ; aucun paramètre de requête ; aucun en-tête `Cache-Control` posé ; voir [`token-usage.md`](token-usage.md) |
| `GET /controls` | session | 200 page (état pause / plafond, formulaires protégés par jeton CSRF) ; 303 vers `/login` sans session valide ; 503 si la base est illisible (aucune action possible) ; `no-store` ; voir [`control-api.md`](control-api.md) |
| `POST /controls/pause` | session + CSRF | 303 vers `/controls` ; 403 jeton CSRF invalide ; 413 corps > 4 Ko ; 503 base illisible ; voir [`control-api.md`](control-api.md) |
| `POST /controls/resume` | session + CSRF | 303 vers `/controls` ; 409 si le plafond est toujours atteint ; mêmes 403 / 413 / 503 ; voir [`control-api.md`](control-api.md) |
| `POST /controls/cap` | session + CSRF | 303 vers `/controls` ; 422 plafond invalide (entier de 1 à 10^12) ; mêmes 403 / 413 / 503 ; voir [`control-api.md`](control-api.md) |
| `GET /stream` | session | 200 `text/event-stream` ; **401** sans session valide (aucune lecture de la base) ; 503 si la base est illisible |
| `GET /static/live.js` | public | client temps réel (`EventSource`), sans dépendance ni CDN |
| `GET /static/tokens.css` | public | feuille de style du design system |
| `GET /static/components.css` | public | styles et placement de la navigation commune (remplace `nav.css`, #92) : barre latérale ≥ 900 px (Accueil, Suivi, Activité, Interactions, Tokens, Contrôles), barre d'onglets en dessous (Accueil, Suivi, Activité, Tokens, puis menu « Plus » : Interactions, Contrôles) ; `aria-current="page"` sur la page active ; lien « Aller au contenu » vers `#contenu` |
| `GET /progress` | session | 200 page « Suivi du projet » ; 303 vers `/login` sans session valide (aucune lecture GitHub ni base) |
| `GET /api/v1/progress` | session | 200 JSON conforme à [`progress.schema.json`](progress.schema.json) ; 401 sans session valide |
| `GET /static/progress.js` | public | rafraîchissement de `/progress` (JS natif, sans CDN) |

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
`ts` décroissant. Vues (spec `docs/design/activity.md`) :

- tableau « Agents » : dernier événement de chaque couple (`agent_type`, `agent_id`) ;
- flux d'événements (`<ol aria-live="polite">`) : heure (UTC), `agent_type`, `agent_id`,
  `hook_event_name`, `tool_name` ; champ absent affiché « — » ;
- « Qui fait quoi » : 20 sessions (agent, ticket) au plus, sur les 24 dernières heures ;
- « Historique des 7 derniers jours » (`#history`), **filtrable** : une ligne par session
  (`agent_type`, `agent_id`, `ticket`) avec agent, ticket (« ticket inconnu » si absent ou non
  numérique), statut, durée (`a duré …` si terminé, `depuis …` sinon), jetons consommés (« — »
  sans mesure) et dernier événement (UTC). Les 500 sessions les plus récentes sont lues, puis
  filtrées, puis les **100** premières affichées. Une ligne avec au moins un événement
  `PostToolUseFailure` est mise en évidence avec son nombre d'erreurs.

Statut d'une session : « terminé » si son dernier événement est un `SubagentStop` ; sinon
« actif » (dernier événement il y a moins de 2 min), « en attente » (2 à 15 min), « terminé »
(au-delà de 15 min). « erreur » n'est pas un état mais un filtre (au moins une erreur).

**Paramètres de requête** (méthode `GET`, formulaire `#history`, tous optionnels, combinés en ET) :

| Paramètre | Valeurs valides | Défaut (absent ou vide) | Effet |
|---|---|---|---|
| `agent` | nom d'agent (`agent_type`, à défaut `agent_id`), tel que listé dans le menu | aucun filtre | garde les sessions de cet agent ; un nom inconnu donne zéro ligne |
| `statut` | `actif`, `en attente`, `terminé`, `erreur` | aucun filtre | `erreur` : au moins un `PostToolUseFailure` ; les autres : statut exact |
| `duree_min` | entier décimal de 0 à 999 999 (minutes) | aucun filtre | garde les sessions dont la durée est d'au moins ce nombre de minutes |

Valeur inconnue : **ignorée, sans erreur** (toujours 200, jamais 4xx) et jamais répétée telle
quelle dans la page : `statut` hors liste, `duree_min` non numérique, négatif, décimal ou
≥ 1 000 000 sont traités comme absents (limite connue : un chiffre non ASCII tel que `²` fait échouer
l'analyse, défaut suivi par #121 ; l'entrée valide reste les chiffres `0-9`) ; `agent` n'est pas validé (liste fermée absente) mais
une valeur inconnue ne correspond à aucune session. Les filtres retenus sont rappelés
(« Filtres actifs », lien « Effacer le filtre ») ; sans résultat : « Aucun résultat pour ce
filtre » ; sans session : « Aucune session sur les 7 derniers jours ». Les filtres ne touchent
ni le flux d'événements, ni le tableau « Agents », ni `GET /stream`. Pas de pagination ni de
tri configurable.

États : vide (« Aucun agent n'a encore travaillé »), erreur (bandeau `role="alert"`,
« Réessayer »). Réponses en `Cache-Control: no-store` (pas de cache, filtres compris). Temps
réel : voir `GET /stream`. Hors périmètre : multi-comptes.

### `GET /interactions`

Lit la table `events` : les **20 dernières sessions** (`session_id`, triées par dernier `ts`
décroissant ; nombre d'événements, première et dernière heure UTC) et, pour chacune, ses
sous-agents. Un sous-agent = un `agent_id` : premier `SubagentStart` et dernier
`SubagentStop` de la même session ; durée = `Stop - Start` (`2 min 02 s`, `12 s`,
`< 1 s`, `1 h 02 min`), « — » sans `Start` ou sans `Stop` ; état « ○ En cours » /
« ● Terminé ». États vide (« Aucune interaction pour l'instant ») et erreur
(`role="alert"`, « Réessayer ») ; `no-store`. Aucun paramètre de requête (tout paramètre est
ignoré). Même mise à jour en direct que `/activity`.

### `GET /stream`

Flux Server-Sent Events (`Content-Type: text/event-stream`, `Cache-Control: no-store`,
`X-Accel-Buffering: no`). Session exigée : **401** sans cookie valide (réponse vide, pas de
redirection : un `EventSource` ne la suivrait pas).

- `id:` = `rowid` croissant de la table `events` (ordre d'insertion ; la table ne doit
  jamais être compactée par `VACUUM`, qui renumérote) ;
- `data:` = JSON sur une ligne : `event_id`, `ts`, `time_label` (UTC), `hook_event_name`,
  `agent_type`, `agent_id`, `tool_name` (`null` si absent) ; ni `session_id` ni autre donnée ;
- première ligne `retry: 2000` ; commentaire `: keepalive` toutes les 15 s sans événement ;
- sondage de la base toutes les 0,5 s : un événement ingéré est émis en moins de 2 s ;
- curseur de départ : en-tête `Last-Event-ID` (reprise automatique du navigateur), sinon
  paramètre `?after=<id>` (la page `/activity` ou `/interactions` embarque son dernier id dans
  `data-last-id`), sinon dernier id connu (rien n'est rejoué). Valeur illisible : ignorée.
  Reprise = événements d'`id` strictement supérieur : ni perte ni doublon ;
- une connexion dure 5 min au plus, puis le serveur la ferme : le navigateur reprend avec
  `Last-Event-ID` et la session est revérifiée (expirée : 401, le flux s'arrête).

Client : `static/live.js` (JS maison, ~80 lignes, pas l'extension HTMX `sse` : aucune
dépendance à vendoriser). Il ajoute la ligne au flux (`<ol data-feed>`, 100 lignes max, sans
doublon d'`event_id`) puis rafraîchit le tableau « Agents » / les sessions en rechargeant la
page et en remplaçant la zone `data-refresh`. DOM construit par `textContent` (aucun HTML
injecté).
### `GET /progress` et `GET /api/v1/progress`

Spec : `docs/design/progress.md`. Schéma : [`progress.schema.json`](progress.schema.json) (aligné sur le
modèle pydantic `dashboard.progress.Progress` par `tests/test_progress.py`). Réponses en `no-store`.

- **GitHub** (décision D20 : dépôt public, **aucun jeton**) : API REST `api.github.com`, dépôt
  `VVA_GITHUB_REPO` (défaut `chebihiF/viral-video-agents`, forme `propriétaire/nom` validée). Trois
  lectures : `issues` (tickets **et** PR, `state=all`, `Link: rel="next"` suivi jusqu'à 5 pages = 500 au
  plus, seuls liens `api.github.com` suivis), `tags`, `commits` (50 derniers). Cache mémoire de **3 min**
  partagé, requêtes conditionnelles (ETag ; un 304 ne compte pas dans la limite de 60 requêtes/heure).
  Toute erreur (réseau, JSON, `IncompleteRead`…) : dernières données connues (ou vides) et
  `github_error_at` renseigné (bandeau `role="alert"`), pas de nouvelle tentative avant l'échéance du
  cache. Sur 403/429 de limite, aucune lecture avant `X-RateLimit-Reset` (attente plafonnée à 1 h).
  Aucun détail d'erreur n'est exposé.
- **Statuts** : épique = ticket labellisé `epic`, ses tickets = ligne `Épique : #n` du corps ; sans lien :
  groupe « Sans épique » (`id` 0). Livré = fermé avec `state_reason = completed` (fermé « not planned » :
  exclu) ; revue = PR ouverte dont le corps contient `Closes/Fixes/Resolves #n` (ou le titre `(#n)`) ;
  en cours = label `in-progress` ; prêt = `P0`/`P1` non `blocked` ; backlog = le reste. Exclus : PR,
  épiques, labels `decision`, `wontfix`, `duplicate`, `invalid`, `question`. Pourcentage =
  livrés × 100 ÷ total, arrondi vers le bas.
- **Sprint** : `{"available": false, "title": null}` (« bientôt disponible », branché par #9).
- **Agents** : 500 derniers événements, un agent par (`agent_type`, `agent_id`) ; événements sans
  `agent_type` ignorés. Actif = dernier événement ≠ `SubagentStop` et de moins de 30 min. `since` = dernier
  `SubagentStart`. Cartes : tous les actifs, puis 5 inactifs au plus (le plus récent de chaque rôle, hors
  rôles actifs). Modèle et étape déduits du rôle (table dans le code), étape `pr` si l'outil contient
  `pull_request` ; `issue` reste `null` tant que les événements ne portent pas le ticket.
- **Frise** : tags joints aux commits par SHA (titre = sujet du commit sans le `(#n)` final, `pr` = ce
  `#n`, date = date du commit), plus récent d'abord, 10 au plus ; tag hors des 50 derniers commits :
  listé en dernier, sans date.
- **Actualisation** : `static/progress.js` recharge `#live` toutes les 60 s (aucune dépendance) ; les données GitHub ne changent qu'au plus toutes les 3 min (cache serveur).

Limites connues : la limite de 5 essais/minute est globale (compte unique : un tiers peut verrouiller la connexion une minute) et en mémoire (un seul worker ; remise à zéro au redémarrage). Pas de déconnexion : seule la rotation de `DASHBOARD_SESSION_KEY` révoque les sessions (ticket de suivi).
