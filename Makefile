# Single entry point for every command in this repo.
.DEFAULT_GOAL := help
# PYTHONPATH=src so targets work whether or not the editable install is current.
PY := PYTHONPATH=src .venv/bin/python

.PHONY: help install run serve review export artifacts resume replay test lint fmt clean

help: ## List available targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

install: ## Create the venv and install the package with dev extras
	python3 -m venv .venv
	$(PY) -m pip install -e '.[dev]'
	cd frontend && npm install

run: ## Run the discovery pipeline over every configured source
	$(PY) -m openclaw_jobsearch.cli run

serve: ## Serve the review API (pair with `make ui`)
	$(PY) -m openclaw_jobsearch.cli serve

ui: ## Run the React review UI against a local API
	cd frontend && npm run dev

review: ## List the pending review queue
	$(PY) -m openclaw_jobsearch.cli review list

export: ## Re-export review queue and approved-job contracts
	$(PY) -m openclaw_jobsearch.cli review export

artifacts: ## Generate tailored resume + cover letter for approved jobs
	$(PY) -m openclaw_jobsearch.cli phase3 generate

resume: ## Render candidate/resume_master.md to PDF
	scripts/render_resume.sh

replay: ## Re-score the stored corpus against config/rules.json, no network
	$(PY) -m openclaw_jobsearch.cli replay --rules config/rules.json

test: ## Run the test suite
	$(PY) -m pytest

lint: ## Lint with ruff
	$(PY) -m ruff check src

fmt: ## Auto-fix lint and import order
	$(PY) -m ruff check --fix src

clean: ## Remove build artifacts and caches
	rm -rf build dist *.egg-info src/*.egg-info
	find src -name __pycache__ -type d -exec rm -rf {} +
