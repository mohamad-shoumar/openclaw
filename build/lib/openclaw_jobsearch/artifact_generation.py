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
from .pdf import html_to_pdf, render_cover_letter_html, render_resume_html

PROMPT_VERSION = "phase3-llm-v2"


class ResumeData(BaseModel):
    candidate_name: str
    contact_line: str
    headline: str
    summary: str
    skills: dict[str, list[str]] = Field(default_factory=dict)
    experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)


class CoverLetterData(BaseModel):
    candidate_name: str
    contact_line: str
    paragraphs: list[str] = Field(default_factory=list)
    closing: str = ""


class LlmArtifactDraft(BaseModel):
    matched_skills: list[str] = Field(default_factory=list)
    role_focus: list[str] = Field(default_factory=list)
    grounded_resume_facts: list[str] = Field(default_factory=list)
    missing_or_weak_requirements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    resume_markdown: str
    cover_letter_markdown: str
    resume_data: dict[str, Any] = Field(default_factory=dict)
    cover_letter_data: dict[str, Any] = Field(default_factory=dict)


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
    resume_md_path = artifact_dir / "resume.md"
    resume_html_path = artifact_dir / "resume.html"
    resume_pdf_path = artifact_dir / "resume.pdf"
    cover_letter_md_path = artifact_dir / "cover_letter.md"
    cover_letter_html_path = artifact_dir / "cover_letter.html"
    cover_letter_pdf_path = artifact_dir / "cover_letter.pdf"
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

    resume_md_path.write_text(draft.resume_markdown.strip() + "\n", encoding="utf-8")
    cover_letter_md_path.write_text(draft.cover_letter_markdown.strip() + "\n", encoding="utf-8")

    profile_dict = {
        "email": config.profile.email,
        "phone": config.profile.phone,
        "linkedin_url": config.profile.linkedin_url,
        "location": config.profile.location,
    }
    resume_html = render_resume_html(draft.resume_data, profile=profile_dict)
    resume_html_path.write_text(resume_html, encoding="utf-8")

    cl_html = render_cover_letter_html(draft.cover_letter_data)
    cover_letter_html_path.write_text(cl_html, encoding="utf-8")

    pdf_error = ""
    try:
        html_to_pdf(resume_html, resume_pdf_path)
        html_to_pdf(cl_html, cover_letter_pdf_path)
    except RuntimeError as exc:
        pdf_error = str(exc)

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
        "outputs": {
            "job_description_path": _relative_path(job_description_path, workspace_root),
            "resume_md": _relative_path(resume_md_path, workspace_root),
            "resume_html": _relative_path(resume_html_path, workspace_root),
            "resume_pdf": _relative_path(resume_pdf_path, workspace_root) if resume_pdf_path.exists() else "",
            "cover_letter_md": _relative_path(cover_letter_md_path, workspace_root),
            "cover_letter_html": _relative_path(cover_letter_html_path, workspace_root),
            "cover_letter_pdf": _relative_path(cover_letter_pdf_path, workspace_root) if cover_letter_pdf_path.exists() else "",
            "artifact_meta_path": _relative_path(artifact_meta_path, workspace_root),
        },
        "pdf_error": pdf_error,
        "approved_job_contract": contract.model_dump(mode="json"),
    }
    artifact_meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return {
        "artifact_dir": _relative_path(artifact_dir, workspace_root),
        "job_description_path": _relative_path(job_description_path, workspace_root),
        "resume_path_generated": _relative_path(resume_md_path, workspace_root),
        "cover_letter_path_generated": _relative_path(cover_letter_md_path, workspace_root),
        "artifact_meta_path": _relative_path(artifact_meta_path, workspace_root),
    }


def regenerate_pdfs_from_html(
    workspace_root: Path,
    data_dir: Path,
    job_slug: str,
) -> dict[str, str]:
    connection = connect(data_dir / "jobs.db")
    job = get_job(connection, job_slug)
    if job is None:
        raise KeyError(f"Unknown job slug: {job_slug}")

    artifact_dir_relative = job.artifact_dir or f"artifacts/jobs/{job_slug}"
    artifact_dir = _resolve_workspace_path(workspace_root, artifact_dir_relative)

    regenerated: list[str] = []
    for name in ("resume", "cover_letter"):
        html_path = artifact_dir / f"{name}.html"
        pdf_path = artifact_dir / f"{name}.pdf"
        if not html_path.exists():
            raise FileNotFoundError(f"HTML source not found: {html_path}")
        html_to_pdf(html_path.read_text(encoding="utf-8"), pdf_path)
        regenerated.append(str(pdf_path.relative_to(workspace_root)))

    return {"regenerated": regenerated}


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
        "You are an expert resume tailor. Adapt the candidate's real resume "
        "for a specific job posting — maximizing relevance while staying truthful.\n\n"
        "CRITICAL RULES:\n"
        "1. ONE PAGE MAXIMUM (US Letter). Be concise.\n"
        "2. Section order: Profile → Professional Experience → Skills → Education.\n"
        "3. HEADLINE: Use ONLY a role title adapted to the target position. "
        "NO languages or frameworks in the headline. E.g. 'Full-Stack Team Lead', "
        "NOT 'Full-Stack Developer (Python · React · FastAPI)'.\n"
        "4. CONTACT INFO: Use EXACTLY the email, phone, and location from the candidate_profile. "
        "NEVER invent or change contact details.\n"
        "5. DATE FORMAT: Always use short month names: 'Sept 2025 – Present', 'Aug 2024 – Aug 2025', "
        "'Jun 2023 – Jul 2024', 'Nov 2022 – May 2023'. NEVER use full month names.\n"
        "6. COMPANY FORMAT: Always include location: 'CoinQuant, Abu Dhabi', NOT just 'CoinQuant'.\n"
        "7. SKILLS: Only include skills the candidate actually has. Do NOT fabricate. "
        "Use exactly these categories in this order:\n"
        "   - Languages (e.g. Python, TypeScript)\n"
        "   - Frameworks (e.g. FastAPI, React)\n"
        "   - Databases (e.g. PostgreSQL, Firebase)\n"
        "   - Tools & Platforms (e.g. Git, Docker, AWS)\n"
        "   - Concepts & Methodologies (e.g. OOP, Microservices, Agile, Scrum)\n"
        "   Order skills within each category from most job-relevant to least. "
        "Drop skills that aren't relevant to the job.\n"
        "8. EDUCATION: Preserve the exact same wording and structure from the source resume. "
        "Use the same school names, degree names, and notes. Do not rephrase.\n"
        "9. EXPERIENCE: 3-4 bullets per role MAX. Rewrite bullets to echo job description language "
        "but ONLY based on real work. Add bullets for underrepresented real experience "
        "(e.g. documentation, testing) if the job values them.\n"
        "10. PROFILE: 3-4 sentences rewritten for this specific job. Write grammatically correct, "
        "natural English. Do not use awkward sentence fragments.\n"
        "11. Do not invent employers, dates, or projects.\n"
        "12. The cover letter must be concise (<250 words), confident, and directly address the company.\n"
        "13. COVER LETTER STYLE RULES:\n"
        "   - NEVER use em dashes (—). Use commas or periods instead.\n"
        "   - Opening line: Say the role title only. Do NOT list languages, frameworks, or tools "
        "in the opening 'I'm excited to apply for...' sentence. "
        "Bad: 'Full-Stack Developer (Python, React, FastAPI) role'. "
        "Good: 'Full-Stack Developer role'.\n"
        "   - Greeting: Use a simplified, human-friendly company name. Drop legal suffixes "
        "like S.R.L., Inc., Ltd., GmbH, etc. Use natural casing (e.g. 'Electe' not 'ELECTE S.R.L.'). "
        "Format: 'Dear [Company] Hiring Team,'.\n"
        "   - Do NOT combine technologies with slashes like 'Python/FastAPI backends' or "
        "'React frontends'. Instead list them naturally: 'Python, React, and AWS'.\n"
        "14. Return only a valid JSON object."
    )
    contact_line = " | ".join(
        p for p in [
            config.profile.email,
            config.profile.phone,
            config.profile.location,
        ] if p
    )
    prompt_payload = {
        "prompt_version": PROMPT_VERSION,
        "candidate_profile": {
            "candidate_name": config.profile.candidate_name,
            "email": config.profile.email,
            "phone": config.profile.phone,
            "linkedin_url": config.profile.linkedin_url,
            "location": config.profile.location,
            "contact_line": contact_line,
            "headline": config.profile.headline,
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
            "Generate a FINAL, submission-ready, tailored resume and cover letter.",
            "",
            "STRICT FORMATTING RULES:",
            "- ONE PAGE MAX (US Letter 8.5x11in).",
            "- Section order: Profile → Professional Experience → Skills → Education.",
            "",
            "CONTACT INFO (use EXACTLY these — do NOT change or invent):",
            f"- Email: {config.profile.email}",
            f"- Phone: {config.profile.phone}",
            f"- LinkedIn: {config.profile.linkedin_url}",
            f"- Location: {config.profile.location}",
            f"- contact_line: {contact_line}",
            "",
            "HEADLINE RULES:",
            "- ONLY the adapted role title. NO languages, frameworks, or parentheticals.",
            "- Good: 'Full-Stack Team Lead', 'Backend Engineer', 'Software Engineer'",
            "- Bad: 'Full-Stack Developer (Python · React · FastAPI)', 'Backend Engineer – Python, FastAPI'",
            "",
            "DATE FORMAT (mandatory):",
            "- Use short month names: Sept 2025 – Present, Aug 2024 – Aug 2025, Jun 2023 – Jul 2024",
            "- Education: Nov 2022 – May 2023, 2018 – 2021",
            "- NEVER use full month names like 'September', 'August', etc.",
            "",
            "COMPANY FORMAT:",
            "- Always include city: 'CoinQuant, Abu Dhabi' (NOT just 'CoinQuant')",
            "",
            "EXPERIENCE:",
            "- 3-4 bullets per role MAX.",
            "- Rewrite bullets to mirror job posting language, but ONLY based on real work.",
            "- Add bullets for real but underrepresented experience if the job values them.",
            "",
            "EDUCATION (preserve exact wording from source resume):",
            "- Entry 1: school='Software Engineering Factory', degree='Full Stack Software Engineering Bootcamp',",
            "  dates='Nov 2022 – May 2023',",
            "  note='Completed the boot camp as a Star Developer with a Full stack web app.'",
            "- Entry 2: school='American University of Beirut', degree='Psychology',",
            "  dates='2018 – 2021', note=''",
            "- Do NOT rephrase. Use these exact values.",
            "",
            "SKILLS (use exactly these category names in this order):",
            "- Languages: (e.g. Python, TypeScript, JavaScript)",
            "- Frameworks: (e.g. FastAPI, React, React Native)",
            "- Databases: (e.g. PostgreSQL, Firebase)",
            "- Tools & Platforms: (e.g. Git, Docker, AWS)",
            "- Concepts & Methodologies: (e.g. OOP, Microservices, Agile, Scrum)",
            "Only include skills the candidate actually has. Order from most job-relevant to least.",
            "Drop categories if empty. Drop skills that aren't relevant.",
            "",
            "PROFILE: 3-4 grammatically correct sentences. No fragments. Rewritten for this job.",
            "",
            "NO sections called 'Role Alignment', 'Tailoring Notes', or 'Base Resume Source'.",
            "",
            "COVER LETTER FORMAT (markdown):",
            "- Start with candidate name as H1.",
            "- 3-4 paragraphs addressing the hiring team.",
            "- Reference the company name and role title.",
            "- Under 250 words.",
            "- Sign off with candidate name.",
            "- NEVER use em dashes (—). Use commas or periods instead.",
            "- Opening sentence: mention only the role title, do NOT list languages/frameworks/tools.",
            "- Greeting: use a clean, human-friendly company name. Drop legal suffixes (S.R.L., Inc., etc.).",
            "  Use natural casing, not ALL CAPS. E.g. 'Dear Electe Hiring Team,' not 'Dear ELECTE S.R.L. Hiring Team,'.",
            "- Do NOT combine technologies with slashes (e.g. 'Python/FastAPI'). List them naturally.",
            "",
            "Return JSON with this exact schema:",
            "{",
            '  "matched_skills": ["string"],',
            '  "role_focus": ["string"],',
            '  "grounded_resume_facts": ["string"],',
            '  "missing_or_weak_requirements": ["string"],',
            '  "warnings": ["string"],',
            '  "resume_markdown": "string (the full resume in markdown)",',
            '  "cover_letter_markdown": "string (the full cover letter in markdown)",',
            '  "resume_data": {',
            f'    "candidate_name": "{config.profile.candidate_name}",',
            f'    "contact_line": "{contact_line}",',
            '    "headline": "string (role title ONLY, no languages/frameworks)",',
            '    "summary": "string (3-4 sentences, grammatically correct)",',
            '    "skills": {"Languages": [...], "Frameworks": [...], "Databases": [...], '
            '"Tools & Platforms": [...], "Concepts & Methodologies": [...]},',
            '    "experience": [{"title":"string","company":"string (with city)","dates":"string (short month)","bullets":["string"]}],',
            '    "education": [{"degree":"string","school":"string","dates":"string","note":"string"}]',
            "  },",
            '  "cover_letter_data": {',
            f'    "candidate_name": "{config.profile.candidate_name}",',
            f'    "contact_line": "{contact_line}",',
            '    "paragraphs": ["string (each paragraph)"],',
            '    "closing": "string (e.g. Sincerely,\\nName)"',
            "  }",
            "}",
            "",
            "Source data:",
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
    cl_lower = draft.cover_letter_markdown.lower()
    if contract.company.lower() not in cl_lower:
        raise RuntimeError("Cover letter does not mention the target company.")
    title_words = [w for w in contract.title.lower().split() if len(w) > 3]
    title_match_count = sum(1 for w in title_words if w in cl_lower)
    if title_words and title_match_count < len(title_words) * 0.5:
        raise RuntimeError("Cover letter does not sufficiently reference the target role.")
    if candidate_name.split()[0].lower() not in cl_lower:
        raise RuntimeError("Cover letter does not include the candidate name.")
    combined = "\n".join([draft.resume_markdown, draft.cover_letter_markdown])
    if _contains_placeholder_text(combined):
        raise RuntimeError("LLM output still contains placeholder text.")


def _contains_placeholder_text(text: str) -> bool:
    lowered = text.lower()
    placeholder_patterns = [
        "[company]", "[your name]", "[insert", "lorem ipsum",
        "<company>", "<name>",
    ]
    return any(pattern in lowered for pattern in placeholder_patterns)


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
