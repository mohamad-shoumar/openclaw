"""Workspace path conventions.

Generated output lives under `var/` so one gitignore line covers it and one
directory is the whole backup target. Human-authored inputs (resume, guides)
live under `candidate/` and are configured in `config/paths.json`, never
hardcoded here - only the machine-generated layout is a convention.

Job artifact directories are stored in the database as workspace-relative
strings, so this module owns the one place that spelling is decided.
"""

from __future__ import annotations

from pathlib import Path

VAR_DIR = "var"
DATA_DIR = f"{VAR_DIR}/data"
OUTPUT_DIR = f"{VAR_DIR}/outputs"
ARTIFACTS_DIR = f"{VAR_DIR}/artifacts"
ARTIFACTS_JOBS_DIR = f"{ARTIFACTS_DIR}/jobs"


def default_artifact_dir(job_slug: str) -> str:
    """Workspace-relative artifact directory for a job. Stored in the db."""
    return f"{ARTIFACTS_JOBS_DIR}/{job_slug}"


def artifacts_jobs_root(workspace_root: Path) -> Path:
    return workspace_root / ARTIFACTS_JOBS_DIR


def resolve_workspace_path(workspace_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return workspace_root / path


def relative_to_workspace(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root))
    except ValueError:
        return str(path)
