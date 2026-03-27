from __future__ import annotations

import html as html_module
from pathlib import Path

_RESUME_CSS = """
@page { size: letter; margin: 0.50in; }
body {
    font-family: Arial, sans-serif;
    font-size: 10pt;
    color: #222;
    line-height: 100%;
    margin: 0;
    padding: 0;
}
h1 {
    font-size: 23pt;
    font-weight: normal;
    margin: 0;
    color: #5B9BD5;
}
.subtitle {
    font-size: 12pt;
    font-weight: normal;
    color: #5B9BD5;
    margin: 0 0 10pt 0;
}
.header-table { width: 100%; border-collapse: collapse; margin-bottom: 0; }
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
"""

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

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><style>{_RESUME_CSS}</style></head>
<body>
<table class="header-table"><tr>
<td><h1>{name}</h1><p class="subtitle">{headline}</p></td>
<td class="header-right">{contact_html}</td>
</tr></table>
<h2>Profile</h2>
<p class="summary">{summary}</p>
<h2>Professional Experience</h2>
{experience_html}
<h2>Skills</h2>
{skills_html}
<h2>Education</h2>
{education_html}
</body>
</html>"""


def render_cover_letter_html(cl_data: dict) -> str:
    name = _e(cl_data.get("candidate_name", ""))
    contact = _e(cl_data.get("contact_line", ""))
    paragraphs = cl_data.get("paragraphs", [])
    closing = _e(cl_data.get("closing", f"Sincerely,\n{name}"))

    body_html = "\n".join(f"<p>{_e(p)}</p>" for p in paragraphs)
    closing_html = closing.replace("\n", "<br>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><style>{_COVER_LETTER_CSS}</style></head>
<body>
<h1>{name}</h1>
<p class="contact">{contact}</p>
{body_html}
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
