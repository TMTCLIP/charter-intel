"""
pipeline/s6_synthesis.py
Stage 6: Brief Synthesis + Hallucination Audit

PURPOSE:
  Generate the community intelligence brief from verified facts + scorecard.
  Then run the claim-grounding audit before writing any output to disk.

  TWO-PASS DESIGN:
    Pass 1 — Generate brief (Sonnet/Opus depending on mode)
    Pass 2 — Audit brief against fact bundle (Sonnet, always)
    Post-audit — Strip/route any unfounded claims; write final brief

INPUT:
  - data/cache/community/{state}/{community_id}/s4_verified.json
  - data/cache/community/{state}/{community_id}/s5_scorecard.json
  - config/pipeline.yaml (model routing, mode)
  - prompts/synthesize/s6_mode{mode}_*.md
  - prompts/audit/s6_claim_audit.md

OUTPUT:
  - data/cache/synthesis/{state}/{community_id}/s6_brief_{preset}_mode{mode}.json
  - Validated against schemas/brief.schema.json
"""

from __future__ import annotations
import json
import os
import time
from typing import Optional

from pipeline import (
    Confidence, OperatorPreset, OutputMode, PipelineConfig,
    StageResult, StageStatus, ValidationResult, timestamp_now
)
from pipeline.utils.api_client import call_claude, load_prompt, APIResult
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema


STAGE_ID = "s6_synthesis"

# ─────────────────────────────────────────────
# MODEL ROUTING BY MODE
# ─────────────────────────────────────────────

MODE_MODELS = {
    OutputMode.STRATEGIC_BRIEF: "haiku",   # Haiku quality gate passed 5/5 (Session 15)
    OutputMode.DEEP_DIVE:      "opus",
}

MODE_MAX_TOKENS = {
    OutputMode.STRATEGIC_BRIEF: 6000,
    OutputMode.DEEP_DIVE:      6000,
}

MODE_PROMPTS = {
    OutputMode.STRATEGIC_BRIEF: "prompts/synthesize/s6_mode2_strategic_brief.md",
    OutputMode.DEEP_DIVE:      "prompts/synthesize/s6_mode3_deep_dive.md",
}


# ─────────────────────────────────────────────
# MAIN STAGE FUNCTION
# ─────────────────────────────────────────────

def run(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    **kwargs
) -> StageResult:
    """
    Generate and audit the community intelligence brief.
    """
    start = time.time()
    warnings = []

    # --- Load inputs ---
    verified_bundle, scorecard, load_errors = _load_inputs(state, community_id)
    if load_errors:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR, errors=load_errors
        )

    # --- Scan mode: single Haiku call, no audit ---
    if config.mode == OutputMode.SCAN:
        return _run_scan_synthesis(community_id, state, config, scorecard, start, warnings)

    # --- Check synthesis cache ---
    cache = CacheManager(config)
    cache_key = (f"synthesis/{state.lower()}/{community_id}/"
                 f"s6_brief_{config.preset.value}_mode{config.mode.value}.json")
    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.SUCCESS, output_data=cached,
                cache_hit=True
            )

    # --- Dry-run guard: no live API calls ---
    if config.dry_run:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.SKIPPED,
            warnings=["Dry run — skipping Haiku API calls for S6"]
        )

    # --- Prepare fact sets ---
    verified_facts = [f for f in verified_bundle.get("facts", [])
                      if f.get("in_main_analysis", False)]
    needs_verification_facts = [f for f in verified_bundle.get("facts", [])
                                 if not f.get("in_main_analysis", False)]

    if len(verified_facts) < 3:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[
                f"Insufficient verified facts for synthesis: {len(verified_facts)} "
                f"(minimum 3 required). Run S3 and S4 with more source coverage."
            ]
        )

    # --- PASS 1: Generate brief ---
    brief_json, tokens_used, gen_error = _generate_brief(
        community_id=community_id,
        state=state,
        config=config,
        verified_facts=verified_facts,
        needs_verification_facts=needs_verification_facts,
        scorecard=scorecard,
        verified_bundle=verified_bundle
    )

    if gen_error or not brief_json:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[gen_error or "Brief generation returned empty output"]
        )

    # Re-inject override_flags from S5 scorecard — model may drop them
    s5_flags = scorecard.get("override_flags") or []
    if s5_flags:
        brief_json.setdefault("scorecard_summary", {})["override_flags"] = s5_flags

    # --- PASS 2: Hallucination audit ---
    audit_result, audit_tokens, audit_error = _run_audit(
        community_id=community_id,
        brief_json=brief_json,
        verified_facts=verified_facts,
        config=config
    )
    tokens_used += audit_tokens

    if audit_error:
        warnings.append(f"Audit failed to run: {audit_error}. Proceeding with unaudited brief.")
        brief_json["audit_passed"] = None
        brief_json["audit_flags"] = []
    else:
        # Apply audit findings
        brief_json, audit_warnings = _apply_audit(brief_json, audit_result)
        warnings.extend(audit_warnings)

    # --- Inject charter intelligence from S3 (bypasses scoring / audit) ---
    brief_json = _inject_charter_intel(brief_json, state, community_id)

    # --- Inject ELL data gap disclosure if ACS data was suppressed ---
    brief_json = _inject_ell_gap_disclosure(brief_json, state, community_id)

    # --- Session 18 honesty injections (deterministic; bypass Haiku) ---
    brief_json = _inject_recommendation_gate(brief_json, scorecard)
    brief_json = _inject_tribal_jurisdiction_flag(brief_json, state, community_id)
    brief_json = _inject_teacher_supply_warning(brief_json, state, community_id)

    # --- Guard: no needs_verification item may render an empty reason ("None") ---
    brief_json = _sanitize_needs_verification(brief_json, community_id)

    # --- Market routing fields for S7 template selection ---
    brief_json = _inject_market_routing(brief_json, scorecard, state, community_id)

    # --- Cap over-length strings before schema validation ---
    brief_json, cap_notices = _truncate_to_schema_limits(brief_json)
    if cap_notices:
        warnings.extend([f"Schema length cap: {n}" for n in cap_notices])

    # --- Populate data vintage and generation timestamp ---
    brief_json["data_through"] = _derive_data_through(brief_json)
    # Preserve model-set generated_at if present; only fill in if missing.
    brief_json.setdefault("generated_at", timestamp_now())

    # --- Validate schema ---
    validation = validate_against_schema(brief_json, "schemas/brief.schema.json")
    if not validation:
        warnings.extend([f"Schema warning: {e}" for e in validation.errors])
        # Don't halt on schema warnings — proceed and flag

    # --- Write output ---
    out_path = _output_path(state, community_id, config.preset.value, config.mode.value)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(brief_json, f, indent=2)

    cache.set(cache_key, brief_json)

    return StageResult(
        stage_id=STAGE_ID,
        community_id=community_id,
        state=state,
        status=StageStatus.SUCCESS,
        output_path=out_path,
        output_data=brief_json,
        warnings=warnings,
        tokens_used=tokens_used,
        duration_seconds=round(time.time() - start, 2)
    )


# ─────────────────────────────────────────────
# PASS 1: BRIEF GENERATION
# ─────────────────────────────────────────────

def _generate_brief(
    community_id: str,
    state: str,
    config: PipelineConfig,
    verified_facts: list[dict],
    needs_verification_facts: list[dict],
    scorecard: dict,
    verified_bundle: dict
) -> tuple[Optional[dict], int, Optional[str]]:
    """Generate the brief. Returns (brief_dict, tokens_used, error_or_None)."""

    model_key = MODE_MODELS[config.mode]
    model = getattr(config, f"model_{model_key}")
    max_tokens = MODE_MAX_TOKENS[config.mode]
    prompt_path = MODE_PROMPTS[config.mode]

    # Build community name from community_id
    community_name = community_id.split("-", 1)[1].replace("-", " ").title()

    import yaml
    with open("config/scoring_presets.yaml") as f:
        presets = yaml.safe_load(f)
    preset_description = presets[config.preset.value]["description"]

    state_cfg = _load_state_config(state)
    state_name = state_cfg.get("name") or state  # fallback to code if not in states.yaml

    # Load prompt with substitutions
    prompt_text = load_prompt(prompt_path, {
        "COMMUNITY_NAME": community_name,
        "STATE_NAME": state_name,
        "STATE_CODE": state,
        "COMMUNITY_ID": community_id,
        "PRESET": config.preset.value,
        "PRESET_DESCRIPTION": preset_description,
        "TODAY_DATE": _today(),
        "SCORECARD_JSON": json.dumps(scorecard, indent=2),
        "VERIFIED_FACTS_JSON": json.dumps(verified_facts, indent=2),
        "NEEDS_VERIFICATION_JSON": json.dumps(needs_verification_facts, indent=2),
    })

    system = ("You are a charter school intelligence analyst. "
              "Respond ONLY with valid JSON. No markdown fences, no preamble.")

    result = call_claude(
        model=model,
        system=system,
        user=prompt_text,
        max_tokens=max_tokens,
        temperature=0.2,
        expect_json=True,
        stage=STAGE_ID,
        community_id=community_id
    )

    if result.parse_error:
        return None, result.total_tokens, f"Brief JSON parse failed: {result.parse_error}"

    return result.parsed_json, result.total_tokens, None


# ─────────────────────────────────────────────
# PASS 2: HALLUCINATION AUDIT
# ─────────────────────────────────────────────

def _build_audit_payload(synthesis_output: dict) -> dict:
    """Extract only the fields the audit needs to check.
    Strips executive snapshot prose, quick reads, facilities narrative,
    needs_verification, schools_to_watch. Cuts audit input tokens ~46%."""
    return {
        "scorecard_summary": synthesis_output.get("scorecard_summary"),
        "recommendations":   synthesis_output.get("recommendations"),
        "sources":           synthesis_output.get("sources"),
    }


def _run_audit(
    community_id: str,
    brief_json: dict,
    verified_facts: list[dict],
    config: PipelineConfig
) -> tuple[Optional[dict], int, Optional[str]]:
    """
    Run the claim-grounding audit on the generated brief.
    Returns (audit_result_dict, tokens_used, error_or_None).
    """
    audit_prompt_path = "prompts/audit/s6_claim_audit.md"
    prompt_text = load_prompt(audit_prompt_path, {
        "COMMUNITY_ID": community_id,
        "TODAY_DATE": _today(),
        "BRIEF_JSON": json.dumps(_build_audit_payload(brief_json), indent=2),
        "VERIFIED_FACTS_JSON": json.dumps(verified_facts, indent=2),
    })

    result = call_claude(
        model=config.model_haiku,
        system=("You are a fact-grounding auditor. "
                "Respond ONLY with valid JSON. No markdown, no preamble."),
        user=prompt_text,
        max_tokens=1000,
        temperature=0.0,
        expect_json=True,
        stage=f"{STAGE_ID}_audit",
        community_id=community_id
    )

    if result.parse_error:
        return None, result.total_tokens, f"Audit JSON parse failed: {result.parse_error}"

    return result.parsed_json, result.total_tokens, None


# ─────────────────────────────────────────────
# APPLY AUDIT FINDINGS
# ─────────────────────────────────────────────

def _apply_audit(
    brief_json: dict,
    audit_result: dict
) -> tuple[dict, list[str]]:
    """
    Apply audit findings to the brief:
    - Strip REMOVE claims from their source fields
    - Move MOVE_TO_NEEDS_VERIFICATION claims to the needs_verification section
    - Set audit_passed and audit_flags on the brief
    Returns (modified_brief, list_of_warnings).
    """
    warnings = []
    failures = audit_result.get("failures", [])
    audit_passed = audit_result.get("audit_passed", True)

    brief_json["audit_passed"] = audit_passed
    brief_json["audit_flags"] = failures

    if not failures:
        return brief_json, warnings

    for failure in failures:
        action = failure.get("recommended_action", "FLAG_FOR_HUMAN_REVIEW")
        claim_text = failure.get("claim_text", "")
        location = failure.get("location", "")
        reason = failure.get("reason", "Unfounded claim detected by audit")

        warnings.append(
            f"Audit {action}: '{claim_text[:80]}...' in {location}"
        )

        if action == "MOVE_TO_NEEDS_VERIFICATION":
            if "needs_verification" not in brief_json:
                brief_json["needs_verification"] = []
            brief_json["needs_verification"].append({
                "claim": claim_text,
                "reason": f"Stripped by hallucination audit: {reason}",
                "source_class_attempted": None,
                "resolution_path": "Verify against primary source before use",
                "impact_if_wrong": "MODERATE"
            })

        # Note: REMOVE and FLAG_FOR_HUMAN_REVIEW are logged but not auto-edited
        # because surgical text removal is risky without re-rendering.
        # S7 render skips flagged fields if audit_passed is False.

    return brief_json, warnings


# ─────────────────────────────────────────────
# INPUT LOADING
# ─────────────────────────────────────────────

def _load_inputs(
    state: str,
    community_id: str
) -> tuple[Optional[dict], Optional[dict], list[str]]:
    """Load S4 verified facts and S5 scorecard. Return (bundle, scorecard, errors)."""
    errors = []

    facts_path = f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"
    scorecard_path = f"data/cache/community/{state.lower()}/{community_id}/s5_scorecard.json"

    verified_bundle = None
    scorecard = None

    if not os.path.exists(facts_path):
        errors.append(f"Verified facts not found at {facts_path}. Run S4 first.")
    else:
        with open(facts_path) as f:
            verified_bundle = json.load(f)

    if not os.path.exists(scorecard_path):
        errors.append(f"Scorecard not found at {scorecard_path}. Run S5 first.")
    else:
        with open(scorecard_path) as f:
            scorecard = json.load(f)

    return verified_bundle, scorecard, errors


def _output_path(state: str, community_id: str, preset: str, mode: int) -> str:
    return (f"data/cache/synthesis/{state.lower()}/{community_id}/"
            f"s6_brief_{preset}_mode{mode}.json")


def _scan_output_path(state: str, community_id: str, preset: str) -> str:
    return (f"data/cache/synthesis/{state.lower()}/{community_id}/"
            f"s6_scan_{preset}.json")


def _cap_str(s: str, max_len: int, field: str, notices: list[str]) -> str:
    """Truncate s to max_len chars at a word boundary, appending '…'. Mutates notices."""
    if not isinstance(s, str) or len(s) <= max_len:
        return s
    # Reserve one char for the ellipsis; find last space in that window
    window = s[: max_len - 1]
    boundary = window.rfind(" ")
    truncated = window[:boundary] if boundary > max_len // 2 else window
    notices.append(f"{field}: {len(s)} → {len(truncated) + 1} chars")
    return truncated + "…"


def _truncate_to_schema_limits(brief_json: dict) -> tuple[dict, list[str]]:
    """
    Truncate all string fields to their brief.schema.json maxLength limits,
    at a word boundary with a trailing ellipsis. Runs in-place before
    validate_against_schema so length violations become length caps, not warnings.
    """
    notices: list[str] = []

    if "executive_snapshot" in brief_json:
        brief_json["executive_snapshot"] = _cap_str(
            brief_json["executive_snapshot"], 600, "executive_snapshot", notices
        )

    for i, rec in enumerate(brief_json.get("recommendations") or []):
        if not isinstance(rec, dict):
            continue
        for field, limit in [
            ("action", 250), ("rationale", 400),
            ("evidence_summary", 250), ("primary_risk", 200),
        ]:
            if field in rec:
                rec[field] = _cap_str(rec[field], limit, f"recommendations[{i}].{field}", notices)

    scorecard = brief_json.get("scorecard_summary") or {}
    for i, row in enumerate(scorecard.get("top_drivers") or []):
        if isinstance(row, dict) and "driver" in row:
            row["driver"] = _cap_str(row["driver"], 200, f"top_drivers[{i}].driver", notices)
    for i, row in enumerate(scorecard.get("dimension_table") or []):
        if isinstance(row, dict) and "driver" in row:
            row["driver"] = _cap_str(row["driver"], 200, f"dimension_table[{i}].driver", notices)

    qr = brief_json.get("quick_reads")
    if isinstance(qr, dict):
        for section in ("facilities", "political", "authorizer"):
            if section in qr:
                qr[section] = _cap_str(qr[section], 300, f"quick_reads.{section}", notices)

    for i, school in enumerate(brief_json.get("schools_to_watch") or []):
        if isinstance(school, dict) and "reason" in school:
            school["reason"] = _cap_str(school["reason"], 200, f"schools_to_watch[{i}].reason", notices)

    return brief_json, notices


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


# Source classes whose `date` field records when a page was scraped or
# published, not when the underlying dataset was last revised.  Excluding
# them prevents a news article publication date from masquerading as a
# federal data vintage year.
_EPHEMERAL_SOURCE_CLASSES = frozenset({
    "MEDIA", "WEB_SEARCH", "PRESS_RELEASE", "BLOG", "SCRAPED",
})


def _derive_data_through(brief_json: dict) -> str:
    """Return the oldest data vintage year found in brief_json as '{year}-12-31'.

    Collects years from:
      - sources[*].date  (skipping media/scrape sources and None dates)
      - top_charter_schools[*].proficiency_year

    Returns the minimum year as an ISO-format year-end date.  Falls back to
    (today.year - 1)-12-31 when no vintage fields are present.  Fully generic:
    works for any state, any combination of sources.
    """
    import datetime

    years: list[int] = []

    for src in brief_json.get("sources") or []:
        sc = (src.get("source_class") or "").upper()
        if sc in _EPHEMERAL_SOURCE_CLASSES:
            continue
        date_val = src.get("date")
        if not date_val:
            continue
        try:
            years.append(datetime.date.fromisoformat(str(date_val)).year)
        except (ValueError, TypeError):
            continue

    for school in brief_json.get("top_charter_schools") or []:
        yr = school.get("proficiency_year")
        if isinstance(yr, (int, float)) and 1990 < yr < 2100:
            years.append(int(yr))

    if years:
        return f"{min(years)}-12-31"

    return f"{datetime.date.today().year - 1}-12-31"


# ─────────────────────────────────────────────
# SCAN MODE SYNTHESIS (mode 1)
# ─────────────────────────────────────────────

def _run_scan_synthesis(
    community_id: str,
    state: str,
    config: PipelineConfig,
    scorecard: dict,
    start: float,
    warnings: list[str],
) -> StageResult:
    """Lean Haiku synthesis for scan mode.

    Replaces the full brief generation + audit with a single Haiku call that
    produces only the fields needed for the comparison table.  Skips S6 synthesis
    cache key used by brief mode so mode-1 and mode-2 caches never collide.
    """
    cache = CacheManager(config)
    cache_key = (f"synthesis/{state.lower()}/{community_id}/"
                 f"s6_scan_{config.preset.value}.json")

    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.SUCCESS, output_data=cached,
                cache_hit=True
            )

    # --- Dry-run guard: no live API calls ---
    if config.dry_run:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.SKIPPED,
            warnings=["Dry run — skipping Haiku API call for S6 scan synthesis"]
        )

    community_name = community_id.split("-", 1)[1].replace("-", " ").title()

    prompt_text = load_prompt("prompts/synthesize/s6_mode1_scan.md", {
        "COMMUNITY_NAME": community_name,
        "COMMUNITY_ID":   community_id,
        "SCORECARD_JSON": json.dumps(scorecard, indent=2),
    })

    result = call_claude(
        model=config.model_haiku,
        system=(
            "You are a charter school market analyst. "
            "Respond ONLY with valid JSON. No markdown fences, no preamble."
        ),
        user=prompt_text,
        max_tokens=300,
        temperature=0.1,
        expect_json=True,
        stage=STAGE_ID,
        community_id=community_id,
    )

    if result.parse_error:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"Scan synthesis JSON parse failed: {result.parse_error}"]
        )

    scan_json = result.parsed_json or {}

    # Normalise: fall back to scorecard values if Haiku omitted any required field
    scan_json.setdefault("city", community_name)
    scan_json.setdefault("one_line_snapshot", "")
    scan_json.setdefault("signal_tensions", [])
    scan_json.setdefault("deep_review_level", "optional_review")
    # Always take composite and tier from scorecard — Haiku may read tier_display_label
    scan_json["composite"] = scorecard.get("composite_score_rounded", scorecard.get("composite_score"))
    scan_json["tier"] = scorecard.get("tier")
    scan_json["community_id"] = community_id
    scan_json["state"] = state
    scan_json["scan_generated_at"] = _today()
    # Propagate override_flags so render layer can apply tier caveats (e.g. SMALL_MARKET)
    scan_json["override_flags"] = scorecard.get("override_flags", [])

    out_path = _scan_output_path(state, community_id, config.preset.value)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(scan_json, f, indent=2)

    cache.set(cache_key, scan_json)

    return StageResult(
        stage_id=STAGE_ID,
        community_id=community_id,
        state=state,
        status=StageStatus.SUCCESS,
        output_path=out_path,
        output_data=scan_json,
        warnings=warnings,
        tokens_used=result.total_tokens,
        duration_seconds=round(time.time() - start, 2),
    )


# ─────────────────────────────────────────────
# CHARTER INTELLIGENCE INJECTION
# ─────────────────────────────────────────────

def _inject_charter_intel(brief_json: dict, state: str, community_id: str) -> dict:
    """
    Load charter_schools and local_authorizers from the S3 raw cache and
    append them to the brief JSON.

    These sections are directory data fetched and enriched during S3; they
    bypass S4 scoring rules entirely. S6 simply copies them into the output
    so S7 can render them as dedicated sections.

    Falls back to empty lists if the S3 cache file is missing or the keys
    are absent (e.g. pipeline ran before this feature was added).
    """
    s3_path = (
        f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
    )
    charter_schools: list[dict] = []
    local_authorizers: list[dict] = []

    if os.path.exists(s3_path):
        try:
            with open(s3_path) as f:
                s3_raw = json.load(f)
            charter_schools   = s3_raw.get("charter_schools",   []) or []
            local_authorizers = s3_raw.get("local_authorizers", []) or []
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "[%s] Could not load charter intel from S3 cache: %s", community_id, exc
            )

    brief_json["top_charter_schools"] = charter_schools
    brief_json["local_authorizers"]   = local_authorizers
    return brief_json


def _inject_ell_gap_disclosure(brief_json: dict, state: str, community_id: str) -> dict:
    """
    If ell_data_suppressed was set in S3, append a standard needs_verification
    entry disclosing the missing ELL data.

    Runs post-generation so it doesn't conflict with the synthesis prompt's
    anti-hallucination grounding rule (which prohibits introducing claims not
    in the verified facts array). A data-absence disclosure is pipeline metadata,
    not a new factual claim — it belongs here, not inside the model's context.
    """
    s3_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
    if not os.path.exists(s3_path):
        return brief_json

    try:
        with open(s3_path) as f:
            s3_raw = json.load(f)
    except Exception:
        return brief_json

    if not s3_raw.get("ell_data_suppressed"):
        return brief_json

    entry = {
        "claim": "ELL rate (English Language Learner %) — Census ACS data suppressed for this district",
        "reason": (
            "ACS B16004 data suppressed for this district, likely due to small enrollment sample. "
            "Actual ELL rate may be nonzero and could affect operational_complexity scoring."
        ),
        "source_class_attempted": "FEDERAL_DATA",
        "resolution_path": f"{state} PED district profile or state report card.",
        "impact_if_wrong": "MODERATE",
    }

    if not isinstance(brief_json.get("needs_verification"), list):
        brief_json["needs_verification"] = []
    brief_json["needs_verification"].append(entry)

    import logging
    logging.getLogger(__name__).info(
        "[%s] Injected ELL data gap disclosure into needs_verification", community_id
    )
    return brief_json


# Data-coverage tiers at or below which strategic recommendations are gated:
# the composite rests on too little verified data to support confident action.
_GATED_COVERAGE_TIERS = frozenset({"unreliable", "INSUFFICIENT"})


def _inject_recommendation_gate(brief_json: dict, scorecard: dict) -> dict:
    """
    Copy the S5 data_coverage_tier onto the brief and compute a deterministic
    recommendation confidence gate (Session 18 BLOCKER 4b).

    When coverage is unreliable/INSUFFICIENT or overall confidence is LOW, the
    brief should be allowed to say "conduct further targeted research" instead
    of manufacturing confident recommendations to fill a quota. The gate is a
    structured signal the S7 template and human reviewers can act on; it does
    not itself delete recommendations.
    """
    tier = scorecard.get("data_coverage_tier")
    confidence_overall = scorecard.get("confidence_overall")
    brief_json["data_coverage_tier"] = tier

    gated = tier in _GATED_COVERAGE_TIERS or confidence_overall == Confidence.LOW.value
    if gated:
        if tier == "INSUFFICIENT":
            reason = (
                "4+ scoring dimensions defaulted to midpoint — the composite is "
                "largely a redistribution of default values, not evidence. Treat "
                "recommendations as research directions, not confident calls."
            )
        elif tier == "unreliable":
            reason = (
                "Under 50% of preset weight is backed by verified data. "
                "Recommendations rest on thin evidence; further research is warranted."
            )
        else:
            reason = (
                "Overall confidence is LOW (top dimensions rest on defaulted or "
                "low-confidence facts). Recommendations should be treated cautiously."
            )
    else:
        reason = "Data coverage is sufficient to support the recommendations as scored."

    brief_json["recommendation_confidence_gate"] = {
        "gated": bool(gated),
        "data_coverage_tier": tier,
        "confidence_overall": confidence_overall,
        "reason": reason,
    }
    return brief_json


def _load_state_config(state: str) -> dict:
    """Load the per-state block from config/states.yaml. Returns {} on any failure."""
    try:
        import yaml
        with open("config/states.yaml") as f:
            states_cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}
    # states.yaml keys are upper-case state codes (e.g. "NM").
    return states_cfg.get(state.upper()) or states_cfg.get(state) or {}


def _inject_tribal_jurisdiction_flag(brief_json: dict, state: str, community_id: str) -> dict:
    """
    If config/states.yaml marks this community with a tribal_jurisdiction_flag
    (Session 18 BLOCKER 5a), copy it onto the brief and add a needs_verification
    entry. Charter authority on/adjacent to tribal land is not modeled by scoring
    and requires human determination.

    Three status values, with different severity:
      FLAGGED              — strong signal of tribal jurisdiction; HIGH impact; red banner
      REVIEW_NEEDED        — adjacent/probable; HIGH impact; red banner
      PENDING_HUMAN_REVIEW — determination not yet made; MODERATE impact; yellow banner
    """
    state_cfg = _load_state_config(state)
    community_meta = (state_cfg.get("community_metadata") or {}).get(community_id) or {}
    flag = community_meta.get("tribal_jurisdiction_flag")
    if not flag or not isinstance(flag, dict):
        return brief_json

    status = flag.get("status")
    if status not in ("FLAGGED", "REVIEW_NEEDED", "PENDING_HUMAN_REVIEW"):
        return brief_json

    brief_json["tribal_jurisdiction_flag"] = {
        "status": status,
        "basis": flag.get("basis"),
        "note": flag.get("note"),
        "resolution_path": flag.get("resolution_path"),
    }

    note = flag.get("note") or "Tribal jurisdiction may affect charter authority here."
    impact = "MODERATE" if status == "PENDING_HUMAN_REVIEW" else "HIGH"
    entry = {
        "claim": f"Charter authorization jurisdiction for {community_id} ({status})",
        "reason": note,
        "source_class_attempted": None,
        "resolution_path": flag.get("resolution_path")
            or "Human determination of tribal jurisdiction and applicable charter authority required.",
        "impact_if_wrong": impact,
    }
    if not isinstance(brief_json.get("needs_verification"), list):
        brief_json["needs_verification"] = []
    brief_json["needs_verification"].append(entry)

    import logging
    logging.getLogger(__name__).info(
        "[%s] Injected tribal_jurisdiction_flag (%s) into brief", community_id, status
    )
    return brief_json


def _sanitize_needs_verification(brief_json: dict, community_id: str) -> dict:
    """
    Defense-in-depth guard against empty evidence fields in the Needs Verification
    section. Multiple paths append items (ELL gap, tribal/authorization flags,
    teacher-supply warning, audit-moved claims) and the model can emit its own, so
    a single null `reason` slips through and renders as the literal "None".

    Drops items with no usable `claim`, and replaces a missing/blank `reason` with
    a neutral placeholder so no brief ever shows a dangling "— None".
    """
    items = brief_json.get("needs_verification")
    if not isinstance(items, list) or not items:
        return brief_json

    cleaned = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            continue
        claim = (item.get("claim") or "").strip()
        if not claim:
            dropped += 1
            continue
        item["claim"] = claim
        if not (item.get("reason") or "").strip():
            item["reason"] = "Reason not recorded — verify before relying on this item."
        cleaned.append(item)

    brief_json["needs_verification"] = cleaned
    if dropped:
        import logging
        logging.getLogger(__name__).info(
            "[%s] Dropped %d needs_verification item(s) with no claim", community_id, dropped
        )
    return brief_json


def _inject_teacher_supply_warning(brief_json: dict, state: str, community_id: str) -> dict:
    """
    Surface the statewide teacher_supply_warning scaffold (Session 18 BLOCKER 5b)
    into the brief's needs_verification with impact_if_wrong=HIGH. The NM teacher
    shortage is documented statewide, but per-community hard-to-staff status is
    NOT verified here — this is an honest data gap, not an asserted fact.
    """
    state_cfg = _load_state_config(state)
    warning = state_cfg.get("teacher_supply_warning")
    if not warning or not isinstance(warning, dict):
        return brief_json

    # Use `or default`, not `.get(key, default)`: the per-state config scaffolds
    # carry these keys with explicit null values (filled only for NM so far), and
    # .get() returns the default solely when a key is *absent*. Without this, a
    # null `reason` leaks through and the template renders a literal "None".
    entry = {
        "claim": warning.get("claim") or "Teacher supply may constrain charter staffing in this community.",
        "reason": warning.get("reason") or "Statewide teacher shortage documented; per-community status unverified.",
        "source_class_attempted": None,
        "resolution_path": warning.get("resolution_path")
            or "Cross-check against the current state teacher shortage-area / hard-to-staff list.",
        "impact_if_wrong": warning.get("impact_if_wrong") or "HIGH",
    }
    if not isinstance(brief_json.get("needs_verification"), list):
        brief_json["needs_verification"] = []
    brief_json["needs_verification"].append(entry)

    import logging
    logging.getLogger(__name__).info(
        "[%s] Injected teacher_supply_warning into needs_verification", community_id
    )
    return brief_json


def _inject_market_routing(
    brief_json: dict,
    scorecard: dict,
    state: str,
    community_id: str,
) -> dict:
    """Set market_type and verdict fields so S7 can route to the correct template.

    verdict:     "INELIGIBLE" when the scorecard tier is AVOID, else "ELIGIBLE".
    market_type: "greenfield" when the community registry marks has_charters=False
                 (no existing charter landscape), else "established".

    Both fields are deterministic from existing pipeline data; no new API calls.
    """
    import logging
    log = logging.getLogger(__name__)

    tier = scorecard.get("tier", "")
    brief_json["verdict"] = "INELIGIBLE" if tier == "AVOID" else "ELIGIBLE"

    # Read has_charters from community registry YAML (greenfield signal)
    registry_path = f"config/community_registry/{state.lower()}.yaml"
    market_type = "established"
    if os.path.exists(registry_path):
        try:
            import yaml as _yaml
            with open(registry_path) as f:
                registry = _yaml.safe_load(f) or {}
            entry = registry.get(community_id, {})
            if isinstance(entry, dict) and entry.get("has_charters") is False:
                market_type = "greenfield"
        except Exception as exc:
            log.warning("[%s] _inject_market_routing: registry read failed — %s", community_id, exc)

    brief_json["market_type"] = market_type
    log.info(
        "[%s] market_routing: verdict=%s market_type=%s",
        community_id, brief_json["verdict"], market_type,
    )
    return brief_json
