# OpenClaw Job Search

Local strict-mode job discovery pipeline plus Phase 2 manual approval queue for OpenClaw.

## Current Scope

Current pipeline does:

- collect jobs from `Greenhouse`, `Lever`, simple custom careers pages, optional `SerpAPI`, and `Remotive`
- normalize jobs into one schema
- apply strict validation and rejection reasons
- enqueue strict accepted jobs into a manual review queue
- dedupe and export a shortlist
- persist results to `SQLite` and `JSONL`
- generate Phase 3 per-job resume and cover-letter drafts after approval

Current watchlist includes direct boards for companies such as `Circle.so`, `GitLab`, `Automattic`, `Fingerprint`, and `Metabase`, plus a custom careers-page source for `MailerLite`.

Current pipeline does not do:

- auto-apply

## Project Layout

- `config/` input config (`profile.json`, `rules.json`, `watchlist.json`)
- `src/openclaw_jobsearch/` pipeline code
- `data/` SQLite database and validated/raw snapshots
- `outputs/` shortlist and run summary exports
- `artifacts/jobs/` generated per-job Phase 3 artifacts

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
- `outputs/phase3_latest.json`
- `outputs/run_summary_latest.json`
- `data/validated_jobs_latest.jsonl`
- `data/jobs.db`

## Review Queue

Strict accepted jobs are automatically moved into `pending_review` in `SQLite`.

Useful commands:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review list --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review show <job_slug> --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review approve deel-automation-specialist-emea-2b131cb2-cc85-41aa-a856-be6029878ec8 --workspace-root . --reason "good fit"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review reject <job_slug> --workspace-root . --reason "not a fit"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review export --workspace-root .
```

Review state is separate from strict validation:

- `validation_status` stays `accepted` or `rejected`
- `review_status` is one of `pending_review`, `approved`, `rejected`, or `archived`

Only approved jobs are emitted into `outputs/approved_jobs_latest.jsonl` for Phase 3 artifact generation.

## Phase 3 Generation

Phase 3 creates per-job artifacts under `artifacts/jobs/<job_slug>/`:

- `job_description.md`
- `resume.md`
- `cover_letter.md`
- `artifact_meta.json`

Phase 3 uses an API-backed LLM generator with grounding metadata:

```bash
export OPENAI_API_KEY=
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 generate --workspace-root . --provider openai --model gpt-4.1-mini
```

Or with Anthropic:

```bash
export ANTHROPIC_API_KEY=
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 generate --workspace-root . --provider anthropic
```

Optional LLM environment variables:

- `OPENCLAW_LLM_PROVIDER`
- `OPENCLAW_LLM_MODEL`
- `OPENCLAW_LLM_TEMPERATURE`
- `OPENCLAW_LLM_MAX_TOKENS`

LLM mode is designed to stay grounded in the source resume text and records provider/model metadata in `artifact_meta.json`.
