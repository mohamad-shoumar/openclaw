# 0004 - The LLM extracts and drafts; it never judges

**Date:** 2026-05 (recorded 2026-08-23)
**Status:** Accepted

## Decision

An LLM is used for two things: mapping a plain-English rejection reason onto the existing rule dimensions, and drafting a tailored resume and cover letter.

It is never used to decide whether a job is eligible.
That decision is made by deterministic rules in `validate_job`.

The model also cannot write to `config/feedback.json`.
It returns proposals; a human confirms.

## Why

Eligibility is the decision that has to be auditable.
When a job is rejected, the reason has to be inspectable, reproducible, and testable against the stored corpus, because that is what makes offline rule tuning work at all.
A model verdict is none of those.
It cannot be replayed, and it gives a different answer on the same posting.

Extraction is a genuinely different task.
"Canonical's work culture is bad" to `{kind: company, value: canonical}` is a mapping problem with a small output space and a human checking the result.
That plays to what a model is good at without putting it anywhere near the audit trail.

Drafting is the same shape: generation with a human reading the output before it goes anywhere.

## Consequences

Every LLM call returns structured JSON validated against a Pydantic model, so a malformed response is an error rather than corrupt state.

Generation output has to be grounded.
`artifact_generation.py` requires the model to return grounded resume facts, checks for placeholder text and unsupported role references, and rejects drafts that fail rather than writing them.
An unfixable model output is a failed generation, not a resume with an invented job on it.

Both LLM paths degrade rather than break without an API key.
Rule proposal falls back to a conservative keyword heuristic that prefers blocking only the single job, and the rest of the pipeline does not need a key at all.

`llm.py` talks to provider HTTP APIs directly, so switching providers is a config change and there is no SDK lock-in.
