# 0002 - Feedback generalizes by dimension

**Date:** 2026-04 (recorded 2026-08-23)
**Status:** Accepted

## Decision

A rejection is recorded as a rule against a reusable *dimension* of the job: the apply domain, the company, a title phrase, the discovery source, a required business domain, or a work-authorization region.
Blocking a single job slug is available but is the last resort, not the default.

## Why

The first version stored rejections per job.
It was useless within one run.
Rejecting one listing from a reposter site did nothing about the next eight from the same host, and the same eight came back on the next run.

The reviewer's actual intent is almost never "not this posting".
It is "not this site", "not this company", "not this kind of role", or "not somewhere I can legally work".
Storing the verdict at the level the human was actually thinking at makes one decision keep paying out on every future run.

Two of the dimensions are deliberately conditional rather than plain substring matches.
`required_domain` only fires when a posting demands the domain as a hard requirement, and `work_authorization` only fires when a region term appears near authorization or sponsorship wording.
That makes them safe to reach for: a posting that mentions healthcare as a nice-to-have still passes, so generalizing does not silently discard viable jobs.

## Consequences

A rule can be too broad, so blast radius is shown before a rule is confirmed, and the rules panel lists hit counts so over-eager rules are visible.

Rules are retro-applied to jobs already queued, and every revocation records the rule id in `approval_reason`, so any rejection can be traced back to the rule that caused it.

Because rules generalize, a bad rule is now capable of real damage.
That is what made `replay.py` necessary: a rule change has to be testable against the stored corpus before adoption.
