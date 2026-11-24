# Cibles silencieuses : n'affichent que les échecs (économie de tokens).
.SILENT:
.PHONY: help sync lint fmt type test audit check mapcheck map run-mock e2e-mock bootstrap ticket ci-b

UV := uv run -q
SH := infra/scripts/quiet.sh

help: ## Liste les cibles
	grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "%-8s %s\n", $$1, $$2}'

sync: ## Installe les dépendances
	uv sync -q

lint: ## ruff (lint + format)
	$(UV) ruff check -q --output-format=concise . && $(UV) ruff format -q --check .

fmt: ## Corrige lint + format
	$(UV) ruff check -q --fix . ; $(UV) ruff format -q .

type: ## mypy strict
	$(SH) $(UV) mypy

test: ## pytest (échecs uniquement)
	$(SH) $(UV) pytest -q -rf --tb=short

audit: ## pip-audit sur les dépendances verrouillées
	uv export -q --frozen --no-hashes --no-emit-workspace -o .venv/req-audit.txt
	$(SH) $(UV) pip-audit -r .venv/req-audit.txt --progress-spinner off

check: lint type test mapcheck ## Tout ce que la CI vérifie
	echo ok

mapcheck: ## docs/MAP.md à jour ? (sinon : make map)
	@python3 infra/scripts/gen_map.py --check

map: ## Régénère docs/MAP.md
	python3 infra/scripts/gen_map.py

run-mock: ## Parcours simulé : DICTEE=fichier (défaut : dictée d'exemple), sortie dans .venv/run-mock
	printf 'Un petit chat vole sur un tapis magique.' > .venv/dictee-mock.txt
	$(UV) python -m orchestrator run --mocks --dictee-file $(or $(DICTEE),.venv/dictee-mock.txt) --out .venv/run-mock

e2e-mock: ## Parcours dictée -> MP4 avec mocks (ffmpeg local requis, sans GPU)
	$(SH) $(UV) pytest -m e2e apps/orchestrator/tests/test_e2e_mock.py

bootstrap: ## Copie .claude/ et CLAUDE.md du checkout principal (VVA_MAIN=chemin)
	infra/scripts/bootstrap_worktree.sh

ticket: ## Ticket N sur un poste libre du pool (N=<n>, DRY=1 pour simuler)
	case '$(N)' in ''|*[!0-9]*) echo "usage : make ticket N=<n> (numérique)" >&2; exit 2;; esac
	infra/scripts/pool.sh $(if $(DRY),--dry-run) '$(N)'

ci-b: ## CI de B sans GPU : contrats, evals --dry-run, garde-fou fichiers Claude
	$(UV) pytest -q tests/contracts infra/scripts/test_no_claude_tracked.py
	$(UV) python evals/classification_eval.py --dry-run
	$(UV) python evals/storyboard_eval.py --dry-run
