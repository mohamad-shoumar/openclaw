# OpenClaw Job Search - Phase 1

Local strict-mode job discovery pipeline for OpenClaw.

## Current Scope

Phase 1 currently does:

- collect jobs from `Greenhouse`, `Lever`, optional `SerpAPI`, and `Remotive`
- normalize jobs into one schema
- apply strict validation and rejection reasons
- dedupe and export a shortlist
- persist results to `SQLite` and `JSONL`

Phase 1 does not do:

- LLM scoring
- approval queue
- cover letters
- CV tailoring

## Project Layout

- `config/` input config (`profile.json`, `rules.json`, `watchlist.json`)
- `src/openclaw_jobsearch/` pipeline code
- `data/` SQLite database and validated/raw snapshots
- `outputs/` shortlist and run summary exports
- `artifacts/jobs/` reserved for later per-job artifacts

## Requirements

- Python `3.11+`
- optional: `SERPAPI_API_KEY` if you want SerpAPI results included

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

From the repo root:

```bash
openclaw-jobs run
```

Or without installing the script:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli run
```

## Useful Commands

Compile the code:

```bash
python3 -m compileall src
```

Run with explicit directories:

```bash
openclaw-jobs run --workspace-root . --config-dir config --data-dir data --output-dir outputs
```

Run with SerpAPI enabled:

```bash
export SERPAPI_API_KEY=
openclaw-jobs run
```
## Outputs

Main generated files:

- `outputs/shortlist_latest.md`
- `outputs/shortlist_latest.csv`
- `outputs/applications_export.csv`
- `outputs/run_summary_latest.json`
- `data/validated_jobs_latest.jsonl`
- `data/jobs.db`
