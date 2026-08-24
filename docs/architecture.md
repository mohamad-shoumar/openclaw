# Architecture

## Stages

A run is a straight line with a persistence point after every stage.

| Stage | Entry point | What it does |
|---|---|---|
| Collect | `sources.build_sources` | Each configured board becomes a `SourceAdapter`; every adapter returns `RawJob` records carrying the untouched upstream payload |
| Snapshot | `pipeline._write_raw_snapshot` | Raw payloads to JSONL, so a normalization bug can be replayed without refetching |
| Normalize | `pipeline.normalize_job` | Board-specific payload to one `JobRecord` schema |
| Dedupe | `pipeline.dedupe_jobs` | Collapse by normalized URL key and content hash |
| Validate | `pipeline.validate_job` | Apply strict rules and feedback rules; attach a reason and evidence for every rejection |
| Persist | `db.upsert_job` | Write to SQLite, preserving human review decisions from earlier runs |
| Queue | `pipeline._queue_job_for_review` | Accepted jobs enter `pending_review` |
| Export | `pipeline.export_review_outputs` | Markdown, CSV, and JSONL contracts to `var/outputs/` |
| Generate | `artifact_generation.generate_phase3_artifacts` | Approved jobs get a tailored resume and cover letter |

## Two interfaces, one core

`cli.py` and `web/app.py` are both thin.
Neither contains pipeline logic.
Both call the same functions in `pipeline`, `db`, `feedback`, and `artifact_generation`.

This is deliberate.
The moment the API grows its own copy of a validation rule, the CLI and the UI start disagreeing about which jobs are eligible, and there is no way to tell which one is right.

A pipeline run takes minutes, so it cannot block an HTTP request.
`web/runner.py` serializes runs behind a lock and exposes progress for the UI to poll.
One run at a time.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Loads and validates every config file into typed models |
| `paths.py` | Owns the generated-output layout conventions, including the one place `var/artifacts/jobs/<slug>` is spelled |
| `models.py` | Pydantic domain model: `JobRecord`, config models, `ApprovedJobContract` |
| `sources.py` | One adapter per board, plus generic careers-page discovery |
| `registry.py` | The worldwide-remote company registry and its audit tooling |
| `pipeline.py` | Orchestration, normalization, validation, and report writing |
| `db.py` | SQLite persistence, review state transitions, schema migrations |
| `feedback.py` | Reviewer rejections stored as reusable rules |
| `rule_proposal.py` | Turns a plain-English rejection into proposed rules for a human to confirm |
| `replay.py` | Offline re-scoring of the stored corpus against a candidate ruleset |
| `llm.py` | Provider-agnostic JSON completion over raw HTTP |
| `artifact_generation.py` | Tailored resume and cover letter generation, with grounding checks |
| `pdf.py` | Markdown to HTML to PDF via WeasyPrint templates |
| `web/` | FastAPI app, request models, serializers, background run state |

## Source adapters

Sixteen adapters across three tiers.

| Tier | Adapters | Notes |
|---|---|---|
| `ats` | Greenhouse, Lever, Ashby, Workday | Public JSON APIs, highest trust, employer is unambiguous |
| `aggregator` | SerpAPI Google Jobs, generic careers pages, Hacker News "Who is hiring", HiringCafe, NoDesk, Remote100K, Arc | Noisiest tier; the employer often has to be inferred from the URL |
| `remote_api` | Remotive, Himalayas, RemoteOK, We Work Remotely | No auth needed |

HiringCafe and Arc are the only sources that state eligibility as structured data rather than prose.
Both hand over an explicit eligible-country list per posting, which `_eligibility_location` renders as "Remote - must be based in ..." so the existing restriction patterns classify it without a second code path.
Everywhere else that verdict has to be inferred from the description.

Neither HiringCafe nor Arc publishes an API.
Both render results server-side, so both are read from the `__NEXT_DATA__` payload of a normal page fetch.
HiringCafe's `buildId` rotates on every deploy and is therefore resolved from the live page per run, never pinned.
Arc applies its `?jobRoles=` and `?page=` filters client-side only, so the skill path (`/remote-jobs/<skill>`) is the sole server-side filter and one request per configured skill is the ceiling of what Arc will return.
Arc also gates full postings behind an account, so its records carry structured metadata as the description rather than posting text.

Wellfound and Contra were evaluated and rejected.
Wellfound puts `/graphql` and its sitemap behind Cloudflare and loads listings after hydration, leaving nothing in the server payload to read.
Contra has no public jobs API and its sitemap indexes freelancer profiles, not openings.

Adding a board means one adapter class in `sources.py`, one normalizer in `pipeline.py`, and one entry in `BoardType`.
Every adapter swallows its own fetch failures so one dead board cannot end a run.
That tradeoff is currently invisible in the output, which is a known gap: a dead board and a board with no matching jobs look identical.

## Review UI

A local two-pane triage surface at `http://localhost:5173`, served by `make ui` against `make serve`.

- Keyboard driven: `j` / `k` to move, `a` approve, `r` reject, `o` open the posting.
- Rejecting takes a plain-English reason. An LLM maps it onto a feedback rule dimension, and the proposal shows its blast radius before you confirm.
- The rules panel lists every learned rule with hit counts, so dead or over-eager rules are visible and removable.
- Refresh runs the pipeline in the background while the queue stays usable.
- Approved jobs track `applied_at` separately, because approving is not applying.
- The artifacts tab generates and previews the tailored resume and cover letter inline.

Without an LLM key, rejection still works and falls back to a conservative keyword heuristic that prefers blocking only the single job.

## Known gaps

Recorded here so they are not rediscovered as surprises.

- No test suite. The classification heuristics in `pipeline.py` are the highest-risk untested code.
- No retries, backoff, or rate limiting on any fetch. `ThreadPoolExecutor` fans out to careers pages unthrottled.
- Fetch failures are swallowed and not counted in `RunSummary`.
- No structured logging. The code prints.
- `pipeline.py` and `sources.py` are oversized and should be split into `normalize/`, `validation/`, `exporters/`, and `sources/` packages.
- The web layer opens a SQLite connection per request and never closes it.
