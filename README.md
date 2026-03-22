# OpenClaw Job Search

Local strict-mode job discovery pipeline plus Phase 2 manual approval queue for OpenClaw.

## Current Scope

Current pipeline does:

- collect jobs from `Greenhouse`, `Lever`, optional `SerpAPI`, and `Remotive`
- normalize jobs into one schema
- apply strict validation and rejection reasons
- enqueue strict accepted jobs into a manual review queue
- dedupe and export a shortlist
- persist results to `SQLite` and `JSONL`

Current pipeline does not do:

- cover letters
- CV tailoring
- auto-apply

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
- `outputs/review_queue_latest.md`
- `outputs/review_queue_latest.csv`
- `outputs/applications_export.csv`
- `outputs/approved_jobs_latest.jsonl`
- `outputs/run_summary_latest.json`
- `data/validated_jobs_latest.jsonl`
- `data/jobs.db`

## Review Queue

Strict accepted jobs are automatically moved into `pending_review` in `SQLite`.

Useful commands:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review list --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review show <job_slug> --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review approve <job_slug> --workspace-root . --reason "good fit"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review reject <job_slug> --workspace-root . --reason "not a fit"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review export --workspace-root .
```

Review state is separate from strict validation:

- `validation_status` stays `accepted` or `rejected`
- `review_status` is one of `pending_review`, `approved`, `rejected`, or `archived`

Only approved jobs are emitted into `outputs/approved_jobs_latest.jsonl` for Phase 3 artifact generation.
