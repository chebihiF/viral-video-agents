# Cibles silencieuses : n'affichent que les échecs (économie de tokens).
.SILENT:
.PHONY: help sync lint fmt type test audit check build map run

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

check: lint type test ## Tout ce que la CI vérifie (hors build)
	echo ok

build: ## Image Docker du dashboard
	docker build -q -f apps/dashboard/Dockerfile -t vva-dashboard:local . >/dev/null

map: ## Régénère docs/MAP.md
	python3 infra/scripts/gen_map.py

run: ## Dashboard en local sur :8000
	$(UV) uvicorn dashboard.main:app --reload
