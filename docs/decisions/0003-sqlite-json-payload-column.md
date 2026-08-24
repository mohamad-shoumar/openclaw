# 0003 - SQLite with a JSON payload column

**Date:** 2026-03 (recorded 2026-08-23)
**Status:** Accepted

## Decision

SQLite, one row per job, storing the full `JobRecord` as JSON in `payload_json`.
The fields that get queried or sorted are also written to their own columns.
The JSON is the source of truth; the columns are a query index over it.

Additive schema changes are applied by `_ensure_column` on connect.

## Why

This is a single-user local tool.
A database server is infrastructure to run, back up, and keep alive for no gain; SQLite is one file that can be copied.

The JSON column exists because the model was still moving.
`JobRecord` gained the review fields, then the applied fields, then the artifact fields, and each of those would have been a migration.
Storing the model as JSON meant adding a field cost nothing, while promoting a column when something needed sorting was a small, deliberate step.

`_ensure_column` covers the only kind of change this schema has actually needed, which is additive.
A migration framework would be more machinery than the problem has ever justified.

## Consequences

Promoted columns are redundant with the JSON, so `_persist_job` must write both, and a promoted column can drift from the payload if a write path forgets one.
Reads go through the payload, so a drifted column shows up as a wrong sort order rather than wrong data.

A destructive change is not covered and needs a real migration script.
`scripts/migrate_artifact_paths.py` is the pattern: a one-shot, idempotent, dry-runnable script that loads each record through the model and rewrites it.

At 3,890 rows the query patterns here do not need indexes beyond the primary key.
That will not hold forever.
