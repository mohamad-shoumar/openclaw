from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import AppConfig
from .db import connect, get_job, insert_run_summary, list_approved_jobs, list_review_jobs, upsert_job
from .models import ApprovedJobContract, EvidenceSnippet, JobRecord, RawJob, RunSummary
from .sources import build_sources


def run_pipeline(workspace_root: Path, config_dir: Path, data_dir: Path, output_dir: Path) -> RunSummary:
    config = AppConfig(workspace_root=workspace_root, config_dir=config_dir)
    config.ensure_inputs_exist()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started_at = datetime.now(timezone.utc)

    sources = build_sources(config.watchlist)
    raw_jobs: list[RawJob] = []
    source_counts: Counter[str] = Counter()
    for source in sources:
        items = source.fetch()
        raw_jobs.extend(items)
        source_counts[source.name] += len(items)

    _write_raw_snapshot(data_dir, run_id, raw_jobs)

    normalized_jobs = [normalize_job(raw_job, run_id, config.rules) for raw_job in raw_jobs]
    unique_jobs = dedupe_jobs(normalized_jobs)
    validated_jobs = [validate_job(job, config) for job in unique_jobs]
    accepted_jobs = [job for job in validated_jobs if job.validation_status == "accepted"]
    for job in accepted_jobs:
        _queue_job_for_review(job)
    shortlisted_jobs = shortlist_jobs(accepted_jobs, config)

    connection = connect(data_dir / "jobs.db")
    for job in validated_jobs:
        upsert_job(connection, job)
    persisted_validated_jobs = [get_job(connection, job.job_slug) or job for job in validated_jobs]

    finished_at = datetime.now(timezone.utc)
    summary = RunSummary(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        total_raw_jobs=len(raw_jobs),
        total_unique_jobs=len(unique_jobs),
        accepted_jobs=len(accepted_jobs),
        rejected_jobs=len(validated_jobs) - len(accepted_jobs),
        source_counts=dict(source_counts),
        top_rejection_reasons=Counter(
            reason for job in validated_jobs for reason in job.rejection_reasons
        ).most_common(10),
    )
    insert_run_summary(connection, summary)
    review_jobs = list_review_jobs(connection)
    approved_jobs = list_approved_jobs(connection)

    _write_validated_snapshot(data_dir, run_id, persisted_validated_jobs)
    _write_shortlist_markdown(output_dir, summary, shortlisted_jobs)
    _write_shortlist_csv(output_dir, shortlisted_jobs)
    _write_review_queue_markdown(output_dir, review_jobs)
    _write_review_queue_csv(output_dir, review_jobs)
    _write_legacy_export(output_dir, review_jobs)
    _write_approved_jobs_contract(output_dir, approved_jobs)
    _write_phase3_summary(output_dir, approved_jobs)
    _write_summary(output_dir, summary)

    return summary


def export_review_outputs(data_dir: Path, output_dir: Path) -> dict[str, int]:
    connection = connect(data_dir / "jobs.db")
    review_jobs = list_review_jobs(connection)
    approved_jobs = list_approved_jobs(connection)
    _write_review_queue_markdown(output_dir, review_jobs)
    _write_review_queue_csv(output_dir, review_jobs)
    _write_legacy_export(output_dir, review_jobs)
    _write_approved_jobs_contract(output_dir, approved_jobs)
    _write_phase3_summary(output_dir, approved_jobs)
    return {
        "review_jobs": len(review_jobs),
        "approved_jobs": len(approved_jobs),
    }


def normalize_job(raw_job: RawJob, run_id: str, rules) -> JobRecord:
    board_type = raw_job.board_type
    if board_type == "greenhouse":
        return _normalize_greenhouse(raw_job, run_id)
    if board_type == "lever":
        return _normalize_lever(raw_job, run_id)
    if board_type == "ashby":
        return _normalize_ashby(raw_job, run_id)
    if board_type == "serpapi":
        return _normalize_serpapi(raw_job, run_id)
    if board_type == "remotive":
        return _normalize_remotive(raw_job, run_id)
    raise ValueError(f"Unsupported board type: {board_type}")


def validate_job(job: JobRecord, config: AppConfig) -> JobRecord:
    rules = config.rules
    title_text = job.title.lower()
    description_text = job.description_text.lower()
    location_text = job.location_raw.lower()
    searchable_text = " ".join(part for part in [title_text, location_text, description_text] if part)

    remote_scope, remote_evidence = _classify_remote_scope(location_text, description_text, job.workplace_type, rules)
    job.remote_scope = remote_scope
    if remote_evidence:
        job.evidence_snippets.append(EvidenceSnippet(field="remote_scope", snippet=remote_evidence))

    country_restrictions, timezone_restrictions = _extract_restrictions(location_text, description_text)
    job.country_restrictions = country_restrictions
    job.timezone_restrictions = timezone_restrictions
    if country_restrictions or timezone_restrictions:
        snippet = "; ".join(country_restrictions + timezone_restrictions)
        job.evidence_snippets.append(EvidenceSnippet(field="restriction", snippet=snippet))

    job.lebanon_eligibility = _classify_lebanon_eligibility(searchable_text)
    if job.lebanon_eligibility == "ineligible":
        job.evidence_snippets.append(
            EvidenceSnippet(field="lebanon_eligibility", snippet="Restriction text suggests Lebanon is excluded.")
        )

    job.seniority_title = _classify_seniority(job.title)
    exp_min, exp_max, exp_evidence = _extract_experience(job.description_text)
    job.experience_required_min = exp_min
    job.experience_required_max = exp_max
    if exp_evidence:
        job.evidence_snippets.append(EvidenceSnippet(field="experience", snippet=exp_evidence))

    required_tech, preferred_tech = _extract_tech(job.description_text, rules)
    job.required_tech = required_tech
    job.preferred_tech = preferred_tech

    reasons: list[str] = []
    if job.remote_scope != "global":
        reasons.append("Remote scope is not explicitly global.")
    if country_restrictions:
        reasons.append("Job has country or regional remote restrictions.")
    if timezone_restrictions:
        reasons.append("Job has timezone restrictions.")
    if job.lebanon_eligibility == "ineligible":
        reasons.append("Lebanon appears to be ineligible for this role.")
    if _matches_any(searchable_text, rules.hybrid_patterns):
        reasons.append("Job appears to be hybrid or on-site.")
    if job.posted_at and (date.today() - job.posted_at).days > rules.max_job_age_days:
        reasons.append(f"Job is older than {rules.max_job_age_days} days.")
    if job.experience_required_max and job.experience_required_max > rules.max_required_experience_years:
        reasons.append("Required experience exceeds the strict maximum.")
    if _matches_any(title_text, rules.rejected_title_keywords):
        reasons.append("Role seniority is above the strict target.")
    if not _is_target_role(title_text, description_text):
        reasons.append("Role type is outside the target backend/trading profile.")
    if "python" not in required_tech and "python" not in preferred_tech:
        reasons.append("Python is not clearly part of the role requirements.")
    if _has_rejected_primary_stack(description_text, rules):
        reasons.append("Primary stack appears misaligned with Python backend focus.")

    # Strict mode treats unclear freshness as acceptable for now but keeps unknown date low-ranked.
    job.rejection_reasons = reasons
    job.validation_status = "accepted" if not reasons else "rejected"
    return job


def shortlist_jobs(jobs: list[JobRecord], config: AppConfig) -> list[JobRecord]:
    accepted = [job for job in jobs if job.validation_status == "accepted"]
    accepted.sort(key=lambda job: _rank_key(job, config), reverse=True)
    return accepted[:10]


def dedupe_jobs(jobs: list[JobRecord]) -> list[JobRecord]:
    deduped: dict[str, JobRecord] = {}
    for job in jobs:
        key = job.normalized_url_key or job.content_hash
        existing = deduped.get(key)
        if not existing:
            deduped[key] = job
            continue
        current_date = job.posted_at or date.min
        existing_date = existing.posted_at or date.min
        if current_date >= existing_date:
            deduped[key] = job
    return list(deduped.values())


def _normalize_greenhouse(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("content", ""))
    location = item.get("location", {}).get("name", "")
    job_url = item.get("absolute_url", "")
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=raw_job.payload.get("board_company", item.get("company_name", "")),
        title=item.get("title", ""),
        job_url=job_url,
        apply_url=job_url,
        posted_at=_parse_date(item.get("updated_at")),
        location_raw=location,
        workplace_type="remote" if "remote" in location.lower() else "unknown",
        description_text=description,
        summary=description[:220],
    )


def _normalize_lever(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    list_sections: list[str] = []
    for group in item.get("lists", []):
        content = group.get("content", "")
        if isinstance(content, str):
            list_sections.append(content)
        elif isinstance(content, list):
            for entry in content:
                if isinstance(entry, str):
                    list_sections.append(entry)
                elif isinstance(entry, dict):
                    list_sections.append(str(entry.get("text", "")))
    description_parts = [
        item.get("descriptionPlain", ""),
        item.get("description", ""),
        " ".join(list_sections),
    ]
    description = _clean_text(" ".join(part for part in description_parts if part))
    location = item.get("categories", {}).get("location", "")
    job_url = item.get("hostedUrl", item.get("applyUrl", ""))
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=raw_job.payload.get("board_company", item.get("company", "")),
        title=item.get("text", ""),
        job_url=job_url,
        apply_url=item.get("applyUrl", job_url),
        posted_at=_parse_date(item.get("createdAt")),
        location_raw=location,
        workplace_type="remote" if "remote" in location.lower() else "unknown",
        description_text=description,
        summary=description[:220],
    )


def _normalize_ashby(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("descriptionHtml", item.get("descriptionPlain", "")))
    location = item.get("locationName", "")
    compensation = item.get("compensation", {}) or {}
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=raw_job.payload.get("board_company", item.get("companyName", "")),
        title=item.get("title", ""),
        job_url=item.get("jobUrl", ""),
        apply_url=item.get("jobUrl", ""),
        posted_at=_parse_date(item.get("publishedAt")),
        location_raw=location,
        workplace_type="remote" if item.get("isRemote") else item.get("workplaceType", "unknown"),
        description_text=description,
        salary_min=compensation.get("minValue"),
        salary_max=compensation.get("maxValue"),
        salary_currency=compensation.get("currencyCode"),
        salary_confidence="confirmed" if compensation else "not found",
        summary=description[:220],
    )


def _normalize_serpapi(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    apply_options = item.get("apply_options", [])
    apply_url = apply_options[0].get("link", "") if apply_options else item.get("related_links", [{}])[0].get("link", "")
    salary = item.get("detected_extensions", {}).get("salary", "")
    salary_min, salary_max, salary_currency = _parse_salary(salary)
    highlight_sections: list[str] = []
    job_highlights = item.get("job_highlights")
    if isinstance(job_highlights, list):
        for section in job_highlights:
            if not isinstance(section, dict):
                continue
            items = section.get("items", [])
            if isinstance(items, list):
                highlight_sections.append(" ".join(str(entry) for entry in items))
    elif isinstance(job_highlights, dict):
        for value in job_highlights.values():
            if isinstance(value, list):
                highlight_sections.append(" ".join(str(entry) for entry in value))
    description = _clean_text(
        " ".join(
            [
                item.get("description", ""),
                " ".join(highlight_sections),
            ]
        )
    )
    location = item.get("location", item.get("detected_extensions", {}).get("schedule_type", ""))
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=item.get("company_name", ""),
        title=item.get("title", ""),
        job_url=item.get("share_link", apply_url),
        apply_url=apply_url or item.get("share_link", ""),
        posted_at=_parse_date(item.get("detected_extensions", {}).get("posted_at")),
        location_raw=location,
        workplace_type="remote" if "remote" in location.lower() else "unknown",
        description_text=description,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_confidence="estimated" if salary else "not found",
        summary=description[:220],
    )


def _normalize_remotive(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("description", ""))
    salary_min, salary_max, salary_currency = _parse_salary(item.get("salary", ""))
    location = item.get("candidate_required_location", "")
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=item.get("company_name", ""),
        title=item.get("title", ""),
        job_url=item.get("url", ""),
        apply_url=item.get("url", ""),
        posted_at=_parse_date(item.get("publication_date")),
        location_raw=location,
        workplace_type="remote",
        description_text=description,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_confidence="confirmed" if item.get("salary") else "not found",
        summary=description[:220],
    )


def _base_job_record(
    *,
    raw_job: RawJob,
    run_id: str,
    company: str,
    title: str,
    job_url: str,
    apply_url: str,
    posted_at: date | None,
    location_raw: str,
    workplace_type: str,
    description_text: str,
    summary: str,
    salary_min: float | None = None,
    salary_max: float | None = None,
    salary_currency: str | None = None,
    salary_confidence: str = "not found",
) -> JobRecord:
    normalized_url_key = _normalize_url(apply_url or job_url)
    content_hash = hashlib.sha1(
        f"{company.lower()}|{title.lower()}|{location_raw.lower()}".encode("utf-8")
    ).hexdigest()
    external_seed = raw_job.external_id.split(":")[-1] if raw_job.external_id else content_hash[:8]
    job_slug = _slugify(f"{company}-{title}-{external_seed}")
    return JobRecord(
        run_id=run_id,
        job_slug=job_slug,
        discovery_source=raw_job.discovery_source,
        source_tier=raw_job.source_tier,
        board_type=raw_job.board_type,
        external_id=raw_job.external_id,
        company=company or "Unknown company",
        title=title or "Unknown title",
        job_url=job_url or apply_url or "",
        apply_url=apply_url or job_url or "",
        posted_at=posted_at,
        location_raw=location_raw or "",
        workplace_type=workplace_type or "unknown",
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_confidence=salary_confidence,  # type: ignore[arg-type]
        summary=_clean_text(summary),
        description_text=description_text,
        normalized_url_key=normalized_url_key,
        content_hash=content_hash,
        source_payload=raw_job.payload,
    )


def _rank_key(job: JobRecord, config: AppConfig) -> tuple[int, int, int]:
    freshness = 0
    if job.posted_at:
        age = (date.today() - job.posted_at).days
        freshness = max(0, 100 - age)
    source_weight = {"ats": 3, "remote_api": 2, "aggregator": 1}.get(job.source_tier, 0)
    preferred_matches = len(
        set(job.required_tech + job.preferred_tech) & set(config.profile.required_skills + config.profile.preferred_skills)
    )
    return (source_weight, preferred_matches, freshness)


def _classify_remote_scope(location_text: str, description_text: str, workplace_type: str, rules) -> tuple[str, str]:
    remote_text = " ".join(part for part in [location_text, workplace_type.lower(), description_text] if part)
    explicit_policy_phrases = [
        "fully remote",
        "remote-first",
        "work from anywhere",
        "remote worldwide",
        "global remote",
        "anywhere in the world",
    ]
    has_location_remote_signal = any(token in location_text for token in ["remote", "worldwide", "global", "anywhere"])
    has_policy_remote_signal = workplace_type.lower() == "remote" or _matches_any(description_text, explicit_policy_phrases)
    has_remote_signal = has_location_remote_signal or has_policy_remote_signal
    if _matches_any(location_text, rules.global_remote_patterns):
        return "global", _first_matching_phrase(location_text, rules.global_remote_patterns)
    if "worldwide" in location_text or "global" in location_text or "anywhere" in location_text:
        return "global", location_text
    if "remote" in location_text and any(token in location_text for token in [",", "usa", "uk", "germany", "france", "india", "europe", "emea", "apac"]):
        return "restricted", location_text
    if _matches_any(location_text, rules.remote_restriction_patterns):
        return "restricted", _first_matching_phrase(location_text, rules.remote_restriction_patterns)
    if _matches_any(remote_text, rules.remote_restriction_patterns):
        return "restricted", _first_matching_phrase(remote_text, rules.remote_restriction_patterns)
    if has_remote_signal and _matches_any(description_text, rules.global_remote_patterns):
        return "global", _first_matching_phrase(description_text, rules.global_remote_patterns)
    if has_remote_signal and "worldwide" in remote_text:
        return "global", "remote worldwide"
    if has_remote_signal and "global" in remote_text:
        return "global", "remote global"
    return "unknown", ""


def _classify_lebanon_eligibility(searchable_text: str) -> str:
    if "not hiring in lebanon" in searchable_text or "excluding lebanon" in searchable_text:
        return "ineligible"
    if "lebanon" in searchable_text and "eligible" in searchable_text:
        return "eligible"
    return "unknown"


def _extract_restrictions(location_text: str, description_text: str) -> tuple[list[str], list[str]]:
    country_hits: list[str] = []
    timezone_hits: list[str] = []
    combined_text = " ".join(part for part in [location_text, description_text] if part)
    for pattern in ["us only", "uk only", "europe only", "emea", "apac", "must be based in", "authorized to work in"]:
        if pattern in combined_text:
            country_hits.append(pattern)
    if "remote" in location_text and any(token in location_text for token in [",", "usa", "uk", "germany", "france", "india", "denmark", "japan", "finland"]):
        country_hits.append(location_text)
    for pattern in [r"\btimezone\b", r"\btime zones\b", r"\best\b", r"\bpst\b", r"\bcet\b"]:
        if re.search(pattern, combined_text):
            timezone_hits.append(pattern)
    return country_hits, timezone_hits


def _classify_seniority(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in ["principal", "staff", "director"]):
        return "lead"
    if "senior" in lowered:
        return "senior"
    if any(word in lowered for word in ["lead", "manager"]):
        return "lead"
    if any(word in lowered for word in ["junior", "jr"]):
        return "junior"
    return "mid"


def _extract_experience(text: str) -> tuple[int | None, int | None, str]:
    lowered = text.lower()
    patterns = [
        r"(\d+)\s*[-–]\s*(\d+)\+?\s*years",
        r"(\d+)\+?\s*years",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        snippet = lowered[max(0, match.start() - 40) : match.end() + 40]
        if len(match.groups()) == 2:
            return int(match.group(1)), int(match.group(2)), snippet
        value = int(match.group(1))
        return value, value, snippet
    return None, None, ""


def _extract_tech(text: str, rules) -> tuple[list[str], list[str]]:
    lowered = text.lower()
    required = sorted({skill for skill in rules.required_skill_keywords if skill in lowered})
    preferred = sorted({skill for skill in rules.preferred_skill_keywords if skill in lowered})
    return required, preferred


def _has_rejected_primary_stack(text: str, rules) -> bool:
    lowered = text.lower()
    if "python" in lowered:
        return False
    return any(keyword in lowered for keyword in rules.rejected_primary_stack_keywords)


def _is_target_role(title_text: str, description_text: str) -> bool:
    role_keywords = [
        "backend",
        "python",
        "software engineer",
        "software developer",
        "developer",
        "engineer",
        "platform",
        "trading",
        "api",
    ]
    blocked_keywords = [
        "account executive",
        "sales",
        "marketing",
        "customer success",
        "recruiter",
        "designer",
        "product manager",
        "business development",
    ]
    if any(keyword in title_text for keyword in blocked_keywords):
        return False
    if any(keyword in title_text for keyword in role_keywords):
        return True
    return any(keyword in description_text for keyword in ["python", "backend", "fastapi", "django", "trading systems"])


def _write_raw_snapshot(data_dir: Path, run_id: str, raw_jobs: list[RawJob]) -> None:
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{run_id}_raw_jobs.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for raw_job in raw_jobs:
            handle.write(raw_job.model_dump_json() + "\n")


def _write_validated_snapshot(data_dir: Path, run_id: str, jobs: list[JobRecord]) -> None:
    validated_dir = data_dir / "validated"
    validated_dir.mkdir(parents=True, exist_ok=True)
    path = validated_dir / f"{run_id}_validated_jobs.jsonl"
    latest_path = data_dir / "validated_jobs_latest.jsonl"
    lines = [job.model_dump_json() for job in jobs]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    latest_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_review_queue_markdown(output_dir: Path, jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped = {status: [] for status in ["pending_review", "approved", "rejected", "archived"]}
    for job in jobs:
        grouped.setdefault(job.review_status, []).append(job)

    lines = [
        "# Review Queue",
        "",
        f"- Total review jobs: {len(jobs)}",
        f"- Pending review: {len(grouped['pending_review'])}",
        f"- Approved: {len(grouped['approved'])}",
        f"- Rejected: {len(grouped['rejected'])}",
        f"- Archived: {len(grouped['archived'])}",
        "",
    ]
    for status in ["pending_review", "approved", "rejected", "archived"]:
        status_jobs = grouped.get(status, [])
        if not status_jobs:
            continue
        lines.extend([f"## {status.replace('_', ' ').title()}", ""])
        for index, job in enumerate(status_jobs, start=1):
            lines.extend(
                [
                    f"### {index}. {job.company} - {job.title}",
                    "",
                    f"- Job slug: `{job.job_slug}`",
                    f"- Review status: `{job.review_status}`",
                    f"- Posted: {job.posted_at or 'unknown'} ({_age_text(job)})",
                    f"- Source: `{job.discovery_source}` ({job.source_tier})",
                    f"- Apply: {job.apply_url or job.job_url}",
                    f"- Required tech: {', '.join(job.required_tech) or 'none detected'}",
                    f"- Preferred tech: {', '.join(job.preferred_tech) or 'none detected'}",
                    f"- Phase 3 status: `{job.phase3_status}`",
                    f"- Summary: {job.summary or 'No summary available.'}",
                ]
            )
            if job.review_notes:
                lines.append(f"- Review notes: {job.review_notes}")
            if job.approval_reason:
                lines.append(f"- Decision reason: {job.approval_reason}")
            if job.review_decision_at:
                lines.append(f"- Decision at: {job.review_decision_at.isoformat()}")
            if job.cover_letter_path_generated:
                lines.append(f"- Cover letter path: `{job.cover_letter_path_generated}`")
            if job.resume_path_generated:
                lines.append(f"- Resume path: `{job.resume_path_generated}`")
            lines.append("")
    (output_dir / "review_queue_latest.md").write_text("\n".join(lines), encoding="utf-8")


def _write_shortlist_markdown(output_dir: Path, summary: RunSummary, jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Shortlist {summary.run_id}",
        "",
        f"- Raw jobs: {summary.total_raw_jobs}",
        f"- Unique jobs: {summary.total_unique_jobs}",
        f"- Accepted jobs: {summary.accepted_jobs}",
        f"- Rejected jobs: {summary.rejected_jobs}",
        "",
    ]
    for index, job in enumerate(jobs, start=1):
        age_text = "unknown"
        if job.posted_at:
            age_text = f"{(date.today() - job.posted_at).days} days old"
        lines.extend(
            [
                f"## {index}. {job.company} - {job.title}",
                "",
                f"- Job slug: `{job.job_slug}`",
                f"- Source: `{job.discovery_source}` ({job.source_tier})",
                f"- Posted: {job.posted_at or 'unknown'} ({age_text})",
                f"- Location: {job.location_raw or 'unknown'}",
                f"- Apply: {job.apply_url or job.job_url}",
                f"- Required tech: {', '.join(job.required_tech) or 'none detected'}",
                f"- Preferred tech: {', '.join(job.preferred_tech) or 'none detected'}",
                f"- Summary: {job.summary or 'No summary available.'}",
                "",
            ]
        )
    (output_dir / "shortlist_latest.md").write_text("\n".join(lines), encoding="utf-8")


def _write_review_queue_csv(output_dir: Path, jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "review_queue_latest.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "job_slug",
                "review_status",
                "review_decision_at",
                "phase3_status",
                "company",
                "title",
                "posted_at",
                "source_tier",
                "discovery_source",
                "apply_url",
                "required_tech",
                "preferred_tech",
                "review_notes",
                "approval_reason",
                "resume_path_generated",
                "cover_letter_path_generated",
            ],
        )
        writer.writeheader()
        for job in jobs:
            writer.writerow(
                {
                    "job_slug": job.job_slug,
                    "review_status": job.review_status,
                    "review_decision_at": job.review_decision_at.isoformat() if job.review_decision_at else "",
                    "phase3_status": job.phase3_status,
                    "company": job.company,
                    "title": job.title,
                    "posted_at": job.posted_at.isoformat() if job.posted_at else "",
                    "source_tier": job.source_tier,
                    "discovery_source": job.discovery_source,
                    "apply_url": job.apply_url or job.job_url,
                    "required_tech": ",".join(job.required_tech),
                    "preferred_tech": ",".join(job.preferred_tech),
                    "review_notes": job.review_notes,
                    "approval_reason": job.approval_reason,
                    "resume_path_generated": job.resume_path_generated,
                    "cover_letter_path_generated": job.cover_letter_path_generated,
                }
            )


def _write_shortlist_csv(output_dir: Path, jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "shortlist_latest.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "job_slug",
                "company",
                "title",
                "source",
                "posted_at",
                "location",
                "apply_url",
                "required_tech",
                "preferred_tech",
            ],
        )
        writer.writeheader()
        for job in jobs:
            writer.writerow(
                {
                    "job_slug": job.job_slug,
                    "company": job.company,
                    "title": job.title,
                    "source": job.discovery_source,
                    "posted_at": job.posted_at.isoformat() if job.posted_at else "",
                    "location": job.location_raw,
                    "apply_url": job.apply_url or job.job_url,
                    "required_tech": ",".join(job.required_tech),
                    "preferred_tech": ",".join(job.preferred_tech),
                }
            )


def _write_legacy_export(output_dir: Path, jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "applications_export.csv"
    fieldnames = [
        "job_slug",
        "run_id",
        "date",
        "company",
        "role",
        "location",
        "salary_range",
        "salary_confidence",
        "interview_style",
        "application_link",
        "resume_path_generated",
        "cover_letter_path",
        "status",
        "phase3_status",
        "artifact_dir",
        "level",
        "notes",
        "rejection_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for job in jobs:
            salary_range = ""
            if job.salary_min or job.salary_max:
                minimum = int(job.salary_min) if job.salary_min else ""
                maximum = int(job.salary_max) if job.salary_max else ""
                currency = job.salary_currency or ""
                salary_range = f"{minimum}-{maximum} {currency}".strip()
            writer.writerow(
                {
                    "job_slug": job.job_slug,
                    "run_id": job.run_id,
                    "date": date.today().isoformat(),
                    "company": job.company,
                    "role": job.title,
                    "location": job.location_raw,
                    "salary_range": salary_range,
                    "salary_confidence": job.salary_confidence,
                    "interview_style": "not found",
                    "application_link": job.apply_url or job.job_url,
                    "resume_path_generated": job.resume_path_generated,
                    "cover_letter_path": job.cover_letter_path_generated,
                    "status": job.review_status,
                    "phase3_status": job.phase3_status,
                    "artifact_dir": job.artifact_dir,
                    "level": job.seniority_title,
                    "notes": job.review_notes or job.summary,
                    "rejection_reason": job.approval_reason if job.review_status == "rejected" else "",
                }
            )


def _write_approved_jobs_contract(output_dir: Path, jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "approved_jobs_latest.jsonl"
    lines = [ApprovedJobContract.from_job(job).model_dump_json() for job in jobs]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_phase3_summary(output_dir: Path, approved_jobs: list[JobRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_jobs = [job for job in approved_jobs if job.phase3_status == "generated"]
    failed_jobs = [job for job in approved_jobs if job.phase3_status == "failed"]
    pending_jobs = [job for job in approved_jobs if job.phase3_status == "not_started"]
    path = output_dir / "phase3_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "approved_jobs": len(approved_jobs),
                "generated_jobs": len(generated_jobs),
                "failed_jobs": len(failed_jobs),
                "pending_jobs": len(pending_jobs),
                "generated_job_slugs": [job.job_slug for job in generated_jobs],
                "failed_job_slugs": [job.job_slug for job in failed_jobs],
                "pending_job_slugs": [job.job_slug for job in pending_jobs],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_summary(output_dir: Path, summary: RunSummary) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "run_summary_latest.json"
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")


def _queue_job_for_review(job: JobRecord) -> None:
    job.review_status = "pending_review"
    job.review_decision_at = None
    job.review_decision_by = None
    job.review_notes = ""
    job.approval_reason = ""
    job.phase3_ready = False
    job.artifact_dir = ""


def _age_text(job: JobRecord) -> str:
    if not job.posted_at:
        return "unknown"
    return f"{(date.today() - job.posted_at).days} days old"


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:120]


def _clean_text(value: str) -> str:
    no_html = re.sub(r"<[^>]+>", " ", html.unescape(value or ""))
    return re.sub(r"\s+", " ", no_html).strip()


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
    if isinstance(value, str):
        stripped = value.strip()
        relative = _parse_relative_date(stripped)
        if relative:
            return relative
        for parser in (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
        ):
            try:
                return datetime.strptime(stripped, parser).date()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _parse_relative_date(value: str) -> date | None:
    lowered = value.lower()
    if "today" in lowered or "just posted" in lowered:
        return date.today()
    if "yesterday" in lowered:
        return date.today() - timedelta(days=1)
    match = re.search(r"(\d+)\s+(day|days|week|weeks|month|months)\s+ago", lowered)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = 1
    if unit.startswith("week"):
        multiplier = 7
    elif unit.startswith("month"):
        multiplier = 30
    return date.today() - timedelta(days=amount * multiplier)


def _parse_salary(value: str) -> tuple[float | None, float | None, str | None]:
    if not value:
        return None, None, None
    normalized = value.replace(",", "")
    numbers = [float(match) for match in re.findall(r"\d+(?:\.\d+)?", normalized)]
    currency = None
    if "usd" in normalized.lower() or "$" in normalized:
        currency = "USD"
    if not numbers:
        return None, None, currency
    if len(numbers) == 1:
        return numbers[0], numbers[0], currency
    return numbers[0], numbers[1], currency


def _matches_any(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def _first_matching_phrase(text: str, patterns: list[str]) -> str:
    lowered = text.lower()
    for pattern in patterns:
        if pattern.lower() in lowered:
            return pattern
    return ""
