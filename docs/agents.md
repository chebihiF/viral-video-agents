# Agents : mesure des tokens et routage des modèles (#19)

Outils : `tools/agents/bin/token-report.sh` (enveloppe) et `tools/agents/token_report.py`
(stdlib, Python 3.9 du Mac comme 3.12). Règles : `tools/agents/config/model-routing.yml`.

## Rapport (≤ 15 lignes, compteurs seuls)

```sh
tools/agents/bin/token-report.sh --db vva.db          # = token-report.sh report --db vva.db
```

Sortie : total, une ligne par ticket (7 plus gros, puis « autres tickets », puis « sans
ticket ») avec la répartition par agent, totaux par agent et par modèle, ratio d'échecs
d'outils par ticket, tickets au-dessus de 2× la médiane, promotions vers Opus. Jamais de
prompt, de contenu ni d'identifiant de session ; un nom d'agent ou de modèle hors
`[A-Za-z0-9._:-]{1,64}` est affiché `?`.

### Sources

| Donnée | Source | Note |
|--------|--------|------|
| tokens par session, agent, modèle | table `token_usage` ([contrat](../contracts/token-usage.md)) | `agent_id` NULL = fil principal |
| appels et échecs d'outils | table `events` ([contrat](../contracts/agent-event.schema.json)) | `PostToolUse`, `PostToolUseFailure` |
| session -> ticket, échecs notés, promotions | journal local `~/.vva/agents-journal.jsonl` ([contrat](../contracts/agents-journal.schema.json)) | `VVA_AGENTS_JOURNAL` pour un autre chemin |

La base est lue **en lecture seule** depuis un fichier SQLite local (`--db`, sinon
`VVA_DB_PATH`, sinon `./vva.db`). Pas d'appel réseau : la base de prod (`/data/vva.db` du
conteneur dashboard) se lit sur une copie cohérente (API `backup` de `sqlite3`, puis `scp`),
faite à la main par le parent. Le dashboard n'expose pas ces agrégats par ticket (hors
périmètre, #7).

### Rattachement d'une session à un ticket

La télémétrie ne porte ni branche ni ticket (seulement `session_id`). Le hook `link` lit le
JSON du hook (`session_id`, `cwd`), lit la branche du poste (`git branch --show-current`) et,
si elle suit `feat|fix|chore|docs|test|ci/<n>-…`, note `session -> n` dans le journal
(seulement quand le ticket change). Un point de tokens va au dernier rattachement antérieur
de sa session (une session réutilisée pour un autre ticket du même poste est donc découpée
dans le temps), sinon au premier ; sans rattachement : « sans ticket ». Les sous-agents
partagent la session du fil qui les lance.

Câblage local (`.claude/settings.json`, jamais commité), à côté des hooks de
[`tools/telemetry`](../tools/telemetry/README.md) :

```json
"SessionStart":      [{ "hooks": [{ "type": "command", "timeout": 2,
  "command": "\"$CLAUDE_PROJECT_DIR/tools/agents/bin/token-report.sh\" link" }] }],
"UserPromptSubmit":  [{ "hooks": [{ "type": "command", "timeout": 2,
  "command": "\"$CLAUDE_PROJECT_DIR/tools/agents/bin/token-report.sh\" link" }] }],
"PostToolUseFailure": [{ "matcher": "*", "hooks": [{ "type": "command", "timeout": 2,
  "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/telemetry/hook_emit.py\"" }] }]
```

Le hook `link` ne garde que `session_id` et le numéro de ticket (le prompt du hook
`UserPromptSubmit` n'est jamais écrit) et ne bloque jamais (code 0, git limité à 1,5 s).

## Routage des modèles

Priorité : agent à modèle fixe > label du ticket > escalade > défaut.

| Règle | Modèle |
|-------|--------|
| défaut | Sonnet |
| agent `scout` | Haiku (toujours) |
| agent `reviewer` | Opus (toujours) |
| agent `tech-lead` (session principale) | Sonnet (toujours) |
| label `complex` | Opus |
| 2 échecs notés pour le même agent sur le même ticket | Opus pour la relance |

```sh
tools/agents/bin/token-report.sh fail 42 dev-backend               # noter un échec
tools/agents/bin/token-report.sh route dev-backend --ticket 42 --label complex
```

`route` affiche l'identifiant du modèle à passer à l'agent (`model:` de l'appel) et, si
c'est une promotion, l'écrit dans le journal avec son motif (`complex`, `2 échecs`), une
seule fois par (ticket, agent, motif). Les échecs se comptent par (ticket, agent) : l'échec
d'un `qa` ne promeut pas `dev-backend`. Ticket ≥ 1 ; agent et label : `[A-Za-z0-9._:-]{1,64}`. Le
rapport liste ces promotions ; un usage d'Opus par un agent routé ailleurs sans promotion
écrite apparaît « (non tracée) ».

Les fichiers d'agents (`.claude/agents/*.md`) restent locaux et ne sont jamais commités : leur
champ `model` doit égaler le modèle de base du routage. `tools/agents/tests` le vérifie quand
ces fichiers sont présents sur le poste (test ignoré en CI).

## Passation automatique (#83)

Quand le contexte d'une session gonfle, la note de passation et la session suivante sont
automatiques. Outil : `tools/agents/bin/handoff.sh` (`tools/agents/handoff.py`, stdlib, Python
3.9). Le tech lead reste sur Sonnet par défaut (`tech-lead: sonnet` du routage) ; Opus seulement
pour le label `complex` (agents d'implémentation) et le `reviewer`.

```sh
tools/agents/bin/handoff.sh check                      # hook UserPromptSubmit (JSON sur stdin)
tools/agents/bin/handoff.sh post  --note-file note.md   # valider et poster dans #21
tools/agents/bin/handoff.sh latest                      # dernière note marquée de #21
tools/agents/bin/handoff.sh relay --note-file note.md   # post, puis session neuve
```

- **Seuil** : contexte = dernier `usage` du fil principal du transcript (input + cache lu +
  cache créé ; les sous-agents sont ignorés). L'alerte douce à 100 000 tokens est gérée ailleurs :
  `check` ne fait rien en dessous de **150 000**. Au seuil, une seule fois par session (marqueur
  `~/.vva/handoff/<session>.done`, `VVA_HANDOFF_DIR`), il émet `additionalContext` : le tech lead
  écrit la note puis lance `relay`. `check` ne bloque jamais (exit 0, silencieux si erreur).
- **Note** (contrat) : ≤ 10 lignes, rubriques `Fait`, `Reste`, `Fichiers clés`, `Pièges` en début
  de ligne ; refusée si elle contient un motif de secret (`token: …`, `api_key=…`, `ghp_…`,
  `-----BEGIN`, chaîne alphanumérique de 32+ caractères…). Aucune donnée d'enfant.
- **Poste** : commentaire de #21 commençant par `<!-- passation -->` (`gh`, dépôt
  `HANDOFF_REPO`). `latest` ne renvoie que la dernière note marquée, d'un auteur de
  `HANDOFF_AUTHORS` (défaut `chebihiF,infinitySoftMA`), jamais l'historique.
- **Relais** : `relay` poste, puis lance `claude -p` (surchargeable : `HANDOFF_CMD`) détaché, le
  prompt (la dernière note seule) sur stdin. Si le démarrage est refusé ou échoue dans les 5 s :
  la note reste postée, une ligne sur stderr prévient le parent, exit 0. La sortie de la session
  relayée va dans `~/.vva/handoff/relay.log` (600, jamais le prompt).

Câblage à faire par le parent (`.claude/settings.json`, local, jamais commité) :

```json
"UserPromptSubmit": [{ "hooks": [{ "type": "command", "timeout": 5,
  "command": "\"$CLAUDE_PROJECT_DIR/tools/agents/bin/handoff.sh\" check" }] }]
```

## Compte rendu (CR) des agents en base (#61)

Chaque agent termine sa tâche par un CR de **5 lignes au plus**, une rubrique par ligne en début
de ligne : `Fait : …`, `Fichiers : …`, `Reste : …`, `Blocage : … (ou « aucun »)`. Les
définitions d'agents (`.claude/agents/*.md`, locales) reprennent ce format dans leur « Rapport
final ». Outil : `tools/agents/bin/cr.sh` (`tools/agents/cr.py`, stdlib, Python 3.9).

- **Écriture** : `cr.sh record` (hooks `SubagentStop` et `SessionEnd`, JSON sur stdin) lit
  `last_assistant_message` (sinon la fin du transcript), extrait les rubriques et ajoute une ligne
  à `agent_reports` ([contrat](../contracts/agent-report.schema.json)) : `ticket` (numéro de la
  branche du poste, sinon NULL), `agent` (`agent_type` ; `tech-lead` en fin de session), `ts`
  (UTC), `summary`, `status` (`blocked` si Blocage renseigné, `partial` si Reste renseigné, `done`
  sinon, `no-cr` si aucune rubrique : rien n'est alors recopié). Une ligne contenant un motif de
  secret est remplacée par `[masqué]`. Jamais de prompt ni de donnée d'enfant dans un CR.
- **Base** : SQLite locale `~/.vva/agent-reports.db` (600 ; `VVA_CR_DB` pour un autre chemin).
- **Silence** : base illisible, JSON invalide ou plus de 1,5 s : code 0, aucune sortie, l'agent n'est
  jamais interrompu.
- **Lecture** : `make status` (`cr.sh status`) affiche au plus 10 lignes, les CR arrivés depuis la
  dernière lecture (marqueur `last_read` dans la base), les plus récents en dernier ; « Aucun
  nouveau CR » sinon. Base illisible : une ligne sur stderr, code 1.

Câblage local (`.claude/settings.json`, jamais commité), à côté des autres hooks :

```json
"SubagentStop": [{ "hooks": [{ "type": "command", "timeout": 2,
  "command": "\"$CLAUDE_PROJECT_DIR/tools/agents/bin/cr.sh\" record" }] }],
"SessionEnd":   [{ "hooks": [{ "type": "command", "timeout": 2,
  "command": "\"$CLAUDE_PROJECT_DIR/tools/agents/bin/cr.sh\" record" }] }]
```

## CI rouge (#76)

- Chaque job de `ci.yml` a `timeout-minutes: 15`.
- `.github/workflows/ci-watch.yml` (déclenché par `workflow_run` de `ci`, sans checkout du code de la PR) :
  si la CI échoue, il pose le label `ci-rouge` sur la PR, la commente avec le **nom** des jobs en échec
  (jamais de log) et ajoute une ligne dans #21. Si un job a atteint le timeout (≥ 14,5 min) lors de la
  première tentative, il relance une fois les jobs en échec (`gh run rerun --failed`) et signale quand même (une relance par `GITHUB_TOKEN` ne redéclenche pas `ci-watch`). Le label est retiré au vert.
- Aucun polling : l'alerte arrive à la fin du run, soit en moins de 10 min dans le cas normal.
- L'Auto-fix de l'app (monitor de PR) reste complémentaire pour relancer la session de la PR ; il se règle dans l'app, pas ici.

## Brancher les hooks

1. Ouvrir [`docs/examples/hooks-settings.example.json`](examples/hooks-settings.example.json) (aucun secret).
2. Fusionner son bloc `"hooks"` dans votre `.claude/settings.json` local (jamais commité) en retirant les appels à l'ancien `log_event.sh`.
3. Exporter `VVA_INGEST_URL` et `VVA_INGEST_TOKEN` (shell, ou bloc `"env"` des réglages) ; `VVA_CONTROL_URL` pour la pause.
4. Lancer `make telemetry-check` : code 0 = hooks et variables en place (jamais de valeur affichée) ; `make telemetry-check LIVE=1` envoie en plus un événement factice.
5. Sinon, le message en français dit quoi corriger ; relancer jusqu'au vert, puis ouvrir le dashboard.

## Limites

- Échec d'une tâche : noté à la main (`fail`) par le tech lead ; la télémétrie ne le détecte
  pas. Le ratio du rapport porte sur les échecs **d'outils** (`PostToolUseFailure`), à câbler.
- Tokens antérieurs au hook `link` : « sans ticket ». Une session sur `main` aussi.
- Le label `complex` n'est pas lu sur GitHub : le passer à `route --label`.
- Coûts en euros : hors périmètre.
