# OpenClaw Job Search

Local strict-mode job discovery pipeline plus Phase 2 manual approval queue for OpenClaw.

## Current Scope

Current pipeline does:

- collect jobs from `Greenhouse`, `Lever`, `Ashby`, `Workday`, custom/generic careers pages, optional `SerpAPI`, `Remotive`, `Himalayas`, `RemoteOK`, `We Work Remotely` and `Hacker News` "Who is hiring"
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

- `config/` input config (`profile.json`, `rules.json`, `watchlist.json`, `feedback.json`)
- `src/openclaw_jobsearch/` pipeline code
- `src/openclaw_jobsearch/web/` FastAPI layer for the review UI
- `frontend/` React + Vite review UI
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
PYTHONPATH=src python3 -m openclaw_jobsearch.cli run
```

## Useful Commands


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

## Web Review UI

A local two-pane triage surface over the same pipeline.
The API is a thin skin over the existing functions, so the CLI and the web UI can never drift apart.

Install the extra dependencies once:

```bash
pip install 'fastapi>=0.115' 'uvicorn[standard]>=0.32'
cd frontend && npm install
```

Run both halves in two terminals:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli serve --workspace-root .   # API on :8099
cd frontend && npm run dev                                                  # UI on :51
```

Open `http://localhost:5173`.

What it does:

- Two panes, keyboard driven: `j` / `k` move, `a` approve, `r` reject, `o` open the posting.
- Reject takes a plain-English reason, an LLM maps it onto a feedback rule, and the proposal shows its blast radius before you confirm.
- Rules panel lists every learned rule with hit counts, so dead or over-eager rules are visible and removable.
- Refresh runs the pipeline in the background; the queue stays usable while it fetches.
- Approved jobs track `applied_at` separately, because approving is not applying.
- The Artifacts tab generates and previews the tailored resume and cover letter inline.

The LLM step needs `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.
Without one, rejection still works and falls back to a conservative keyword heuristic that prefers blocking only the single job.

## Feedback

Feedback is recorded against a reusable *dimension* of a job, not the job itself.
Rejecting one reposter listing does nothing about the next eight, so a verdict is stored as a rule on the apply domain, the company, a title phrase, or the discovery source.

Rules live in `config/feedback.json`.
They are applied during validation on every future run, and can be retro-applied to jobs already sitting in the review queue.

Rule kinds:

| kind | matches | example |
|---|---|---|
| `apply_domain` | host of the apply/job URL, suffix match | `up.railway.app` covers every subdomain |
| `company` | company name, case-insensitive | `Marsbased` |
| `title_pattern` | phrase in the job title | `technical mentor` |
| `source` | discovery source | `serpapi` |
| `job` | one job slug, does not generalize | a scraped landing page |

Add a rule from an explicit value:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback add --workspace-root . \
  --kind apply_domain --value up.railway.app \
  --reason "hobby-hosted job reposter, not an employer"
```

Or reject a job and generalize it in one step, deriving the value from that job:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback from-job <job_slug> --workspace-root . \
  --kind apply_domain --reason "job reposter site"
```

Both forms immediately revoke every matching job already in the queue.
Other commands:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback list --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback apply --workspace-root . --dry-run
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback remove <rule_id> --workspace-root .
```

Revoked jobs move to `review_status = rejected` with `approval_reason` recording the rule id, so every revocation is auditable.

## Rule Tuning (Replay)

`replay` re-scores every job already stored in `data/jobs.db` against a candidate rules file.
It makes no network calls, and judges each job as of the date of the run that discovered it, so the freshness rule stays meaningful on an old archive.

Score the active rules:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli replay --workspace-root .
```

A/B a candidate rules file against a baseline:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli replay --workspace-root . \
  --baseline config/rules_baseline.json \
  --rules config/rules_candidate.json
```

The comparison prints the acceptance delta plus a sample of newly accepted jobs, so a rule change can be inspected before it is adopted.
`config/rules_baseline.json` is the original strict ruleset, kept for comparison.

## Review Queue

Strict accepted jobs are automatically moved into `pending_review` in `SQLite`.

Useful commands:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review list --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review show <job_slug> --workspace-root .
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review approve deel-automation-specialist-emea-2b131cb2-cc85-41aa-a856-be6029878ec8 --workspace-root . --reason "good fit"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review reject kraken-lead-software-engineer-kraken-utilities-oss-telco-integrations-f2e2022c-9494-4601-a115-fbcdc6d8f282 --workspace-root . --reason "hybrid"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review reject canonical-remote-distributed-systems-engineer-python-go-cloud-testing-eyjqb2jfdgl0bguioijszw1vdgugrglzdhjpynv0zwqgu3lzdg --workspace-root . --reason "no extensive experience in distributed ststem adn cloud"

PYTHONPATH=src python3 -m openclaw_jobsearch.cli review export --workspace-root .
```

Review state is separate from strict validation:

- `validation_status` stays `accepted` or `rejected`
- `review_status` is one of `pending_review`, `approved`, `rejected`, or `archived`

Only approved jobs are emitted into `outputs/approved_jobs_latest.jsonl` for Phase 3 artifact generation.

## Phase 3 Generation

Phase 3 creates per-job artifacts under `artifacts/jobs/<job_slug>/`:

- `job_description.md`
- `resume.md` / `resume.html` / `resume.pdf`
- `cover_letter.md` / `cover_letter.html` / `cover_letter.pdf`
- `artifact_meta.json`

Phase 3 uses an API-backed LLM generator with ATS-optimized output and automated PDF rendering:

```bash
export OPENAI_API_KEY=
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 generate flex-peeps-full-stack-developer-python-react-fastapi-100-remote-eyjqb2jfdgl0bguioijgdwxslvn0ywnrierldmvsb3blcibqexrob24s --workspace-root . --provider openai --model gpt-4.1-mini
```

Or with Anthropic:

```bash
export ANTHROPIC_API_KEY=
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 generate --workspace-root . --provider anthropic
```

Behavior:

- If you pass `<job_slug>`, generation runs only for that approved job.
- If you omit `<job_slug>`, generation scans all approved jobs and skips ones that already have a resume PDF in their artifact folder.

Optional LLM environment variables:

- `OPENCLAW_LLM_PROVIDER`
- `OPENCLAW_LLM_MODEL`
- `OPENCLAW_LLM_TEMPERATURE`
- `OPENCLAW_LLM_MAX_TOKENS`

LLM mode is designed to stay grounded in the source resume text and records provider/model metadata in `artifact_meta.json`.

Regenerate pdfs:

```bash
openclaw-jobs phase3 regen-pdf <job_slug>
```