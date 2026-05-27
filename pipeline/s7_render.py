"""
pipeline/s7_render.py
Stage 7: Output Rendering

Renders the structured brief JSON into human-readable markdown (or PDF).
NO LLM CALLS. Pure Jinja2 template rendering from structured data.

INPUT:  data/cache/synthesis/{state}/{community_id}/s6_brief_{preset}_mode{mode}.json
OUTPUT: outputs/by_community/{community_id}/{community_id}_{preset}_mode{mode}_{date}.md
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import PipelineConfig, StageResult, StageStatus, timestamp_now, today_str
from pipeline.utils.token_logger import token_logger

STAGE_ID = "s7_render"

MODE_TEMPLATES = {
    1: "snapshot.md.j2",
    2: "strategic_brief.md.j2",
    3: "deep_dive.md.j2",
}

MODE_HTML_TEMPLATES = {
    1: "snapshot.html.j2",
    2: "strategic_brief.html.j2",
    3: "deep_dive.html.j2",
}


def run(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    warn_lines: Optional[list[str]] = None,
    **kwargs
) -> StageResult:
    start = time.time()

    # Load brief from S6
    if previous_result and previous_result.output_data:
        brief = previous_result.output_data
    else:
        brief_path = (
            f"data/cache/synthesis/{state.lower()}/{community_id}/"
            f"s6_brief_{config.preset.value}_mode{config.mode.value}.json"
        )
        if not os.path.exists(brief_path):
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.ERROR,
                errors=[f"Brief not found at {brief_path}. Run S6 first."]
            )
        with open(brief_path) as f:
            brief = json.load(f)

    # Shared Jinja environment
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape([])
    )

    out_dir = f"outputs/by_community/{community_id}"
    os.makedirs(out_dir, exist_ok=True)
    warnings = []

    # --- Markdown output ---
    template_name = MODE_TEMPLATES.get(config.mode.value, "strategic_brief.md.j2")
    template_path = os.path.join("templates", template_name)

    if not os.path.exists(template_path):
        rendered_md = _render_fallback(brief)
        warnings.append(
            f"Template {template_path} not found. Used fallback renderer. "
            f"Create templates/{template_name} for formatted output."
        )
    else:
        rendered_md = env.get_template(template_name).render(brief=brief)

    md_filename = f"{community_id}_{config.preset.value}_mode{config.mode.value}_{today_str()}.md"
    out_path = os.path.join(out_dir, md_filename)
    with open(out_path, "w") as f:
        f.write(rendered_md)

    # --- HTML output ---
    html_template_name = MODE_HTML_TEMPLATES.get(config.mode.value, "strategic_brief.html.j2")
    html_template_path = os.path.join("templates", html_template_name)

    if os.path.exists(html_template_path):
        token_rows = token_logger.get_community_summary(community_id)
        debug = {
            "run_id": token_logger.run_id,
            "timestamp": timestamp_now(),
            "depth": config.depth,
            "token_rows": token_rows,
            "warn_lines": warn_lines or [],
        }
        rendered_html = env.get_template(html_template_name).render(brief=brief, debug=debug)
        html_filename = f"{community_id}_{config.preset.value}_mode{config.mode.value}.html"
        html_out_path = os.path.join(out_dir, html_filename)
        with open(html_out_path, "w") as f:
            f.write(rendered_html)
    else:
        warnings.append(f"HTML template {html_template_path} not found. Skipped HTML output.")

    return StageResult(
        stage_id=STAGE_ID, community_id=community_id, state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        warnings=warnings, duration_seconds=round(time.time() - start, 2)
    )


def _render_fallback(brief: dict) -> str:
    """
    Minimal fallback renderer when no Jinja template exists.
    Produces readable markdown from brief JSON.
    """
    lines = [
        f"# {brief.get('community_name', brief.get('community_id'))} — "
        f"{brief.get('mode_label', 'Strategic Brief')}",
        f"**Classification:** {brief.get('classification', '—')}  "
        f"**Score:** {brief.get('composite_score', '—')}/10  "
        f"**Confidence:** {brief.get('confidence_overall', '—')}",
        f"*Data through {brief.get('data_through', '—')} · "
        f"Generated {brief.get('generated_at', '—')[:10]}*",
        "",
        "## Executive Snapshot",
        brief.get("executive_snapshot", "_Not available_"),
        "",
    ]

    # Scorecard
    scorecard = brief.get("scorecard_summary", {})
    if scorecard:
        lines += [
            "## Scorecard",
            f"**Composite:** {scorecard.get('composite_score', '—')}/10  "
            f"**Tier:** {scorecard.get('tier_display_label', '—')}",
            "",
            "| Dimension | Score | Weight | Confidence | Driver |",
            "|---|---|---|---|---|",
        ]
        for row in scorecard.get("dimension_table", []):
            lines.append(
                f"| {row.get('display_name', row.get('dimension'))} "
                f"| {row.get('score', '—')} "
                f"| {int(row.get('weight', 0) * 100)}% "
                f"| {row.get('confidence', '—')} "
                f"| {row.get('driver', '—')} |"
            )
        # Override flags
        for flag in scorecard.get("override_flags", []):
            lines.append(
                f"\n- {flag.get('visual', '⚠️')} **{flag.get('flag')}** — "
                f"{flag.get('triggered_by')}"
            )
        lines.append("")

    # Recommendations
    recs = brief.get("recommendations", [])
    if recs:
        lines += ["## Recommendations", ""]
        for i, rec in enumerate(recs, 1):
            lines += [
                f"**{i}. {rec.get('action', '—')}**",
                rec.get("rationale", ""),
                f"*Evidence:* {rec.get('evidence_summary', '—')}  "
                f"*Risk:* {rec.get('primary_risk', '—')}  "
                f"*Confidence:* {rec.get('confidence', '—')}",
                "",
            ]

    # Top Charter Schools
    schools = brief.get("top_charter_schools", [])
    if schools:
        n = len(schools)
        if n == 1:
            schools_header = "## Top Charter School in Market"
        elif n < 5:
            schools_header = f"## Top {n} Charter Schools in Market"
        else:
            schools_header = "## Top 5 Charter Schools in Market"
        lines += [
            schools_header,
            "",
            "> ⚠️ Contact information requires human verification before external use.",
            "",
        ]
        for i, s in enumerate(schools, 1):
            ela  = f"{s['ela_proficiency_pct']}%" if s.get("ela_proficiency_pct") is not None else "—"
            math = f"{s['math_proficiency_pct']}%" if s.get("math_proficiency_pct") is not None else "—"
            lines += [
                f"**{i}. {s.get('school_name', '—')}** "
                f"| {s.get('grades_served', '—')} "
                f"| {s.get('academic_model', '—')} "
                f"| ELA {ela} / Math {math}",
                s.get("notable_note", ""),
                f"*{s.get('contact_phone', '')}  {s.get('contact_email', '')}*  "
                f"*Confidence: {s.get('confidence', '—')}*",
                "",
            ]

    # Local Authorizers
    authorizers = brief.get("local_authorizers", [])
    if authorizers:
        lines += [
            "## Local Authorizers",
            "",
            "> ⚠️ Contact information requires human verification before external use.",
            "",
        ]
        for a in authorizers:
            lines += [
                f"**{a.get('authorizer_name', '—')}** ({a.get('type', '—')})"
                f" — {a.get('num_schools_in_community', '?')} schools in community",
                a.get("reputation_note", ""),
                f"*{a.get('contact_phone', '')}  {a.get('contact_email', '')}*  "
                f"*Confidence: {a.get('confidence', '—')}*",
                "",
            ]

    # Needs Verification
    nv = brief.get("needs_verification", [])
    if nv:
        lines += ["## Needs Verification", ""]
        for item in nv:
            lines.append(
                f"- **{item.get('claim', '—')}** — {item.get('reason', '—')} "
                f"*(Impact: {item.get('impact_if_wrong', '—')})*"
            )
        lines.append("")

    # Sources
    sources = brief.get("sources", [])
    if sources:
        lines += ["## Sources", ""]
        for s in sources:
            url_part = f" · {s['url']}" if s.get("url") else ""
            lines.append(
                f"[{s['ref_num']}] {s.get('title', '—')} · "
                f"{s.get('source_class', '—')}{url_part}"
            )
        lines.append("")

    # Disclosure
    disclosure = brief.get("disclosure", "")
    if disclosure:
        lines += ["---", f"*{disclosure}*"]

    # Audit warning
    if brief.get("audit_passed") is False:
        flags = brief.get("audit_flags", [])
        lines += [
            "",
            f"> ⚠️ **Audit flag:** {len(flags)} claim(s) were stripped or "
            f"moved to Needs Verification by the hallucination audit. "
            f"Review before external use."
        ]

    return "\n".join(lines)
