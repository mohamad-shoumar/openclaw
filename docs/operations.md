# Operations

Every target is in the `Makefile`; run `make help` to list them.
The raw CLI form is shown where a command takes arguments the Makefile does not pass.

All CLI invocations accept `--workspace-root`, `--config-dir`, `--data-dir`, and `--output-dir`.
The defaults are the repo root, `config`, `var/data`, and `var/outputs`.

## Discovery

```bash
make run
```

Fetches every configured source, normalizes, validates, persists, and writes exports.
Takes minutes, mostly waiting on careers-page fetches.

Read the run summary afterwards at `var/outputs/run_summary_latest.json` for source counts and the top rejection reasons.

## Review queue

```bash
make review                                          # list pending
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review show <job_slug>
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review approve <job_slug> --reason "good fit"
PYTHONPATH=src python3 -m openclaw_jobsearch.cli review reject <job_slug> --reason "hybrid, not remote"
make export                                          # rewrite queue and contract exports
```

Or use the UI, which is faster for anything past a handful of jobs:

```bash
make serve    # API on :8099
make ui       # UI on :5173, in a second terminal
```

## Artifact generation

Creates `var/artifacts/jobs/<job_slug>/` containing `job_description.md`, `resume.md` / `.html` / `.pdf`, `cover_letter.md` / `.html` / `.pdf`, and `artifact_meta.json`.

```bash
make artifacts                                       # every approved job missing artifacts
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 generate <job_slug>
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 generate --provider openai --model gpt-4.1-mini
PYTHONPATH=src python3 -m openclaw_jobsearch.cli phase3 regen-pdf <job_slug>
```

With a job slug, generation runs only for that job.
Without one, it scans every approved job and skips any that already has a resume PDF.

Generation stays grounded in `candidate/resume_plaintext.txt`.
The model is required to return grounded resume facts, and output that fails the grounding checks in `artifact_generation.py` is rejected rather than written.
Provider and model metadata are recorded in `artifact_meta.json` alongside the grounding report and any warnings.

## Master resume

```bash
make resume                                          # candidate/resume_master.md -> .pdf
scripts/render_resume.sh <source.md> <output.pdf>    # any source and target
```

Rendering needs Homebrew's pango and cairo on macOS; the script sets `DYLD_FALLBACK_LIBRARY_PATH` for WeasyPrint.

When you send a tailored resume, copy the final version into `candidate/applications/<date>-<company>/`.
That directory is your record of what you claimed to whom.
It is not in git, so it is only as safe as your backup.

## Company registry

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli registry audit
```

Audits `reference/worldwide_companies.md`, checking that each company's careers URL still resolves and that its ingestion strategy still applies.
Results land in `var/outputs/worldwide_registry_audit_latest.{json,csv}`.

A company in the registry gets its remote scope upgraded to `global` during validation unless the posting itself shows a restriction, so a stale registry quietly admits ineligible jobs.
Worth re-auditing every few months.

## Configuration

| File | Purpose |
|---|---|
| `config/profile.json` | Who you are, target roles, skills, excluded role keywords |
| `config/paths.json` | Where human-authored inputs live |
| `config/rules.json` | Active strict rules |
| `config/rules_baseline.json` | Original strict rules, kept for replay comparison |
| `config/watchlist.json` | Which boards and companies to fetch |
| `config/feedback.json` | Rules learned from your rejections |

## Environment

Copy `.env.example` to `.env`.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` | Required for artifact generation and LLM rule proposal |
| `OPENCLAW_LLM_PROVIDER` | `anthropic` or `openai` |
| `OPENCLAW_LLM_MODEL` | Model override |
| `OPENCLAW_LLM_TEMPERATURE` | Defaults to 0.2 |
| `OPENCLAW_LLM_MAX_TOKENS` | Defaults to 2500 |
| `SERPAPI_API_KEY` | Optional, enables the SerpAPI Google Jobs source |
| `OPENCLAW_WORKSPACE_ROOT` | Workspace root for the API server |

## Backups

Git does not cover the two directories that matter most.
`var/` is excluded because it is disposable and `candidate/` because it holds personal documents, so both need backing up by other means.

| Path | Why it cannot be recovered |
|---|---|
| `var/data/jobs.db` | Every review decision you have ever made. Cannot be recomputed from anything |
| `candidate/` | Your master resume, its edit history, and the exact documents you sent to each company |

```bash
cp var/data/jobs.db var/data/jobs.db.$(date +%Y%m%d)
```

For `candidate/`, use whatever you already trust: a private remote, a synced folder, or an encrypted archive.
The point is that it is deliberately outside this repo's history and nothing here will save it for you.

Everything else regenerates.
Exports come back with `make export`, artifacts with `make artifacts`, and the master resume PDF with `make resume`.

`config/`, `reference/`, `src/`, `frontend/`, `docs/`, and `scripts/` are covered by git.

## Troubleshooting

**`ModuleNotFoundError: openclaw_jobsearch`**
The editable install is stale. Run `make install`, or use `PYTHONPATH=src` as every Makefile target already does.

**`Required input files are missing`**
`config/paths.json` points at a resume file that does not exist. Check `resume_pdf` and `resume_text`, and run `make resume` if the master PDF has never been rendered.

**A run returns far fewer jobs than usual**
Fetch failures are currently swallowed per source, so a dead board looks identical to a board with no matches. Compare `source_counts` in `var/outputs/run_summary_latest.json` against a previous run to find which adapter went quiet.

**A rule change rejected jobs you had already approved**
It did not. Human decisions survive re-validation, by design. See the state table in [data-model.md](data-model.md).

**WeasyPrint fails to load pango or cairo**
Install them with `brew install pango cairo`, and invoke rendering through `scripts/render_resume.sh` so `DYLD_FALLBACK_LIBRARY_PATH` is set.
