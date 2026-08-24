"""Offline rule-tuning harness.

Re-scores every job already stored in `data/jobs.db` against an arbitrary rules
file, without making a single network call. Each job is judged as of the date of
the run that discovered it, so the freshness rule stays meaningful instead of
rejecting the whole archive for being months old.

Use it to see the acceptance curve of a candidate `rules.json` before adopting it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import AppConfig
from .models import JobRecord, RulesConfig
from .paths import DATA_DIR
from .pipeline import validate_job

# Fields that `validate_job` derives; cleared before each replay so a re-scored
# job never inherits classifications from the run that stored it.
DERIVED_FIELDS = {
    "remote_scope": "unknown",
    "lebanon_eligibility": "unknown",
    "seniority_title": "unknown",
    "experience_required_min": None,
    "experience_required_max": None,
    "validation_status": "rejected",
}
DERIVED_LIST_FIELDS = (
    "country_restrictions",
    "timezone_restrictions",
    "required_tech",
    "preferred_tech",
    "rejection_reasons",
    "evidence_snippets",
)


@dataclass
class ReplayResult:
    label: str
    total: int
    accepted: list[JobRecord] = field(default_factory=list)
    rejected: list[JobRecord] = field(default_factory=list)
    reason_counts: Counter = field(default_factory=Counter)

    @property
    def accept_rate(self) -> float:
        return len(self.accepted) / self.total if self.total else 0.0

    @property
    def accepted_slugs(self) -> set[str]:
        return {job.job_slug for job in self.accepted}


def _run_id_to_date(run_id: str) -> date:
    """Run ids are `YYYYMMDDTHHMMSSZ` timestamps."""
    try:
        return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").date()
    except ValueError:
        return date.today()


def load_stored_jobs(workspace_root: Path) -> list[tuple[JobRecord, date]]:
    """Rebuild every stored job from its persisted payload, paired with its run date."""
    db_path = workspace_root / DATA_DIR / "jobs.db"
    if not db_path.exists():
        raise FileNotFoundError(f"No job database at {db_path}")

    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT run_id, payload_json FROM jobs").fetchall()
    finally:
        connection.close()

    jobs: list[tuple[JobRecord, date]] = []
    for run_id, payload_json in rows:
        try:
            jobs.append((JobRecord.model_validate(json.loads(payload_json)), _run_id_to_date(run_id)))
        except Exception:
            continue
    return jobs


def _reset_derived(job: JobRecord) -> JobRecord:
    fresh = job.model_copy(deep=True)
    for name, value in DERIVED_FIELDS.items():
        setattr(fresh, name, value)
    for name in DERIVED_LIST_FIELDS:
        setattr(fresh, name, [])
    return fresh


def replay(
    jobs: list[tuple[JobRecord, date]],
    config: AppConfig,
    label: str = "candidate",
) -> ReplayResult:
    result = ReplayResult(label=label, total=len(jobs))
    for job, as_of in jobs:
        scored = validate_job(_reset_derived(job), config, as_of=as_of)
        if scored.validation_status == "accepted":
            result.accepted.append(scored)
        else:
            result.rejected.append(scored)
            result.reason_counts.update(scored.rejection_reasons)
    return result


def config_with_rules(base: AppConfig, rules_path: Path) -> AppConfig:
    """Clone an AppConfig with a different rules file, reusing the loaded registry."""
    clone = base.__class__.__new__(base.__class__)
    clone.__dict__.update(base.__dict__)
    clone.rules = RulesConfig.model_validate(json.loads(rules_path.read_text()))
    return clone


def format_result(result: ReplayResult, top_reasons: int = 12) -> str:
    lines = [
        f"[{result.label}]",
        f"  total     {result.total}",
        f"  accepted  {len(result.accepted)} ({result.accept_rate:.1%})",
        f"  rejected  {len(result.rejected)}",
        "  top rejection reasons:",
    ]
    for reason, count in result.reason_counts.most_common(top_reasons):
        lines.append(f"    {count:>5}  {reason}")
    return "\n".join(lines)


def format_comparison(baseline: ReplayResult, candidate: ReplayResult, sample: int = 15) -> str:
    gained = candidate.accepted_slugs - baseline.accepted_slugs
    lost = baseline.accepted_slugs - candidate.accepted_slugs
    by_slug = {job.job_slug: job for job in candidate.accepted}

    lines = [
        "",
        f"delta: {len(baseline.accepted)} -> {len(candidate.accepted)} accepted "
        f"({baseline.accept_rate:.1%} -> {candidate.accept_rate:.1%})",
        f"  newly accepted  {len(gained)}",
        f"  newly rejected  {len(lost)}",
    ]
    if gained:
        lines.append("")
        lines.append(f"  sample of newly accepted (showing {min(sample, len(gained))}):")
        for slug in sorted(gained)[:sample]:
            job = by_slug[slug]
            location = (job.location_raw or job.workplace_type or "?")[:34]
            lines.append(f"    {job.company[:24]:<24} | {job.title[:44]:<44} | {location}")
    return "\n".join(lines)
