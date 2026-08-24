# OpenClaw Job Search

A local, strict-mode job discovery pipeline with a human approval queue and per-job tailored application drafts.

It collects postings from ATS boards and remote job boards, normalizes them into one schema, rejects the ones you are not eligible for with a written reason for each rejection, and pushes survivors into a review queue you triage by hand.
Approved jobs get a tailored resume and cover letter draft.

It deliberately does not auto-apply.
A human approves everything.

## How it works

```
sources  ->  normalize  ->  dedupe  ->  validate  ->  review queue  ->  artifacts
(16 board    one JobRecord            strict rules   human approves    tailored resume
 adapters)   schema                   + feedback     in CLI or UI      + cover letter
                                      rules
```

Every stage persists.
Raw payloads and validated snapshots land in JSONL, job state lives in SQLite, and exports land in `var/outputs/`.

The FastAPI layer is a thin skin over the same functions the CLI calls, so the two interfaces cannot drift apart.

See [docs/architecture.md](docs/architecture.md) for the module map and [docs/decisions/](docs/decisions/) for why it is built this way.

## Layout

| Path | What lives here |
|---|---|
| `src/openclaw_jobsearch/` | The pipeline, CLI, and FastAPI layer |
| `frontend/` | React + Vite review UI |
| `config/` | Machine config: profile, rules, watchlist, feedback, paths |
| `candidate/` | Human-authored source of truth: master resume, guides, sent applications. Gitignored |
| `reference/` | Reference datasets, currently the worldwide-remote company registry |
| `var/` | Everything generated. Disposable, regenerable, gitignored |
| `docs/` | Architecture, data model, operations, decision records |
| `scripts/` | Maintenance scripts |

The line that matters: `var/` is regenerable and `candidate/` is not.
A machine draft in `var/artifacts/jobs/<slug>/` can be thrown away and rebuilt.
The polished resume you actually sent to a company is your record of what you claimed to whom, and it belongs under `candidate/applications/<date>-<company>/`.

Neither directory is in git.
`var/` is excluded because it is disposable, `candidate/` because it holds personal documents.
That means the two things git will not save for you are the job database and your resume history, so both need their own backup.
See [backups](docs/operations.md#backups).

## Install

Requires Python 3.11+ and Node 18+ for the UI.

```bash
make install
cp .env.example .env    # then add an LLM API key
```

## Quickstart

```bash
make run          # run discovery over every configured source
make review       # list the pending review queue
make serve        # review API on :8099   (pair with `make ui` on :5173)
make artifacts    # tailored resume + cover letter for approved jobs
make replay       # re-score the stored corpus against current rules, offline
make help         # every available target
```

An LLM key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) is needed for artifact generation and for turning a plain-English rejection into a reusable rule.
Everything else works without one.

## Where things land

| File | Contents |
|---|---|
| `var/data/jobs.db` | Job state, review decisions, run history |
| `var/outputs/review_queue_latest.{md,csv}` | The current triage queue |
| `var/outputs/shortlist_latest.{md,csv}` | Top-ranked accepted jobs |
| `var/outputs/approved_jobs_latest.jsonl` | Approved-job contract, the input to artifact generation |
| `var/outputs/run_summary_latest.json` | Per-run counts and top rejection reasons |
| `var/artifacts/jobs/<slug>/` | Generated resume, cover letter, job description, metadata |

## Docs

- [Architecture](docs/architecture.md) - stages, modules, source adapters, the review UI
- [Data model](docs/data-model.md) - `JobRecord`, the review state machine, the remote-scope taxonomy
- [Rules and feedback](docs/rules-tuning.md) - how rejections become reusable rules, and how to test a rule change offline
- [Operations](docs/operations.md) - every command, environment variables, backups, troubleshooting
- [Decisions](docs/decisions/) - short records of the choices that shaped this
