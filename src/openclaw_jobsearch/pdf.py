from __future__ import annotations

import html as html_module
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from string import Template

_DEFAULT_RESUME_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><style>
@page { size: letter; margin: 0.50in; }
body {
    font-family: Arial, sans-serif;
    font-size: 10pt;
    color: #222;
    line-height: 115%;
    margin: 0;
    padding: 0;
}
h1 {
    font-size: 23pt;
    font-weight: normal;
    line-height: 1.05;
    margin: 0 0 2pt 0;
    color: #5B9BD5;
}
.subtitle {
    font-size: 12pt;
    font-weight: normal;
    color: #5B9BD5;
    line-height: 1.1;
    margin: 0 0 12pt 0;
}
.header-table { width: 100%; border-collapse: collapse; margin-bottom: 4pt; }
.header-table td { vertical-align: top; border: none; padding: 0; }
.header-right {
    text-align: right;
    font-size: 10pt;
    color: #222;
    line-height: 140%;
}
.header-right a { color: #5B9BD5; text-decoration: none; }
h2 {
    font-size: 18pt;
    font-weight: normal;
    color: #5B9BD5;
    padding-bottom: 2pt;
    margin: 14pt 0 8pt 0;
}
h2:first-of-type { margin-top: 12pt; }
.summary {
    margin: 0 0 4pt 0;
    font-size: 10pt;
    line-height: 130%;
}
ul { margin: 4pt 0 4pt 0; padding-left: 16pt; }
li { margin-bottom: 4pt; line-height: 130%; }
.exp-block { margin-bottom: 8pt; }
.exp-header { width: 100%; border-collapse: collapse; margin-bottom: 0; }
.exp-header td { border: none; padding: 0; vertical-align: top; }
.exp-company { font-weight: bold; font-size: 10pt; }
.exp-title { font-weight: bold; font-size: 10pt; }
.exp-dates { text-align: right; font-size: 10pt; white-space: nowrap; }
.skills-table { width: 100%; border-collapse: collapse; margin: 0; }
.skills-table td { vertical-align: top; padding: 2pt 6pt 2pt 0; font-size: 10pt; line-height: 130%; }
.skills-label { font-weight: bold; white-space: nowrap; }
.edu-block { margin-bottom: 12pt; }
.edu-header { width: 100%; border-collapse: collapse; }
.edu-header td { border: none; padding: 0; vertical-align: top; }
.edu-dates { text-align: right; font-size: 10pt; white-space: nowrap; }
</style></head>
<body>
<table class="header-table"><tr>
<td><h1>$name</h1>$headline_html</td>
<td class="header-right">$contact_html</td>
</tr></table>
$profile_html
<h2>Professional Experience</h2>
$experience_html
<h2>Skills</h2>
$skills_html
<h2>Education</h2>
$education_html
</body>
</html>"""

_COVER_LETTER_CSS = """
@page { size: letter; margin: 1in; }
body { font-family: Arial, sans-serif; font-size: 11pt; color: #222; line-height: 1.55; }
h1 { font-size: 14pt; margin: 0 0 6pt 0; }
.contact { font-size: 9pt; color: #555; margin: 0 0 16pt 0; }
p { margin: 0 0 10pt 0; }
.closing { margin-top: 20pt; }
"""


def render_resume_html(resume_data: dict, profile: dict | None = None) -> str:
    name = _e(resume_data.get("candidate_name", ""))
    headline = _e(resume_data.get("headline", ""))
    summary = _e(resume_data.get("summary", ""))

    contact_html = _render_contact_right(resume_data, profile)
    skills_html = _render_skills_table(resume_data.get("skills", {}))
    experience_html = _render_experience(resume_data.get("experience", []))
    education_html = _render_education(resume_data.get("education", []))
    headline_html = f'<p class="subtitle">{headline}</p>' if headline else ""
    profile_html = f"<h2>Profile</h2>\n<p class=\"summary\">{summary}</p>" if summary else ""

    return _resume_template().substitute(
        name=name,
        headline_html=headline_html,
        contact_html=contact_html,
        profile_html=profile_html,
        experience_html=experience_html,
        skills_html=skills_html,
        education_html=education_html,
    )


def render_cover_letter_html(cl_data: dict) -> str:
    name = _e(cl_data.get("candidate_name", ""))
    contact = _e(cl_data.get("contact_line", ""))
    greeting = _e(cl_data.get("greeting", ""))
    paragraphs = cl_data.get("paragraphs", [])
    closing = _e(cl_data.get("closing", f"Sincerely,\n{name}"))

    greeting_html = f"<p>{greeting}</p>\n" if greeting else ""
    body_html = "\n".join(f"<p>{_e(p)}</p>" for p in paragraphs)
    closing_html = closing.replace("\n", "<br>")
    contact_html = f'<p class="contact">{contact}</p>' if contact else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><style>{_COVER_LETTER_CSS}</style></head>
<body>
<h1>{name}</h1>
{contact_html}
{greeting_html}{body_html}
<p class="closing">{closing_html}</p>
</body>
</html>"""


def html_to_pdf(html_content: str, output_path: Path) -> None:
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "weasyprint is required for PDF generation. Install it with: pip install weasyprint"
        ) from exc
    HTML(string=html_content).write_pdf(str(output_path))


def render_resume_html_from_markdown(markdown_content: str) -> str:
    resume_data, profile = _parse_resume_markdown(markdown_content)
    return render_resume_html(resume_data, profile=profile)


def render_cover_letter_html_from_markdown(markdown_content: str) -> str:
    return render_cover_letter_html(_parse_cover_letter_markdown(markdown_content))


def _render_contact_right(resume_data: dict, profile: dict | None = None) -> str:
    if profile:
        email = profile.get("email", "")
        phone = profile.get("phone", "")
        linkedin = profile.get("linkedin_url", "")
        location = profile.get("location", "")
    else:
        contact_line = resume_data.get("contact_line", "")
        parts = [p.strip() for p in contact_line.split("|")]
        return "<br>".join(_e(p) for p in parts if p)

    lines = []
    if email:
        lines.append(f'<a href="mailto:{_e(email)}">{_e(email)}</a>')
    if phone:
        lines.append(_e(phone))
    if linkedin:
        label = "LinkedIn"
        lines.append(f'<a href="{_e(linkedin)}">{label}</a>')
    if location:
        lines.append(_e(location))
    return "<br>".join(lines)


def _parse_resume_markdown(markdown_content: str) -> tuple[dict, dict | None]:
    lines = markdown_content.splitlines()
    candidate_name = ""
    headline = ""
    contact_line = ""
    summary = ""
    sections: dict[str, list[str]] = {
        "profile": [],
        "experience": [],
        "skills": [],
        "education": [],
    }

    start_idx = 0
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("# "):
            candidate_name = _strip_markdown_inline(line[2:])
            start_idx = idx + 1
            break

    current_section = ""
    for raw_line in lines[start_idx:]:
        line = raw_line.strip()
        section_key = _section_key(line)
        if section_key:
            current_section = section_key
            continue
        if line == "---":
            continue
        if current_section:
            sections[current_section].append(raw_line)
            continue
        if not line:
            continue
        if not contact_line and _looks_like_contact(line):
            contact_line = _strip_markdown_inline(line)
            continue
        if not headline:
            extracted_headline = _extract_headline(line)
            if extracted_headline:
                headline = extracted_headline

    if sections["profile"]:
        summary = " ".join(_strip_markdown_inline(line) for line in sections["profile"] if line.strip())

    resume_data = {
        "candidate_name": candidate_name,
        "contact_line": contact_line,
        "headline": headline,
        "summary": summary,
        "skills": _parse_skills_section(sections["skills"]),
        "experience": _parse_experience_section(sections["experience"]),
        "education": _parse_education_section(sections["education"]),
    }
    return resume_data, _profile_from_contact_line(contact_line)


def _parse_cover_letter_markdown(markdown_content: str) -> dict:
    lines = markdown_content.splitlines()
    candidate_name = ""
    contact_line = ""
    greeting = ""

    start_idx = 0
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("# "):
            candidate_name = _strip_markdown_inline(line[2:])
            start_idx = idx + 1
            break

    content_lines: list[str] = []
    for raw_line in lines[start_idx:]:
        line = raw_line.strip()
        if line == "---":
            continue
        if not line and not content_lines:
            continue
        if not contact_line and _looks_like_contact(line):
            contact_line = _strip_markdown_inline(line)
            continue
        content_lines.append(raw_line)

    trimmed_lines = list(content_lines)
    while trimmed_lines and not trimmed_lines[-1].strip():
        trimmed_lines.pop()

    closing = f"Sincerely,\n{candidate_name}" if candidate_name else "Sincerely,"
    if len(trimmed_lines) >= 2:
        signoff = _strip_markdown_inline(trimmed_lines[-2])
        signature = _strip_markdown_inline(trimmed_lines[-1])
        if _looks_like_signoff(signoff):
            closing = f"{signoff}\n{signature}".strip()
            trimmed_lines = trimmed_lines[:-2]
            while trimmed_lines and not trimmed_lines[-1].strip():
                trimmed_lines.pop()

    if trimmed_lines:
        first_content = trimmed_lines[0].strip()
        if first_content.startswith("Dear "):
            greeting = _strip_markdown_inline(first_content)
            trimmed_lines = trimmed_lines[1:]

    paragraphs = _markdown_paragraphs(trimmed_lines)
    return {
        "candidate_name": candidate_name,
        "contact_line": contact_line,
        "greeting": greeting,
        "paragraphs": paragraphs,
        "closing": closing,
    }


def _section_key(line: str) -> str:
    if not line.startswith("#"):
        return ""
    heading = line.lstrip("#").strip().lower()
    aliases = {
        "profile": "profile",
        "professional experience": "experience",
        "experience": "experience",
        "skills": "skills",
        "education": "education",
    }
    return aliases.get(heading, "")


def _extract_headline(line: str) -> str:
    if _section_key(line):
        return ""
    cleaned = _strip_markdown_inline(line.lstrip("#").strip())
    if not cleaned or ":" in cleaned or "|" in cleaned:
        return ""
    return cleaned


def _looks_like_contact(line: str) -> bool:
    lowered = line.lower()
    return "@" in line or "|" in line or "linkedin.com" in lowered


def _parse_experience_section(lines: list[str]) -> list[dict]:
    experience: list[dict] = []
    current: dict | None = None
    entry_pattern = re.compile(r"^\*\*(?P<title>.+?)\*\*\s*\|\s*(?P<company>.+?)\s*\|\s*(?P<dates>.+)$")
    title_only_pattern = re.compile(r"^\*\*(?P<title>.+?)\*\*$")
    company_dates_pattern = re.compile(r"^(?P<company>.+?)\s*\|\s*(?P<dates>.+)$")

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = entry_pattern.match(line)
        if match:
            if current:
                experience.append(current)
            current = {
                "title": _strip_markdown_inline(match.group("title")),
                "company": _strip_markdown_inline(match.group("company")),
                "dates": _strip_markdown_inline(match.group("dates")),
                "bullets": [],
            }
            continue
        title_only_match = title_only_pattern.match(line)
        if title_only_match:
            if current:
                experience.append(current)
            current = {
                "title": _strip_markdown_inline(title_only_match.group("title")),
                "company": "",
                "dates": "",
                "bullets": [],
            }
            continue
        if current is not None and (not current.get("company") or not current.get("dates")) and not line.startswith("- "):
            company_dates_match = company_dates_pattern.match(line)
            if company_dates_match:
                current["company"] = _strip_markdown_inline(company_dates_match.group("company"))
                current["dates"] = _strip_markdown_inline(company_dates_match.group("dates"))
                continue
        if line.startswith("- ") and current is not None:
            current["bullets"].append(_strip_markdown_inline(line[2:]))
            continue
        if current is not None and current["bullets"]:
            current["bullets"][-1] = f"{current['bullets'][-1]} {_strip_markdown_inline(line)}".strip()
    if current:
        experience.append(current)
    return experience


def _parse_education_section(lines: list[str]) -> list[dict]:
    education: list[dict] = []
    current: dict | None = None
    entry_pattern = re.compile(r"^\*\*(?P<degree>.+?)\*\*\s*\|\s*(?P<school>.+?)\s*\|\s*(?P<dates>.+)$")

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = entry_pattern.match(line)
        if match:
            if current:
                education.append(current)
            current = {
                "degree": _strip_markdown_inline(match.group("degree")),
                "school": _strip_markdown_inline(match.group("school")),
                "dates": _strip_markdown_inline(match.group("dates")),
                "note": "",
            }
            continue
        if current is not None:
            note = _strip_markdown_inline(line.removeprefix("- ").strip())
            current["note"] = f"{current['note']} {note}".strip()
    if current:
        education.append(current)
    return education


def _parse_skills_section(lines: list[str]) -> dict[str, list[str]]:
    skills: dict[str, list[str]] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        cleaned = _strip_markdown_inline(line)
        if ":" not in cleaned:
            continue
        label, value = cleaned.split(":", 1)
        items = [item.strip() for item in value.split(",") if item.strip()]
        skills[label.strip()] = items
    return skills


def _profile_from_contact_line(contact_line: str) -> dict | None:
    if not contact_line:
        return None
    profile: dict[str, str] = {}
    leftovers: list[str] = []
    for raw_part in contact_line.split("|"):
        part = raw_part.strip()
        lowered = part.lower()
        if not part:
            continue
        if "@" in part and "email" not in profile:
            profile["email"] = part
        elif "linkedin.com" in lowered and "linkedin_url" not in profile:
            profile["linkedin_url"] = part if part.startswith(("http://", "https://")) else f"https://{part}"
        elif re.search(r"\+?\d[\d\s().-]{5,}", part) and "phone" not in profile:
            profile["phone"] = part
        else:
            leftovers.append(part)
    if leftovers:
        profile["location"] = " | ".join(leftovers)
    return profile or None


def _looks_like_signoff(line: str) -> bool:
    normalized = line.lower().rstrip(",")
    return normalized in {"sincerely", "best", "regards", "best regards", "kind regards", "thank you", "thanks"}


def _markdown_paragraphs(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(_strip_markdown_inline(line))
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _strip_markdown_inline(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda match: match.group(1) or match.group(2), cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _render_skills_table(skills: dict) -> str:
    if not skills:
        return "<p>See experience section.</p>"
    rows = []
    for label, items in skills.items():
        val = _e(", ".join(items)) if isinstance(items, list) else _e(str(items))
        rows.append(f'<tr><td class="skills-label">{_e(label)}:</td><td>{val}</td></tr>')
    return f'<table class="skills-table">{"".join(rows)}</table>'


def _render_experience(experience: list[dict]) -> str:
    parts = []
    for job in experience:
        title = _e(job.get("title", ""))
        company = _e(job.get("company", ""))
        dates = _e(job.get("dates", ""))
        bullets = job.get("bullets", [])
        bullet_html = "\n".join(f"<li>{_e(b)}</li>" for b in bullets)
        parts.append(
            f'<div class="exp-block">'
            f'<table class="exp-header"><tr>'
            f'<td><span class="exp-company">{company}</span><br>'
            f'<span class="exp-title">{title}</span></td>'
            f'<td class="exp-dates">{dates}</td>'
            f"</tr></table>\n"
            f"<ul>{bullet_html}</ul>"
            f"</div>"
        )
    return "\n".join(parts)


def _render_education(education: list[dict]) -> str:
    parts = []
    for entry in education:
        degree = _e(entry.get("degree", ""))
        school = _e(entry.get("school", ""))
        dates = _e(entry.get("dates", ""))
        note = _e(entry.get("note", ""))
        header = (
            f'<table class="edu-header"><tr>'
            f"<td><strong>{school}, {degree}</strong></td>"
            f'<td class="edu-dates">{dates}</td>'
            f"</tr></table>"
        )
        if note:
            header += f"\n<ul><li>{note}</li></ul>"
        parts.append(f'<div class="edu-block">{header}</div>')
    return "\n".join(parts)


def _e(text: str) -> str:
    return html_module.escape(text)


@lru_cache(maxsize=1)
def _resume_template() -> Template:
    try:
        template_text = (
            resources.files("openclaw_jobsearch")
            .joinpath("templates/resume.html")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError):
        template_text = _DEFAULT_RESUME_TEMPLATE
    return Template(template_text)
