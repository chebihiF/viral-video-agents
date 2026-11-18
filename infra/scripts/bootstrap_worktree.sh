#!/usr/bin/env bash
# Copie .claude/ et CLAUDE.md (fichiers locaux, jamais commités) du checkout principal vers le worktree courant.
# Usage : infra/scripts/bootstrap_worktree.sh [SOURCE]  (défaut : $VVA_MAIN, sinon le checkout principal du dépôt)
set -euo pipefail
main="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
src="${1:-${VVA_MAIN:-$main}}"
dst="$(git rev-parse --show-toplevel)"
[ -d "$src/.claude" ] || { echo "bootstrap : $src/.claude introuvable" >&2; exit 1; }
[ "$(cd "$src" && pwd -P)" != "$(cd "$dst" && pwd -P)" ] || { echo "bootstrap : source = destination" >&2; exit 1; }
# Sans écraser l'existant ; sans les worktrees imbriqués ni la télémétrie.
mkdir -p "$dst/.claude"
rsync -a --ignore-existing --exclude worktrees/ --exclude telemetry/ "$src/.claude/" "$dst/.claude/"
[ ! -f "$src/CLAUDE.md" ] || [ -e "$dst/CLAUDE.md" ] || cp "$src/CLAUDE.md" "$dst/CLAUDE.md"
