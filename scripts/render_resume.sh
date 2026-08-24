#!/usr/bin/env bash
# Render the master resume markdown to PDF (and HTML) via the WeasyPrint template.
# DYLD_FALLBACK_LIBRARY_PATH is required so cffi can dlopen Homebrew's pango/cairo.
set -euo pipefail

cd "$(dirname "$0")/.."

SOURCE="${1:-candidate/resume_master.md}"
OUTPUT="${2:-candidate/resume_master.pdf}"

DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib PYTHONPATH=src .venv/bin/python - "$SOURCE" "$OUTPUT" <<'PY'
import sys
from pathlib import Path

from openclaw_jobsearch.pdf import html_to_pdf, render_resume_html_from_markdown

source, output = Path(sys.argv[1]), Path(sys.argv[2])
html = render_resume_html_from_markdown(source.read_text())
output.with_suffix(".html").write_text(html)
html_to_pdf(html, output)
print(f"Rendered {source} -> {output}")
PY
