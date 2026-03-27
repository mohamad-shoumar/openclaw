from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import BoardConfig, CustomPageConfig


DEFAULT_HEADERS = {
    "User-Agent": "openclaw-jobsearch/0.1 (+https://openclaw.local)"
}


@dataclass(frozen=True)
class WorldwideCompanyRecord:
    slug: str
    company: str
    company_key: str
    source_path: str
    careers_url: str = ""
    website_url: str = ""
    source_url: str = ""
    region: str = ""
    remote_policy: str = ""
    worldwide_evidence: str = ""
    host_type: str = "unknown"
    ingestion_strategy: str = "skip"
    board_token: str = ""
    job_link_pattern: str = ""


@dataclass(frozen=True)
class RegistryAuditRecord:
    slug: str
    company: str
    source_url: str
    host_type: str
    ingestion_strategy: str
    board_token: str
    reachable: bool
    http_status: int | None
    resolved_url: str
    error: str = ""


def normalize_company_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def load_worldwide_company_registry(path: Path) -> list[WorldwideCompanyRecord]:
    if not path.exists():
        return []
    records: list[WorldwideCompanyRecord] = []
    current_slug = ""
    current_lines: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^\s*src/companies/([a-z0-9-]+)\.md:\s*$", line)
        if match:
            if current_slug:
                record = _parse_company_block(current_slug, current_lines)
                if record:
                    records.append(record)
            current_slug = match.group(1)
            current_lines = []
            continue
        if current_slug:
            current_lines.append(line)
    if current_slug:
        record = _parse_company_block(current_slug, current_lines)
        if record:
            records.append(record)
    return records


def build_registry_source_configs(
    companies: list[WorldwideCompanyRecord],
) -> tuple[list[BoardConfig], list[BoardConfig], list[BoardConfig], list[CustomPageConfig]]:
    greenhouse: list[BoardConfig] = []
    lever: list[BoardConfig] = []
    ashby: list[BoardConfig] = []
    custom_pages: list[CustomPageConfig] = []
    seen_board_tokens: set[tuple[str, str]] = set()
    seen_custom_urls: set[str] = set()
    for company in companies:
        if company.ingestion_strategy == "greenhouse_board" and company.board_token:
            key = ("greenhouse", company.board_token)
            if key not in seen_board_tokens:
                greenhouse.append(BoardConfig(company=company.company, board_token=company.board_token))
                seen_board_tokens.add(key)
            continue
        if company.ingestion_strategy == "lever_board" and company.board_token:
            key = ("lever", company.board_token)
            if key not in seen_board_tokens:
                lever.append(BoardConfig(company=company.company, board_token=company.board_token))
                seen_board_tokens.add(key)
            continue
        if company.ingestion_strategy == "ashby_board" and company.board_token:
            key = ("ashby", company.board_token)
            if key not in seen_board_tokens:
                ashby.append(BoardConfig(company=company.company, board_token=company.board_token))
                seen_board_tokens.add(key)
            continue
        if company.ingestion_strategy != "custom_page" or not company.source_url or not company.job_link_pattern:
            continue
        if company.source_url in seen_custom_urls:
            continue
        custom_pages.append(
            CustomPageConfig(
                company=company.company,
                url=company.source_url,
                job_link_pattern=company.job_link_pattern,
                default_location="Remote",
                default_workplace_type="remote",
            )
        )
        seen_custom_urls.add(company.source_url)
    return greenhouse, lever, ashby, custom_pages


def audit_worldwide_company_registry(
    companies: list[WorldwideCompanyRecord],
    output_dir: Path,
    *,
    limit: int | None = None,
    max_workers: int = 16,
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    target_companies = [company for company in companies[:limit] if company.source_url]
    worker_count = max(1, min(max_workers, len(target_companies) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        records = list(executor.map(_audit_record, target_companies))
    json_path = output_dir / "worldwide_registry_audit_latest.json"
    csv_path = output_dir / "worldwide_registry_audit_latest.csv"
    json_path.write_text(json.dumps([asdict(record) for record in records], indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "slug",
                "company",
                "source_url",
                "host_type",
                "ingestion_strategy",
                "board_token",
                "reachable",
                "http_status",
                "resolved_url",
                "error",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))
    summary = {
        "audited_companies": len(records),
        "reachable_companies": sum(1 for record in records if record.reachable),
        "unreachable_companies": sum(1 for record in records if not record.reachable),
        "auto_ingest_companies": sum(
            1
            for record in records
            if record.ingestion_strategy
            in {"greenhouse_board", "lever_board", "ashby_board", "custom_page", "workday_api", "generic_careers"}
        ),
        "custom_page_companies": sum(1 for record in records if record.ingestion_strategy == "custom_page"),
        "generic_careers_companies": sum(1 for record in records if record.ingestion_strategy == "generic_careers"),
        "workday_companies": sum(1 for record in records if record.ingestion_strategy == "workday_api"),
        "native_board_companies": sum(
            1
            for record in records
            if record.ingestion_strategy in {"greenhouse_board", "lever_board", "ashby_board"}
        ),
        "manual_or_needs_adapter_companies": sum(
            1
            for record in records
            if record.ingestion_strategy in {"manual_only", "needs_adapter", "skip"}
        ),
    }
    (output_dir / "worldwide_registry_audit_summary_latest.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_company_block(slug: str, lines: list[str]) -> WorldwideCompanyRecord | None:
    careers_url = ""
    website_url = ""
    region = ""
    remote_policy = ""
    evidence_lines: list[str] = []
    for raw_line in lines:
        line = _clean_dump_line(raw_line)
        if not line:
            continue
        if line.startswith("careers_url:"):
            careers_url = _extract_first_url(line.partition(":")[2].strip())
            continue
        if line.startswith("website:"):
            website_url = _extract_first_url(line.partition(":")[2].strip())
            continue
        if line.startswith("region:"):
            region = line.partition(":")[2].strip().lower()
            continue
        if line.startswith("remote_policy:"):
            remote_policy = line.partition(":")[2].strip().lower()
            continue
        if _looks_like_evidence(line):
            evidence_lines.append(line)
    evidence = next((line for line in evidence_lines if "worldwide" in line.lower() or "global" in line.lower()), "")
    company = _humanize_slug(slug)
    source_url = careers_url or website_url
    host_type = _classify_host_type(source_url)
    board_token = _extract_board_token(source_url, host_type)
    job_link_pattern = _derive_job_link_pattern(source_url, host_type)
    ingestion_strategy = _derive_ingestion_strategy(host_type, source_url, board_token, job_link_pattern)
    return WorldwideCompanyRecord(
        slug=slug,
        company=company,
        company_key=normalize_company_name(company),
        source_path=f"src/companies/{slug}.md",
        careers_url=careers_url,
        website_url=website_url,
        source_url=source_url,
        region=region,
        remote_policy=remote_policy,
        worldwide_evidence=evidence,
        host_type=host_type,
        ingestion_strategy=ingestion_strategy,
        board_token=board_token,
        job_link_pattern=job_link_pattern,
    )


def _clean_dump_line(line: str) -> str:
    line = re.sub(r"^L\d+:", "", line)
    line = re.sub(r"^\s*\d+\s*:?\s*", "", line)
    return line.strip()


def _looks_like_evidence(line: str) -> bool:
    lowered = line.lower()
    if lowered.startswith("src/"):
        return False
    if lowered.startswith(("careers_url:", "website:", "region:", "remote_policy:")):
        return False
    return bool(line)


def _extract_first_url(value: str) -> str:
    matches = re.findall(r"https?://[^\s)>\]]+", value)
    if not matches:
        return ""
    return matches[-1].rstrip(".,")


def _humanize_slug(slug: str) -> str:
    parts = slug.replace("_", "-").split("-")
    transformed = []
    upper_tokens = {"ai", "api", "aws", "db", "io", "qa", "ui", "ux"}
    for part in parts:
        if not part:
            continue
        if part in upper_tokens:
            transformed.append(part.upper())
        elif part.isdigit() or any(char.isdigit() for char in part):
            transformed.append(part.upper())
        else:
            transformed.append(part.capitalize())
    return " ".join(transformed)


def _classify_host_type(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if "boards.greenhouse.io" in host:
        return "greenhouse"
    if "jobs.lever.co" in host:
        return "lever"
    if "jobs.ashbyhq.com" in host:
        return "ashby"
    if "workable.com" in host:
        return "workable"
    if "bamboohr.com" in host:
        return "bamboohr"
    if "jobvite.com" in host:
        return "jobvite"
    if "breezy.hr" in host:
        return "breezy"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "myworkdayjobs.com" in host:
        return "workday"
    if "linkedin.com" in host:
        return "linkedin"
    if "angel.co" in host or "wellfound.com" in host:
        return "wellfound"
    if host:
        return "custom"
    return "unknown"


def _extract_board_token(url: str, host_type: str) -> str:
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if host_type in {"greenhouse", "lever", "ashby"} and segments:
        return segments[0]
    return ""


def _derive_job_link_pattern(url: str, host_type: str) -> str:
    parsed = urlsplit(url)
    domain = re.escape(f"{parsed.scheme}://{parsed.netloc}")
    base_path = parsed.path.rstrip("/")
    escaped_base_path = re.escape(base_path) if base_path else ""
    if host_type == "workable":
        return rf"^{domain}/[^?#]*/j/[^/?#]+/?(?:\?[^#]*)?$"
    if host_type == "bamboohr":
        return (
            rf"^{domain}/careers/\d+(?:\?[^#]*)?$"
            rf"|^{domain}/jobs/view\.php\?id=\d+(?:[^#]*)?$"
        )
    if host_type == "jobvite":
        return rf"^{domain}/[^?#]*/job/[^?#]+(?:\?[^#]*)?$"
    if host_type == "breezy":
        return rf"^{domain}/p/[^/?#]+(?:\?[^#]*)?$"
    if host_type == "smartrecruiters":
        return rf"^{domain}/[^?#]+/[^?#]+(?:\?[^#]*)?$"
    if host_type == "custom":
        if escaped_base_path:
            return rf"^{domain}{escaped_base_path}/[^?#]+(?:\?[^#]*)?$"
        if parsed.netloc.lower().startswith(("jobs.", "careers.")):
            return rf"^{domain}/[^?#]+(?:/[^?#]+)*(?:\?[^#]*)?$"
        return (
            rf"^{domain}/.*(?:jobs|careers?|positions|openings|open-roles|work-with-us|join)"
            rf".*/[^?#]+(?:\?[^#]*)?$"
        )
    return ""


def _derive_ingestion_strategy(host_type: str, source_url: str, board_token: str, job_link_pattern: str) -> str:
    parsed = urlsplit(source_url)
    if host_type == "greenhouse" and board_token:
        return "greenhouse_board"
    if host_type == "lever" and board_token:
        return "lever_board"
    if host_type == "ashby" and board_token:
        return "ashby_board"
    if host_type in {"workable", "bamboohr", "jobvite", "breezy", "smartrecruiters"} and job_link_pattern:
        return "custom_page"
    if host_type in {"linkedin", "wellfound"}:
        return "manual_only"
    if host_type == "workday":
        return "workday_api"
    if host_type == "custom":
        if _looks_like_generic_careers_url(source_url):
            return "generic_careers"
        if parsed.path in {"", "/"} and not parsed.netloc.lower().startswith(("jobs.", "careers.")):
            return "skip"
        return "needs_adapter"
    return "skip"


def _looks_like_generic_careers_url(source_url: str) -> bool:
    parsed = urlsplit(source_url)
    host = parsed.netloc.lower()
    path = parsed.path.lower().rstrip("/")
    joined = f"{host}{path}"
    if any(token in joined for token in ["notion.site", "sharearticle", "recent-activity", "privacy", "terms"]):
        return False
    if "contact" in path and "careers" not in path and "jobs" not in path:
        return False
    if host.startswith(("jobs.", "careers.")):
        return True
    if path in {
        "/jobs",
        "/careers",
        "/open-positions",
        "/openings",
        "/work-with-us",
        "/join",
        "/join-us",
        "/company/careers",
        "/careers/open-positions",
        "/careers/all-vacancies",
    }:
        return True
    return path.endswith(("/jobs", "/careers", "/open-positions", "/work-with-us", "/openings"))


def _audit_record(company: WorldwideCompanyRecord) -> RegistryAuditRecord:
    try:
        request = Request(company.source_url, headers=DEFAULT_HEADERS)
        with urlopen(request, timeout=10) as response:
            return RegistryAuditRecord(
                slug=company.slug,
                company=company.company,
                source_url=company.source_url,
                host_type=company.host_type,
                ingestion_strategy=company.ingestion_strategy,
                board_token=company.board_token,
                reachable=True,
                http_status=getattr(response, "status", None),
                resolved_url=response.geturl(),
            )
    except HTTPError as error:
        return RegistryAuditRecord(
            slug=company.slug,
            company=company.company,
            source_url=company.source_url,
            host_type=company.host_type,
            ingestion_strategy=company.ingestion_strategy,
            board_token=company.board_token,
            reachable=False,
            http_status=error.code,
            resolved_url=company.source_url,
            error=str(error),
        )
    except URLError as error:
        return RegistryAuditRecord(
            slug=company.slug,
            company=company.company,
            source_url=company.source_url,
            host_type=company.host_type,
            ingestion_strategy=company.ingestion_strategy,
            board_token=company.board_token,
            reachable=False,
            http_status=None,
            resolved_url=company.source_url,
            error=str(error.reason),
        )
    except Exception as error:
        return RegistryAuditRecord(
            slug=company.slug,
            company=company.company,
            source_url=company.source_url,
            host_type=company.host_type,
            ingestion_strategy=company.ingestion_strategy,
            board_token=company.board_token,
            reachable=False,
            http_status=None,
            resolved_url=company.source_url,
            error=str(error),
        )
