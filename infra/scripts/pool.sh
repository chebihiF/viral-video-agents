#!/usr/bin/env bash
# Pool de postes : un ticket = une branche neuve sur un poste libre.
# Postes = worktrees liés situés sous <checkout principal>/.claude/worktrees/ (jamais créés ni supprimés ici).
# Usage : infra/scripts/pool.sh [--dry-run] <n>   (GH=commande gh à utiliser, défaut : gh)
set -euo pipefail
MAX_POSTES=3
GH="${GH:-gh}"
dry=0
if [ "${1:-}" = "--dry-run" ]; then dry=1; shift; fi
n="${1:-}"
[[ "$n" =~ ^[0-9]+$ ]] || { echo "pool : usage pool.sh [--dry-run] <n>" >&2; exit 2; }

# Refus si stop-agents ou pas « Prêt » ; sinon imprime <feat|fix|chore>/<n>-<slug>.
branche() {
  "$GH" issue view "$n" --json title,labels,projectItems | python3 -c '
import json, re, sys, unicodedata
n, t = sys.argv[1], json.load(sys.stdin)
labels = {lab["name"] for lab in t["labels"]}
statuts = {(i.get("status") or {}).get("name") for i in t.get("projectItems") or []}
if "stop-agents" in labels:
    sys.exit(f"pool : ticket {n} labellisé stop-agents, refus")
if "Prêt" not in statuts:
    sys.exit(f"pool : ticket {n} pas en « Prêt », refus")
typ = "fix" if "type:bug" in labels else "chore" if "type:chore" in labels else "feat"
titre = re.sub(r"^\w+:\s*", "", t["title"])
mots = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", titre).encode("ascii", "ignore").decode().lower())
print(f"{typ}/{n}-" + ("-".join(mots[:4]) or "ticket"))
' "$n"
}

# Résumé des PR de la branche : OUVERTE | FERMEE_HEAD (HEAD = tête de PR) | FERMEE | AUCUNE.
etat_pr() {
  local p="$1" br="$2" head="$3"
  [ -n "$br" ] || { echo AUCUNE; return; }
  (cd "$p" && "$GH" pr list --head "$br" --state all --json state,headRefOid) | python3 -c '
import json, sys
prs = json.load(sys.stdin)
if any(p["state"] == "OPEN" for p in prs): print("OUVERTE")
elif any(p["headRefOid"] == sys.argv[1] for p in prs): print("FERMEE_HEAD")
else: print("FERMEE" if prs else "AUCUNE")
' "$head"
}

# Raison d'occupation d'un poste ; vide si libre.
occupation() {
  local p="$1" head br arbre
  # Pas d'errexit dans $(...) (bash 3.2) : chaque échec git rend le poste occupé.
  arbre="$(git -C "$p" status --porcelain -- . ':!.claude' ':!CLAUDE.md')" || { echo "évaluation impossible"; return; }
  [ -z "$arbre" ] || { echo "arbre modifié"; return; }
  head="$(git -C "$p" rev-parse HEAD)" || { echo "évaluation impossible"; return; }
  br="$(git -C "$p" branch --show-current)" || { echo "évaluation impossible"; return; }
  local non_pousse
  non_pousse="$(git -C "$p" rev-list -n1 HEAD --not --remotes=origin)" || { echo "évaluation impossible"; return; }
  local etat
  etat="$(etat_pr "$p" "$br" "$head")" || etat=INCONNU
  case "$etat" in
    OUVERTE) echo "PR ouverte" ;;
    FERMEE_HEAD) ;;
    FERMEE) [ -z "$non_pousse" ] || echo "commits non poussés" ;;
    AUCUNE)
      if [ -n "$non_pousse" ]; then echo "commits non poussés"
      elif ! git -C "$p" merge-base --is-ancestor HEAD origin/main; then echo "sans PR, hors origin/main"
      fi ;;
    *) echo "état PR inconnu" ;;
  esac
}

br="$(branche)"
verrou="$(git rev-parse --path-format=absolute --git-common-dir)/vva-pool.lock"
mkdir "$verrou" 2>/dev/null || { echo "pool : déjà en cours (verrou $verrou), refus" >&2; exit 1; }
trap 'rmdir "$verrou"' EXIT
main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
git -C "$main" fetch -q origin
postes="$(git -C "$main" worktree list --porcelain | sed -n 's/^worktree //p' \
  | grep -F "$main/.claude/worktrees/" | sort || true)"
[ -n "$postes" ] || { echo "pool : aucun poste sous $main/.claude/worktrees/" >&2; exit 1; }
nb="$(grep -c . <<<"$postes")"
[ "$nb" -le "$MAX_POSTES" ] || { echo "pool : $nb postes sous $main/.claude/worktrees/ (max $MAX_POSTES), refus" >&2; exit 1; }

libre="" occupes=""
while IFS= read -r p; do
  raison="$(occupation "$p")" || raison="évaluation impossible"
  if [ -z "$raison" ]; then libre="$p"; break; fi
  occupes+="  $p : $raison"$'\n'
done <<<"$postes"

if [ -z "$libre" ]; then
  printf 'pool : aucun poste libre, refus ; occupés :\n%s' "$occupes" >&2
  exit 1
fi
if [ "$dry" = 1 ]; then
  echo "pool : dry-run, $br sur $libre"
  exit 0
fi
git -C "$libre" switch -q --no-track -c "$br" origin/main
make --no-print-directory -C "$libre" bootstrap
echo "pool : $br"
echo "$libre"
