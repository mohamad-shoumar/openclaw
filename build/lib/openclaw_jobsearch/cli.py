from __future__ import annotations

import argparse
import os
from pathlib import Path

from .artifact_generation import generate_phase3_artifacts, regenerate_pdfs_from_html
from .config import AppConfig
from .db import connect, get_job, list_review_jobs, update_review_status
from .pipeline import export_review_outputs, run_pipeline
from .registry import audit_worldwide_company_registry


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def add_common_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace root containing config, data, and outputs directories.",
    )
    parser.add_argument(
        "--config-dir",
        default="config",
        help="Configuration directory relative to the workspace root.",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Data directory relative to the workspace root.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Output directory relative to the workspace root.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenClaw job pipeline and review queue")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the strict pipeline.")
    add_common_path_arguments(run_parser)

    review_parser = subparsers.add_parser("review", help="Review queue commands.")
    review_subparsers = review_parser.add_subparsers(dest="review_command", required=True)

    list_parser = review_subparsers.add_parser("list", help="List jobs in the review queue.")
    add_common_path_arguments(list_parser)
    list_parser.add_argument(
        "--status",
        choices=["pending_review", "approved", "rejected", "archived"],
        help="Filter by review status.",
    )
    list_parser.add_argument("--source", help="Filter by discovery source.")
    list_parser.add_argument("--max-age-days", type=int, help="Only show jobs newer than this age.")
    list_parser.add_argument("--limit", type=int, help="Limit the number of rows shown.")

    show_parser = review_subparsers.add_parser("show", help="Show a single review job.")
    add_common_path_arguments(show_parser)
    show_parser.add_argument("job_slug", help="Job slug to display.")

    approve_parser = review_subparsers.add_parser("approve", help="Approve a review job.")
    add_common_path_arguments(approve_parser)
    approve_parser.add_argument("job_slug", help="Job slug to approve.")
    approve_parser.add_argument("--reason", default="", help="Approval reason.")
    approve_parser.add_argument("--note", default="", help="Additional review note.")
    approve_parser.add_argument("--by", default=None, help="Decision owner.")

    reject_parser = review_subparsers.add_parser("reject", help="Reject a review job.")
    add_common_path_arguments(reject_parser)
    reject_parser.add_argument("job_slug", help="Job slug to reject.")
    reject_parser.add_argument("--reason", required=True, help="Rejection reason.")
    reject_parser.add_argument("--note", default="", help="Additional review note.")
    reject_parser.add_argument("--by", default=None, help="Decision owner.")

    archive_parser = review_subparsers.add_parser("archive", help="Archive a review job.")
    add_common_path_arguments(archive_parser)
    archive_parser.add_argument("job_slug", help="Job slug to archive.")
    archive_parser.add_argument("--note", default="", help="Archive note.")
    archive_parser.add_argument("--by", default=None, help="Decision owner.")

    export_parser = review_subparsers.add_parser("export", help="Regenerate review exports.")
    add_common_path_arguments(export_parser)
    export_parser.add_argument(
        "--status",
        choices=["all", "approved_only"],
        default="all",
        help="Reserved for future export filtering. Current export always writes the full queue and approved contract.",
    )

    phase3_parser = subparsers.add_parser("phase3", help="Phase 3 artifact generation commands.")
    phase3_subparsers = phase3_parser.add_subparsers(dest="phase3_command", required=True)

    phase3_generate_parser = phase3_subparsers.add_parser("generate", help="Generate Phase 3 artifacts.")
    add_common_path_arguments(phase3_generate_parser)
    phase3_generate_parser.add_argument("job_slug", nargs="?", help="Optional approved job slug to generate.")
    phase3_generate_parser.add_argument(
        "--provider",
        choices=["openai", "anthropic"],
        help="LLM provider override.",
    )
    phase3_generate_parser.add_argument(
        "--model",
        help="LLM model override.",
    )
    phase3_generate_parser.add_argument(
        "--temperature",
        type=float,
        help="LLM temperature override.",
    )
    phase3_generate_parser.add_argument(
        "--max-tokens",
        type=int,
        help="LLM max output tokens override.",
    )

    phase3_show_parser = phase3_subparsers.add_parser("show", help="Show Phase 3 status for a job.")
    add_common_path_arguments(phase3_show_parser)
    phase3_show_parser.add_argument("job_slug", help="Job slug to inspect.")

    phase3_regen_pdf_parser = phase3_subparsers.add_parser("regen-pdf", help="Regenerate PDFs from existing HTML artifacts.")
    add_common_path_arguments(phase3_regen_pdf_parser)
    phase3_regen_pdf_parser.add_argument("job_slug", help="Job slug to regenerate PDFs for.")

    phase3_export_parser = phase3_subparsers.add_parser("export", help="Refresh Phase 3 related exports.")
    add_common_path_arguments(phase3_export_parser)

    registry_parser = subparsers.add_parser("registry", help="Worldwide company registry commands.")
    registry_subparsers = registry_parser.add_subparsers(dest="registry_command", required=True)

    registry_audit_parser = registry_subparsers.add_parser("audit", help="Verify worldwide company source links.")
    add_common_path_arguments(registry_audit_parser)
    registry_audit_parser.add_argument("--limit", type=int, help="Optional limit for quick audits.")
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    workspace_root = Path(args.workspace_root).resolve()
    load_dotenv(workspace_root / ".env")
    data_dir = workspace_root / args.data_dir
    output_dir = workspace_root / args.output_dir
    return workspace_root, data_dir, output_dir


def print_review_job(job) -> None:
    print(f"Job slug: {job.job_slug}")
    print(f"Company: {job.company}")
    print(f"Title: {job.title}")
    print(f"Validation status: {job.validation_status}")
    print(f"Review status: {job.review_status}")
    print(f"Posted: {job.posted_at or 'unknown'}")
    print(f"Source: {job.discovery_source} ({job.source_tier})")
    print(f"Location: {job.location_raw or 'unknown'}")
    print(f"Apply URL: {job.apply_url or job.job_url}")
    print(f"Required tech: {', '.join(job.required_tech) or 'none detected'}")
    print(f"Preferred tech: {', '.join(job.preferred_tech) or 'none detected'}")
    print(f"Salary confidence: {job.salary_confidence}")
    print(f"Summary: {job.summary or 'No summary available.'}")
    if job.review_notes:
        print(f"Review notes: {job.review_notes}")
    if job.approval_reason:
        print(f"Decision reason: {job.approval_reason}")
    if job.review_decision_at:
        print(f"Decision at: {job.review_decision_at.isoformat()}")
    if job.artifact_dir:
        print(f"Artifact dir: {job.artifact_dir}")
    if job.rejection_reasons:
        print("Strict rejection reasons:")
        for reason in job.rejection_reasons:
            print(f"- {reason}")
    if job.evidence_snippets:
        print("Evidence:")
        for snippet in job.evidence_snippets:
            print(f"- {snippet.field}: {snippet.snippet}")


def print_phase3_job(job) -> None:
    print(f"Job slug: {job.job_slug}")
    print(f"Company: {job.company}")
    print(f"Title: {job.title}")
    print(f"Review status: {job.review_status}")
    print(f"Phase 3 status: {job.phase3_status}")
    print(f"Artifact dir: {job.artifact_dir or 'not set'}")
    print(f"Job description path: {job.job_description_path or 'not generated'}")
    print(f"Resume path: {job.resume_path_generated or 'not generated'}")
    print(f"Cover letter path: {job.cover_letter_path_generated or 'not generated'}")
    print(f"Artifact metadata path: {job.artifact_meta_path or 'not generated'}")
    if job.phase3_generated_at:
        print(f"Generated at: {job.phase3_generated_at.isoformat()}")
    if job.phase3_error:
        print(f"Phase 3 error: {job.phase3_error}")


def handle_review_list(args: argparse.Namespace, data_dir: Path) -> None:
    connection = connect(data_dir / "jobs.db")
    jobs = list_review_jobs(
        connection,
        status=args.status,
        source=args.source,
        max_age_days=args.max_age_days,
    )
    jobs.sort(
        key=lambda job: (
            job.posted_at.toordinal() if job.posted_at else -1,
            job.job_slug,
        ),
        reverse=True,
    )
    if args.limit:
        jobs = jobs[: args.limit]
    if not jobs:
        print("No review jobs found.")
        return
    for job in jobs:
        posted = job.posted_at.isoformat() if job.posted_at else "unknown"
        print(
            f"[{job.review_status}] {posted} | {job.job_slug} | "
            f"{job.company} - {job.title} | {job.discovery_source}/{job.source_tier}"
        )


def handle_review_show(args: argparse.Namespace, data_dir: Path) -> None:
    connection = connect(data_dir / "jobs.db")
    job = get_job(connection, args.job_slug)
    if job is None or job.review_status == "not_queued":
        raise SystemExit(f"Review job not found: {args.job_slug}")
    print_review_job(job)


def handle_review_update(args: argparse.Namespace, data_dir: Path, output_dir: Path, status: str) -> None:
    connection = connect(data_dir / "jobs.db")
    job = update_review_status(
        connection,
        args.job_slug,
        status,
        reason=getattr(args, "reason", ""),
        notes=getattr(args, "note", ""),
        decision_by=getattr(args, "by", None),
    )
    export_review_outputs(data_dir, output_dir)
    print(f"{job.job_slug}: {job.review_status}")


def handle_review_export(data_dir: Path, output_dir: Path) -> None:
    counts = export_review_outputs(data_dir, output_dir)
    print(
        f"Exported review outputs for {counts['review_jobs']} review jobs "
        f"and {counts['approved_jobs']} approved jobs."
    )


def handle_phase3_generate(
    args: argparse.Namespace,
    workspace_root: Path,
    data_dir: Path,
    output_dir: Path,
) -> None:
    config_dir = workspace_root / args.config_dir
    summary = generate_phase3_artifacts(
        workspace_root=workspace_root,
        config_dir=config_dir,
        data_dir=data_dir,
        output_dir=output_dir,
        job_slug=args.job_slug,
        llm_provider=args.provider,
        llm_model=args.model,
        llm_temperature=args.temperature,
        llm_max_tokens=args.max_tokens,
    )
    export_review_outputs(data_dir, output_dir)
    print(
        f"Generated Phase 3 artifacts for {summary['generated_jobs']} jobs; "
        f"failed for {summary['failed_jobs']} jobs."
    )


def handle_phase3_show(args: argparse.Namespace, data_dir: Path) -> None:
    connection = connect(data_dir / "jobs.db")
    job = get_job(connection, args.job_slug)
    if job is None:
        raise SystemExit(f"Job not found: {args.job_slug}")
    print_phase3_job(job)


def handle_phase3_regen_pdf(args: argparse.Namespace, workspace_root: Path, data_dir: Path) -> None:
    result = regenerate_pdfs_from_html(workspace_root, data_dir, args.job_slug)
    for path in result["regenerated"]:
        print(f"Regenerated: {path}")


def handle_phase3_export(data_dir: Path, output_dir: Path) -> None:
    counts = export_review_outputs(data_dir, output_dir)
    print(
        f"Exported Phase 3 related outputs for {counts['approved_jobs']} approved jobs "
        f"and {counts['review_jobs']} review jobs."
    )


def handle_registry_audit(workspace_root: Path, config_dir: Path, output_dir: Path, limit: int | None) -> None:
    config = AppConfig(workspace_root=workspace_root, config_dir=config_dir)
    summary = audit_worldwide_company_registry(config.worldwide_companies, output_dir, limit=limit)
    print(json_dump(summary))


def json_dump(value: dict[str, int]) -> str:
    import json

    return json.dumps(value, indent=2)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        workspace_root, data_dir, output_dir = resolve_paths(args)
        config_dir = workspace_root / args.config_dir
        summary = run_pipeline(
            workspace_root=workspace_root,
            config_dir=config_dir,
            data_dir=data_dir,
            output_dir=output_dir,
        )
        print(summary.model_dump_json(indent=2))
        return

    workspace_root, data_dir, output_dir = resolve_paths(args)
    if args.command == "review":
        if args.review_command == "list":
            handle_review_list(args, data_dir)
        elif args.review_command == "show":
            handle_review_show(args, data_dir)
        elif args.review_command == "approve":
            handle_review_update(args, data_dir, output_dir, "approved")
        elif args.review_command == "reject":
            handle_review_update(args, data_dir, output_dir, "rejected")
        elif args.review_command == "archive":
            handle_review_update(args, data_dir, output_dir, "archived")
        elif args.review_command == "export":
            handle_review_export(data_dir, output_dir)
        return

    if args.command == "registry":
        config_dir = workspace_root / args.config_dir
        if args.registry_command == "audit":
            handle_registry_audit(workspace_root, config_dir, output_dir, args.limit)
        return

    if args.phase3_command == "generate":
        handle_phase3_generate(args, workspace_root, data_dir, output_dir)
    elif args.phase3_command == "show":
        handle_phase3_show(args, data_dir)
    elif args.phase3_command == "regen-pdf":
        handle_phase3_regen_pdf(args, workspace_root, data_dir)
    elif args.phase3_command == "export":
        handle_phase3_export(data_dir, output_dir)


if __name__ == "__main__":
    main()
