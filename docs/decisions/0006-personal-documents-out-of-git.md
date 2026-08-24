# 0006 - Personal documents stay out of version control

**Date:** 2026-08-24
**Status:** Accepted. Supersedes the version-control consequence of [0005](0005-generated-output-under-var.md)

## Decision

`candidate/` is gitignored in full.
That includes the master resume and its markdown sources, the plaintext resume, the prompt guides, interview notes, and every application actually sent.

`.agents/` is gitignored too.

The repository tracks code, config, docs, reference data, and scripts.
It tracks no personal documents.

## Why

[0005](0005-generated-output-under-var.md) split the tree by whether a file is regenerable, and that split was right.
The conclusion it drew from that split was that `candidate/` should be versioned *because* it is irreplaceable, and that conclusion was wrong.

Being irreplaceable argues for a backup.
It does not argue for a git history in a repository whose value is the pipeline, not the paperwork.

Three reasons the paperwork does not belong here:

The repository is a candidate for being shared, published, or shown to an interviewer.
The pipeline is the interesting part.
A full resume history, phone number, and the exact text sent to each employer are not things to hand over as a side effect of sharing the code.

Version history over these files buys almost nothing.
The useful diff on a resume is between the version you sent to company A and the one you sent to company B, and that comparison is already available as two files sitting side by side in `candidate/applications/`.
Nobody bisects a resume.

They generate constant, meaningless churn.
Every `make resume` rewrites a PDF, so every render shows up as a binary diff in `git status`, which trains you to ignore the one signal you actually want from `git status`.

`.agents/` follows the same reasoning for a different motive.
It holds 22 vendored skill definitions that are agent tooling, not this project's code, and `.claude/` and `skills-lock.json` were already excluded.
Tracking one and not the others was inconsistent.

## Consequences

Git no longer protects anything irreplaceable.
Both `var/data/jobs.db` and all of `candidate/` are now outside version control, which means the backup discipline in [operations.md](../operations.md#backups) is the only thing standing between a disk failure and losing every review decision plus the entire resume history.
This is the real cost of the decision and it is worth restating out loud rather than discovering later.

A fresh clone cannot run the pipeline.
`config/paths.json` points into `candidate/`, and `ensure_inputs_exist` fails when the resume files are absent, which is the correct failure: the pipeline needs a resume and the repository deliberately does not carry one.

`config/profile.json` remains tracked and still contains a name, email, and phone number.
If the goal becomes keeping all personal data out of the repository rather than just the documents, that file is the remaining gap, and the fix is to move those fields into `.env` or an ignored overlay.
It was left tracked here because it is machine config that the rules and validation read, not a document.

The directory split from 0005 is unchanged.
`var/` is disposable, `candidate/` is not, and the difference still determines how each is backed up.
Only the git conclusion moved.
