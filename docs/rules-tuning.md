# Rules and feedback

Two mechanisms decide what gets rejected.

`config/rules.json` holds the standing strict rules: age limits, allowed remote scopes, experience ceilings, title and stack keywords.
`config/feedback.json` holds rules learned from your own rejections.

## Feedback generalizes by dimension

A verdict on a single job is close to worthless.
Rejecting one reposter listing does nothing about the next eight from the same site.

So a rejection is recorded against a reusable *dimension* of the job rather than the job itself.

| Kind | Matches | Example value |
|---|---|---|
| `apply_domain` | Host of the apply or job URL, suffix match | `up.railway.app` covers every subdomain |
| `company` | Company name, case-insensitive after normalization | `Marsbased` |
| `title_pattern` | Phrase in the job title, case-insensitive substring | `technical mentor` |
| `source` | An entire discovery source | `serpapi` |
| `required_domain` | Business-domain terms, but only when the posting demands the domain as a hard requirement | `healthcare, ehr, hipaa, clinical` |
| `work_authorization` | Region terms, but only near authorization or sponsorship wording | `u.s, usa, united states, us-based` |
| `job` | One job slug. Does not generalize | A broken scrape or a genuine one-off |

`required_domain` and `work_authorization` are safe by construction.
They only fire when the term appears as a must-have or next to authorization wording, so a posting that mentions healthcare as a nice-to-have still passes.
Prefer them over `job` whenever the reason is a domain or eligibility mismatch.

Two value choices are worth knowing about.
Never use the bare word `us` for work authorization, because it collides with the pronoun; use `u.s`, `usa`, or a phrase like `us citizenship`.
Avoid acronyms that collide with unrelated tech, because `emr` also means Amazon EMR; use `electronic medical records` instead.

## Adding a rule

From an explicit value:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback add \
  --kind apply_domain --value up.railway.app \
  --reason "hobby-hosted job reposter, not an employer"
```

Or reject a job and generalize it in one step, deriving the value from that job:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback from-job <job_slug> \
  --kind apply_domain --reason "job reposter site"
```

Both forms immediately revoke every matching job already in the queue.

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback list
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback apply --dry-run
PYTHONPATH=src python3 -m openclaw_jobsearch.cli feedback remove <rule_id>
```

Revoked jobs move to `review_status = rejected` with `approval_reason` recording the rule id, so every revocation is auditable back to the rule that caused it.

## The LLM proposes, you decide

In the review UI, rejecting a job takes a plain-English reason.
`rule_proposal.py` sends it to an LLM whose only job is extraction: map the sentence onto the existing rule dimensions and report what it found.

It does not decide whether the rejection is correct, and it cannot write to `config/feedback.json`.
The proposal is shown with its blast radius, meaning how many stored jobs the rule would block, and a human confirms.

Without an API key, this falls back to a conservative keyword heuristic that prefers blocking only the single job.

## Testing a rule change offline

`replay` re-scores every job already in `var/data/jobs.db` against a candidate rules file.
No network calls.

Each job is judged as of the date of the run that discovered it, so the freshness rule stays meaningful instead of rejecting the whole archive for being months old.
Derived classifications are cleared before each replay, so a re-scored job never inherits a verdict from the run that stored it.

Score the active rules:

```bash
make replay
```

A/B a candidate ruleset against a baseline:

```bash
PYTHONPATH=src python3 -m openclaw_jobsearch.cli replay \
  --baseline config/rules_baseline.json \
  --rules config/rules_candidate.json
```

The output prints the acceptance delta plus a sample of newly accepted and newly rejected jobs, so a rule change can be inspected before it is adopted.

`config/rules_baseline.json` is the original strict ruleset, kept for comparison.

## Working order for a rule change

1. Write the candidate rules file.
2. Replay it against the baseline.
3. Read the newly accepted and newly rejected samples. This is the step that catches over-eager rules.
4. Adopt by copying over `config/rules.json`.
5. Run `feedback apply --dry-run` if the change should also affect jobs already queued.
