# Data model

Everything is one Pydantic model, `JobRecord`, defined in `models.py`.
It accumulates fields as a job moves through the pipeline rather than being transformed into different types per stage.

## JobRecord by lifecycle stage

| Stage that writes it | Fields |
|---|---|
| Collection | `run_id`, `discovery_source`, `source_tier`, `board_type`, `external_id` |
| Normalization | `job_slug`, `company`, `title`, `job_url`, `apply_url`, `posted_at`, `location_raw`, `workplace_type`, `summary`, `description_text`, `salary_*`, `normalized_url_key`, `content_hash`, `source_payload` |
| Validation | `remote_scope`, `country_restrictions`, `timezone_restrictions`, `lebanon_eligibility`, `experience_required_*`, `seniority_title`, `required_tech`, `preferred_tech`, `validation_status`, `rejection_reasons`, `evidence_snippets` |
| Review | `review_status`, `review_decision_at`, `review_decision_by`, `review_notes`, `approval_reason` |
| Application | `applied_at`, `applied_notes` |
| Generation | `phase3_*`, `artifact_dir`, `job_description_path`, `resume_path_generated`, `cover_letter_path_generated`, `artifact_meta_path` |

## Rejection is explanatory, not boolean

`validate_job` does not return a pass or fail.
It accumulates `rejection_reasons` as human-readable sentences, and `evidence_snippets` that record which field a classification came from and the text that triggered it.

The reason a job was dropped is therefore recoverable months later, which is what makes offline rule tuning possible at all.

## The remote-scope taxonomy

`RemoteScope` has four values, and the distinction between two of them is the point.

| Value | Meaning |
|---|---|
| `global` | Explicit worldwide or work-from-anywhere phrasing |
| `open` | A remote role with no geographic restriction found. Absence of evidence |
| `restricted` | Positive evidence of a geographic limit |
| `unknown` | No remote signal at all |

`open` and `restricted` are not opposites.
`open` means the posting did not say, `restricted` means it did say and the answer was no.
Collapsing them into a single boolean would either discard viable jobs or admit ineligible ones, and there is no threshold that avoids both.

Which scopes are acceptable is configured in `rules.allowed_remote_scopes`, not hardcoded.

## Two independent verdicts

A job carries two separate statuses that are often confused.

- `validation_status` is the machine's verdict: `accepted` or `rejected`.
- `review_status` is the human's verdict: `not_queued`, `pending_review`, `approved`, `rejected`, or `archived`.

A job can be machine-accepted and human-rejected.
Only human-approved jobs are emitted into `var/outputs/approved_jobs_latest.jsonl`.

And approving is not applying.
`applied_at` is a third, separate fact, because the number that matters is applications submitted, not jobs approved.

## Review state on re-ingestion

The same posting is re-fetched on every run.
`_prepare_job_for_upsert` in `db.py` decides what survives, and this table is the contract.

| Existing `review_status` | New `validation_status` | Result |
|---|---|---|
| `approved`, `rejected`, or `archived` | anything | Human decision preserved in full. Re-validation cannot overturn a human |
| `pending_review` | `accepted` | Stays `pending_review`, decision fields cleared, notes and artifact paths preserved |
| `pending_review` | `rejected` | Falls through to the rules below |
| none, or not yet decided | `accepted` | Enters `pending_review` |
| none, or not yet decided | `rejected` | Set to `not_queued`, review and artifact fields cleared |

The rule behind the table: **a human decision is never silently reversed by a rules change.**
If a rule tightens and would now reject a job you already approved, the approval stands and the job stays approved.
Withdrawing it is an explicit action, either a manual requeue or a feedback rule applied deliberately.

## Storage

SQLite, at `var/data/jobs.db`.

Two tables: `jobs` keyed by `job_slug`, and `runs` keyed by `run_id`.

Each row stores the full `JobRecord` as JSON in `payload_json`, plus a set of promoted columns for the fields that get queried or sorted.
The JSON payload is the source of truth and the columns are a query index over it.
This trades some redundancy for the ability to add a model field without a migration.

Schema changes are handled by `_ensure_column`, which adds missing columns idempotently on connect.
That covers additive changes, which is all this schema has needed.
A destructive change would need a real migration script, of the kind in `scripts/`.

Artifact directories are stored as workspace-relative strings.
`paths.py` owns the one place that path is spelled, so moving the output tree is a one-line change plus a migration over stored rows.
