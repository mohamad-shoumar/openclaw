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
    raise ValueError(f"Unsupported board type: {board_type}")


def validate_job(job: JobRecord, config: AppConfig, as_of: date | None = None) -> JobRecord:
    rules = config.rules
    as_of = as_of or date.today()
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
    exp_min, exp_max, exp_evidence = _extract_experience(job.description_text)
    job.experience_required_min = exp_min
    job.experience_required_max = exp_max
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
    if job.experience_required_max and job.experience_required_max > rules.max_required_experience_years:
        reasons.append("Required experience exceeds the strict maximum.")
    if _matches_any(title_text, rules.rejected_title_keywords):
        reasons.append("Role seniority is above the strict target.")
    if not _is_target_role(title_text, description_text, config.profile):
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

    def is_role(segment: str) -> bool:
        lowered = segment.lower()
        return any(word in lowered for word in HN_ROLE_WORDS)

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
    title = roles[0] if roles else next((s for s in segments if s != company), first_line[:120])
    location = next(
        (s for s in metas if re.search(r"remote|onsite|on-site|hybrid|worldwide|anywhere", s, re.I)),
        "Remote" if re.search(r"\bremote\b", text, re.I) else "",
    )
    return company[:120].strip(), title[:160].strip(), location[:120].strip()


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


def _is_target_role(title_text: str, description_text: str, profile=None) -> bool:
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
    if profile is not None:
        blocked_keywords = blocked_keywords + [kw.lower() for kw in profile.excluded_role_keywords]
    if _matches_any(title_text, blocked_keywords):
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
    """Match a phrase on word boundaries so 'office' never matches 'officer'."""
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
