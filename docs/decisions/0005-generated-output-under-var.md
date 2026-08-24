# 0005 - Generated output under var/, human-authored input under candidate/

**Date:** 2026-08-23
**Status:** Accepted. The version-control consequence is superseded by [0006](0006-personal-documents-out-of-git.md)

## Decision

Everything the pipeline generates lives under `var/`: the database, exports, and job artifacts.
Everything a human authored or polished lives under `candidate/`: the master resume, the prompt guides, and the applications actually sent.

Reference datasets live in `reference/`, machine config in `config/`.

## Why

The repository root had accumulated 23 loose files mixing four unrelated lifecycles: application code, human source-of-truth documents, machine output, and finished deliverables.
Tailored resumes landed at the root with ad-hoc names like `Shoumar_Resume_FullStack_Huzzle_Aug2026.pdf`, which made it impossible to tell at a glance what was regenerable and what was irreplaceable.

The organizing question is not what a file is about, it is whether losing it matters.

`var/` is regenerable by definition, so it is one gitignore line and one backup target.
`candidate/` is not regenerable, so losing it is unrecoverable.

The original conclusion drawn from that was to version `candidate/` in git.
[0006](0006-personal-documents-out-of-git.md) reverses that specific conclusion while keeping the directory split intact.
A machine draft of a resume can be rebuilt from the job description; the polished version you actually sent to a company is your record of what you claimed to whom, and no amount of recomputation gets it back.

## Consequences

Root went from 23 loose files to 5.

Job artifact directories are stored in the database as workspace-relative strings, so this move required a migration over stored rows.
`paths.py` now owns the one place that path is spelled, and `scripts/migrate_artifact_paths.py` handled the existing rows.

Three previously hardcoded input paths became configuration in `config/paths.json`: the company registry and the two prompt guides.
Hardcoding them was what made the old layout hard to change.

The database at `var/data/jobs.db` is the one generated file that is not disposable, because it holds review decisions that cannot be recomputed.
It needs its own backup discipline despite living under a gitignored tree.
