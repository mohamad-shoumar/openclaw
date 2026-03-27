from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .models import BoardConfig, CustomPageConfig, RawJob, WatchlistConfig
from .registry import WorldwideCompanyRecord, build_registry_source_configs


DEFAULT_HEADERS = {
    "User-Agent": "openclaw-jobsearch/0.1 (+https://openclaw.local)"
}


def fetch_json(url: str) -> dict | list:
    request = Request(url, headers=DEFAULT_HEADERS)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_plain(url: str) -> dict | list:
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_post(url: str, payload: dict, *, timeout: int = 30) -> dict | list:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**DEFAULT_HEADERS, "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_html_details(url: str, *, timeout: int = 30) -> tuple[str, str]:
    request = Request(url, headers=DEFAULT_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore"), response.geturl()


def fetch_html(url: str, *, timeout: int = 30) -> str:
    return fetch_html_details(url, timeout=timeout)[0]


class SourceAdapter:
    name = "source"

    def fetch(self) -> list[RawJob]:
        raise NotImplementedError


class GreenhouseSource(SourceAdapter):
    name = "greenhouse"

    def __init__(self, boards: Iterable):
        self.boards = list(boards)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for board in self.boards:
            url = f"https://boards-api.greenhouse.io/v1/boards/{board.board_token}/jobs?content=true"
            try:
                payload = fetch_json(url)
            except Exception:
                continue
            for item in payload.get("jobs", []):
                external_id = str(item.get("id", ""))
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="greenhouse",
                        external_id=f"{board.board_token}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "board_company": board.company,
                            "board_token": board.board_token,
                            "job": item,
                        },
                    )
                )
        return jobs


class LeverSource(SourceAdapter):
    name = "lever"

    def __init__(self, boards: Iterable):
        self.boards = list(boards)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for board in self.boards:
            url = f"https://api.lever.co/v0/postings/{board.board_token}?mode=json"
            try:
                payload = fetch_json(url)
            except Exception:
                continue
            for item in payload:
                external_id = str(item.get("id", ""))
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="lever",
                        external_id=f"{board.board_token}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "board_company": board.company,
                            "board_token": board.board_token,
                            "job": item,
                        },
                    )
                )
        return jobs


class AshbySource(SourceAdapter):
    name = "ashby"

    def __init__(self, boards: Iterable):
        self.boards = list(boards)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for board in self.boards:
            params = urlencode({"includeCompensation": "true"})
            url = f"https://api.ashbyhq.com/posting-api/job-board/{board.board_token}?{params}"
            try:
                payload = fetch_json(url)
            except Exception:
                continue
            for item in payload.get("jobs", []):
                external_id = str(item.get("id", ""))
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="ashby",
                        external_id=f"{board.board_token}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "board_company": board.company,
                            "board_token": board.board_token,
                            "job": item,
                        },
                    )
                )
        return jobs


class WorkdaySource(SourceAdapter):
    name = "workday"

    def __init__(self, companies: Iterable[WorldwideCompanyRecord]):
        self.companies = [company for company in companies if company.ingestion_strategy == "workday_api"]

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for company in self.companies:
            jobs.extend(_fetch_workday_company_jobs(company))
        return jobs


class CareerPageSource(SourceAdapter):
    name = "career_page"

    def __init__(self, pages: Iterable):
        self.pages = list(pages)

    def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in self.pages:
            try:
                listing_html = fetch_html(page.url)
            except Exception:
                continue
            job_links = _extract_job_links(listing_html, page.url, page.job_link_pattern)
            for job_url in job_links:
                try:
                    job_html = fetch_html(job_url)
                except Exception:
                    continue
                external_id = _job_external_id(job_url)
                jobs.append(
                    RawJob(
                        discovery_source=self.name,
                        source_tier="ats",
                        board_type="custom_page",
                        external_id=f"{page.company}:{external_id}",
                        fetched_at=datetime.now(timezone.utc),
                        payload={
                            "company": page.company,
                            "listing_url": page.url,
                            "job": {
                                "title": _extract_page_title(job_html),
                                "job_url": job_url,
                                "apply_url": job_url,
                                "posted_at": _extract_page_date(job_html),
                                "location": page.default_location,
                                "workplace_type": page.default_workplace_type,
                                "description": _clean_html_text(job_html),
                                "summary": _extract_page_summary(job_html),
                            },
                        },
                    )
                )
        return jobs


class GenericCareerSource(SourceAdapter):
    name = "generic_careers"

    def __init__(self, companies: Iterable[WorldwideCompanyRecord], *, max_workers: int = 8):
        self.companies = [company for company in companies if company.ingestion_strategy == "generic_careers"]
        self.max_workers = max_workers

    def fetch(self) -> list[RawJob]:
        if not self.companies:
            return []
        worker_count = max(1, min(self.max_workers, len(self.companies)))
        jobs: list[RawJob] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for company_jobs in executor.map(_fetch_generic_company_jobs, self.companies):
                jobs.extend(company_jobs)
        return jobs


class SerpApiSource(SourceAdapter):
    name = "serpapi"

    def __init__(self, serpapi_config):
        self.config = serpapi_config
        self.api_key = os.getenv("SERPAPI_API_KEY", "")

    def fetch(self) -> list[RawJob]:
        if not self.config or not self.api_key:
            return []
        jobs: list[RawJob] = []
        for query in self.config.queries:
            next_page_token: str | None = None
            for page in range(self.config.pages_per_query):
                query_params = {
                    "engine": self.config.engine,
                    "q": query,
                    "api_key": self.api_key,
                }
                if next_page_token:
                    query_params["next_page_token"] = next_page_token
                params = urlencode(query_params)
                url = f"https://serpapi.com/search.json?{params}"
                try:
                    payload = fetch_json_plain(url)
                except Exception:
                    continue
                for item in payload.get("jobs_results", []):
                    external_id = str(item.get("job_id", item.get("id", "")))
                    jobs.append(
                        RawJob(
                            discovery_source=self.name,
                            source_tier="aggregator",
                            board_type="serpapi",
                            external_id=f"{query}:{external_id}",
                            fetched_at=datetime.now(timezone.utc),
                            payload={
                                "query": query,
                                "job": item,
                            },
                        )
                    )
                next_page_token = payload.get("serpapi_pagination", {}).get("next_page_token")
                if not next_page_token:
                    break
        return jobs


class RemotiveSource(SourceAdapter):
    name = "remotive"

    def __init__(self, remote_api_config):
        self.config = remote_api_config

    def fetch(self) -> list[RawJob]:
        if not self.config:
            return []
        try:
            payload = fetch_json("https://remotive.com/api/remote-jobs")
        except Exception:
            return []
        jobs: list[RawJob] = []
        for item in payload.get("jobs", [])[: self.config.limit]:
            external_id = str(item.get("id", ""))
            jobs.append(
                RawJob(
                    discovery_source=self.name,
                    source_tier="remote_api",
                    board_type="remotive",
                    external_id=external_id,
                    fetched_at=datetime.now(timezone.utc),
                    payload={"job": item},
                )
            )
        return jobs


def _extract_job_links(html_text: str, base_url: str, job_link_pattern: str) -> list[str]:
    links: list[str] = []
    for href in re.findall(r"""href=["']([^"']+)["']""", html_text, flags=re.IGNORECASE):
        absolute_url = urljoin(base_url, html.unescape(href))
        if absolute_url == base_url:
            continue
        if not re.search(job_link_pattern, absolute_url):
            continue
        links.append(absolute_url)
    return list(dict.fromkeys(links))


def _extract_page_title(html_text: str) -> str:
    for pattern in [r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>"]:
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = _clean_html_text(match.group(1))
            if title:
                return title.removeprefix("Open Position: ").strip()
    return "Unknown title"


def _extract_page_summary(html_text: str) -> str:
    match = re.search(
        r"""<meta\s+name=["']description["']\s+content=["'](.*?)["']""",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return _clean_html_text(match.group(1))
    return _clean_html_text(html_text)[:220]


def _extract_page_date(html_text: str) -> str:
    patterns = [
        r"""<time[^>]+datetime=["']([^"']+)["']""",
        r'"datePosted"\s*:\s*"([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _clean_html_text(value: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    no_html = re.sub(r"<[^>]+>", " ", html.unescape(without_styles))
    return re.sub(r"\s+", " ", no_html).strip()


def _job_external_id(job_url: str) -> str:
    parts = urlsplit(job_url)
    slug = parts.path.rstrip("/").split("/")[-1]
    return slug or job_url


def build_sources(
    watchlist: WatchlistConfig,
    worldwide_companies: list[WorldwideCompanyRecord] | None = None,
) -> list[SourceAdapter]:
    greenhouse_boards = list(watchlist.greenhouse_boards)
    lever_boards = list(watchlist.lever_boards)
    ashby_boards = list(watchlist.ashby_boards)
    custom_pages = list(watchlist.custom_pages)
    workday_companies: list[WorldwideCompanyRecord] = []
    generic_companies: list[WorldwideCompanyRecord] = []
    if worldwide_companies:
        registry_greenhouse, registry_lever, registry_ashby, registry_custom_pages = build_registry_source_configs(
            worldwide_companies
        )
        greenhouse_boards = _dedupe_boards(greenhouse_boards + registry_greenhouse)
        lever_boards = _dedupe_boards(lever_boards + registry_lever)
        ashby_boards = _dedupe_boards(ashby_boards + registry_ashby)
        custom_pages = _dedupe_custom_pages(custom_pages + registry_custom_pages)
        workday_companies = [company for company in worldwide_companies if company.ingestion_strategy == "workday_api"]
        generic_companies = [company for company in worldwide_companies if company.ingestion_strategy == "generic_careers"]
    return [
        GreenhouseSource(greenhouse_boards),
        LeverSource(lever_boards),
        AshbySource(ashby_boards),
        WorkdaySource(workday_companies),
        CareerPageSource(custom_pages),
        GenericCareerSource(generic_companies),
        SerpApiSource(watchlist.serpapi),
        RemotiveSource(watchlist.remote_api),
    ]


def _dedupe_boards(boards: list[BoardConfig]) -> list[BoardConfig]:
    seen_tokens: set[str] = set()
    deduped: list[BoardConfig] = []
    for board in boards:
        if board.board_token in seen_tokens:
            continue
        deduped.append(board)
        seen_tokens.add(board.board_token)
    return deduped


def _dedupe_custom_pages(pages: list[CustomPageConfig]) -> list[CustomPageConfig]:
    seen_urls: set[str] = set()
    deduped: list[CustomPageConfig] = []
    for page in pages:
        if page.url in seen_urls:
            continue
        deduped.append(page)
        seen_urls.add(page.url)
    return deduped


def _fetch_workday_company_jobs(company: WorldwideCompanyRecord) -> list[RawJob]:
    endpoint = _workday_jobs_endpoint(company.source_url)
    if not endpoint:
        return []
    jobs: list[RawJob] = []
    offset = 0
    limit = 20
    total = limit
    while offset < total and offset < 200:
        try:
            payload = fetch_json_post(endpoint, {"limit": limit, "offset": offset, "searchText": ""}, timeout=15)
        except Exception:
            break
        if not isinstance(payload, dict):
            break
        postings = payload.get("jobPostings", [])
        if not postings:
            break
        total = int(payload.get("total", len(postings)))
        for posting in postings:
            external_path = posting.get("externalPath", "")
            if not external_path:
                continue
            job_url = _build_workday_job_url(company.source_url, external_path)
            try:
                job_html = fetch_html(job_url, timeout=15)
            except Exception:
                continue
            description = _clean_html_text(job_html)
            location = posting.get("locationsText", "")
            remote_type = posting.get("remoteType", "")
            jobs.append(
                RawJob(
                    discovery_source="workday",
                    source_tier="ats",
                    board_type="custom_page",
                    external_id=f"{company.slug}:{_job_external_id(job_url)}",
                    fetched_at=datetime.now(timezone.utc),
                    payload={
                        "company": company.company,
                        "listing_url": company.source_url,
                        "job": {
                            "title": posting.get("title", "") or _extract_page_title(job_html),
                            "job_url": job_url,
                            "apply_url": job_url,
                            "posted_at": _extract_page_date(job_html) or _workday_relative_date(posting.get("postedOn", "")),
                            "location": location,
                            "workplace_type": "remote" if "remote" in f"{location} {remote_type}".lower() else "unknown",
                            "description": description,
                            "summary": _extract_page_summary(job_html),
                        },
                    },
                )
            )
        offset += len(postings)
    return jobs


def _fetch_generic_company_jobs(company: WorldwideCompanyRecord) -> list[RawJob]:
    jobs: list[RawJob] = []
    for job_url, job_html in _discover_generic_job_pages(company.source_url):
        description = _clean_html_text(job_html)
        jobs.append(
            RawJob(
                discovery_source="generic_careers",
                source_tier="ats",
                board_type="custom_page",
                external_id=f"{company.slug}:{_job_external_id(job_url)}",
                fetched_at=datetime.now(timezone.utc),
                payload={
                    "company": company.company,
                    "listing_url": company.source_url,
                    "job": {
                        "title": _extract_page_title(job_html),
                        "job_url": job_url,
                        "apply_url": job_url,
                        "posted_at": _extract_page_date(job_html),
                        "location": "Remote",
                        "workplace_type": "remote",
                        "description": description,
                        "summary": _extract_page_summary(job_html),
                    },
                },
            )
        )
    return jobs


def _workday_jobs_endpoint(source_url: str) -> str:
    parsed = urlsplit(source_url)
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if len(path_segments) != 1:
        return ""
    tenant = parsed.netloc.split(".")[0]
    site = path_segments[0]
    return f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"


def _build_workday_job_url(source_url: str, external_path: str) -> str:
    parsed = urlsplit(source_url)
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    if not path_segments:
        return urljoin(source_url, external_path)
    site = path_segments[0]
    normalized_external_path = external_path if external_path.startswith("/") else f"/{external_path}"
    return f"{parsed.scheme}://{parsed.netloc}/{site}{normalized_external_path}"


def _workday_relative_date(value: str) -> str:
    lowered = value.lower().strip()
    today = date.today()
    if lowered == "posted today":
        return today.isoformat()
    if lowered == "posted yesterday":
        return (today - timedelta(days=1)).isoformat()
    match = re.search(r"posted\s+(\d+)\s+days?\s+ago", lowered)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    return ""


def _discover_generic_job_pages(start_url: str) -> list[tuple[str, str]]:
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    visited_pages: set[str] = set()
    seen_candidates: set[str] = set()
    job_pages: list[tuple[str, str]] = []
    max_listing_pages = 3
    max_job_pages = 8
    while queue and len(visited_pages) < max_listing_pages and len(job_pages) < max_job_pages:
        current_url, depth = queue.popleft()
        normalized_current = _normalize_generic_url(current_url)
        if not normalized_current or normalized_current in visited_pages:
            continue
        try:
            html_text, resolved_url = fetch_html_details(current_url, timeout=10)
        except Exception:
            continue
        normalized_resolved = _normalize_generic_url(resolved_url)
        if not normalized_resolved or normalized_resolved in visited_pages:
            continue
        visited_pages.add(normalized_resolved)
        if resolved_url != start_url and _looks_like_job_detail_page(html_text, resolved_url):
            job_pages.append((resolved_url, html_text))
            continue
        candidate_links = _extract_generic_candidate_links(html_text, resolved_url)
        for candidate_url in candidate_links:
            normalized_candidate = _normalize_generic_url(candidate_url)
            if not normalized_candidate or normalized_candidate in seen_candidates or normalized_candidate in visited_pages:
                continue
            seen_candidates.add(normalized_candidate)
            try:
                candidate_html, candidate_resolved_url = fetch_html_details(candidate_url, timeout=10)
            except Exception:
                continue
            normalized_resolved_candidate = _normalize_generic_url(candidate_resolved_url)
            if not normalized_resolved_candidate:
                continue
            if _looks_like_job_detail_page(candidate_html, candidate_resolved_url):
                job_pages.append((candidate_resolved_url, candidate_html))
            elif depth < 1 and (
                _looks_like_listing_page(candidate_resolved_url)
                or bool(_extract_generic_candidate_links(candidate_html, candidate_resolved_url))
            ):
                queue.append((candidate_resolved_url, depth + 1))
            if len(job_pages) >= max_job_pages:
                break
    deduped: list[tuple[str, str]] = []
    seen_job_urls: set[str] = set()
    for job_url, job_html in job_pages:
        normalized_job_url = _normalize_generic_url(job_url)
        if not normalized_job_url or normalized_job_url in seen_job_urls:
            continue
        deduped.append((job_url, job_html))
        seen_job_urls.add(normalized_job_url)
    return deduped


def _extract_generic_candidate_links(html_text: str, base_url: str) -> list[str]:
    scored_links: list[tuple[int, str]] = []
    normalized_base = _normalize_generic_url(base_url)
    for href in re.findall(r"""href=["']([^"']+)["']""", html_text, flags=re.IGNORECASE):
        absolute_url = _normalize_generic_url(urljoin(base_url, html.unescape(href)))
        if not absolute_url or absolute_url == normalized_base:
            continue
        if not _same_domain_family(absolute_url, base_url):
            continue
        if not _has_generic_job_signal(absolute_url):
            continue
        if _is_excluded_generic_link(absolute_url):
            continue
        score = _generic_link_score(absolute_url)
        if score <= 0:
            continue
        scored_links.append((score, absolute_url))
    scored_links.sort(key=lambda item: (item[0], item[1]), reverse=True)
    links: list[str] = []
    seen_links: set[str] = set()
    for _, link in scored_links:
        if link in seen_links:
            continue
        links.append(link)
        seen_links.add(link)
        if len(links) >= 12:
            break
    return links


def _normalize_generic_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    cleaned_query = parsed.query if any(token in parsed.query.lower() for token in ["job", "gh_jid", "gh_src", "lever"]) else ""
    cleaned = parsed._replace(query=cleaned_query, fragment="")
    return urlunsplit(cleaned)


def _same_domain_family(left_url: str, right_url: str) -> bool:
    left_host = urlsplit(left_url).netloc.lower()
    right_host = urlsplit(right_url).netloc.lower()
    return (
        left_host == right_host
        or left_host.endswith(f".{right_host}")
        or right_host.endswith(f".{left_host}")
    )


def _has_generic_job_signal(url: str) -> bool:
    lowered = url.lower()
    return bool(re.search(r"(jobs?|careers?|positions?|openings?|vacanc|work-with-us|open-roles|join|apply)", lowered))


def _is_excluded_generic_link(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in [
            "mailto:",
            "tel:",
            "/privacy",
            "/terms",
            "/benefits",
            "/locations",
            "/news",
            "/blog/",
            "/category/",
            "/tag/",
            "linkedin.com",
            "facebook.com",
            "twitter.com",
            "instagram.com",
        ]
    )


def _generic_link_score(url: str) -> int:
    parsed = urlsplit(url)
    path = parsed.path.lower().rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    last = segments[-1] if segments else ""
    listing_tokens = {
        "jobs",
        "careers",
        "positions",
        "openings",
        "open-positions",
        "open-roles",
        "all",
        "engineering",
        "product",
        "design",
        "sales",
        "marketing",
        "support",
        "department",
    }
    score = 0
    if "/job/" in path or path.endswith("/jobs") or "/jobs/" in path:
        score += 4
    if any(token in path for token in ["/careers/", "/positions/", "/openings/", "/open-positions/", "/open-roles/"]):
        score += 3
    if len(last) >= 12 and last not in listing_tokens:
        score += 2
    if any(char.isdigit() for char in last):
        score += 2
    if parsed.netloc.lower().startswith(("jobs.", "careers.")):
        score += 1
    if last in listing_tokens:
        score -= 3
    return score


def _looks_like_listing_page(url: str) -> bool:
    path = urlsplit(url).path.lower().rstrip("/")
    if not path:
        return True
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return True
    last = segments[-1]
    return last in {
        "jobs",
        "careers",
        "positions",
        "openings",
        "open-positions",
        "open-roles",
        "all",
        "engineering",
        "product",
        "design",
        "sales",
        "support",
        "departments",
    }


def _looks_like_job_detail_page(html_text: str, url: str) -> bool:
    lowered_html = html_text.lower()
    if "jobposting" in lowered_html:
        return True
    title = _extract_page_title(html_text).strip().lower()
    if not title:
        return False
    generic_title_fragments = [
        "careers",
        "jobs",
        "open positions",
        "work with us",
        "join our team",
        "all vacancies",
        "job openings",
        "ready for a new challenge",
    ]
    if any(fragment in title for fragment in generic_title_fragments) and len(title) < 80:
        return False
    text = _clean_html_text(html_text)
    if len(text) < 700:
        return False
    detail_keywords = [
        "responsibilities",
        "requirements",
        "qualifications",
        "what you'll do",
        "what you will do",
        "job description",
        "about the role",
        "about you",
        "apply for this job",
        "full time",
    ]
    hits = sum(1 for keyword in detail_keywords if keyword in lowered_html)
    if hits >= 2:
        return True
    path = urlsplit(url).path.lower()
    return bool(re.search(r"/(job|jobs|careers?|positions?|openings?)/[^/]{10,}", path)) and len(text) > 1200
