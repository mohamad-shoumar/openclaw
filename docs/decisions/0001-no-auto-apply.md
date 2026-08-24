# 0001 - No auto-apply

**Date:** 2026-03 (recorded 2026-08-23)
**Status:** Accepted

## Decision

The pipeline discovers, filters, and drafts.
It never submits an application.
A human approves every job, and `applied_at` is only ever set by an explicit human action.

## Why

The obvious next feature is to auto-submit to everything that passes the rules, and it is the wrong feature.

The filters are heuristics over scraped text.
They mistake a hybrid role for remote, they miss a work-authorization clause buried in a benefits paragraph, and they infer the employer from a URL on the aggregator tier.
A wrong verdict that reaches a review queue costs a keystroke.
A wrong verdict that reaches an employer costs a first impression that cannot be retracted, and at volume it costs a reputation.

The asymmetry is the whole argument: false positives are cheap to catch by hand and expensive to send.

There is also a quality argument.
A tailored application that a human read before sending is a different artifact from one generated and fired blind, and the difference is visible to the reader.

## Consequences

Throughput is bounded by review time, which is the intended bound.
The review queue must stay pleasant to work through, which is why the two-pane keyboard-driven UI exists at all.

`review_status` and `applied_at` are separate fields specifically so "approved" can never be mistaken for "sent".
