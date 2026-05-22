import json
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
import markdown

# ---- CONFIG ----
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"
OUTPUT_DIR = REPO_ROOT / "outputs/by_community/nm-albuquerque"

S6_JSON_PATH = OUTPUT_DIR / "s6_brief_growth_mode2.json"
MD_OUTPUT_PATH = OUTPUT_DIR / "nm-albuquerque_growth_mode2.md"
HTML_OUTPUT_PATH = OUTPUT_DIR / "nm-albuquerque_growth_mode2.html"

# ---- LOAD JSON ----
with open(S6_JSON_PATH, "r") as f:
    data = json.load(f)

# ---- LOAD TEMPLATE ----
env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
template = env.get_template("strategic_brief.md.j2")

# ---- RENDER MARKDOWN ----
md_content = template.render(**data)
MD_OUTPUT_PATH.write_text(md_content)
print(f"[OK] Markdown written to {MD_OUTPUT_PATH}")

# ---- CONVERT TO HTML ----
html_content = markdown.markdown(md_content, extensions=["tables", "fenced_code"])
HTML_OUTPUT_PATH.write_text(html_content)
print(f"[OK] HTML written to {HTML_OUTPUT_PATH}")
