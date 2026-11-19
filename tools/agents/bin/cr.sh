#!/bin/sh
# CR d'agents en base SQLite (#61). Voir docs/agents.md.
# record (hooks SubagentStop et SessionEnd, JSON sur stdin) | status (≤ 10 lignes, depuis la dernière lecture).
exec python3 "$(dirname "$0")/../cr.py" "$@"
