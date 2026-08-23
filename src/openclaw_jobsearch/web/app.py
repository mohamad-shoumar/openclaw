"""FastAPI application for the OpenClaw review UI."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..artifact_generation import generate_phase3_artifacts
from ..cli import load_dotenv
from ..config import AppConfig
from ..db import connect, get_job, list_review_jobs, mark_applied, update_review_status
from ..feedback import RULE_KINDS, FeedbackStore
from ..models import JobRecord
from ..pipeline import export_review_outputs
from ..rule_proposal import blast_radius, propose_rules
from .runner import PipelineRunner

REVIEW_STATUSES = ("pending_review", "approved", "rejected", "archived")
REVOCABLE_STATUSES = ("pending_review", "approved")

# Hosts that republish other boards' listings rather than employing anyone. Used
# only to badge a job in the UI - never to filter. Filtering is the human's call.
KNOWN_AGGREGATOR_HOSTS = (
    "learn4good.com", "dailyremote.com", "ziprecruiter.com", "indeed.com",
    "linkedin.com", "glassdoor.com", "simplyhired.com", "jooble.org",
    "talent.com", "jobrapido.com", "neuvoo.com", "upwork.com",
)


# ---------------------------------------------------------------- request models

class ApproveRequest(BaseModel):
    reason: str = ""
    note: str = ""


class ConfirmedRule(BaseModel):
    kind: str
    value: str
    reason: str = ""


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1)
    note: str = ""
    rules: list[ConfirmedRule] = Field(default_factory=list)


class ProposeRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class AppliedRequest(BaseModel):
    applied: bool = True
    notes: str = ""


class RuleCreateRequest(BaseModel):
    kind: str
    value: str
    reason: str = ""


class BlastRequest(BaseModel):
    kind: str
    value: str


# ---------------------------------------------------------------- serialization

def _apply_host(job: JobRecord) -> str:
    from urllib.parse import urlsplit
    for candidate in (job.apply_url, job.job_url):
        if candidate and "://" in candidate:
            host = urlsplit(candidate).netloc.lower()
            return host[4:] if host.startswith("www.") else host
    return ""


def _age_days(job: JobRecord) -> int | None:
    if job.posted_at is None:
        return None
    return (date.today() - job.posted_at).days


def job_summary(job: JobRecord) -> dict[str, Any]:
    host = _apply_host(job)
    return {
        "job_slug": job.job_slug,
        "company": job.company,
        "title": job.title,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "age_days": _age_days(job),
        "location_raw": job.location_raw,
        "remote_scope": job.remote_scope,
        "seniority_title": job.seniority_title,
        "discovery_source": job.discovery_source,
        "source_tier": job.source_tier,
        "apply_host": host,
        "is_aggregator": any(host == h or host.endswith("." + h) for h in KNOWN_AGGREGATOR_HOSTS),
        "review_status": job.review_status,
        "applied_at": job.applied_at.isoformat() if job.applied_at else None,
        "phase3_status": job.phase3_status,
        "required_tech": job.required_tech,
        "preferred_tech": job.preferred_tech,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency,
    }


def job_detail(job: JobRecord) -> dict[str, Any]:
    payload = job_summary(job)
    payload.update(
        {
            "job_url": job.job_url,
            "apply_url": job.apply_url or job.job_url,
            "workplace_type": job.workplace_type,
            "description_text": job.description_text,
            "summary": job.summary,
            "country_restrictions": job.country_restrictions,
            "timezone_restrictions": job.timezone_restrictions,
            "lebanon_eligibility": job.lebanon_eligibility,
            "experience_required_min": job.experience_required_min,
            "experience_required_max": job.experience_required_max,
            "evidence_snippets": [s.model_dump() for s in job.evidence_snippets],
            "rejection_reasons": job.rejection_reasons,
            "review_notes": job.review_notes,
            "approval_reason": job.approval_reason,
            "applied_notes": job.applied_notes,
            "artifact_dir": job.artifact_dir,
            "phase3_error": job.phase3_error,
        }
    )
    return payload


# ---------------------------------------------------------------- app factory

def create_app(workspace_root: Path | None = None) -> FastAPI:
    root = Path(workspace_root or os.environ.get("OPENCLAW_WORKSPACE_ROOT", ".")).resolve()
    config_dir = root / "config"
    data_dir = root / "data"
    output_dir = root / "outputs"
    db_path = data_dir / "jobs.db"

    load_dotenv(root / ".env")

    app = FastAPI(title="OpenClaw Review UI", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    runner = PipelineRunner(root, config_dir, data_dir, output_dir)

    def db():
        return connect(db_path)

    def store() -> FeedbackStore:
        return FeedbackStore.load(config_dir)

    def require_job(connection, slug: str) -> JobRecord:
        job = get_job(connection, slug)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {slug}")
        return job

    def apply_rules_now(feedback: FeedbackStore, connection) -> list[dict[str, str]]:
        """Revoke every queued job matching the current rule set."""
        revoked: list[dict[str, str]] = []
        for status in REVOCABLE_STATUSES:
            for job in list_review_jobs(connection, status=status):
                rule = feedback.match(job)
                if rule is None:
                    continue
                update_review_status(
                    connection, job.job_slug, "rejected",
                    reason=f"feedback:{rule.id} {rule.reason}", decision_by="feedback",
                )
                revoked.append({"job_slug": job.job_slug, "label": f"{job.company} - {job.title}", "rule_id": rule.id})
        return revoked

    # ------------------------------------------------------------ jobs

    @app.get("/api/stats")
    def stats():
        connection = db()
        counts = {row[0]: row[1] for row in connection.execute(
            "SELECT review_status, COUNT(*) FROM jobs GROUP BY 1")}
        applied = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL").fetchone()[0]
        total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        last = connection.execute(
            "SELECT run_id, finished_at, accepted_jobs, total_unique_jobs FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return {
            "total_jobs": total,
            "runs": runs,
            "by_review_status": counts,
            "applied": applied,
            "rules": len(store().rules),
            "last_run": dict(last) if last else None,
        }

    @app.get("/api/jobs")
    def list_jobs(
        status: Literal["pending_review", "approved", "rejected", "archived"] = "pending_review",
        source: str | None = None,
        max_age_days: int | None = None,
        limit: int = 200,
    ):
        connection = db()
        jobs = list_review_jobs(connection, status=status, source=source, max_age_days=max_age_days)
        return {"status": status, "count": len(jobs), "jobs": [job_summary(j) for j in jobs[:limit]]}

    @app.get("/api/jobs/{slug}")
    def read_job(slug: str):
        return job_detail(require_job(db(), slug))

    @app.post("/api/jobs/{slug}/approve")
    def approve(slug: str, body: ApproveRequest):
        connection = db()
        require_job(connection, slug)
        job = update_review_status(
            connection, slug, "approved",
            reason=body.reason, notes=body.note, decision_by="web",
        )
        export_review_outputs(data_dir=data_dir, output_dir=output_dir)
        return job_detail(job)

    @app.post("/api/jobs/{slug}/reject")
    def reject(slug: str, body: RejectRequest):
        connection = db()
        require_job(connection, slug)
        feedback = store()
        added = []
        for rule in body.rules:
            if rule.kind not in RULE_KINDS:
                raise HTTPException(status_code=400, detail=f"Unknown rule kind: {rule.kind}")
            created = feedback.add(rule.kind, rule.value, rule.reason or body.reason, origin_job=slug)
            added.append(created.to_dict())
        if added:
            feedback.save()

        update_review_status(
            connection, slug, "rejected",
            reason=body.reason, notes=body.note, decision_by="web",
        )
        # Rules created here apply retroactively to everything else still queued.
        revoked = apply_rules_now(feedback, connection) if added else []
        export_review_outputs(data_dir=data_dir, output_dir=output_dir)
        return {"job_slug": slug, "rules_added": added, "also_revoked": revoked}

    @app.post("/api/jobs/{slug}/applied")
    def set_applied(slug: str, body: AppliedRequest):
        connection = db()
        require_job(connection, slug)
        job = mark_applied(connection, slug, applied=body.applied, notes=body.notes)
        return job_detail(job)

    @app.post("/api/jobs/{slug}/requeue")
    def requeue(slug: str):
        """Undo a review decision and put the job back in the queue."""
        connection = db()
        require_job(connection, slug)
        job = update_review_status(connection, slug, "pending_review", decision_by="web")
        export_review_outputs(data_dir=data_dir, output_dir=output_dir)
        return job_detail(job)

    # ------------------------------------------------------------ feedback

    @app.get("/api/feedback")
    def list_rules():
        connection = db()
        feedback = store()
        rules = []
        for rule in feedback.rules:
            entry = rule.to_dict()
            entry["hits"] = blast_radius(connection, rule.kind, rule.value)
            entry["description"] = RULE_KINDS.get(rule.kind, "")
            rules.append(entry)
        return {"count": len(rules), "kinds": RULE_KINDS, "rules": rules}

    @app.post("/api/feedback")
    def create_rule(body: RuleCreateRequest):
        if body.kind not in RULE_KINDS:
            raise HTTPException(status_code=400, detail=f"Unknown rule kind: {body.kind}")
        feedback = store()
        try:
            rule = feedback.add(body.kind, body.value, body.reason)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        feedback.save()
        connection = db()
        revoked = apply_rules_now(feedback, connection)
        export_review_outputs(data_dir=data_dir, output_dir=output_dir)
        return {"rule": rule.to_dict(), "also_revoked": revoked}

    @app.delete("/api/feedback/{rule_id}")
    def delete_rule(rule_id: str):
        feedback = store()
        if not feedback.remove(rule_id):
            raise HTTPException(status_code=404, detail=f"Unknown rule: {rule_id}")
        feedback.save()
        return {"removed": rule_id, "remaining": len(feedback.rules)}

    @app.post("/api/feedback/blast")
    def rule_blast(body: BlastRequest):
        return {"kind": body.kind, "value": body.value, "blast_radius": blast_radius(db(), body.kind, body.value)}

    @app.post("/api/jobs/{slug}/propose")
    def propose(slug: str, body: ProposeRequest):
        connection = db()
        job = require_job(connection, slug)
        result = propose_rules(job, body.reason, connection=connection, store=store())
        return result.to_dict()

    # ------------------------------------------------------------ runs

    @app.post("/api/runs")
    def start_run():
        if runner.is_running:
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        return runner.start()

    @app.get("/api/runs/current")
    def current_run():
        return runner.state()

    @app.get("/api/runs")
    def run_history(limit: int = 10):
        rows = db().execute(
            "SELECT run_id, started_at, finished_at, total_unique_jobs, accepted_jobs, rejected_jobs "
            "FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"runs": [dict(r) for r in rows]}

    # ------------------------------------------------------------ artifacts

    @app.post("/api/jobs/{slug}/artifacts")
    def build_artifacts(slug: str):
        connection = db()
        job = require_job(connection, slug)
        if job.review_status != "approved":
            raise HTTPException(status_code=400, detail="Only approved jobs can generate artifacts.")
        try:
            result = generate_phase3_artifacts(
                workspace_root=root, config_dir=config_dir, data_dir=data_dir,
                output_dir=output_dir, job_slug=slug,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        return {"job_slug": slug, "result": payload}

    @app.get("/api/jobs/{slug}/artifacts")
    def read_artifacts(slug: str):
        """Inline markdown for the resume and cover letter, for in-pane preview."""
        job = require_job(db(), slug)
        base = root / job.artifact_dir if job.artifact_dir else None
        files: dict[str, Any] = {}
        if base and base.exists():
            for label, name in (
                ("resume", "resume.md"),
                ("cover_letter", "cover_letter.md"),
                ("job_description", "job_description.md"),
            ):
                path = base / name
                files[label] = path.read_text(encoding="utf-8") if path.exists() else None
            pdfs = sorted(p.name for p in base.glob("*.pdf"))
        else:
            pdfs = []
        return {
            "job_slug": slug,
            "artifact_dir": job.artifact_dir,
            "phase3_status": job.phase3_status,
            "phase3_error": job.phase3_error,
            "files": files,
            "pdfs": pdfs,
        }

    @app.get("/api/health")
    def health():
        return {"ok": True, "workspace_root": str(root), "db": str(db_path), "time": datetime.now(timezone.utc).isoformat()}

    return app


app = create_app()
