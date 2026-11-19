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

## Limites

- Échec d'une tâche : noté à la main (`fail`) par le tech lead ; la télémétrie ne le détecte
  pas. Le ratio du rapport porte sur les échecs **d'outils** (`PostToolUseFailure`), à câbler.
- Tokens antérieurs au hook `link` : « sans ticket ». Une session sur `main` aussi.
- Le label `complex` n'est pas lu sur GitHub : le passer à `route --label`.
- Coûts en euros : hors périmètre.
