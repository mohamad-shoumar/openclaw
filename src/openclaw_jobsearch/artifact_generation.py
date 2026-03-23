from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .db import connect, get_job, list_jobs_ready_for_phase3, update_phase3_artifacts
from .models import ApprovedJobContract


def generate_phase3_artifacts(
    workspace_root: Path,
    config_dir: Path,
    data_dir: Path,
    output_dir: Path,
    *,
    job_slug: str | None = None,
) -> dict[str, Any]:
    config = AppConfig(workspace_root=workspace_root, config_dir=config_dir)
    config.ensure_inputs_exist()

    resume_text = _clean_text(config.resume_text_path.read_text(encoding="utf-8"))
    resume_guide = _read_optional_text(workspace_root / "tailor_resume_guide.md")
    cover_letter_guide = _read_optional_text(workspace_root / "cover_letter_guide.md")

    connection = connect(data_dir / "jobs.db")
    jobs = list_jobs_ready_for_phase3(connection, job_slug=job_slug)
    if job_slug and not jobs:
        existing = get_job(connection, job_slug)
        if existing is None:
            raise KeyError(f"Unknown job slug: {job_slug}")
        raise ValueError(f"Job is not approved for Phase 3 generation: {job_slug}")

    generated_job_slugs: list[str] = []
    failed_job_slugs: list[str] = []

    for job in jobs:
        contract = ApprovedJobContract.from_job(job)
        try:
            paths = _generate_job_artifacts(
                workspace_root=workspace_root,
                config=config,
                contract=contract,
                resume_text=resume_text,
                resume_guide=resume_guide,
                cover_letter_guide=cover_letter_guide,
            )
            update_phase3_artifacts(
                connection,
                job.job_slug,
                phase3_status="generated",
                phase3_generated_at=datetime.now(timezone.utc),
                job_description_path=paths["job_description_path"],
                resume_path_generated=paths["resume_path_generated"],
                cover_letter_path_generated=paths["cover_letter_path_generated"],
                artifact_meta_path=paths["artifact_meta_path"],
                phase3_error="",
                artifact_dir=paths["artifact_dir"],
            )
            generated_job_slugs.append(job.job_slug)
        except Exception as exc:
            update_phase3_artifacts(
                connection,
                job.job_slug,
                phase3_status="failed",
                phase3_generated_at=None,
                phase3_error=str(exc),
                artifact_dir=contract.artifact_dir,
            )
            failed_job_slugs.append(job.job_slug)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_job_slug": job_slug,
        "approved_jobs_considered": len(jobs),
        "generated_jobs": len(generated_job_slugs),
        "failed_jobs": len(failed_job_slugs),
        "generated_job_slugs": generated_job_slugs,
        "failed_job_slugs": failed_job_slugs,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "phase3_latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _generate_job_artifacts(
    *,
    workspace_root: Path,
    config: AppConfig,
    contract: ApprovedJobContract,
    resume_text: str,
    resume_guide: str,
    cover_letter_guide: str,
) -> dict[str, str]:
    artifact_dir_relative = contract.artifact_dir or f"artifacts/jobs/{contract.job_slug}"
    artifact_dir = _resolve_workspace_path(workspace_root, artifact_dir_relative)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    matched_skills = _matched_skills(contract, config.profile.required_skills + config.profile.preferred_skills)
    resume_highlights = _select_resume_highlights(resume_text, contract, matched_skills)

    job_description_path = artifact_dir / "job_description.md"
    resume_path = artifact_dir / "resume.md"
    cover_letter_path = artifact_dir / "cover_letter.md"
    artifact_meta_path = artifact_dir / "artifact_meta.json"

    job_description_path.write_text(_render_job_description(contract, matched_skills), encoding="utf-8")
    resume_path.write_text(
        _render_resume(config, contract, matched_skills, resume_highlights, resume_guide),
        encoding="utf-8",
    )
    cover_letter_path.write_text(
        _render_cover_letter(config, contract, matched_skills, resume_highlights, cover_letter_guide),
        encoding="utf-8",
    )
    artifact_meta_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "generation_mode": "template",
                "job_slug": contract.job_slug,
                "artifact_dir": artifact_dir_relative,
                "matched_skills": matched_skills,
                "selected_resume_highlights": resume_highlights,
                "inputs": {
                    "profile_path": "config/profile.json",
                    "resume_text_path": config.profile.resume_text_path,
                    "resume_guide_path": "tailor_resume_guide.md" if resume_guide else "",
                    "cover_letter_guide_path": "cover_letter_guide.md" if cover_letter_guide else "",
                },
                "outputs": {
                    "job_description_path": _relative_path(job_description_path, workspace_root),
                    "resume_path_generated": _relative_path(resume_path, workspace_root),
                    "cover_letter_path_generated": _relative_path(cover_letter_path, workspace_root),
                    "artifact_meta_path": _relative_path(artifact_meta_path, workspace_root),
                },
                "approved_job_contract": contract.model_dump(mode="json"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "artifact_dir": _relative_path(artifact_dir, workspace_root),
        "job_description_path": _relative_path(job_description_path, workspace_root),
        "resume_path_generated": _relative_path(resume_path, workspace_root),
        "cover_letter_path_generated": _relative_path(cover_letter_path, workspace_root),
        "artifact_meta_path": _relative_path(artifact_meta_path, workspace_root),
    }


def _render_job_description(contract: ApprovedJobContract, matched_skills: list[str]) -> str:
    return "\n".join(
        [
            "# Job Description",
            "",
            f"## {contract.company} - {contract.title}",
            "",
            "- Draft type: canonical approved-job snapshot",
            f"- Job slug: `{contract.job_slug}`",
            f"- Source: `{contract.discovery_source}` ({contract.source_tier})",
            f"- Posted: {contract.posted_at or 'unknown'}",
            f"- Location: {contract.location_raw or 'unknown'}",
            f"- Apply URL: {contract.apply_url}",
            f"- Required tech: {', '.join(contract.required_tech) or 'none detected'}",
            f"- Preferred tech: {', '.join(contract.preferred_tech) or 'none detected'}",
            f"- Matched profile skills: {', '.join(matched_skills) or 'none detected'}",
            "",
            "## Approval Context",
            "",
            f"- Approval reason: {contract.approval_reason or 'No approval reason recorded.'}",
            f"- Review notes: {contract.review_notes or 'No review notes recorded.'}",
            "",
            "## Summary",
            "",
            contract.summary or "No summary available.",
            "",
            "## Canonical Description",
            "",
            contract.description_text or "No canonical description available.",
            "",
        ]
    )


def _render_resume(
    config: AppConfig,
    contract: ApprovedJobContract,
    matched_skills: list[str],
    resume_highlights: list[str],
    resume_guide: str,
) -> str:
    lines = [
        "# Tailored Resume Draft",
        "",
        f"Draft tailored for **{contract.company}** - **{contract.title}**.",
        "",
        "## Candidate",
        "",
        f"- Name: {config.profile.candidate_name}",
        f"- Headline: {config.profile.headline}",
        f"- Location: {config.profile.location}",
        f"- Experience: {config.profile.experience_years} years",
        "",
        "## Tailored Summary",
        "",
        f"{config.profile.candidate_name} is a backend-focused engineer with {config.profile.experience_years} years of experience across Python systems, FastAPI services, AWS environments, and product delivery. "
        f"This draft emphasizes alignment with the {contract.title} role at {contract.company}, especially around {', '.join(matched_skills[:4]) or 'Python backend delivery'}.",
        "",
        "## Role Alignment",
        "",
        f"- Strongest matched skills: {', '.join(matched_skills) or 'python, backend, api development'}",
        f"- Job-required tech: {', '.join(contract.required_tech) or 'none detected'}",
        f"- Job-preferred tech: {', '.join(contract.preferred_tech) or 'none detected'}",
        "",
        "## Selected Experience Highlights",
        "",
    ]
    for highlight in (resume_highlights or ["Add the strongest experience bullets from the master resume here."])[:5]:
        lines.append(f"- {highlight}")
    lines.extend(
        [
            "",
            "## Resume Tailoring Notes",
            "",
            "- Keep the title and summary aligned with the posting terminology where it is truthful.",
            "- Prioritize backend, Python, API, system-design, and delivery-impact bullets over frontend-heavy work.",
            "- Preserve measurable outcomes whenever possible.",
        ]
    )
    for note in _guide_notes(resume_guide, limit=3):
        lines.append(f"- Guide anchor: {note}")
    lines.extend(
        [
            "",
            "## Base Resume Source",
            "",
            f"- Resume text path: `{config.profile.resume_text_path}`",
            f"- Artifact directory: `{contract.artifact_dir}`",
            "",
        ]
    )
    return "\n".join(lines)


def _render_cover_letter(
    config: AppConfig,
    contract: ApprovedJobContract,
    matched_skills: list[str],
    resume_highlights: list[str],
    cover_letter_guide: str,
) -> str:
    opening_skill = matched_skills[0] if matched_skills else "Python backend development"
    proof_points = resume_highlights[:2] or [
        "Led backend and trading-systems work with a strong delivery focus.",
        "Shipped production features in fast-moving teams while maintaining code quality.",
    ]
    lines = [
        "# Cover Letter Draft",
        "",
        "Dear Hiring Team,",
        "",
        f"I am applying for the {contract.title} role at {contract.company}. "
        f"With {config.profile.experience_years} years of experience across backend engineering and product delivery, "
        f"I believe I can contribute quickly in areas such as {opening_skill}.",
        "",
        f"My background includes {proof_points[0].lower()} "
        f"and {proof_points[1].lower() if len(proof_points) > 1 else 'building reliable systems with clean, maintainable code'} "
        f"I have worked with technologies aligned to this role, including {', '.join(matched_skills[:5]) or 'Python, FastAPI, and AWS'}.",
        "",
        f"I am especially interested in this opportunity because the role combines {', '.join(contract.required_tech[:3]) or 'backend engineering responsibilities'} "
        f"with real product ownership. I would be excited to bring a practical, delivery-focused mindset to {contract.company}'s team.",
        "",
        f"Thank you for your time and consideration. I would welcome the opportunity to discuss how my background could support the {contract.title} position.",
        "",
        f"Sincerely,  \n{config.profile.candidate_name}",
        "",
    ]
    for note in _guide_notes(cover_letter_guide, limit=3):
        lines.append(f"<!-- Guide anchor: {note} -->")
    return "\n".join(lines)


def _select_resume_highlights(resume_text: str, contract: ApprovedJobContract, matched_skills: list[str]) -> list[str]:
    lines = []
    in_work_experience = False
    for raw_line in resume_text.splitlines():
        line = _clean_text(raw_line)
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("WORK EXPERIENCE"):
            in_work_experience = True
            continue
        if in_work_experience and upper.startswith("EDUCATION"):
            break
        if not in_work_experience:
            continue
        if re.search(r"\b\d{4}\b", line) and " at " in line.lower():
            continue
        if len(line) < 30:
            continue
        lines.append(line)

    lines = list(dict.fromkeys(lines))
    keywords = {skill.lower() for skill in matched_skills + contract.required_tech + contract.preferred_tech}
    scored = sorted(lines, key=lambda line: _line_score(line, keywords), reverse=True)
    return scored[:5] if scored else lines[:5]


def _matched_skills(contract: ApprovedJobContract, profile_skills: list[str]) -> list[str]:
    job_text = _clean_text(" ".join([contract.title, contract.summary, contract.description_text])).lower()
    matched = [skill for skill in profile_skills if skill.lower() in job_text]
    merged = matched + [skill for skill in contract.required_tech + contract.preferred_tech if skill not in matched]
    return list(dict.fromkeys(merged))


def _guide_notes(text: str, *, limit: int) -> list[str]:
    notes: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_text(raw_line)
        if not line or len(line) < 20:
            continue
        notes.append(line)
        if len(notes) >= limit:
            break
    return notes


def _line_score(line: str, keywords: set[str]) -> tuple[int, int]:
    lowered = line.lower()
    matches = sum(1 for keyword in keywords if keyword and keyword in lowered)
    return (matches, len(line))


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return _clean_text(path.read_text(encoding="utf-8"))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("&nbsp;", " ")).strip()


def _resolve_workspace_path(workspace_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return workspace_root / path


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root))
    except ValueError:
        return str(path)
