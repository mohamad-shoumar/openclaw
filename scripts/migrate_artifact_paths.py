"""One-shot migration: rewrite stored artifact paths for the var/ layout move.

Job artifact locations are persisted as workspace-relative strings, so moving
`artifacts/` under `var/` leaves every stored path pointing at nothing. This
rewrites the five affected fields on every job whose paths still use the old
prefix. Idempotent - running it twice is a no-op.

Delete this file once it has been run against every copy of the database.

    python3 scripts/migrate_artifact_paths.py [--db var/data/jobs.db] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openclaw_jobsearch.db import _persist_job, connect  # noqa: E402
from openclaw_jobsearch.models import JobRecord  # noqa: E402

OLD_PREFIX = "artifacts/jobs/"
NEW_PREFIX = "var/artifacts/jobs/"
PATH_FIELDS = (
    "artifact_dir",
    "job_description_path",
    "resume_path_generated",
    "cover_letter_path_generated",
    "artifact_meta_path",
)


def migrate(db_path: Path, *, dry_run: bool) -> int:
    connection = connect(db_path)
    slugs = [
        row["job_slug"]
        for row in connection.execute(
            "SELECT job_slug FROM jobs WHERE payload_json LIKE ?",
            (f'%"{OLD_PREFIX}%',),
        )
    ]

    changed = 0
    for slug in slugs:
        row = connection.execute(
            "SELECT payload_json FROM jobs WHERE job_slug = ?", (slug,)
        ).fetchone()
        job = JobRecord.model_validate_json(row["payload_json"])
        updates = {
            field: NEW_PREFIX + getattr(job, field)[len(OLD_PREFIX):]
            for field in PATH_FIELDS
            if getattr(job, field).startswith(OLD_PREFIX)
        }
        if not updates:
            continue
        changed += 1
        print(f"{slug}")
        for field, value in updates.items():
            print(f"    {field}: {getattr(job, field)} -> {value}")
            setattr(job, field, value)
        if not dry_run:
            _persist_job(connection, job)

    if dry_run:
        print(f"\ndry run: {changed} job(s) would change")
    else:
        connection.commit()
        print(f"\nmigrated {changed} job(s)")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="var/data/jobs.db", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}")
    migrate(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
