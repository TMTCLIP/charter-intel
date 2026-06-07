"""
pipeline/s7_render.py
Stage 7: Output Rendering

Renders the structured brief JSON into human-readable markdown (or PDF).
NO LLM CALLS. Pure Jinja2 template rendering from structured data.

INPUT:  data/cache/synthesis/{state}/{community_id}/s6_brief_{preset}_mode{mode}.json
OUTPUT: outputs/by_community/{community_id}/{community_id}_{preset}_mode{mode}_{date}.md
"""
from __future__ import annotations
import datetime
import json
import logging
import os
import yaml
import time
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import OutputMode, PipelineConfig, StageResult, StageStatus, timestamp_now, today_str
from pipeline.utils.token_logger import token_logger

logger = logging.getLogger(__name__)

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

    # Scan mode: skip individual brief rendering — pass S6 scan result through.
    # The comparison table is rendered by render_scan_table() after all cities complete.
    if config.mode == OutputMode.SCAN:
        return _handle_scan_passthrough(community_id, state, config, previous_result)

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

    # Fix 1: LLM sometimes zeroes weights for excluded dimensions in dimension_table.
    # Restore original weights from the S5 scorecard so the table is accurate.
    _patch_excluded_weights(brief, state, community_id)

    # Check whether S4 promoted political_climate_index from a stale cache entry.
    pci_promoted = False
    s4_path = f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"
    if os.path.exists(s4_path):
        try:
            with open(s4_path) as _f:
                pci_promoted = json.load(_f).get("pci_promoted_from_cache", False)
        except Exception:
            pass

    # Shared Jinja environment
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(default=True, default_for_string=True)
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
    # Route to specialised templates before falling back to the mode default.
    _market_type = brief.get("market_type", "established")
    _verdict     = brief.get("verdict", "ELIGIBLE")
    if _market_type == "greenfield":
        html_template_name = "greenfield_brief.html.j2"
    elif _verdict == "INELIGIBLE":
        html_template_name = "ineligible_brief.html.j2"
    else:
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
        rendered_html = env.get_template(html_template_name).render(
            brief=brief,
            debug=debug,
            schools=brief.get("top_charter_schools", []),
            pci_promoted=pci_promoted,
        )
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


def _patch_excluded_weights(brief: dict, state: str, community_id: str) -> None:
    """Restore original weights for excluded dimension rows in dimension_table.

    The LLM sometimes sets weight=0 for excluded dimensions (those where
    used_default is True) because it interprets 'excluded from composite' as
    'weight = 0'.  The display should show the original design weight so readers
    can see what the dimension *would have* been worth — the footnote already
    explains that it is redistributed away.

    Silently no-ops when the scorecard file is absent (e.g. unit tests, dry runs).
    """
    dim_table = (brief.get("scorecard_summary") or {}).get("dimension_table")
    if not dim_table:
        return

    sc_path = f"data/cache/community/{state.lower()}/{community_id}/s5_scorecard.json"
    if not os.path.exists(sc_path):
        return

    with open(sc_path) as f:
        sc_dims = json.load(f).get("dimensions", {})

    for row in dim_table:
        if row.get("used_default") and row.get("weight") == 0:
            dim_name = row.get("dimension")
            original = sc_dims.get(dim_name, {}).get("weight")
            if original is not None:
                row["weight"] = original


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
            if row.get("used_default"):
                score_cell = f"{row.get('score', '5.0')}*"
                conf_cell = "(excluded — no data)"
            else:
                score_cell = str(row.get("score", "—"))
                conf_cell = row.get("confidence", "—")
            lines.append(
                f"| {row.get('display_name', row.get('dimension'))} "
                f"| {score_cell} "
                f"| {int(row.get('weight', 0) * 100)}% "
                f"| {conf_cell} "
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


# ─────────────────────────────────────────────
# SCAN MODE — per-city passthrough
# ─────────────────────────────────────────────

def _handle_scan_passthrough(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult],
) -> StageResult:
    """Pass the S6 scan result through without rendering a per-city brief.

    The comparison table is rendered by render_scan_table() in main.py after
    all communities complete.  output_data is preserved so main.py can collect
    scan results from stage results without a second disk read.
    """
    scan_data = None
    if previous_result and previous_result.output_data:
        scan_data = previous_result.output_data
    else:
        scan_path = (
            f"data/cache/synthesis/{state.lower()}/{community_id}/"
            f"s6_scan_{config.preset.value}.json"
        )
        if os.path.exists(scan_path):
            with open(scan_path) as f:
                scan_data = json.load(f)

    return StageResult(
        stage_id=STAGE_ID,
        community_id=community_id,
        state=state,
        status=StageStatus.SUCCESS,
        output_data=scan_data,
    )


# ─────────────────────────────────────────────
# SCAN MODE — statewide comparison table
# ─────────────────────────────────────────────

def render_scan_table(
    scan_results: list[dict],
    config: PipelineConfig,
) -> Optional[str]:
    """Render the ranked comparison table HTML from all city scan results.

    Called from main.py after the per-community loop completes.
    Writes outputs/by_state/{state}/{state}_scan_{date}.html.
    Returns the output path, or None if the template is missing.
    """
    sorted_results = sorted(
        scan_results,
        key=lambda r: (r.get("composite") or 0.0),
        reverse=True,
    )
    for i, r in enumerate(sorted_results, 1):
        r["rank"] = i

    # Enrich each result with data_coverage fields from the S5 scorecard.
    # S5 is authoritative; this also backfills old S6 scan caches that predate
    # the data_coverage feature.
    state_code = config.state.lower()
    for r in sorted_results:
        cid = r.get("community_id")
        if not cid:
            continue
        sc_path = f"data/cache/community/{state_code}/{cid}/s5_scorecard.json"
        if not os.path.exists(sc_path):
            continue
        try:
            with open(sc_path) as _f:
                sc = json.load(_f)
            r["data_coverage_pct"] = sc.get("data_coverage_pct")
            r["data_coverage_tier"] = sc.get("data_coverage_tier")
            # Backfill override_flags for scan JSONs that predate Task 4
            if "override_flags" not in r:
                r["override_flags"] = sc.get("override_flags", [])
        except Exception:
            pass

    # Enrich with community_metadata (special_considerations etc.) from states.yaml.
    try:
        with open("config/states.yaml") as _f:
            states_cfg = yaml.safe_load(_f)
        community_meta = (
            states_cfg.get(config.state, {})
                      .get("community_metadata", {})
        )
        for r in sorted_results:
            cid = r.get("community_id")
            if cid and cid in community_meta:
                r.update(community_meta[cid])
    except Exception:
        pass

    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape(default=True, default_for_string=True)
    )

    date_str = datetime.date.today().strftime("%Y%m%d")
    out_dir = f"outputs/by_state/{state_code}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = f"{out_dir}/{state_code}_scan_{date_str}.html"

    template_name = "scan_results.html.j2"
    if not os.path.exists(os.path.join("templates", template_name)):
        logger.warning("Scan table template not found: templates/%s", template_name)
        return None

    insufficient_count = sum(
        1 for r in sorted_results if r.get("data_coverage_tier") == "INSUFFICIENT"
    )

    rendered = env.get_template(template_name).render(
        results=sorted_results,
        state=config.state,
        preset=config.preset.value,
        generated_at=timestamp_now(),
        date_str=date_str,
        city_count=len(sorted_results),
    )

    with open(out_path, "w") as f:
        f.write(rendered)

    if insufficient_count:
        logger.info(
            "Scan table: %d cities excluded from ranking (insufficient data coverage)",
            insufficient_count,
        )
    logger.info("Scan table written to %s", out_path)
    print(f"\n  Scan table: {out_path}\n")
    return out_path
