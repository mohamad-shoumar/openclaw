from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SourceTier = Literal["ats", "aggregator", "remote_api"]
BoardType = Literal["greenhouse", "lever", "ashby", "serpapi", "remotive", "unknown"]
ValidationStatus = Literal["accepted", "rejected"]
RemoteScope = Literal["global", "restricted", "unknown"]


class ProfileConfig(BaseModel):
    candidate_name: str
    location: str
    experience_years: int
    resume_path: str
    resume_text_path: str
    headline: str
    target_roles: list[str]
    required_skills: list[str]
    preferred_skills: list[str] = Field(default_factory=list)
    excluded_role_keywords: list[str] = Field(default_factory=list)


class RulesConfig(BaseModel):
    strict_mode: bool = True
    max_job_age_days: int
    priority_job_age_days: int
    max_required_experience_years: int
    allowed_remote_scopes: list[str]
    rejected_title_keywords: list[str]
    required_skill_keywords: list[str]
    preferred_skill_keywords: list[str] = Field(default_factory=list)
    rejected_primary_stack_keywords: list[str] = Field(default_factory=list)
    remote_restriction_patterns: list[str] = Field(default_factory=list)
    hybrid_patterns: list[str] = Field(default_factory=list)
    global_remote_patterns: list[str] = Field(default_factory=list)
    lebanon_block_patterns: list[str] = Field(default_factory=list)


class BoardConfig(BaseModel):
    company: str
    board_token: str


class SerpApiConfig(BaseModel):
    engine: str = "google_jobs"
    queries: list[str]
    pages_per_query: int = 1


class RemoteApiConfig(BaseModel):
    provider: str = "remotive"
    limit: int = 100


class WatchlistConfig(BaseModel):
    greenhouse_boards: list[BoardConfig] = Field(default_factory=list)
    lever_boards: list[BoardConfig] = Field(default_factory=list)
    ashby_boards: list[BoardConfig] = Field(default_factory=list)
    serpapi: SerpApiConfig | None = None
    remote_api: RemoteApiConfig | None = None


class EvidenceSnippet(BaseModel):
    field: str
    snippet: str


class RawJob(BaseModel):
    discovery_source: str
    source_tier: SourceTier
    board_type: BoardType
    external_id: str
    fetched_at: datetime
    payload: dict[str, Any]


class JobRecord(BaseModel):
    run_id: str
    job_slug: str
    discovery_source: str
    source_tier: SourceTier
    board_type: BoardType
    external_id: str
    company: str
    title: str
    job_url: str
    apply_url: str
    posted_at: date | None = None
    location_raw: str = ""
    workplace_type: str = ""
    remote_scope: RemoteScope = "unknown"
    country_restrictions: list[str] = Field(default_factory=list)
    timezone_restrictions: list[str] = Field(default_factory=list)
    lebanon_eligibility: Literal["eligible", "ineligible", "unknown"] = "unknown"
    experience_required_min: int | None = None
    experience_required_max: int | None = None
    seniority_title: str = "unknown"
    required_tech: list[str] = Field(default_factory=list)
    preferred_tech: list[str] = Field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_confidence: Literal["confirmed", "estimated", "not found"] = "not found"
    summary: str = ""
    description_text: str = ""
    validation_status: ValidationStatus = "rejected"
    rejection_reasons: list[str] = Field(default_factory=list)
    evidence_snippets: list[EvidenceSnippet] = Field(default_factory=list)
    normalized_url_key: str = ""
    content_hash: str = ""
    source_payload: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime
    total_raw_jobs: int
    total_unique_jobs: int
    accepted_jobs: int
    rejected_jobs: int
    source_counts: dict[str, int]
    top_rejection_reasons: list[tuple[str, int]]
