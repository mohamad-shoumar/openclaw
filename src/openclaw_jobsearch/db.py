from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import JobRecord, RunSummary


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
            normalized_url_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
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
    connection.execute(
        """
        INSERT INTO jobs (
            job_slug, run_id, discovery_source, source_tier, board_type, external_id,
            company, title, job_url, apply_url, posted_at, location_raw, workplace_type,
            remote_scope, lebanon_eligibility, seniority_title, salary_confidence,
            validation_status, normalized_url_key, content_hash, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
