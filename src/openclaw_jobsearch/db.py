from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .models import JobRecord, Phase3Status, ReviewStatus, RunSummary


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_slug TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            discovery_source TEXT NOT NULL,
            source_tier TEXT NOT NULL,
            board_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            job_url TEXT NOT NULL,
            apply_url TEXT NOT NULL,
            posted_at TEXT,
            location_raw TEXT,
            workplace_type TEXT,
            remote_scope TEXT,
            lebanon_eligibility TEXT,
            seniority_title TEXT,
            salary_confidence TEXT,
            validation_status TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'not_queued',
            review_decision_at TEXT,
            review_decision_by TEXT,
            review_notes TEXT NOT NULL DEFAULT '',
            approval_reason TEXT NOT NULL DEFAULT '',
            phase3_ready INTEGER NOT NULL DEFAULT 0,
            phase3_status TEXT NOT NULL DEFAULT 'not_started',
            phase3_generated_at TEXT,
            artifact_dir TEXT NOT NULL DEFAULT '',
            job_description_path TEXT NOT NULL DEFAULT '',
            resume_path_generated TEXT NOT NULL DEFAULT '',
            cover_letter_path_generated TEXT NOT NULL DEFAULT '',
            artifact_meta_path TEXT NOT NULL DEFAULT '',
            phase3_error TEXT NOT NULL DEFAULT '',
            normalized_url_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    _ensure_column(connection, "jobs", "review_status", "TEXT NOT NULL DEFAULT 'not_queued'")
    _ensure_column(connection, "jobs", "review_decision_at", "TEXT")
    _ensure_column(connection, "jobs", "review_decision_by", "TEXT")
    _ensure_column(connection, "jobs", "review_notes", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "approval_reason", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "applied_at", "TEXT")
    _ensure_column(connection, "jobs", "applied_notes", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "phase3_ready", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(connection, "jobs", "phase3_status", "TEXT NOT NULL DEFAULT 'not_started'")
    _ensure_column(connection, "jobs", "phase3_generated_at", "TEXT")
    _ensure_column(connection, "jobs", "artifact_dir", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "job_description_path", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "resume_path_generated", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "cover_letter_path_generated", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "artifact_meta_path", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "jobs", "phase3_error", "TEXT NOT NULL DEFAULT ''")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            total_raw_jobs INTEGER NOT NULL,
            total_unique_jobs INTEGER NOT NULL,
            accepted_jobs INTEGER NOT NULL,
            rejected_jobs INTEGER NOT NULL,
            source_counts_json TEXT NOT NULL,
            top_rejection_reasons_json TEXT NOT NULL
        )
        """
    )
    connection.commit()


def upsert_job(connection: sqlite3.Connection, job: JobRecord) -> None:
    existing = get_job(connection, job.job_slug)
    persisted_job = _prepare_job_for_upsert(job, existing)
    _persist_job(connection, persisted_job)


def get_job(connection: sqlite3.Connection, job_slug: str) -> JobRecord | None:
    row = connection.execute(
        "SELECT payload_json FROM jobs WHERE job_slug = ?",
        (job_slug,),
    ).fetchone()
    if row is None:
        return None
    return JobRecord.model_validate_json(row["payload_json"])


def list_review_jobs(
    connection: sqlite3.Connection,
    *,
    status: ReviewStatus | None = None,
    source: str | None = None,
    max_age_days: int | None = None,
) -> list[JobRecord]:
    clauses = ["review_status != ?"]
    params: list[object] = ["not_queued"]
    if status:
        clauses.append("review_status = ?")
        params.append(status)
    if source:
        clauses.append("discovery_source = ?")
        params.append(source)
    query = f"SELECT payload_json FROM jobs WHERE {' AND '.join(clauses)}"
    rows = connection.execute(query, tuple(params)).fetchall()
    jobs = [JobRecord.model_validate_json(row["payload_json"]) for row in rows]
    if max_age_days is not None:
        jobs = [
            job
            for job in jobs
            if job.posted_at is not None and (date.today() - job.posted_at).days <= max_age_days
        ]
    jobs.sort(key=_review_sort_key, reverse=True)
    return jobs


def list_approved_jobs(connection: sqlite3.Connection) -> list[JobRecord]:
    rows = connection.execute(
        "SELECT payload_json FROM jobs WHERE review_status = ?",
        ("approved",),
    ).fetchall()
    jobs = [JobRecord.model_validate_json(row["payload_json"]) for row in rows]
    jobs.sort(key=_review_sort_key, reverse=True)
    return jobs


def list_jobs_ready_for_phase3(
    connection: sqlite3.Connection,
    *,
    job_slug: str | None = None,
) -> list[JobRecord]:
    clauses = ["review_status = ?"]
    params: list[object] = ["approved"]
    if job_slug:
        clauses.append("job_slug = ?")
        params.append(job_slug)
    query = f"SELECT payload_json FROM jobs WHERE {' AND '.join(clauses)}"
    rows = connection.execute(query, tuple(params)).fetchall()
    jobs = [JobRecord.model_validate_json(row["payload_json"]) for row in rows]
    jobs.sort(key=_review_sort_key, reverse=True)
    return jobs


def update_review_status(
    connection: sqlite3.Connection,
    job_slug: str,
    review_status: ReviewStatus,
    *,
    reason: str = "",
    notes: str = "",
    decision_by: str | None = None,
) -> JobRecord:
    job = get_job(connection, job_slug)
    if job is None:
        raise KeyError(f"Unknown job slug: {job_slug}")
    if job.validation_status != "accepted" and job.review_status == "not_queued":
        raise ValueError("Only strict accepted jobs can enter the review queue.")

    job.review_status = review_status
    if review_status == "pending_review":
        job.review_decision_at = None
        job.review_decision_by = None
        job.phase3_ready = False
        if notes:
            job.review_notes = notes
    else:
        job.review_decision_at = datetime.now(timezone.utc)
        job.review_decision_by = decision_by
        if notes:
            job.review_notes = notes
        if reason:
            job.approval_reason = reason
        job.phase3_ready = review_status == "approved"
        if job.phase3_ready and not job.artifact_dir:
            job.artifact_dir = _default_artifact_dir(job.job_slug)

    _persist_job(connection, job)
    return job


def mark_applied(
    connection: sqlite3.Connection,
    job_slug: str,
    *,
    applied: bool = True,
    notes: str = "",
) -> JobRecord:
    """Record that an application was actually submitted, separate from review approval."""
    job = get_job(connection, job_slug)
    if job is None:
        raise KeyError(f"Unknown job slug: {job_slug}")
    job.applied_at = datetime.now(timezone.utc) if applied else None
    if notes or not applied:
        job.applied_notes = notes
    _persist_job(connection, job)
    return job


def update_phase3_artifacts(
    connection: sqlite3.Connection,
    job_slug: str,
    *,
    phase3_status: Phase3Status,
    phase3_generated_at: datetime | None = None,
    job_description_path: str = "",
    resume_path_generated: str = "",
    cover_letter_path_generated: str = "",
    artifact_meta_path: str = "",
    phase3_error: str = "",
    artifact_dir: str | None = None,
) -> JobRecord:
    job = get_job(connection, job_slug)
    if job is None:
        raise KeyError(f"Unknown job slug: {job_slug}")
    if job.review_status != "approved":
        raise ValueError("Phase 3 artifacts can only be generated for approved jobs.")

    job.phase3_status = phase3_status
    job.phase3_generated_at = phase3_generated_at
    job.job_description_path = job_description_path
    job.resume_path_generated = resume_path_generated
    job.cover_letter_path_generated = cover_letter_path_generated
    job.artifact_meta_path = artifact_meta_path
    job.phase3_error = phase3_error
    if artifact_dir is not None:
        job.artifact_dir = artifact_dir
    if phase3_status == "generated":
        job.phase3_ready = True

    _persist_job(connection, job)
    return job


def exportable_review_statuses() -> tuple[ReviewStatus, ...]:
    return ("pending_review", "approved", "rejected", "archived")


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing_columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in existing_columns:
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _prepare_job_for_upsert(job: JobRecord, existing: JobRecord | None) -> JobRecord:
    prepared = job.model_copy(deep=True)
    if existing is not None and existing.review_status in {"approved", "rejected", "archived"}:
        _copy_review_fields(prepared, existing)
    elif existing is not None and existing.review_status == "pending_review" and prepared.validation_status == "accepted":
        _copy_review_fields(prepared, existing)
        prepared.review_status = "pending_review"
        prepared.review_decision_at = None
        prepared.review_decision_by = None
        prepared.phase3_ready = False
    elif prepared.validation_status == "accepted":
        prepared.review_status = "pending_review"
        prepared.review_decision_at = None
        prepared.review_decision_by = None
        prepared.phase3_ready = False
    else:
        prepared.review_status = "not_queued"
        prepared.review_decision_at = None
        prepared.review_decision_by = None
        prepared.review_notes = ""
        prepared.approval_reason = ""
        prepared.phase3_ready = False
        prepared.artifact_dir = ""
    if prepared.review_status == "approved" and not prepared.artifact_dir:
        prepared.artifact_dir = _default_artifact_dir(prepared.job_slug)
    return prepared


def _copy_review_fields(target: JobRecord, source: JobRecord) -> None:
    target.review_status = source.review_status
    target.review_decision_at = source.review_decision_at
    target.review_decision_by = source.review_decision_by
    target.review_notes = source.review_notes
    target.approval_reason = source.approval_reason
    target.applied_at = source.applied_at
    target.applied_notes = source.applied_notes
    target.phase3_ready = source.phase3_ready
    target.phase3_status = source.phase3_status
    target.phase3_generated_at = source.phase3_generated_at
    target.artifact_dir = source.artifact_dir
    target.job_description_path = source.job_description_path
    target.resume_path_generated = source.resume_path_generated
    target.cover_letter_path_generated = source.cover_letter_path_generated
    target.artifact_meta_path = source.artifact_meta_path
    target.phase3_error = source.phase3_error


def _persist_job(connection: sqlite3.Connection, job: JobRecord) -> None:
    connection.execute(
        """
        INSERT INTO jobs (
            job_slug, run_id, discovery_source, source_tier, board_type, external_id,
            company, title, job_url, apply_url, posted_at, location_raw, workplace_type,
            remote_scope, lebanon_eligibility, seniority_title, salary_confidence,
            validation_status, review_status, review_decision_at, review_decision_by,
            review_notes, approval_reason, applied_at, applied_notes,
            phase3_ready, phase3_status, phase3_generated_at,
            artifact_dir, job_description_path, resume_path_generated,
            cover_letter_path_generated, artifact_meta_path, phase3_error,
            normalized_url_key, content_hash, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_slug) DO UPDATE SET
            run_id = excluded.run_id,
            discovery_source = excluded.discovery_source,
            source_tier = excluded.source_tier,
            board_type = excluded.board_type,
            external_id = excluded.external_id,
            company = excluded.company,
            title = excluded.title,
            job_url = excluded.job_url,
            apply_url = excluded.apply_url,
            posted_at = excluded.posted_at,
            location_raw = excluded.location_raw,
            workplace_type = excluded.workplace_type,
            remote_scope = excluded.remote_scope,
            lebanon_eligibility = excluded.lebanon_eligibility,
            seniority_title = excluded.seniority_title,
            salary_confidence = excluded.salary_confidence,
            validation_status = excluded.validation_status,
            review_status = excluded.review_status,
            review_decision_at = excluded.review_decision_at,
            review_decision_by = excluded.review_decision_by,
            review_notes = excluded.review_notes,
            approval_reason = excluded.approval_reason,
            applied_at = excluded.applied_at,
            applied_notes = excluded.applied_notes,
            phase3_ready = excluded.phase3_ready,
            phase3_status = excluded.phase3_status,
            phase3_generated_at = excluded.phase3_generated_at,
            artifact_dir = excluded.artifact_dir,
            job_description_path = excluded.job_description_path,
            resume_path_generated = excluded.resume_path_generated,
            cover_letter_path_generated = excluded.cover_letter_path_generated,
            artifact_meta_path = excluded.artifact_meta_path,
            phase3_error = excluded.phase3_error,
            normalized_url_key = excluded.normalized_url_key,
            content_hash = excluded.content_hash,
            payload_json = excluded.payload_json
        """,
        (
            job.job_slug,
            job.run_id,
            job.discovery_source,
            job.source_tier,
            job.board_type,
            job.external_id,
            job.company,
            job.title,
            job.job_url,
            job.apply_url,
            job.posted_at.isoformat() if job.posted_at else None,
            job.location_raw,
            job.workplace_type,
            job.remote_scope,
            job.lebanon_eligibility,
            job.seniority_title,
            job.salary_confidence,
            job.validation_status,
            job.review_status,
            job.review_decision_at.isoformat() if job.review_decision_at else None,
            job.review_decision_by,
            job.review_notes,
            job.approval_reason,
            job.applied_at.isoformat() if job.applied_at else None,
            job.applied_notes,
            int(job.phase3_ready),
            job.phase3_status,
            job.phase3_generated_at.isoformat() if job.phase3_generated_at else None,
            job.artifact_dir,
            job.job_description_path,
            job.resume_path_generated,
            job.cover_letter_path_generated,
            job.artifact_meta_path,
            job.phase3_error,
            job.normalized_url_key,
            job.content_hash,
            job.model_dump_json(),
        ),
    )
    connection.commit()


def insert_run_summary(connection: sqlite3.Connection, summary: RunSummary) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO runs (
            run_id, started_at, finished_at, total_raw_jobs, total_unique_jobs,
            accepted_jobs, rejected_jobs, source_counts_json, top_rejection_reasons_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary.run_id,
            summary.started_at.isoformat(),
            summary.finished_at.isoformat(),
            summary.total_raw_jobs,
            summary.total_unique_jobs,
            summary.accepted_jobs,
            summary.rejected_jobs,
            json.dumps(summary.source_counts),
            json.dumps(summary.top_rejection_reasons),
        ),
    )
    connection.commit()


def _default_artifact_dir(job_slug: str) -> str:
    return f"artifacts/jobs/{job_slug}"


def _review_sort_key(job: JobRecord) -> tuple[int, int, str]:
    status_order = {
        "pending_review": 4,
        "approved": 3,
        "rejected": 2,
        "archived": 1,
        "not_queued": 0,
    }
    posted_ordinal = job.posted_at.toordinal() if job.posted_at else -1
    return (status_order.get(job.review_status, 0), posted_ordinal, job.job_slug)
