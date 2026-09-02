from __future__ import annotations

import csv
import hashlib
import html
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
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

    sources = build_sources(config.watchlist, config.worldwide_companies)
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
    if board_type == "custom_page":
        return _normalize_custom_page(raw_job, run_id)
    if board_type == "serpapi":
        return _normalize_serpapi(raw_job, run_id)
    if board_type == "remotive":
        return _normalize_remotive(raw_job, run_id)
    if board_type == "himalayas":
        return _normalize_himalayas(raw_job, run_id)
    if board_type == "remoteok":
        return _normalize_remoteok(raw_job, run_id)
    if board_type == "weworkremotely":
        return _normalize_weworkremotely(raw_job, run_id)
    if board_type == "hackernews":
        return _normalize_hackernews(raw_job, run_id)
    if board_type == "hiringcafe":
        return _normalize_hiringcafe(raw_job, run_id)
    if board_type == "nodesk":
        return _normalize_nodesk(raw_job, run_id)
    if board_type == "remote100k":
        return _normalize_remote100k(raw_job, run_id)
    if board_type == "arc":
        return _normalize_arc(raw_job, run_id)
    raise ValueError(f"Unsupported board type: {board_type}")


def validate_job(job: JobRecord, config: AppConfig, as_of: date | None = None) -> JobRecord:
    rules = config.rules
    as_of = as_of or date.today()
    title_text = job.title.lower()
    description_text = job.description_text.lower()
    location_text = job.location_raw.lower()
    searchable_text = " ".join(part for part in [title_text, location_text, description_text] if part)

    remote_scope, remote_evidence = _classify_remote_scope(
        location_text, description_text, job.workplace_type, rules, title_text
    )
    job.remote_scope = remote_scope
    if remote_evidence:
        job.evidence_snippets.append(EvidenceSnippet(field="remote_scope", snippet=remote_evidence))

    country_restrictions, timezone_restrictions = _extract_restrictions(location_text, description_text)
    job.country_restrictions = country_restrictions
    job.timezone_restrictions = timezone_restrictions
    if country_restrictions:
        snippet = "; ".join(country_restrictions)
        job.evidence_snippets.append(EvidenceSnippet(field="restriction", snippet=snippet))

    trusted_worldwide_company = config.is_worldwide_company(job.company)
    if trusted_worldwide_company and job.remote_scope != "restricted" and not country_restrictions:
        job.remote_scope = "global"
        job.evidence_snippets.append(
            EvidenceSnippet(field="company_registry", snippet="Matched verified worldwide company registry.")
        )

    job.lebanon_eligibility = _classify_lebanon_eligibility(searchable_text, rules)
    if job.lebanon_eligibility == "ineligible":
        job.evidence_snippets.append(
            EvidenceSnippet(field="lebanon_eligibility", snippet="Restriction text suggests Lebanon is excluded.")
        )

    job.seniority_title = _classify_seniority(job.title)
    exp_min, exp_max, exp_open_ended, exp_evidence = _extract_experience(job.description_text)
    job.experience_required_min = exp_min
    job.experience_required_max = exp_max
    job.experience_open_ended = exp_open_ended
    if exp_evidence:
        job.evidence_snippets.append(EvidenceSnippet(field="experience", snippet=exp_evidence))

    required_tech, preferred_tech = _extract_tech(job.description_text, rules)
    job.required_tech = required_tech
    job.preferred_tech = preferred_tech

    reasons: list[str] = []
    blocking_rule = getattr(config, "feedback", None) and config.feedback.match(job)
    if blocking_rule:
        reasons.append(f"Blocked by feedback rule {blocking_rule.id} ({blocking_rule.value}): {blocking_rule.reason}")
    if job.remote_scope not in rules.allowed_remote_scopes:
        reasons.append(f"Remote scope '{job.remote_scope}' is not in the allowed scopes.")
    if country_restrictions:
        reasons.append("Job has country or regional remote restrictions.")
    if job.lebanon_eligibility == "ineligible":
        reasons.append("Lebanon appears to be ineligible for this role.")
    if _is_hybrid_or_onsite(searchable_text, rules):
        reasons.append("Job appears to be hybrid or on-site.")
    if job.posted_at and (as_of - job.posted_at).days > rules.max_job_age_days:
        reasons.append(f"Job is older than {rules.max_job_age_days} days.")
    experience_reason = _experience_rejection_reason(job, rules.max_required_experience_years)
    if experience_reason:
        reasons.append(experience_reason)
    if _matches_any(title_text, rules.rejected_title_keywords):
        reasons.append("Role seniority is above the strict target.")
    if _is_excluded_function(title_text, config.profile):
        reasons.append("Role function is on the excluded list.")
    if _matches_any(title_text, rules.non_posting_title_patterns):
        reasons.append("Title is not a job posting.")
    if rules.require_target_role_match and not _matches_target_role(title_text, description_text):
        reasons.append("Role type is outside the target backend/trading profile.")
    if not _has_required_skill_signal(required_tech, preferred_tech, searchable_text, rules):
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


def _normalize_himalayas(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("description", ""))
    # Himalayas states eligibility explicitly, which is exactly what the strict
    # remote-scope check needs. Empty restrictions means worldwide.
    restrictions = item.get("locationRestrictions") or []
    timezones = item.get("timezoneRestrictions") or []
    if restrictions:
        location = ", ".join(str(r) for r in restrictions)
    elif timezones:
        location = "Remote (" + ", ".join(str(t) for t in timezones) + ")"
    else:
        location = "Remote - Worldwide"
    salary_min = item.get("minSalary")
    salary_max = item.get("maxSalary")
    apply_url = item.get("applicationLink", "")
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=item.get("companyName", ""),
        title=item.get("title", ""),
        job_url=apply_url,
        apply_url=apply_url,
        posted_at=_parse_date(item.get("pubDate")),
        location_raw=location,
        workplace_type="remote",
        description_text=description,
        salary_min=float(salary_min) if isinstance(salary_min, (int, float)) and salary_min else None,
        salary_max=float(salary_max) if isinstance(salary_max, (int, float)) and salary_max else None,
        salary_currency=item.get("currency") or None,
        salary_confidence="confirmed" if salary_min or salary_max else "not found",
        summary=_clean_text(item.get("excerpt", "")) or description[:220],
    )


def _normalize_remoteok(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("description", ""))
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    location = item.get("location") or item.get("candidate_required_location") or ""
    if not location and "worldwide" in tags:
        location = "Worldwide"
    salary_min = item.get("salary_min")
    salary_max = item.get("salary_max")
    url = item.get("url") or item.get("apply_url") or ""
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=item.get("company", ""),
        title=item.get("position") or item.get("title", ""),
        job_url=url,
        apply_url=item.get("apply_url") or url,
        posted_at=_parse_date(item.get("date") or item.get("epoch")),
        location_raw=location,
        workplace_type="remote",
        description_text=description,
        salary_min=float(salary_min) if isinstance(salary_min, (int, float)) and salary_min else None,
        salary_max=float(salary_max) if isinstance(salary_max, (int, float)) and salary_max else None,
        salary_currency="USD" if salary_min or salary_max else None,
        salary_confidence="estimated" if salary_min or salary_max else "not found",
        summary=(description[:220] or ", ".join(tags[:8])),
    )


def _normalize_weworkremotely(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("description", ""))
    # WWR titles are "Company: Role"; the region lives in its own element.
    raw_title = item.get("title", "")
    company, _, role = raw_title.partition(":")
    if not role:
        company, role = "", raw_title
    location = item.get("region") or ""
    if not location:
        match = re.search(r"(anywhere in the world|worldwide|[A-Z][A-Za-z ]+ only)", description)
        location = match.group(1) if match else "Remote"
    link = item.get("link", "")
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=company.strip(),
        title=role.strip(),
        job_url=link,
        apply_url=link,
        posted_at=_parse_date(item.get("pubDate")),
        location_raw=location,
        workplace_type="remote",
        description_text=description,
        summary=description[:220],
    )


def _eligibility_location(worldwide_ok: bool, countries: list[str]) -> str:
    """Render a machine-known eligibility list into text the rule engine can read.

    HiringCafe and Arc both hand us the eligible-country list as structured data,
    which every prose-based board makes us infer. Phrasing it as "must be based
    in ..." keeps a single code path: the same restriction patterns that catch
    the phrase in a job description catch it here.
    """
    if worldwide_ok:
        return "Remote - Worldwide"
    listed = [str(code).strip() for code in countries if str(code).strip()]
    if not listed:
        # No stated restriction is absence of evidence, not worldwide eligibility;
        # "open" is the scope that says so.
        return "Remote"
    return f"Remote - must be based in {', '.join(listed)}"


def _normalize_hiringcafe(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    processed = item.get("v5_processed_job_data") or {}
    company_data = item.get("enriched_company_data") or {}
    info = item.get("job_information") or {}

    worldwide_ok = bool(processed.get("is_workplace_worldwide_ok"))
    countries = processed.get("boundless_workplace_countries") or processed.get("workplace_countries") or []
    location = _eligibility_location(worldwide_ok, list(countries))

    # Search hits carry no full posting body, only HiringCafe's own extraction of
    # it. Compose the fields that actually decide a match so skill and
    # eligibility checks have real text to read rather than an empty string.
    parts = [
        processed.get("requirements_summary") or "",
        "Tools: " + ", ".join(str(t) for t in (processed.get("technical_tools") or [])),
        "Responsibilities: " + ", ".join(str(a) for a in (processed.get("role_activities") or [])),
        f"Seniority: {processed.get('seniority_level') or 'unknown'}.",
        f"Workplace: {processed.get('formatted_workplace_location') or location}.",
        location + ".",
        company_data.get("tagline") or processed.get("company_tagline") or "",
    ]
    description = _clean_text(" ".join(part for part in parts if part.strip(" ,:")))

    salary_min = processed.get("yearly_min_compensation")
    salary_max = processed.get("yearly_max_compensation")
    apply_url = item.get("apply_url") or ""
    workplace_type = str(processed.get("workplace_type") or "remote").lower()
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=processed.get("company_name") or company_data.get("name") or "",
        title=info.get("title") or processed.get("core_job_title") or info.get("job_title_raw", ""),
        job_url=apply_url,
        apply_url=apply_url,
        posted_at=_parse_date(
            processed.get("estimated_publish_date") or processed.get("estimated_publish_date_millis")
        ),
        location_raw=location,
        workplace_type=workplace_type,
        description_text=description,
        salary_min=float(salary_min) if isinstance(salary_min, (int, float)) and salary_min else None,
        salary_max=float(salary_max) if isinstance(salary_max, (int, float)) and salary_max else None,
        salary_currency=processed.get("listed_compensation_currency") or None,
        salary_confidence=(
            "confirmed"
            if processed.get("is_compensation_transparent") and (salary_min or salary_max)
            else "estimated" if (salary_min or salary_max) else "not found"
        ),
        summary=_clean_text(processed.get("requirements_summary") or "") or description[:220],
    )


def _normalize_nodesk(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("description", ""))
    # NoDesk titles read "Role at Company"; split on the last " at " because role
    # names contain the word themselves ("Engineer at ..." vs "Look at Media").
    # The feed double-escapes entities, so one unescape in the RSS reader leaves
    # "&amp;" behind in titles like "Audio &amp; Display Specialist".
    raw_title = html.unescape(item.get("title", ""))
    role, separator, company = raw_title.rpartition(" at ")
    if not separator:
        role, company = raw_title, ""
    match = re.search(
        r"(anywhere in the world|worldwide|remote, [A-Z][A-Za-z ]+|[A-Z][A-Za-z ]+ only)", description
    )
    link = item.get("link", "")
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=company.strip(),
        title=role.strip(),
        job_url=link,
        apply_url=link,
        posted_at=_parse_date(item.get("pubDate")),
        location_raw=match.group(1) if match else "Remote",
        workplace_type="remote",
        description_text=description,
        summary=description[:220],
    )


def _normalize_remote100k(raw_job: RawJob, run_id: str) -> JobRecord:
    posting = raw_job.payload["job"]
    description = _clean_text(posting.get("description", ""))
    organization = posting.get("hiringOrganization")
    company = organization.get("name", "") if isinstance(organization, dict) else str(organization or "")

    requirements = posting.get("applicantLocationRequirements")
    entries = requirements if isinstance(requirements, list) else [requirements]
    countries = [
        entry.get("name", "") if isinstance(entry, dict) else str(entry or "")
        for entry in entries
        if entry
    ]
    telecommute = str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE"
    location = _eligibility_location(False, countries) if countries else ("Remote" if telecommute else "")

    salary_min = salary_max = None
    currency = None
    base_salary = posting.get("baseSalary")
    if isinstance(base_salary, dict):
        currency = base_salary.get("currency")
        value = base_salary.get("value")
        if isinstance(value, dict):
            salary_min = value.get("minValue")
            salary_max = value.get("maxValue")

    url = posting.get("url") or raw_job.payload.get("url", "")
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=company,
        title=posting.get("title", ""),
        job_url=url,
        apply_url=url,
        posted_at=_parse_date(posting.get("datePosted")),
        location_raw=location,
        workplace_type="remote" if telecommute else "unknown",
        description_text=description,
        salary_min=float(salary_min) if isinstance(salary_min, (int, float)) and salary_min else None,
        salary_max=float(salary_max) if isinstance(salary_max, (int, float)) and salary_max else None,
        salary_currency=currency or None,
        # The board's whole premise is a $100k floor, and the posting states the
        # band outright, so treat it as the employer's own figure.
        salary_confidence="confirmed" if salary_min or salary_max else "not found",
        summary=description[:220],
    )


def _normalize_arc(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    company_data = item.get("company") if isinstance(item.get("company"), dict) else {}
    company = company_data.get("name") or ""

    countries = [str(c).strip() for c in (item.get("requiredCountries") or []) if str(c).strip()]
    location = _eligibility_location(not countries, countries)

    categories = [
        c.get("name", "") for c in (item.get("categories") or []) if isinstance(c, dict) and c.get("name")
    ]
    levels = item.get("experienceLevels") or ([item["experienceLevel"]] if item.get("experienceLevel") else [])
    # Arc's list payload has no posting body and gates the full posting behind an
    # account, so the record carries its structured metadata as the description.
    description = _clean_text(
        " ".join(
            part
            for part in [
                f"{item.get('title', '')} - {item.get('jobType') or ''} {item.get('jobRole') or item.get('positionType') or ''} role at {company or 'an undisclosed company'}.",
                "Skills: " + ", ".join(categories) + "." if categories else "",
                "Experience level: " + ", ".join(str(level) for level in levels) + "." if levels else "",
                location + ".",
                "Arc requires an account to view the full posting and apply.",
            ]
            if part.strip()
        )
    )

    salary_min = item.get("minAnnualSalary")
    salary_max = item.get("maxAnnualSalary")
    hourly_min, hourly_max = item.get("minHourlyRate"), item.get("maxHourlyRate")
    if not (salary_min or salary_max) and (hourly_min or hourly_max):
        # Marketplace contracts quote an hourly rate; annualise at 2,080 hours so
        # the band is comparable to every other source in the archive.
        salary_min = hourly_min * 2080 if isinstance(hourly_min, (int, float)) else None
        salary_max = hourly_max * 2080 if isinstance(hourly_max, (int, float)) else None
        confidence = "estimated"
    else:
        confidence = "confirmed" if salary_min or salary_max else "not found"

    # Deduplication keys on the normalized URL, which keeps only scheme, host and
    # path - so every job on a listing page sharing that page's URL would collapse
    # to a single record. Address each posting by Arc's own slug instead: the path
    # is unique per job and resolves for a signed-in user, falling back to the
    # listing page for anyone who is not.
    listing_url = raw_job.payload.get("listing_url", "https://arc.dev/remote-jobs")
    slug = item.get("urlString") or ""
    job_url = f"https://arc.dev/remote-jobs/{slug}" if slug else listing_url
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=company,
        title=item.get("title", ""),
        job_url=job_url,
        apply_url=job_url,
        posted_at=_parse_date(item.get("postedAt")),
        location_raw=location,
        workplace_type="remote",
        description_text=description,
        salary_min=float(salary_min) if isinstance(salary_min, (int, float)) and salary_min else None,
        salary_max=float(salary_max) if isinstance(salary_max, (int, float)) and salary_max else None,
        salary_currency="USD" if salary_min or salary_max else None,
        salary_confidence=confidence,
        summary=description[:220],
    )


# Words that mark a pipe-delimited segment as a job title rather than a company.
HN_ROLE_WORDS = (
    "engineer", "developer", "scientist", "designer", "architect", "analyst",
    "manager", "lead", "director", "devops", "sre", "researcher", "programmer",
    "consultant", "specialist", "administrator", "intern", "founding",
    "full-stack", "fullstack", "frontend", "front-end", "backend", "back-end",
)
# Segments that describe logistics, not the company or the role.
HN_META_WORDS = (
    "remote", "onsite", "on-site", "hybrid", "worldwide", "anywhere", "visa",
    "full-time", "full time", "part-time", "part time", "contract", "intern",
    "salary", "equity", "usd", "eur", "$", "location:", "comp:", "email",
)


# Hosts that are never an employer or an application target. HN posters link
# demo videos, screenshots and social profiles freely, and the first href in a
# comment is very often one of those rather than the way to apply.
HN_NON_APPLY_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "loom.com", "asciinema.org",
    "twitter.com", "x.com", "bsky.app", "mastodon.social", "threads.net",
    "imgur.com", "i.redd.it", "reddit.com", "facebook.com", "instagram.com",
    "producthunt.com", "crunchbase.com", "wikipedia.org", "medium.com",
    "substack.com", "calendly.com", "cal.com", "discord.gg", "t.me",
)
# Hosts that host an application form or an ATS posting: strongest apply signal.
HN_APPLY_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "recruitee.com",
    "smartrecruiters.com", "breezy.hr", "teamtailor.com", "jobvite.com",
    "bamboohr.com", "rippling.com", "pinpointhq.com", "workatastartup.com",
    "forms.gle", "docs.google.com", "typeform.com", "airtable.com", "tally.so",
    "notion.site", "fillout.com", "jotform.com",
)
# Path fragments that mark a careers or job page on a company's own domain.
HN_CAREER_PATH_HINTS = (
    "/careers", "/career", "/jobs", "/job/", "/join", "/hiring", "/apply",
    "/work-with-us", "/we-are-hiring", "/opportunities", "/positions", "/roles",
)


def _host_of(url: str) -> str:
    if not url or "://" not in url:
        return ""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _host_in(host: str, group: tuple[str, ...]) -> bool:
    return any(host == entry or host.endswith("." + entry) for entry in group)


def _pick_hn_apply_url(raw_text: str, item_id: Any) -> str:
    """Choose the link a candidate should actually follow to apply.

    HN comments routinely lead with a demo video or a social link, so position
    in the comment says nothing. Score each candidate by what it looks like, and
    fall back to the HN permalink rather than guessing wrong.
    """
    unescaped = html.unescape(raw_text or "")
    candidates = re.findall(r'href="([^"]+)"', unescaped)
    if not candidates:
        candidates = re.findall(r"https?://[^\s<>\"')]+", unescaped)

    plain = re.sub(r"<[^>]+>", " ", unescaped)

    def score(url: str) -> int:
        host = _host_of(url)
        if not host or _host_in(host, HN_NON_APPLY_HOSTS):
            return -1
        path = urlsplit(url).path.lower()
        value = 1  # a plausible company link
        if _host_in(host, HN_APPLY_HOSTS):
            value = 100
        elif any(hint in path for hint in HN_CAREER_PATH_HINTS):
            value = 80
        elif not path.strip("/"):
            # A bare domain root is a marketing homepage; nobody applies there.
            # Zeroing it lets the email and HN-permalink fallbacks win, and both
            # carry the poster's own instructions. Cogram, IVPN and Pingintel all
            # linked only their homepage and it was stored as the apply URL.
            return 0
        # "Apply here: <url>" and friends: proximity to an apply verb is a
        # stronger signal than anything in the URL itself.
        position = plain.find(url)
        if position > 0:
            preceding = plain[max(0, position - 60) : position].lower()
            if re.search(r"appl(y|ication)|hiring|job post|to apply|send.*resume", preceding):
                value += 60
        return value

    best = max(candidates, key=score, default="")
    if best and score(best) > 0:
        return best

    # An email application is common and better than a wrong link.
    mail = re.search(r"mailto:([^\"'\s>]+)", unescaped) or re.search(
        r"[\w.+-]+@[\w-]+\.[\w.]+", plain
    )
    if mail:
        address = mail.group(1) if mail.lastindex else mail.group(0)
        return f"mailto:{address.lstrip('mailto:')}"

    return f"https://news.ycombinator.com/item?id={item_id}"


# ATS hosts where the employer is the first path segment, not the domain.
ATS_HOSTS_WITH_COMPANY_PATH = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "recruitee.com", "smartrecruiters.com", "breezy.hr", "teamtailor.com",
)


def _company_from_url(url: str) -> str:
    """Derive a display company name from a careers URL: seeq.com -> Seeq."""
    if not url or "://" not in url:
        return ""
    split = urlsplit(url)
    host = split.netloc.lower()
    host = host[4:] if host.startswith("www.") else host

    # Generic form and media hosts carry no company identity: forms.gle would
    # otherwise become the company "Gle".
    GENERIC_HOSTS = ("forms.gle", "docs.google.com", "typeform.com", "airtable.com",
                     "tally.so", "notion.site", "fillout.com", "jotform.com",
                     "news.ycombinator.com")
    if _host_in(host, GENERIC_HOSTS) or _host_in(host, HN_NON_APPLY_HOSTS):
        return ""

    # On an ATS the domain is the vendor; the employer is in a subdomain
    # (acme.recruitee.com) or a path segment (greenhouse.io/acme/...).
    if any(host == ats or host.endswith("." + ats) for ats in ATS_HOSTS_WITH_COMPANY_PATH):
        vendor_subdomains = {"boards", "board", "job-boards", "jobs", "apply", "api", "www", "secure"}
        subdomain = host.split(".")[0]
        if subdomain not in vendor_subdomains and not host.startswith(tuple(ATS_HOSTS_WITH_COMPANY_PATH)):
            return subdomain.replace("-", " ").title()
        # Skip routing noise; the employer slug is the last meaningful segment.
        noise = {"jobs", "j", "o", "embed", "companies", "posting-api", "job-board", "v1", "api"}
        segments = [s for s in split.path.split("/") if s and s.lower() not in noise]
        if segments:
            return segments[0].replace("-", " ").title()
        return ""

    parts = [p for p in host.split(".") if p not in {"com", "io", "org", "net", "co", "ai", "dev", "app", "uk", "de"}]
    parts = [p for p in parts if p not in {"jobs", "careers", "boards", "apply", "job-boards", "www"}]
    if not parts:
        return ""
    return parts[-1].replace("-", " ").title()


def _parse_hn_headline(text: str, author: str, fallback_url: str = "") -> tuple[str, str, str]:
    """Pull company, title and location out of a free-form HN hiring comment.

    The thread convention is "Company | Role | Location | ..." but posters vary
    it constantly: the company may carry an inline URL, the role may come first,
    and segments may be pure logistics. So classify each segment instead of
    trusting position.
    """
    first_line = text.split("\n")[0][:300]
    segments = [
        re.sub(r"https?://\S+", "", part).strip(" -–—(),:")
        for part in re.split(r"\s*[|·—]\s*", first_line)
    ]
    segments = [s for s in segments if s]

    # A job title is a short phrase. Prose that happens to contain a role word is
    # not one: "AI platform for the architecture, engineering, and construction
    # industry" matched "engineering" and became the title of a Cogram posting.
    HN_MAX_TITLE_CHARS = 90

    def is_role(segment: str) -> bool:
        lowered = segment.lower()
        return len(segment) <= HN_MAX_TITLE_CHARS and any(word in lowered for word in HN_ROLE_WORDS)

    def is_meta(segment: str) -> bool:
        lowered = segment.lower()
        return any(word in lowered for word in HN_META_WORDS)

    roles = [s for s in segments if is_role(s)]
    # A company segment is one that is neither a role nor pure logistics.
    companies = [s for s in segments if not is_role(s) and not is_meta(s)]
    metas = [s for s in segments if is_meta(s) and not is_role(s)]

    # With a role-first headline there is no company segment. Falling back to
    # segments[0] would print the job title as the employer, so derive the name
    # from the linked domain instead and only then give up to the HN handle.
    company = companies[0] if companies else (_company_from_url(fallback_url) or f"HN: {author}")

    def pick_title() -> str:
        if roles:
            return roles[0]
        # No segment names a role. Skip the logistics segments rather than taking
        # the first leftover: that is how "Miami or Remote" and "Remote (US)
        # First with offices in NYC and SF" ended up as job titles.
        for segment in segments:
            if segment != company and not is_meta(segment) and len(segment) <= HN_MAX_TITLE_CHARS:
                return segment
        # Headlines that name no role at all do exist. Look for one in the body
        # before resorting to printing the company's opening sentence.
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text[:800]):
            candidate = sentence.strip(" -:*")
            if candidate and is_role(candidate):
                return candidate
        return first_line[:120]

    title = pick_title()
    location = next(
        (s for s in metas if re.search(r"remote|onsite|on-site|hybrid|worldwide|anywhere", s, re.I)),
        "Remote" if re.search(r"\bremote\b", text, re.I) else "",
    )
    # A headline that omits the separator after the location runs it straight into
    # the company blurb, e.g. "Remote (US) First with offices in NYC and SF We're
    # building a healthier future...". Keep the locative phrase, drop the prose.
    location = re.split(r"(?<=[.!?])\s|\s+(?:we|our|the company)\b", location, maxsplit=1, flags=re.I)[0]
    return company[:120].strip(), title[:160].strip(), location[:80].strip()


def _normalize_hackernews(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    # HN comment text is HTML with entity-escaped URLs; unescape before any
    # regex work or every extracted link comes out as https:&#x2F;&#x2F;...
    raw_text = html.unescape(item.get("text", "") or "")
    text = _clean_text(raw_text)
    url = _pick_hn_apply_url(item.get("text", ""), item.get("id"))
    company, title, location = _parse_hn_headline(text, item.get("author") or "unknown", url)
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=company[:120],
        title=title,
        job_url=url,
        apply_url=url,
        posted_at=_parse_date(item.get("created_at")),
        location_raw=location,
        workplace_type="remote" if re.search(r"\bremote\b", text, re.I) else "",
        description_text=text,
        summary=text.split("\n")[0][:300],
    )


def _normalize_custom_page(raw_job: RawJob, run_id: str) -> JobRecord:
    item = raw_job.payload["job"]
    description = _clean_text(item.get("description", ""))
    summary = _clean_text(item.get("summary", "")) or description[:220]
    location = item.get("location", "")
    workplace_type = item.get("workplace_type", "unknown")
    job_url = item.get("job_url", item.get("apply_url", ""))
    apply_url = item.get("apply_url", job_url)
    return _base_job_record(
        raw_job=raw_job,
        run_id=run_id,
        company=raw_job.payload.get("company", ""),
        title=item.get("title", ""),
        job_url=job_url,
        apply_url=apply_url,
        posted_at=_parse_date(item.get("posted_at")),
        location_raw=location,
        workplace_type=workplace_type,
        description_text=description,
        summary=summary,
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


def _classify_remote_scope(
    location_text: str, description_text: str, workplace_type: str, rules, title_text: str = ""
) -> tuple[str, str]:
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

    # A stated geographic limit outranks worldwide phrasing, so this runs before
    # the global patterns below. Postings say "Fully Remote (US-Based Candidates)"
    # and "Only W2 || REMOTE" constantly: the limit is the operative half, and
    # checking the marketing phrase first classified those as globally open.
    # The title is scanned too because posters put "US Only" and "W2" there.
    restriction_scope_text = " ".join(part for part in [location_text, title_text.lower()] if part)
    if _matches_any(restriction_scope_text, rules.remote_restriction_patterns):
        return "restricted", _first_matching_phrase(restriction_scope_text, rules.remote_restriction_patterns)

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
    # A remote role that never says "worldwide" is the common case, not a red flag.
    # Absence of restriction evidence is treated as open rather than disqualifying;
    # ranking and manual review sort out the rest.
    if has_remote_signal:
        return "open", "remote with no stated geographic restriction"
    return "unknown", ""


def _classify_lebanon_eligibility(searchable_text: str, rules=None) -> str:
    if "not hiring in lebanon" in searchable_text or "excluding lebanon" in searchable_text:
        return "ineligible"
    # Regions named as excluded from hiring, e.g. "middle east".
    if rules is not None:
        for pattern in rules.lebanon_block_patterns:
            if _phrase_pattern(f"cannot hire in {pattern}").search(searchable_text):
                return "ineligible"
            if _phrase_pattern(f"not hiring in {pattern}").search(searchable_text):
                return "ineligible"
            if _phrase_pattern(f"excluding {pattern}").search(searchable_text):
                return "ineligible"
    if "lebanon" in searchable_text and "eligible" in searchable_text:
        return "eligible"
    return "unknown"


# Phrases that genuinely bound where a role can be performed. Bare region names
# ("emea", "apac") are deliberately absent: they appear constantly in descriptions
# of the business ("our EMEA customers") without limiting the hire.
RESTRICTION_PHRASES = (
    "us only",
    "usa only",
    "united states only",
    "uk only",
    "canada only",
    "europe only",
    "eu only",
    "emea only",
    "apac only",
    "latam only",
    "emea region only",
    "must be based in",
    "must reside in",
    "must be located in",
    "must live in",
    "authorized to work in",
    "eligible to work in",
    "work authorization in",
    "residents of",
    "no visa sponsorship",
    "visa sponsorship unavailable",
    "cannot hire in",
    "not hiring in",
    "restricted to candidates",
)

# Location strings naming a specific country/region alongside "remote" indicate a
# bounded remote role. "worldwide"/"global"/"anywhere" override this.
LOCATION_COUNTRY_TOKENS = (
    "usa", "u.s.", "united states", "uk", "united kingdom", "germany", "france",
    "india", "denmark", "japan", "finland", "canada", "australia", "brazil",
    "poland", "spain", "netherlands", "portugal", "ireland", "singapore",
)
GLOBAL_LOCATION_TOKENS = ("worldwide", "global", "anywhere", "international")


def _extract_restrictions(location_text: str, description_text: str) -> tuple[list[str], list[str]]:
    country_hits: list[str] = []
    combined_text = " ".join(part for part in [location_text, description_text] if part)
    for phrase in RESTRICTION_PHRASES:
        if _phrase_pattern(phrase).search(combined_text):
            country_hits.append(phrase)

    stripped_location = location_text.strip()
    location_is_global = any(token in location_text for token in GLOBAL_LOCATION_TOKENS)
    if stripped_location and not location_is_global:
        if "remote" in location_text:
            # "Remote - Germany" style: remote, but pinned to a country.
            if any(_phrase_pattern(token).search(location_text) for token in LOCATION_COUNTRY_TOKENS):
                country_hits.append(stripped_location)
        elif not _is_placeless_location(stripped_location):
            # A location that names a physical place and says nothing about remote
            # is a location requirement, whatever the description claims.
            country_hits.append(stripped_location)
    return country_hits, []


# Location strings that carry no geographic meaning; anything else that fails to
# mention remote work is treated as naming a physical place.
PLACELESS_LOCATIONS = {
    "", "-", "n/a", "na", "none", "any", "various", "multiple", "flexible",
    "distributed", "unspecified", "not specified", "tbd",
}


def _is_placeless_location(location_text: str) -> bool:
    return location_text.strip().lower().strip(".,;:") in PLACELESS_LOCATIONS


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


# Words that stand in for a digit in "five years of experience".
NUMBER_WORD_VALUES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20,
}

# Headings under which a year count is a bonus rather than the bar for the role.
# A "2+ years of Python" nice-to-have must not stand in for the "8+ years" the
# posting actually requires.
EXPERIENCE_BONUS_MARKERS = (
    "nice to have", "nice-to-have", "nice to haves", "bonus points", "bonus if",
    "bonus:", "preferred qualifications", "preferred experience",
    "preferred skills", "good to have", "desirable", "a plus", "is a plus",
    "pluses", "optional", "not required", "would be great",
)

# Headings that reopen a requirements block after a bonus block.
EXPERIENCE_REQUIRED_MARKERS = (
    "requirement", "required", "must have", "qualifications", "what you need",
    "what you'll need", "who you are", "we're looking for", "we are looking for",
    "you have", "you bring", "about you", "experience:", "minimum qualifications",
    "basic qualifications", "skills and qualifications",
)

_YEARS_UNIT = r"(?:years?|yrs?\.?)(?![a-z0-9])"
_NUMBER = r"(?:\d{1,2}|" + "|".join(NUMBER_WORD_VALUES) + r")"
# Trailing markers that make a floor unbounded: "5+", "5 plus", "5 or more".
_OPEN_SUFFIX = r"(?:\s*\+|\s+plus\b|\s+or\s+more\b|\s+or\s+above\b|\s+or\s+greater\b)"

_EXPERIENCE_RANGE_RE = re.compile(
    rf"({_NUMBER})\s*(?:[-–—]|\bto\b)\s*({_NUMBER})({_OPEN_SUFFIX})?\s*{_YEARS_UNIT}"
)
_EXPERIENCE_SINGLE_RE = re.compile(
    rf"({_NUMBER})({_OPEN_SUFFIX})?\s*{_YEARS_UNIT}"
)
# Leading phrases that make a floor unbounded even without a "+".
_OPEN_PREFIX_RE = re.compile(
    r"(?:at\s+least|a\s+minimum\s+of|minimum\s+of|minimum|min\.?|more\s+than|"
    r"no\s+less\s+than|upwards\s+of|over)\s+$"
)
# A count that measures elapsed time or company history, not candidate experience.
_TIME_DECOY_PREFIX_RE = re.compile(
    r"(?:past|last|next|previous|founded|since"
    # "we have been licensing Kraken for over 4 years" is company history, while
    # "you have over 4 years of experience" is the requirement, so the elapsed-time
    # reading needs the "for" in front of it.
    r"|for\s+(?:over|more\s+than|nearly|almost|the\s+past))\s+$"
)
_TIME_DECOY_SUFFIX_RE = re.compile(r"^\s*(?:ago|of\s+operation)(?!\w)")


def _experience_section_is_bonus(text: str, position: int) -> bool:
    """Whether the year count at `position` sits under a bonus heading.

    Bonus and requirement headings are located independently and the nearest
    preceding one wins. Requirement markers falling inside a bonus marker are
    skipped so "preferred qualifications" is not read as a requirements heading
    through its own "qualifications" substring.
    """
    window = text[:position]
    bonus_spans = [
        match.span()
        for marker in EXPERIENCE_BONUS_MARKERS
        for match in re.finditer(re.escape(marker), window)
    ]
    last_bonus = max((start for start, _ in bonus_spans), default=-1)
    last_required = -1
    for marker in EXPERIENCE_REQUIRED_MARKERS:
        for match in re.finditer(re.escape(marker), window):
            if any(start <= match.start() < end for start, end in bonus_spans):
                continue
            last_required = max(last_required, match.start())
    return last_bonus > last_required


def _parse_experience_number(token: str) -> int | None:
    return int(token) if token.isdigit() else NUMBER_WORD_VALUES.get(token)


def _collect_experience_matches(lowered: str) -> list[tuple[int, int | None, bool, bool, str]]:
    """Every year-count requirement in the text, as (floor, ceiling, open_ended, is_bonus, snippet).

    Ranges are collected first and their spans block the single-number pattern,
    so "3 - 5 years" yields one 3..5 requirement rather than a bare 3 and a bare 5.
    """
    found: list[tuple[int, int | None, bool, bool, str]] = []
    consumed: list[tuple[int, int]] = []

    for pattern, is_range in ((_EXPERIENCE_RANGE_RE, True), (_EXPERIENCE_SINGLE_RE, False)):
        for match in pattern.finditer(lowered):
            if any(start <= match.start() < end for start, end in consumed):
                continue
            floor = _parse_experience_number(match.group(1))
            if floor is None:
                continue
            ceiling = _parse_experience_number(match.group(2)) if is_range else floor
            open_marker = match.group(3) if is_range else match.group(2)

            before = lowered[max(0, match.start() - 30) : match.start()]
            after = lowered[match.end() : match.end() + 20]
            if _TIME_DECOY_PREFIX_RE.search(before) or _TIME_DECOY_SUFFIX_RE.match(after):
                continue

            open_ended = bool(open_marker) or bool(_OPEN_PREFIX_RE.search(before))
            if open_ended:
                ceiling = None
            if ceiling is not None and ceiling < floor:
                floor, ceiling = ceiling, floor

            consumed.append(match.span())
            snippet = lowered[max(0, match.start() - 40) : match.end() + 40]
            is_bonus = _experience_section_is_bonus(lowered, match.start())
            found.append((floor, ceiling, open_ended, is_bonus, snippet))
    return found


def _extract_experience(text: str) -> tuple[int | None, int | None, bool, str]:
    """The binding years-of-experience requirement as (min, max, open_ended, evidence).

    The floor is the highest one stated in a requirements block, because a
    posting listing several counts is bounded by its largest hard requirement,
    not by whichever one happens to appear first. `open_ended` records a "5+"
    that the old closed (5, 5) range silently dropped, so the gate can treat
    "5 or more" as exceeding a ceiling of 5. Counts appearing only under a
    bonus heading are used when nothing else states one, and the snippet says
    so, so a reviewer can see where the number came from.
    """
    lowered = text.lower()
    matches = _collect_experience_matches(lowered)
    if not matches:
        return None, None, False, ""

    required = [entry for entry in matches if not entry[3]]
    pool = required or matches
    floor, ceiling, open_ended, is_bonus, snippet = max(pool, key=lambda entry: (entry[0], entry[2]))
    prefix = "preferred/bonus section: " if is_bonus else ""
    return floor, ceiling, open_ended, f"{prefix}{snippet}"


def _format_experience_requirement(job: JobRecord) -> str:
    floor = job.experience_required_min
    if floor is None:
        return "an unstated number of years"
    if job.experience_open_ended:
        return f"{floor}+ years"
    if job.experience_required_max is not None and job.experience_required_max != floor:
        return f"{floor}-{job.experience_required_max} years"
    return f"{floor} years"


def _experience_rejection_reason(job: JobRecord, ceiling: int) -> str | None:
    """Reject a posting that asks for more experience than the ceiling allows.

    The floor carries the requirement: "5+ years" asks for at least five and
    possibly many more, so it exceeds a ceiling of five, while a closed
    "3 - 5 years" fits inside it. The previous check compared only the range
    ceiling with a strict `>`, so every posting whose floor sat exactly on the
    limit survived - including "5+ years", which the extractor had already
    flattened into a closed (5, 5).
    """
    floor = job.experience_required_min
    if floor is None:
        return None
    exceeds = (
        floor >= ceiling
        if job.experience_open_ended
        else floor > ceiling
        or (job.experience_required_max is not None and job.experience_required_max > ceiling)
    )
    if not exceeds:
        return None
    return (
        f"Requires {_format_experience_requirement(job)}, "
        f"above the strict maximum of {ceiling}."
    )


def _extract_tech(text: str, rules) -> tuple[list[str], list[str]]:
    lowered = text.lower()
    required = sorted({skill for skill in rules.required_skill_keywords if skill in lowered})
    preferred = sorted({skill for skill in rules.preferred_skill_keywords if skill in lowered})
    return required, preferred


# Phrases where "office" describes a remote-work perk or a company fact rather than
# the work mode of this role. Stripped before hybrid detection so a fully remote job
# offering a "home office stipend" is not read as on-site.
REMOTE_OFFICE_DECOYS = (
    "home office",
    "remote office",
    "home-office",
    "office stipend",
    "office support",
    "office setup",
    "office set-up",
    "office equipment",
    "office budget",
    "office allowance",
    "office furniture",
    "office reimbursement",
    "offices in",
    "office in",
    "office space of your choosing",
    "back office",
    "front office",
    "middle office",
    "family office",
)


def _is_hybrid_or_onsite(searchable_text: str, rules) -> bool:
    """Detect a role that requires physical presence.

    The naive version matched the bare substring "office", which fired on remote
    perks ("home office stipend"), on job titles ("compliance officer"), and on
    company blurbs ("offices in London"). Decoys are removed first, then the
    remaining text is matched on word boundaries.
    """
    cleaned = searchable_text.lower()
    for decoy in REMOTE_OFFICE_DECOYS:
        cleaned = cleaned.replace(decoy, " ")
    return _matches_any(cleaned, rules.hybrid_patterns)


def _has_rejected_primary_stack(text: str, rules) -> bool:
    lowered = text.lower()
    if "python" in lowered:
        return False
    return any(keyword in lowered for keyword in rules.rejected_primary_stack_keywords)


EXCLUDED_FUNCTION_KEYWORDS = (
    "account executive",
    "sales",
    "marketing",
    "customer success",
    "recruiter",
    "designer",
    "product manager",
    "business development",
)

TARGET_ROLE_TITLE_KEYWORDS = (
    "backend",
    "python",
    "software engineer",
    "software developer",
    "developer",
    "engineer",
    "platform",
    "trading",
    "api",
)


def _is_excluded_function(title_text: str, profile=None) -> bool:
    """A title naming a job function the candidate does not do at all.

    Kept separate from the target-role test because the two failed for opposite
    reasons under one verdict: "Account Executive" is a hard no, while a title
    the keyword list simply does not recognise is a candidate for manual review.
    """
    blocked = list(EXCLUDED_FUNCTION_KEYWORDS)
    if profile is not None:
        blocked += [kw.lower() for kw in profile.excluded_role_keywords]
    return _matches_any(title_text, blocked)


def _matches_target_role(title_text: str, description_text: str) -> bool:
    """Positive evidence that this is a backend/trading engineering role."""
    if any(keyword in title_text for keyword in TARGET_ROLE_TITLE_KEYWORDS):
        return True
    return any(keyword in description_text for keyword in ["python", "backend", "fastapi", "django", "trading systems"])


def _has_required_skill_signal(
    required_tech: list[str], preferred_tech: list[str], searchable_text: str, rules
) -> bool:
    """Whether the posting shows evidence of the required skill.

    An empty `required_skill_keywords` disables the gate. Otherwise a posting
    passes on a direct keyword hit, or on any `required_skill_alternatives`
    phrase: a job asking for FastAPI or Celery is a Python job whether or not
    the word "Python" survived the scrape.
    """
    if not rules.required_skill_keywords:
        return True
    found = set(required_tech) | set(preferred_tech)
    if any(skill in found for skill in rules.required_skill_keywords):
        return True
    return _matches_any(searchable_text, rules.required_skill_alternatives)


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
            # RFC 822, used by RSS feeds such as We Work Remotely.
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%d %b %Y %H:%M:%S %z",
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


@lru_cache(maxsize=2048)
def _phrase_pattern(phrase: str) -> re.Pattern:
    """Match a phrase on word boundaries so 'office' never matches 'officer'.

    Cached because the rule sets are fixed while a run or a replay walks
    thousands of postings, so the same few hundred patterns are otherwise
    recompiled once per job.
    """
    return re.compile(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)")


def _matches_any(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(_phrase_pattern(pattern).search(lowered) for pattern in patterns)


def _first_matching_phrase(text: str, patterns: list[str]) -> str:
    lowered = text.lower()
    for pattern in patterns:
        if _phrase_pattern(pattern).search(lowered):
            return pattern
    return ""
