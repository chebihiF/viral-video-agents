#!/bin/sh
# Tokens par ticket, agent et modèle, ≤ 15 lignes, compteurs seuls (#19). Voir docs/agents.md.
# Sans sous-commande : rapport. Autres : link (hook), fail <ticket> <agent>, route <agent>.
exec python3 "$(dirname "$0")/../token_report.py" "$@"
