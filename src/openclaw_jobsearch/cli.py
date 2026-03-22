from __future__ import annotations

import argparse
import os
from pathlib import Path

from .db import connect, get_job, list_review_jobs, update_review_status
from .pipeline import export_review_outputs, run_pipeline


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
    _ = workspace_root
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


if __name__ == "__main__":
    main()
