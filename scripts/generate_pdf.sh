#!/usr/bin/env bash
# generate_pdf.sh — convert the most recent markdown brief to a browser-printable HTML file
#
# Usage: bash scripts/generate_pdf.sh "Santa Fe"
#        bash scripts/generate_pdf.sh nm-santa-fe
#
# Accepts either a plain city name or an already-slugified ID.
# Opens the result in the default browser. Use File > Print > Save as PDF.
#
# Run from repo root or any directory — script resolves repo root automatically.

STATE="nm"  # change here when adding a second state

# ── Input validation ──────────────────────────────────────────────────────────
if [[ -z "${1-}" ]]; then
  echo "Usage: bash scripts/generate_pdf.sh \"City Name\"  OR  bash scripts/generate_pdf.sh nm-city-name"
  exit 1
fi

# ── Slug derivation ───────────────────────────────────────────────────────────
INPUT="$1"
if [[ "$INPUT" == "${STATE}-"* ]]; then
  SLUG="$INPUT"
else
  SLUG="${STATE}-$(echo "$INPUT" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
fi

# ── Resolve repo root ─────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

OUTPUT_DIR="outputs/by_community/${SLUG}"

if [[ ! -d "$OUTPUT_DIR" ]]; then
  echo "Error: no output directory for '${SLUG}'"
  echo "  Expected: ${REPO_ROOT}/${OUTPUT_DIR}"
  exit 1
fi

# ── Find most recent markdown file ────────────────────────────────────────────
MD_FILE=$(ls -t "${OUTPUT_DIR}"/*.md 2>/dev/null | head -1)

if [[ -z "$MD_FILE" ]]; then
  echo "Error: no markdown output found in ${OUTPUT_DIR}"
  exit 1
fi

# ── Derive output HTML path (strip date suffix, add _print) ──────────────────
BASENAME=$(basename "$MD_FILE" .md | sed 's/_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$//')
HTML_FILE="${OUTPUT_DIR}/${BASENAME}_print.html"

# ── Convert markdown → styled HTML ───────────────────────────────────────────
python3 - "$MD_FILE" "$HTML_FILE" <<'PYEOF'
import sys
import markdown

md_path, html_path = sys.argv[1], sys.argv[2]
md_text = open(md_path, encoding="utf-8").read()
body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
title = md_path.split("/")[-1].replace("_", " ").replace(".md", "")

# CSS matches strategic_brief.html.j2 exactly, plus print media overrides
css = """
    body { font-family: Georgia, serif; max-width: 860px; margin: 2em auto; padding: 0 1.5em; line-height: 1.6; color: #222; }
    h1 { font-size: 1.6em; margin-bottom: 0.2em; }
    h2 { font-size: 1.2em; border-bottom: 1px solid #ccc; padding-bottom: 0.2em; margin-top: 1.8em; }
    h3 { font-size: 1em; margin-bottom: 0.3em; }
    table { border-collapse: collapse; width: 100%; margin: 1em 0; }
    th, td { border: 1px solid #ccc; padding: 0.45em 0.65em; text-align: left; }
    th { background: #f2f2f2; font-weight: bold; }
    td:nth-child(2), td:nth-child(3) { text-align: right; }
    code { background: #f4f4f4; padding: 0.15em 0.35em; border-radius: 3px; font-size: 0.9em; }
    hr { border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }
    ul { margin: 0.4em 0 0.8em 1.4em; padding: 0; }
    li { margin-bottom: 0.3em; }
    blockquote { border-left: 4px solid #e67e22; margin: 1em 0; padding: 0.5em 1em; background: #fdf6ec; }
    .meta { color: #555; font-size: 0.92em; }
    .disclosure { color: #666; font-size: 0.88em; font-style: italic; margin-top: 2em; }
    @media print {
      body { margin: 0; padding: 1em; max-width: 100%; }
      a { color: inherit; text-decoration: none; }
      h2 { page-break-after: avoid; }
      table { page-break-inside: avoid; }
    }
"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>{css}</style>
</head>
<body>
{body}
</body>
</html>"""

open(html_path, "w", encoding="utf-8").write(html)
PYEOF

echo "HTML ready: ${HTML_FILE}"
echo "In browser: File > Print > Save as PDF"
open "${HTML_FILE}" 2>/dev/null || echo "(Could not auto-open — open manually: ${HTML_FILE})"
