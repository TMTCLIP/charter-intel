"""
pipeline/s5_scoring.py
Stage 5: Community Scoring

PURPOSE:
  Compute weighted dimension scores and composite score from verified facts.
  This stage is DETERMINISTIC — no LLM calls. All logic is explicit Python.
  If you find yourself adding a model call here, that's a design error.

INPUT:
  - data/cache/community/{state}/{community_id}/s4_verified.json
  - config/scoring_weights.yaml
  - config/scoring_presets.yaml

OUTPUT:
  - data/cache/community/{state}/{community_id}/s5_scorecard.json
  - Validated against schemas/scorecard.schema.json

DESIGN PRINCIPLES:
  - Every score is traceable to specific fact IDs
  - When facts are missing, confidence degrades (score uses default; not inflated)
  - Override flags are checked AFTER composite is computed; they cap the tier
  - Humans should be able to reproduce any score manually from the fact bundle
"""

from __future__ import annotations
import json
import logging
import os
from typing import Any, Optional

import yaml

from pipeline import (
    Confidence, OperatorPreset, OutputMode, PipelineConfig,
    StageResult, StageStatus, ValidationResult, timestamp_now
)
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema


STAGE_ID = "s5_scoring"

logger = logging.getLogger(__name__)

# competitive_opportunity may only score ABOVE midpoint when at least one of
# these demand-side signals is present in the fact bundle. Without a demand
# signal, a high "opportunity" score is an artifact of default_to_midpoint
# inflation, not evidence — see Session 18 Area D.
#
# NOTE (Review Fix 1): geographic_whitespace was removed from this set. It is
# itself a web-search-derived field subject to the same no-evidence→5 fallback
# the bias pass was correcting, so it cannot serve as a demand gate signal.
# Only observed-demand signals (waitlist data, grade-band coverage gaps) qualify.
_COMPETITIVE_DEMAND_SIGNAL_KEYS = frozenset({
    "known_waitlist_data", "grade_band_gaps",
})

# ─────────────────────────────────────────────
# FACILITIES & REPLICATION FEASIBILITY THRESHOLDS
# ─────────────────────────────────────────────
# PROVISIONAL — NEEDS CALIBRATION. These thresholds translate free federal-data
# counts into 1–10 dimension scores for the two dimensions that previously always
# defaulted to a neutral 5. They are starting-point estimates, not validated
# breakpoints; tune them here (single source of truth) once operator profiles and
# real outcomes are available. See [[feedback-clip-standing-rules]].
#
# Fact keys these functions read (injected by s3_fact_extraction.py from the
# free federal fetchers):
#   facilities_closed_schools_count  — CCD closed/temporarily-closed schools,
#                                       district+county deduped (ccd_closures_fetcher)
#   csp_distinct_operators           — distinct CSP (CFDA 84.282) operators with
#                                       place-of-performance IN the community's
#                                       county (usaspending_csp_fetcher;
#                                       community-specific)
#   cip13_completers                 — IPEDS CIP-13 (education) completers, statewide
#                                       (ipeds_completers_fetcher)
#
# A closed-school count of 0 (or 0 operators / 0 completers) is a VALID scored
# value — a real signal of constraint — NOT missing data. Missing data is the
# absence of the fact entirely (fetcher returned None), which keeps the neutral
# default and tags the dimension with an unscored_reason.

FACILITIES_CLOSED_SCHOOLS_FACT_KEY = "facilities_closed_schools_count"
CSP_OPERATORS_FACT_KEY = "csp_distinct_operators"
CIP13_COMPLETERS_FACT_KEY = "cip13_completers"

# facilities_feasibility: more recently-closed schools => more candidate real
# estate => higher feasibility. CAPPED AT 8 (never 9–10): a closed school is only
# a *signal* of availability, not confirmed-available real estate, so the count
# alone cannot justify a top-band score.
def _facilities_score_from_closed_count(c: int) -> float:
    if c <= 0:
        return 3.0
    if c == 1:
        return 5.0
    if c <= 3:
        return 6.0
    if c <= 6:
        return 7.0
    return 8.0  # c >= 7, capped at 8

# replication_feasibility operator sub-signal: distinct regional CSP operators.
def _replication_operator_score(operators: int) -> float:
    if operators <= 0:
        return 3.0
    if operators <= 2:
        return 5.0
    if operators <= 5:
        return 6.0
    if operators <= 10:
        return 7.0
    return 8.0  # > 10

# replication_feasibility pipeline sub-signal: statewide CIP-13 completers.
def _replication_pipeline_score(completers: int) -> float:
    if completers < 200:
        return 3.0
    if completers < 500:
        return 5.0
    if completers < 1000:
        return 6.0
    if completers < 2000:
        return 7.0
    return 8.0  # >= 2000

# ─────────────────────────────────────────────
# CONFIG LOADING
# ─────────────────────────────────────────────

def load_scoring_config() -> tuple[dict, dict]:
    """Load scoring_weights.yaml and scoring_presets.yaml."""
    with open("config/scoring_weights.yaml") as f:
        weights_cfg = yaml.safe_load(f)
    with open("config/scoring_presets.yaml") as f:
        presets_cfg = yaml.safe_load(f)
    return weights_cfg, presets_cfg


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
    Compute the community scorecard from verified facts.
    Reads S4 output; writes scorecard JSON.
    """
    import time
    start = time.time()

    # --- Load verified facts ---
    if previous_result and previous_result.output_data:
        verified_bundle = previous_result.output_data
    else:
        facts_path = _facts_path(state, community_id)
        if not os.path.exists(facts_path):
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.ERROR,
                errors=[f"Verified facts not found at {facts_path}. Run S4 first."]
            )
        with open(facts_path) as f:
            verified_bundle = json.load(f)

    weights_cfg, presets_cfg = load_scoring_config()
    preset = config.preset.value

    # --- Score each dimension ---
    dimensions_out = {}
    facts_used = []
    missing_dimensions = []
    warnings = []

    preset_weights = presets_cfg[preset]["dimensions"]
    dimension_defs = weights_cfg["dimensions"]

    for dim_name, dim_def in dimension_defs.items():
        weight = preset_weights.get(dim_name, 0.0)
        result = score_dimension(
            dim_name=dim_name,
            dim_def=dim_def,
            verified_bundle=verified_bundle,
            weight=weight
        )
        dimensions_out[dim_name] = result
        facts_used.extend(result.get("supporting_fact_ids", []))

        if result.get("used_default"):
            missing_dimensions.append(dim_name)
            warnings.append(f"Dimension '{dim_name}': no relevant facts found; used default score 5.")

    # --- Compute composite (excluding defaulted dimensions) ---
    # Dimensions where used_default is True carry no real data; including them
    # would silently pull the composite toward 5.0.  We exclude them and
    # redistribute their weight proportionally across the remaining dimensions.
    excluded_dimensions = [
        name for name, d in dimensions_out.items()
        if d.get("used_default")
    ]
    included = {
        name: d for name, d in dimensions_out.items()
        if not d.get("used_default")
    }
    weight_sum = sum(d["weight"] for d in included.values())

    if weight_sum > 0:
        effective_weights = {
            name: round(d["weight"] / weight_sum, 6)
            for name, d in included.items()
        }
        composite = round(sum(
            d["score"] * effective_weights[name]
            for name, d in included.items()
        ), 2)
    else:
        # All dimensions defaulted — no real data at all; fall back to midpoint
        effective_weights = {}
        composite = 5.0

    # --- Compute data coverage ---
    total_weight_sum = sum(d["weight"] for d in dimensions_out.values())
    real_weight_sum = sum(d["weight"] for d in included.values())
    if total_weight_sum > 0:
        data_coverage_pct = round(real_weight_sum / total_weight_sum * 100, 1)
    else:
        data_coverage_pct = 0.0
    cov_thresholds = weights_cfg.get("data_coverage_thresholds", {"reliable": 70, "provisional": 50})
    if data_coverage_pct >= cov_thresholds["reliable"]:
        data_coverage_tier = "reliable"
    elif data_coverage_pct >= cov_thresholds["provisional"]:
        data_coverage_tier = "provisional"
    else:
        data_coverage_tier = "unreliable"

    # Session 18 Area C: when 4+ dimensions defaulted (no real data), the
    # composite is mostly redistributed midpoints. Coverage % alone can mask
    # this, so an explicit INSUFFICIENT tier overrides the percentage-based
    # label to make the data gap unmistakable downstream (brief banner).
    if len(excluded_dimensions) >= 4:
        data_coverage_tier = "INSUFFICIENT"
        logger.warning(
            "s5_scoring: %s/%s — %d dimensions defaulted; data_coverage_tier "
            "forced to INSUFFICIENT (composite is largely redistributed midpoints)",
            state, community_id, len(excluded_dimensions),
        )

    # --- Check override flags ---
    override_flags = check_override_flags(verified_bundle, dimensions_out)

    # --- Compute tier ---
    tier, tier_label = compute_tier(composite, override_flags, weights_cfg)

    # --- Compute overall confidence ---
    top_3_dims = sorted(
        dimensions_out.items(),
        key=lambda x: x[1]["weight"],
        reverse=True
    )[:3]
    confidence_levels = [Confidence(d["confidence"]) for _, d in top_3_dims]
    overall_confidence = Confidence.minimum(confidence_levels)

    # --- Build scorecard ---
    scorecard = {
        "scorecard_id": f"sc_{community_id}_{preset}_{_today()}",
        "community_id": community_id,
        "state": state,
        "preset": preset,
        "dimensions": dimensions_out,
        "composite_score": composite,
        "composite_score_rounded": round(composite, 1),
        "tier": tier,
        "tier_display_label": tier_label,
        "override_flags": override_flags,
        "confidence_overall": overall_confidence.value,
        "confidence_by_dimension": {
            k: v["confidence"] for k, v in dimensions_out.items()
        },
        "facts_used": list(set(facts_used)),
        "dimensions_with_missing_data": missing_dimensions,
        "excluded_dimensions": excluded_dimensions,
        "effective_weights": effective_weights,
        "data_coverage_pct": data_coverage_pct,
        "data_coverage_tier": data_coverage_tier,
        "scored_at": timestamp_now(),
        "scoring_version": weights_cfg.get("version", "1.0")
    }

    # --- Validate schema ---
    validation = validate_against_schema(scorecard, "schemas/scorecard.schema.json")
    if not validation:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"Scorecard failed schema validation: {validation.errors}"]
        )

    # --- Write output ---
    out_path = _output_path(state, community_id)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(scorecard, f, indent=2)

    return StageResult(
        stage_id=STAGE_ID,
        community_id=community_id,
        state=state,
        status=StageStatus.SUCCESS,
        output_path=out_path,
        output_data=scorecard,
        warnings=warnings,
        duration_seconds=round(time.time() - start, 2)
    )


# ─────────────────────────────────────────────
# DIMENSION SCORING
# ─────────────────────────────────────────────

def score_dimension(
    dim_name: str,
    dim_def: dict,
    verified_bundle: dict,
    weight: float
) -> dict:
    """
    Score a single dimension against the verified fact bundle.
    Returns a dict matching the scorecard dimension_score schema.
    """
    scoring_rules = dim_def.get("scoring_rules", {})
    primary_fact_key = scoring_rules.get("primary_fact")
    fact_keys_required = dim_def.get("fact_keys_required", [])
    fact_keys_optional = dim_def.get("fact_keys_optional", [])
    missing_behavior = dim_def.get("missing_data_behavior", "degrade_confidence")

    # Find relevant facts from the verified bundle
    relevant_facts = extract_facts_for_dimension(dim_name, verified_bundle)
    supporting_ids = [f["datapoint_id"] for f in relevant_facts]

    # Check if primary fact is available
    primary_value = _get_fact_value(primary_fact_key, relevant_facts)

    # academic_need: when no verified proficiency data exists (TN, WI, etc.),
    # force used_default=True so it's excluded from the composite rather than
    # anchoring at 5.0 on a hallucinated or absent ELA fact.
    if dim_name == "academic_need" and not verified_bundle.get("has_proficiency_data", True):
        return {
            "score": 5.0,
            "weight": weight,
            "weighted_contribution": round(5.0 * weight, 4),
            "confidence": Confidence.LOW.value,
            "primary_driver": "State proficiency data not yet available — excluded from composite",
            "supporting_fact_ids": supporting_ids,
            "used_default": True,
        }

    # Special case: score population_trends from NCES enrollment data when present.
    # Falls back to the default-to-5 path below if pct_change_total is absent.
    if dim_name == "population_trends":
        enrollment_result = _score_population_trends(relevant_facts, weight)
        if enrollment_result is not None:
            return enrollment_result

    # Special case: facilities_feasibility and replication_feasibility are scored
    # from free federal-data counts (see threshold constants above). These own
    # their dimensions entirely — they always return a complete score dict,
    # either a real value or a neutral default tagged with an unscored_reason
    # when the source was unavailable. No other dimension's behavior changes.
    if dim_name == "facilities_feasibility":
        return _score_facilities_feasibility(relevant_facts, weight)
    if dim_name == "replication_feasibility":
        return _score_replication_feasibility(relevant_facts, weight)

    # Special case: web-search-derived index dimensions (score_is_index: true in YAML).
    # The index value is already calibrated to the 1–9 reporting scale; passing it
    # through apply_scoring_rules would double-translate (e.g. index=6 → score=5).
    # When the primary fact is present and score_is_index is set, use the raw value
    # directly, clamped to [1.0, 10.0].  Falls through to normal path when no data.
    if scoring_rules.get("score_is_index") and primary_value is not None:
        try:
            raw = float(primary_value)
            score = round(max(1.0, min(10.0, raw)), 2)
            confidence = _aggregate_fact_confidence(relevant_facts)
            driver = (
                f"{primary_fact_key.replace('_', ' ')}: "
                f"{primary_value} → direct index score {score:.1f}"
            )
            return {
                "score": score,
                "weight": weight,
                "weighted_contribution": round(score * weight, 4),
                "confidence": confidence,
                "primary_driver": driver,
                "supporting_fact_ids": supporting_ids,
                "used_default": False,
            }
        except (TypeError, ValueError):
            pass  # non-numeric value — fall through to threshold path

    if primary_value is None and missing_behavior == "default_to_midpoint":
        return {
            "score": 5.0,
            "weight": weight,
            "weighted_contribution": round(5.0 * weight, 4),
            "confidence": Confidence.LOW.value,
            "primary_driver": "Insufficient data — default score used",
            "supporting_fact_ids": supporting_ids,
            "used_default": True
        }

    if primary_value is None and missing_behavior == "degrade_confidence":
        # Try to compute from optional facts; mark LOW confidence
        score = 5.0  # neutral default
        confidence = Confidence.LOW.value
        driver = "Key fact unavailable — score estimated from limited data"
        used_default = True
    else:
        # Compute score from primary + secondary facts
        score, driver = apply_scoring_rules(primary_value, primary_fact_key, scoring_rules, dim_def)
        score = apply_secondary_adjustments(score, relevant_facts, dim_def)
        confidence = _aggregate_fact_confidence(relevant_facts)
        used_default = False

    # Session 18 Area D: competitive_opportunity may not score above midpoint
    # unless an actual demand-side signal is present. The primary fact
    # (demand_supply_gap_index) can itself be a default-to-midpoint artifact,
    # so a high "opportunity" score with no waitlist / grade-band gap evidence
    # is unsupported. Clamp to midpoint and mark used_default so it is excluded
    # from the composite.
    # NOTE FOR HUMAN REVIEW: this changes competitive_opportunity rankings.
    if dim_name == "competitive_opportunity" and not used_default and round(score, 2) > 5.0:
        signal_facts = [
            f.get("fact_key") for f in relevant_facts
            if f.get("fact_key") in _COMPETITIVE_DEMAND_SIGNAL_KEYS
            and f.get("value") is not None
        ]
        has_demand_signal = bool(signal_facts)
        if has_demand_signal:
            logger.info(
                "s5_scoring: competitive_opportunity scored %.2f — demand gate "
                "passed, signal(s) present: %s",
                round(score, 2), ", ".join(sorted(signal_facts)),
            )
        else:
            logger.warning(
                "s5_scoring: competitive_opportunity scored %.2f with no demand "
                "signal (%s) — clamping to 5.0 and excluding from composite",
                round(score, 2), ", ".join(sorted(_COMPETITIVE_DEMAND_SIGNAL_KEYS)),
            )
            score = 5.0
            confidence = Confidence.LOW.value
            driver = (
                "Opportunity score gated to midpoint — no observed demand evidence "
                "(waitlist data or grade-band gap) present"
            )
            used_default = True

    # charter_saturation reliability gate (mirrors the competitive_opportunity gate
    # above). The scored primary fact (num_charter_schools) counts ONLY PED-roster
    # charters by design, so a market with charter-adjacent / unofficial schools
    # (e.g. Jackson, MS) can read as "near-empty → wide-open opportunity" when it is
    # not. When the formal count yields an optimistic low-saturation score (>5.0) but
    # S3 found charter-adjacent schools (num_charter_adjacent_schools > 0, a non-
    # scoring fact), we cannot confidently call the market wide-open. Clamp to neutral
    # and exclude from the composite rather than fabricate a saturation number from
    # unverified web data. This withdraws an unsupported optimistic reading; it does
    # NOT assert high saturation.
    # NOTE FOR HUMAN REVIEW: this changes charter_saturation rankings. It does not
    # change any weight. Promoting num_charter_adjacent_schools into the scored count
    # requires separate operator sign-off.
    if dim_name == "charter_saturation" and not used_default and round(score, 2) > 5.0:
        adjacent_count = _get_charter_adjacent_count(verified_bundle)
        if adjacent_count and adjacent_count > 0:
            logger.warning(
                "s5_scoring: charter_saturation scored %.2f (low formal saturation) but "
                "%d charter-adjacent school(s) present — clamping to 5.0 and excluding "
                "from composite (low-saturation reading unverified)",
                round(score, 2), adjacent_count,
            )
            score = 5.0
            confidence = Confidence.LOW.value
            driver = (
                f"Saturation gated to midpoint — formal roster shows low saturation but "
                f"{adjacent_count} charter-adjacent school(s) were found; the wide-open "
                f"reading is unverified pending review"
            )
            used_default = True

    return {
        "score": round(score, 2),
        "weight": weight,
        "weighted_contribution": round(score * weight, 4),
        "confidence": confidence,
        "primary_driver": driver,
        "supporting_fact_ids": supporting_ids,
        "used_default": used_default
    }


def _get_charter_adjacent_count(verified_bundle: dict) -> Optional[int]:
    """Read the non-scoring num_charter_adjacent_schools count from the full bundle.

    extract_facts_for_dimension() filters to in_main_analysis=True, so this fact
    (deliberately non-scoring) is invisible there; scan the raw fact list instead.
    """
    for f in verified_bundle.get("facts", []):
        if f.get("fact_key") == "num_charter_adjacent_schools":
            try:
                return int(f.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def apply_scoring_rules(
    value: Any,
    fact_key: str,
    scoring_rules: dict,
    dim_def: dict
) -> tuple[float, str]:
    """
    Translate a raw fact value into a 1–10 score using threshold rules.
    Returns (score, one_line_driver).
    """
    direction = scoring_rules.get("direction", "direct")
    thresholds = scoring_rules.get("thresholds", [])

    if not thresholds or not isinstance(value, (int, float)):
        return 5.0, f"Value '{value}' — no numeric threshold rule; midpoint used"

    for threshold in thresholds:
        max_val = threshold.get("max")
        if max_val is None or value <= max_val:
            raw_score = float(threshold["score"])
            # For inverse dimensions, score is already inverted in the thresholds
            driver = _build_driver(fact_key, value, raw_score, direction)
            return raw_score, driver

    return 5.0, f"Value '{value}' matched no threshold; midpoint used"


def apply_secondary_adjustments(
    primary_score: float,
    relevant_facts: list[dict],
    dim_def: dict
) -> float:
    """
    Blend secondary facts into the primary score using weighted averaging.
    Primary weight is defined in dim_def.primary_weight (default 0.55).
    """
    scoring_rules = dim_def.get("scoring_rules", {})
    primary_weight = dim_def.get("primary_weight", 0.55)
    secondary_defs = dim_def.get("secondary_facts", [])

    if not secondary_defs:
        return primary_score

    total_weight = primary_weight
    weighted_sum = primary_score * primary_weight

    for sec_def in secondary_defs:
        key = sec_def["key"]
        w = sec_def.get("weight", 0.15)
        val = _get_fact_value(key, relevant_facts)
        if val is not None:
            sec_score, _ = apply_scoring_rules(
                val, key,
                {"direction": sec_def.get("direction", "direct"),
                 "thresholds": sec_def.get("thresholds", [])},
                {}
            )
            weighted_sum += sec_score * w
            total_weight += w

    if total_weight == 0:
        return primary_score
    return round(weighted_sum / total_weight, 2)


# ─────────────────────────────────────────────
# OVERRIDE FLAG CHECKING
# ─────────────────────────────────────────────

def check_override_flags(
    verified_bundle: dict,
    dimensions_out: dict
) -> list[dict]:
    """
    Check all override flag conditions. Returns list of triggered flags.
    Conditions are checked against specific fact values in the verified bundle.
    """
    flags = []
    fact_index = _build_fact_index(verified_bundle)

    # OVERSATURATED
    share = fact_index.get("charter_seat_share_pct")
    trend = fact_index.get("charter_enrollment_trend_3yr")
    if share is not None and float(share) >= 30 and trend in ("DECLINING", "FLAT"):
        flags.append({
            "flag": "OVERSATURATED",
            "triggered_by": f"Charter seat share {share}% with {trend} enrollment trend",
            "tier_effect": "Capped at WATCHLIST",
            "visual": "🔴"
        })

    # HOSTILE_AUTHORIZER
    num_auth = fact_index.get("num_accessible_authorizers")
    approval_rate = fact_index.get("authorizer_approval_rate_pct")
    if (num_auth is not None and int(num_auth) == 0) or \
       (approval_rate is not None and float(approval_rate) < 15):
        flags.append({
            "flag": "HOSTILE_AUTHORIZER",
            "triggered_by": f"Authorizer approval rate: {approval_rate}%; accessible authorizers: {num_auth}",
            "tier_effect": "Capped at WATCHLIST",
            "visual": "🔴"
        })

    # FACILITIES_BOTTLENECK
    fac_index = fact_index.get("facilities_feasibility_index")
    no_public = fact_index.get("no_public_facility_access")
    if fac_index is not None and float(fac_index) <= 2 and no_public is True:
        flags.append({
            "flag": "FACILITIES_BOTTLENECK",
            "triggered_by": "Facilities feasibility critically low with no public facility access",
            "tier_effect": "Capped at MODERATE_OPPORTUNITY",
            "visual": "🟠"
        })

    # POLITICAL_RISK_HIGH
    pol_index = fact_index.get("political_climate_index")
    if pol_index is not None and float(pol_index) <= 3:
        flags.append({
            "flag": "POLITICAL_RISK_HIGH",
            "triggered_by": f"Political climate index {pol_index} (RESISTANT or HOSTILE)",
            "tier_effect": "Capped at WATCHLIST",
            "visual": "🔴"
        })

    # REPLICATION_FRIENDLY (positive)
    rep_index = fact_index.get("replication_readiness_index")
    auth_score = dimensions_out.get("authorizer_friendliness", {}).get("score", 0)
    if rep_index is not None and float(rep_index) >= 7 and auth_score >= 7:
        flags.append({
            "flag": "REPLICATION_FRIENDLY",
            "triggered_by": "Strong replication readiness AND favorable authorizer",
            "tier_effect": "No tier cap — positive signal",
            "visual": "🟢"
        })

    # SMALL_MARKET threshold: 1500 is a placeholder.
    # Derive the real value from: min_viable_enrollment / capture_rate.
    # min_viable_enrollment ≈ 150–300 students (charter unit economics).
    # capture_rate ≈ 10–20% in rural NM.
    # Real floor ≈ 750–3000 depending on operator assumptions.
    # This is the same underlying variable as the greenfield catchment
    # problem — district enrollment is a proxy for viable catchment pop.
    # Solve both when NCES-at-S1 is built. Do not pick a round number
    # and treat it as validated.
    enrollment = fact_index.get("k12_enrollment_total")
    enrollment_source = None
    if enrollment is not None:
        enrollment_source = "k12_enrollment_total"
    else:
        # Fallback: NCES parquet enrollment_by_year dict {year: count}
        by_year = fact_index.get("enrollment_by_year")
        if by_year and isinstance(by_year, dict) and len(by_year) > 0:
            latest_year = str(max(int(y) for y in by_year.keys()))
            # Keys may be int or str depending on fetcher — try both
            enrollment = by_year.get(latest_year) or by_year.get(int(latest_year))
            enrollment_source = f"NCES {latest_year}"

    if enrollment is not None and float(enrollment) < 1500:
        flags.append({
            "flag": "SMALL_MARKET",
            "visual": "⚠️",
            "triggered_by": f"District enrollment: {enrollment} students ({enrollment_source})",
            "note": (
                "Small district — composite scores are not normalized for catchment scale. "
                "Competitive opportunity and charter saturation signals may overstate "
                "addressable demand at this enrollment level. Threshold (1500) is a "
                "placeholder — derive from (min_viable_school_enrollment / "
                "realistic_capture_rate) before treating as policy."
            ),
            "tier_effect": "None — informational only",
        })

    return flags


def compute_tier(
    composite: float,
    override_flags: list[dict],
    weights_cfg: dict
) -> tuple[str, str]:
    """
    Assign tier from composite score, then apply override flag caps.
    Returns (tier_key, tier_display_label).
    """
    thresholds = weights_cfg["tier_thresholds"]

    # Find raw tier from composite
    tier_key = "AVOID"
    tier_label = thresholds["AVOID"]["label"]
    for key in ["HIGH_PRIORITY", "STRONG", "MODERATE", "WATCHLIST", "AVOID"]:
        if composite >= thresholds[key]["min"]:
            tier_key = key
            tier_label = thresholds[key]["label"]
            break

    # Apply flag caps (flags can only cap downward)
    cap_map = {
        "OVERSATURATED":      "WATCHLIST",
        "HOSTILE_AUTHORIZER": "WATCHLIST",
        "POLITICAL_RISK_HIGH": "WATCHLIST",
        "FACILITIES_BOTTLENECK": "MODERATE",
    }
    tier_order = ["HIGH_PRIORITY", "STRONG", "MODERATE", "WATCHLIST", "AVOID"]

    for flag_entry in override_flags:
        cap = cap_map.get(flag_entry["flag"])
        if cap and tier_order.index(tier_key) < tier_order.index(cap):
            tier_key = cap
            tier_label = thresholds[cap]["label"]

    return tier_key, tier_label


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

def extract_facts_for_dimension(
    dim_name: str,
    verified_bundle: dict
) -> list[dict]:
    """Filter the verified fact bundle to those tagged for this dimension."""
    all_facts = verified_bundle.get("facts", [])
    return [
        f for f in all_facts
        if f.get("dimension") == dim_name and f.get("in_main_analysis", False)
    ]


def _get_fact_value(fact_key: Optional[str], facts: list[dict]) -> Any:
    """Find a fact by its fact_key and return its value, or None."""
    if not fact_key:
        return None
    for f in facts:
        if f.get("fact_key") == fact_key:
            return f.get("value")
    return None


def _build_fact_index(verified_bundle: dict) -> dict:
    """Build a flat {fact_key: value} index for quick flag checking."""
    index = {}
    for f in verified_bundle.get("facts", []):
        key = f.get("fact_key")
        if key:
            index[key] = f.get("value")
    return index


def _aggregate_fact_confidence(facts: list[dict]) -> str:
    """Return the minimum confidence across a set of facts."""
    if not facts:
        return Confidence.LOW.value
    levels = [Confidence(f.get("confidence", "LOW")) for f in facts]
    return Confidence.minimum(levels).value


def _score_population_trends(facts: list[dict], weight: float) -> Optional[dict]:
    """Score population_trends from NCES enrollment pct_change_total.

    Returns a complete dimension score dict when pct_change_total is present,
    or None to signal the caller to fall through to the default-to-5 path.

    Thresholds (enrollment pct change, earliest→latest year):
        >= +10%        → 9.0   (strong growth)
        +5% to +10%   → 7.5
        +1% to +5%    → 6.5
        -1% to +1%    → 5.5   (stable)
        -5% to -1%    → 4.5
        -10% to -5%   → 3.0
        < -10%        → 1.5   (sharp decline)
    """
    pct_change = _get_fact_value("pct_change_total", facts)
    if pct_change is None or not isinstance(pct_change, (int, float)):
        return None

    if pct_change >= 10:
        score = 9.0
    elif pct_change >= 5:
        score = 7.5
    elif pct_change >= 1:
        score = 6.5
    elif pct_change >= -1:
        score = 5.5
    elif pct_change >= -5:
        score = 4.5
    elif pct_change >= -10:
        score = 3.0
    else:
        score = 1.5

    supporting_ids = [f["datapoint_id"] for f in facts]
    driver = f"NCES enrollment pct_change={pct_change:+.1f}% → score {score:.1f}"

    return {
        "score":                  round(score, 2),
        "weight":                 weight,
        "weighted_contribution":  round(score * weight, 4),
        "confidence":             Confidence.HIGH.value,
        "primary_driver":         driver,
        "supporting_fact_ids":    supporting_ids,
        "used_default":           False,
    }


def _default_dimension_with_tag(
    weight: float,
    supporting_ids: list[str],
    unscored_reason: str,
) -> dict:
    """Neutral default (5.0) tagged with an unscored_reason.

    used_default=True so the composite excludes it (same as any defaulted
    dimension). The unscored_reason makes "source unavailable" transparent and
    distinguishable from a real, scored 5.
    """
    return {
        "score": 5.0,
        "weight": weight,
        "weighted_contribution": round(5.0 * weight, 4),
        "confidence": Confidence.LOW.value,
        "primary_driver": "Insufficient data — neutral default used",
        "supporting_fact_ids": supporting_ids,
        "used_default": True,
        "unscored_reason": unscored_reason,
    }


def _score_facilities_feasibility(facts: list[dict], weight: float) -> dict:
    """Score facilities_feasibility from the CCD closed-school count.

    A present count (including 0) is scored via the provisional threshold table
    (capped at 8). An absent fact (fetcher returned None) keeps the neutral
    default and is tagged unscored.
    """
    supporting_ids = [f["datapoint_id"] for f in facts]
    closed = _get_fact_value(FACILITIES_CLOSED_SCHOOLS_FACT_KEY, facts)

    if closed is None or not isinstance(closed, (int, float)):
        return _default_dimension_with_tag(
            weight, supporting_ids,
            "closed-school data unavailable (CCD fetcher returned no data)",
        )

    c = int(closed)
    score = _facilities_score_from_closed_count(c)
    driver = (
        f"{c} closed/temporarily-closed school(s) in district+county (last 6 "
        f"CCD years) → facilities score {score:.1f} (closed≠confirmed-available)"
    )
    return {
        "score": round(score, 2),
        "weight": weight,
        "weighted_contribution": round(score * weight, 4),
        "confidence": Confidence.MODERATE.value,
        "primary_driver": driver,
        "supporting_fact_ids": supporting_ids,
        "used_default": False,
    }


def _score_replication_feasibility(facts: list[dict], weight: float) -> dict:
    """Score replication_feasibility as the mean of two federal sub-signals:
    distinct regional CSP operators and statewide CIP-13 completers.

    If one sub-signal is missing, the other is used alone. If both are missing,
    the neutral default is kept and tagged unscored.
    """
    supporting_ids = [f["datapoint_id"] for f in facts]
    operators = _get_fact_value(CSP_OPERATORS_FACT_KEY, facts)
    completers = _get_fact_value(CIP13_COMPLETERS_FACT_KEY, facts)

    sub_scores: list[float] = []
    parts: list[str] = []
    if isinstance(operators, (int, float)):
        op_score = _replication_operator_score(int(operators))
        sub_scores.append(op_score)
        parts.append(f"{int(operators)} CSP operators→{op_score:.0f}")
    if isinstance(completers, (int, float)):
        pipe_score = _replication_pipeline_score(int(completers))
        sub_scores.append(pipe_score)
        parts.append(f"{int(completers)} CIP-13 completers→{pipe_score:.0f}")

    if not sub_scores:
        return _default_dimension_with_tag(
            weight, supporting_ids,
            "operator and pipeline signals unavailable (USAspending + IPEDS "
            "fetchers returned no data)",
        )

    score = round(sum(sub_scores) / len(sub_scores))
    score = float(max(1.0, min(10.0, score)))
    driver = f"replication = mean({', '.join(parts)}) → {score:.1f}"
    return {
        "score": score,
        "weight": weight,
        "weighted_contribution": round(score * weight, 4),
        "confidence": Confidence.MODERATE.value,
        "primary_driver": driver,
        "supporting_fact_ids": supporting_ids,
        "used_default": False,
    }


def _build_driver(fact_key: str, value: Any, score: float, direction: str) -> str:
    """Generate a one-line driver explanation."""
    key_labels = {
        "district_proficiency_ela_pct": "District ELA proficiency",
        "charter_seat_share_pct": "Charter seat share",
        "k12_population_trend_5yr_pct": "K-12 population trend (5yr)",
        "authorizer_approval_rate_pct": "Authorizer approval rate",
        "per_pupil_revenue_vs_state_avg_pct": "Per-pupil revenue vs. state avg",
    }
    label = key_labels.get(fact_key, fact_key.replace("_", " "))
    return f"{label}: {value} → score {score:.1f}"


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _facts_path(state: str, community_id: str) -> str:
    return f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"


def _output_path(state: str, community_id: str) -> str:
    return f"data/cache/community/{state.lower()}/{community_id}/s5_scorecard.json"
