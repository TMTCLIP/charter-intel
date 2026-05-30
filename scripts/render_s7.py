#!/usr/bin/env python3
"""
scripts/render_s7.py
Standalone re-render of strategic_brief.html.j2 from a cached S6 brief JSON.

Usage (run from repo root):
  python3 scripts/render_s7.py [community_id] [preset]

  community_id — e.g. nm-albuquerque (default: nm-albuquerque)
  preset       — e.g. growth, maturity_adjusted (default: growth)

The template uses brief.X attribute access throughout. Pass the full S6 JSON
as brief=data; do NOT unpack with **data or pass individual flat variables.
"""
import json
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates"

# ---- CONFIG ----
community_id = sys.argv[1] if len(sys.argv) > 1 else "nm-albuquerque"
preset       = sys.argv[2] if len(sys.argv) > 2 else "growth"

state = community_id.split("-")[0]  # e.g. "nm"

S6_JSON_PATH = (
    REPO_ROOT / "data" / "cache" / "synthesis" / state / community_id
    / f"s6_brief_{preset}_mode2.json"
)

OUTPUT_DIR   = REPO_ROOT / "outputs" / "by_community" / community_id
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
HTML_OUTPUT  = OUTPUT_DIR / f"{community_id}_{preset}_mode2.html"

# ---- LOAD S6 BRIEF ----
if not S6_JSON_PATH.exists():
    print(f"[ERROR] S6 brief not found: {S6_JSON_PATH}")
    print(f"  Run the pipeline first: python3 main.py '{community_id}' --preset {preset}")
    sys.exit(1)

with open(S6_JSON_PATH) as f:
    brief = json.load(f)

# ---- RENDER ----
# The template uses brief.X throughout (e.g. brief.community_name, brief.state).
# Pass the full S6 dict as brief= so all expressions resolve correctly.
env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False)

debug = {
    "run_id":     "manual-render",
    "timestamp":  "",
    "depth":      "standard",
    "token_rows": [],
    "warn_lines": [],
}

rendered = env.get_template("strategic_brief.html.j2").render(brief=brief, debug=debug)
HTML_OUTPUT.write_text(rendered, encoding="utf-8")
print(f"[OK] HTML written to {HTML_OUTPUT}")
