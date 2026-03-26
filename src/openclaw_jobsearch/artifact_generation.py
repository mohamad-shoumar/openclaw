from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .config import AppConfig
from .db import connect, get_job, list_jobs_ready_for_phase3, update_phase3_artifacts
from .llm import LlmConfig, generate_json_completion, resolve_llm_config
from .models import ApprovedJobContract

PROMPT_VERSION = "phase3-llm-v1"


class LlmArtifactDraft(BaseModel):
    matched_skills: list[str] = Field(default_factory=list)
    role_focus: list[str] = Field(default_factory=list)
    grounded_resume_facts: list[str] = Field(default_factory=list)
    missing_or_weak_requirements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    resume_markdown: str
    cover_letter_markdown: str


def generate_phase3_artifacts(
    workspace_root: Path,
    config_dir: Path,
    data_dir: Path,
    output_dir: Path,
    *,
    job_slug: str | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_temperature: float | None = None,
    llm_max_tokens: int | None = None,
) -> dict[str, Any]:
    config = AppConfig(workspace_root=workspace_root, config_dir=config_dir)
    config.ensure_inputs_exist()

    resume_text = config.resume_text_path.read_text(encoding="utf-8")
    resume_guide = _read_optional_text(workspace_root / "tailor_resume_guide.md")
    cover_letter_guide = _read_optional_text(workspace_root / "cover_letter_guide.md")
    llm_config = resolve_llm_config(
        provider=llm_provider,
        model=llm_model,
        temperature=llm_temperature,
        max_output_tokens=llm_max_tokens,
    )

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
                llm_config=llm_config,
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
        "generation_mode": "llm",
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model,
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
    llm_config: LlmConfig,
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

    draft = _generate_llm_artifact_draft(
        config=config,
        contract=contract,
        resume_text=resume_text,
        resume_guide=resume_guide,
        cover_letter_guide=cover_letter_guide,
        matched_skills=matched_skills,
        resume_highlights=resume_highlights,
        llm_config=llm_config,
    )
    _validate_llm_draft(draft, contract, config.profile.candidate_name)
    resume_path.write_text(draft.resume_markdown.strip() + "\n", encoding="utf-8")
    cover_letter_path.write_text(draft.cover_letter_markdown.strip() + "\n", encoding="utf-8")
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generation_mode": "llm",
        "prompt_version": PROMPT_VERSION,
        "job_slug": contract.job_slug,
        "artifact_dir": artifact_dir_relative,
        "matched_skills": matched_skills,
        "selected_resume_highlights": resume_highlights,
        "llm": {
            "provider": llm_config.provider,
            "model": llm_config.model,
            "temperature": llm_config.temperature,
            "max_output_tokens": llm_config.max_output_tokens,
        },
        "grounding": {
            "role_focus": draft.role_focus,
            "grounded_resume_facts": draft.grounded_resume_facts,
            "missing_or_weak_requirements": draft.missing_or_weak_requirements,
            "warnings": draft.warnings,
        },
        "inputs": {
            "profile_path": "config/profile.json",
            "resume_text_path": config.profile.resume_text_path,
            "resume_guide_path": "tailor_resume_guide.md" if resume_guide else "",
            "cover_letter_guide_path": "cover_letter_guide.md" if cover_letter_guide else "",
        },
    }

    metadata["outputs"] = {
        "job_description_path": _relative_path(job_description_path, workspace_root),
        "resume_path_generated": _relative_path(resume_path, workspace_root),
        "cover_letter_path_generated": _relative_path(cover_letter_path, workspace_root),
        "artifact_meta_path": _relative_path(artifact_meta_path, workspace_root),
    }
    metadata["approved_job_contract"] = contract.model_dump(mode="json")
    artifact_meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

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
def _select_resume_highlights(resume_text: str, contract: ApprovedJobContract, matched_skills: list[str]) -> list[str]:
    lines = _extract_resume_experience_lines(resume_text)
    if not lines:
        lines = _fallback_resume_lines(resume_text)
    keywords = {skill.lower() for skill in matched_skills + contract.required_tech + contract.preferred_tech}
    scored = sorted(lines, key=lambda line: _line_score(line, keywords), reverse=True)
    return scored[:5] if scored else lines[:5]


def _matched_skills(contract: ApprovedJobContract, profile_skills: list[str]) -> list[str]:
    job_text = _clean_text(" ".join([contract.title, contract.summary, contract.description_text])).lower()
    matched = [skill for skill in profile_skills if skill.lower() in job_text]
    merged = matched + [skill for skill in contract.required_tech + contract.preferred_tech if skill not in matched]
    return list(dict.fromkeys(merged))


def _line_score(line: str, keywords: set[str]) -> tuple[int, int]:
    lowered = line.lower()
    matches = sum(1 for keyword in keywords if keyword and keyword in lowered)
    return (matches, len(line))


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _generate_llm_artifact_draft(
    *,
    config: AppConfig,
    contract: ApprovedJobContract,
    resume_text: str,
    resume_guide: str,
    cover_letter_guide: str,
    matched_skills: list[str],
    resume_highlights: list[str],
    llm_config: LlmConfig,
) -> LlmArtifactDraft:
    system_prompt = (
        "You are an expert resume and cover-letter writer. "
        "Only use facts grounded in the provided candidate profile and source resume text. "
        "Do not invent employers, dates, projects, metrics, certifications, or tools. "
        "If a requirement is not supported by the source material, record it under "
        "`missing_or_weak_requirements` instead of claiming it. "
        "Return only a valid JSON object."
    )
    prompt_payload = {
        "prompt_version": PROMPT_VERSION,
        "candidate_profile": {
            "candidate_name": config.profile.candidate_name,
            "headline": config.profile.headline,
            "location": config.profile.location,
            "experience_years": config.profile.experience_years,
            "target_roles": config.profile.target_roles,
            "required_skills": config.profile.required_skills,
            "preferred_skills": config.profile.preferred_skills,
        },
        "approved_job_contract": contract.model_dump(mode="json"),
        "matched_skills": matched_skills,
        "selected_resume_highlights": resume_highlights,
        "resume_tailoring_guide": resume_guide,
        "cover_letter_guide": cover_letter_guide,
        "source_resume_text": resume_text.strip(),
    }
    user_prompt = "\n".join(
        [
            "Create a grounded tailored resume draft and cover letter draft for this approved job.",
            "Use an ATS-friendly tone and concise, specific wording.",
            "Resume markdown requirements:",
            "- Start with `# Tailored Resume Draft`.",
            "- Include a target headline, tailored summary, role alignment bullets, and selected experience highlights.",
            "- Keep claims faithful to the source resume.",
            "Cover letter markdown requirements:",
            "- Start with `# Cover Letter Draft`.",
            "- Address the company and role directly.",
            "- Keep it under 300 words.",
            "Return JSON using this exact schema:",
            "{",
            '  "matched_skills": ["string"],',
            '  "role_focus": ["string"],',
            '  "grounded_resume_facts": ["string"],',
            '  "missing_or_weak_requirements": ["string"],',
            '  "warnings": ["string"],',
            '  "resume_markdown": "string",',
            '  "cover_letter_markdown": "string"',
            "}",
            "Here is the source data:",
            json.dumps(prompt_payload, indent=2),
        ]
    )
    raw_response = generate_json_completion(
        config=llm_config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    try:
        return LlmArtifactDraft.model_validate(raw_response)
    except ValidationError as exc:
        raise RuntimeError(f"LLM response did not match the expected schema: {exc}") from exc


def _validate_llm_draft(
    draft: LlmArtifactDraft,
    contract: ApprovedJobContract,
    candidate_name: str,
) -> None:
    if len(draft.grounded_resume_facts) < 2:
        raise RuntimeError("LLM output did not provide enough grounded resume evidence.")
    if contract.company.lower() not in draft.cover_letter_markdown.lower():
        raise RuntimeError("Cover letter does not mention the target company.")
    if contract.title.lower() not in draft.cover_letter_markdown.lower():
        raise RuntimeError("Cover letter does not mention the target role title.")
    if candidate_name.lower() not in draft.cover_letter_markdown.lower():
        raise RuntimeError("Cover letter does not include the candidate name.")
    combined_output = "\n".join([draft.resume_markdown, draft.cover_letter_markdown])
    if _contains_placeholder_text(combined_output):
        raise RuntimeError("LLM output still contains placeholder text.")


def _contains_placeholder_text(text: str) -> bool:
    lowered = text.lower()
    placeholder_patterns = [
        "[company]",
        "[your name]",
        "[insert",
        "lorem ipsum",
        "todo",
        "tbd",
        "<company>",
        "<name>",
    ]
    return any(pattern in lowered for pattern in placeholder_patterns)


def _extract_resume_experience_lines(resume_text: str) -> list[str]:
    lines: list[str] = []
    in_experience = False
    for raw_line in resume_text.splitlines():
        line = _clean_text(raw_line)
        if not line:
            continue
        upper = line.upper()
        if _looks_like_experience_header(upper):
            in_experience = True
            continue
        if in_experience and _looks_like_end_of_experience_header(upper):
            break
        if not in_experience:
            continue
        if _should_skip_resume_line(line):
            continue
        lines.append(line)
    return list(dict.fromkeys(lines))


def _fallback_resume_lines(resume_text: str) -> list[str]:
    lines = []
    for raw_line in resume_text.splitlines():
        line = _clean_text(raw_line)
        if _should_skip_resume_line(line):
            continue
        lines.append(line)
    return list(dict.fromkeys(lines))


def _looks_like_experience_header(text: str) -> bool:
    return text.startswith(("WORK EXPERIENCE", "PROFESSIONAL EXPERIENCE", "EXPERIENCE"))


def _looks_like_end_of_experience_header(text: str) -> bool:
    return text.startswith(("EDUCATION", "PROJECTS", "SKILLS", "CERTIFICATIONS", "SUMMARY"))


def _should_skip_resume_line(line: str) -> bool:
    if not line or len(line) < 30:
        return True
    if re.search(r"\b\d{4}\b", line) and " at " in line.lower():
        return True
    return False


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
