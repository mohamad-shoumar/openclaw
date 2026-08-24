# Decision records

Short notes on choices that shaped this system, kept because the reasoning is
harder to recover than the code.

Each record states the decision, why it was made, and what it costs.
They are dated and not revised; a reversal gets a new record that supersedes the old one.

- [0001](0001-no-auto-apply.md) - No auto-apply. A human approves every application
- [0002](0002-feedback-generalizes-by-dimension.md) - Feedback is recorded against a dimension of a job, not the job
- [0003](0003-sqlite-json-payload-column.md) - SQLite with a JSON payload column and promoted query columns
- [0004](0004-llm-extracts-never-judges.md) - The LLM extracts and drafts; it never decides eligibility
- [0005](0005-generated-output-under-var.md) - Generated output lives under `var/`, human-authored input under `candidate/`
- [0006](0006-personal-documents-out-of-git.md) - Personal documents stay out of version control, superseding part of 0005
