"""
pipeline/s4_verification.py
Stage 4: Verification and Confidence Tagging

Two-pass verification:
  Pass A — Deterministic: URL pattern matching against sources.yaml
  Pass B — LLM (Haiku): Classify remaining facts and set in_main_analysis

INPUT:  data/cache/community/{state}/{community_id}/s3_facts_raw.json
OUTPUT: data/cache/community/{state}/{community_id}/s4_verified.json
"""
from __future__ import annotations
import difflib
import json
import logging
import os
import re
import time
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

from pipeline import PipelineConfig, StageResult, StageStatus, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager
from pipeline.ccd_entity_verifier import (
    SOURCE_LABEL as CCD_SOURCE_LABEL,
    extract_named_entities,
    fetch_charter_roster,
)

STAGE_ID = "s4_verification"

# Source classes that block in_main_analysis regardless of confidence
BLOCKED_SOURCE_CLASSES = {"ADVOCACY", "SELF_REPORTED", "UNVERIFIED"}

# Source classes exempt from null-URL demotion (Pass C): these data types
# legitimately lack a web URL but are authoritative primary sources.
TRUSTED_SOURCE_CLASSES = {"FEDERAL_DATA", "PED_DATA", "STATUTE"}

# Pass D: fact dimensions/keys for which an unconfirmed named entity warrants
# demotion (role-assignment claims, where naming a non-existent operator is the
# KIPP-type hallucination). Numeric/stat claims are checked but never demoted.
ENTITY_DEMOTE_CATEGORIES = ("operator", "replication", "cmo", "partner", "management")
ENTITY_MATCH_CUTOFF = 0.82


def run(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    **kwargs
) -> StageResult:
    start = time.time()

    cache = CacheManager(config)
    cache_key = f"community/{state.lower()}/{community_id}/s4_verified.json"

    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            s3_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
            s4_path = f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"
            if (
                os.path.exists(s3_path)
                and os.path.exists(s4_path)
                and os.path.getmtime(s3_path) > os.path.getmtime(s4_path)
            ):
                logger.info(
                    "[%s] S4 cache predates S3 output — invalidating to propagate fresh facts",
                    community_id,
                )
                # Fall through to full re-run
            else:
                return StageResult(
                    stage_id=STAGE_ID, community_id=community_id, state=state,
                    status=StageStatus.SUCCESS, output_data=cached,
                    cache_hit=True, duration_seconds=round(time.time() - start, 2)
                )

    # Load S3 output
    if previous_result and previous_result.output_data:
        raw_bundle = previous_result.output_data
    else:
        raw_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
        if not os.path.exists(raw_path):
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.ERROR,
                errors=[f"S3 output not found at {raw_path}. Run S3 first."]
            )
        with open(raw_path) as f:
            raw_bundle = json.load(f)

    # Load source patterns from config
    with open("config/sources.yaml") as f:
        sources_cfg = yaml.safe_load(f)
    url_patterns = sources_cfg.get("url_patterns", [])
    source_classes = sources_cfg.get("source_classes", {})

    # Pass A: Deterministic URL classification
    facts = raw_bundle.get("facts", [])
    facts = [_classify_by_url(f, url_patterns) for f in facts]

    # Pass B: LLM confidence tagging (Haiku)
    if not config.dry_run:
        sources_summary = _build_sources_summary(source_classes)
        prompt_text = load_prompt("prompts/audit/s4_verification_tags.md", {
            "FACTS_JSON": json.dumps(facts, indent=2),
            "COMMUNITY_ID": community_id,
            "TODAY_DATE": today_str(),
            "SOURCES_YAML_SUMMARY": sources_summary,
        })
        result = call_claude(
            model=config.model_haiku,
            system="You are a verification classifier. Respond ONLY with valid JSON.",
            user=prompt_text,
            max_tokens=1000,
            temperature=0.0,
            expect_json=True,
            stage=STAGE_ID,
            community_id=community_id
        )
        if not result.parse_error and result.parsed_json:
            facts = result.parsed_json.get("facts", facts)
        tokens_used = result.total_tokens if not config.dry_run else 0
    else:
        tokens_used = 0

    # Enforce in_main_analysis rules deterministically (non-negotiable)
    facts = [_enforce_main_analysis_rules(f) for f in facts]

    # Detect PCI promotion (stale S3 cache recovery) and strip the internal marker.
    pci_promoted = any(f.pop("_pci_promoted", False) for f in facts)
    if pci_promoted:
        logger.warning(
            "[%s] PCI promoted from legacy cache entry (in_main_analysis was False). "
            "Re-run S3 to generate a fresh signal.",
            community_id,
        )

    # Pass D: live NCES CCD charter-roster entity verification.
    # Degrades gracefully — if the roster cannot be fetched, facts are untouched.
    facts, entity_summary = _pass_d_entity_verification(facts, community_id, state, config)

    # Pass E: static config resolution for NEEDS_VERIFICATION flags.
    # Reads pec_renewal_stats.yaml and nmped_shortage_areas.yaml; no-ops
    # when configs have null values (placeholder state).
    facts = _pass_e_static_resolution(facts, community_id, state)

    summary = _build_summary(facts)
    summary["entity_verification"] = entity_summary

    # True when real proficiency data was injected into S3 prompt (NM, MS).
    # False for states without proficiency adapters (TN, WI, etc.) — S5 uses
    # this to exclude academic_need from the composite rather than defaulting to 5.0.
    has_proficiency_data = any(
        f.get("fact_key") == "district_proficiency_ela_pct"
        and f.get("source_class") == "PED_DATA"
        and f.get("confidence") in ("HIGH", "MODERATE")
        for f in facts
    )

    verified_bundle = {
        "community_id": community_id,
        "state": state,
        "verified_at": today_str(),
        "facts": facts,
        "summary": summary,
        "pci_promoted_from_cache": pci_promoted,
        "has_proficiency_data": has_proficiency_data,
    }

    out_path = f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(verified_bundle, f, indent=2)

    cache.set(cache_key, verified_bundle)

    return StageResult(
        stage_id=STAGE_ID, community_id=community_id, state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        output_data=verified_bundle, tokens_used=tokens_used,
        duration_seconds=round(time.time() - start, 2)
    )


def _classify_by_url(fact: dict, url_patterns: list[dict]) -> dict:
    """Infer source_class from URL before LLM pass."""
    url = fact.get("source_url") or ""
    for pattern in url_patterns:
        if pattern["pattern"] in url:
            fact = dict(fact)
            fact["source_class"] = pattern["source_class"]
            if "bias_direction" in pattern:
                fact["bias_flag"] = pattern["bias_direction"]
            return fact
    return fact


def _enforce_main_analysis_rules(fact: dict) -> dict:
    """Hard rule: certain source classes or low confidence always blocks in_main_analysis."""
    fact = dict(fact)
    # Use None as default (not "UNVERIFIED" / "NONE") so that facts whose
    # source_class or confidence were never set by Pass A or Pass B are not
    # silently blocked. The original defaults caused false positives: a fact
    # whose URL didn't match any pattern AND whose LLM classification was
    # absent would default to UNVERIFIED/NONE and be incorrectly excluded.
    source_class = fact.get("source_class")
    confidence = fact.get("confidence")

    # political_climate_index is exempt from the LOW-confidence block (S29):
    # a present index value always passes through to S5, regardless of source_class,
    # confidence, or what S3 previously set. Confidence label is preserved.
    _is_pci = fact.get("fact_key") == "political_climate_index" and fact.get("value") is not None

    if (source_class in BLOCKED_SOURCE_CLASSES or confidence in ("LOW", "NONE")) and not _is_pci:
        fact["in_main_analysis"] = False
        if not fact.get("needs_verification_reason"):
            fact["needs_verification_reason"] = (
                f"Blocked: source_class={source_class}, confidence={confidence}"
            )
    elif _is_pci:
        # Actively gate-open: promote in_main_analysis=True unconditionally so
        # stale S3 caches (which may carry in_main_analysis=False) are corrected.
        if fact.get("in_main_analysis") is False:
            fact["_pci_promoted"] = True  # signals run() to set pci_promoted_from_cache
        fact["in_main_analysis"] = True

    # Null-URL VERIFIED demotion: a fact cannot remain VERIFIED + in_main_analysis
    # without a traceable source URL (closes the KIPP _002 pattern — VERIFIED with
    # a null source_url). Exempt trusted source classes (FEDERAL_DATA, PED_DATA,
    # STATUTE) that legitimately lack a web URL. Missing/unknown source_class is
    # treated as untrusted and still triggers demotion.
    if (
        fact.get("verification_status") == "VERIFIED"
        and not (fact.get("source_url") or "").strip()
        and fact.get("in_main_analysis") is True
        and fact.get("source_class") not in TRUSTED_SOURCE_CLASSES
    ):
        fact["in_main_analysis"] = False
        fact["verification_status"] = "PROVISIONAL"
        fact["needs_verification_reason"] = (
            "Marked VERIFIED but no source URL present — cannot confirm claim "
            "against primary source. Requires human verification."
        )
    return fact


# ─────────────────────────────────────────────────────────────────────────────
# Pass E — static config resolution
# ─────────────────────────────────────────────────────────────────────────────

def _load_pec_renewal_stats() -> Optional[dict]:
    """Return pec_renewal_stats.yaml as a dict only when all three numeric
    fields are non-null; otherwise None (config not ready to resolve flags)."""
    try:
        with open("config/pec_renewal_stats.yaml") as f:
            data = yaml.safe_load(f) or {}
        required = ("renewal_rate", "denial_rate", "median_approval_days")
        if all(data.get(k) is not None for k in required):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("S4 Pass E: could not load pec_renewal_stats.yaml — %s", exc)
    return None


def _load_shortage_districts() -> list:
    """Return the district list from nmped_shortage_areas.yaml, or []."""
    try:
        with open("config/nmped_shortage_areas.yaml") as f:
            data = yaml.safe_load(f) or {}
        return data.get("districts") or []
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("S4 Pass E: could not load nmped_shortage_areas.yaml — %s", exc)
        return []


def _is_pec_renewal_fact(fact: dict) -> bool:
    text = " ".join(filter(None, [
        fact.get("fact_key"), fact.get("dimension"),
        fact.get("needs_verification_reason"), fact.get("claim"),
    ])).lower()
    return "pec" in text and ("renewal" in text or "denial" in text)


def _is_shortage_fact(fact: dict) -> bool:
    text = " ".join(filter(None, [
        fact.get("fact_key"), fact.get("dimension"),
        fact.get("needs_verification_reason"), fact.get("claim"),
    ])).lower()
    return "shortage" in text and "teacher" in text


def _pass_e_static_resolution(
    facts: list[dict], community_id: str, state: str
) -> list[dict]:
    """Resolve NEEDS_VERIFICATION flags using static hand-populated config.

    PEC renewal: resolves facts whose text references PEC + renewal/denial
    when config/pec_renewal_stats.yaml has all three numeric fields non-null.

    Shortage areas: resolves teacher shortage facts when the community's
    city appears in config/nmped_shortage_areas.yaml districts list.
    """
    pec_stats = _load_pec_renewal_stats()
    shortage_districts = _load_shortage_districts()

    _, city_display, _ = _resolve_district(community_id, state)
    district_in_shortage = bool(shortage_districts) and any(
        city_display.lower() in d.lower() for d in shortage_districts
    )

    out: list[dict] = []
    for fact in facts:
        fact = dict(fact)
        if fact.get("verification_status") == "NEEDS_VERIFICATION":
            if pec_stats and _is_pec_renewal_fact(fact):
                fact["verification_status"] = "VERIFIED"
                fact["in_main_analysis"] = True
                fact["needs_verification_reason"] = None
                fact["verification_note"] = (
                    f"Resolved via config/pec_renewal_stats.yaml "
                    f"(source: {pec_stats.get('source')}, vintage: {pec_stats.get('vintage')})"
                )
            elif district_in_shortage and _is_shortage_fact(fact):
                fact["verification_status"] = "VERIFIED"
                fact["in_main_analysis"] = True
                fact["needs_verification_reason"] = None
                # TODO(S35-sweep): verification note references NM-specific config file.
                # Generalize to config/{state}_ped_shortage_areas.yaml or a states.yaml entry.
                fact["verification_note"] = (
                    "Resolved via config/nmped_shortage_areas.yaml — "
                    "district confirmed on NM PED teacher shortage list"
                )
        out.append(fact)
    return out


def _build_summary(facts: list[dict]) -> dict:
    return {
        "total_facts": len(facts),
        "in_main_analysis": sum(1 for f in facts if f.get("in_main_analysis")),
        "routed_to_verification": sum(1 for f in facts if not f.get("in_main_analysis")),
        "high_confidence": sum(1 for f in facts if f.get("confidence") == "HIGH"),
        "moderate_confidence": sum(1 for f in facts if f.get("confidence") == "MODERATE"),
        "low_confidence": sum(1 for f in facts if f.get("confidence") in ("LOW", "NONE")),
    }


def _build_sources_summary(source_classes: dict) -> str:
    lines = []
    for name, cfg in source_classes.items():
        lines.append(
            f"{name}: default_confidence={cfg['confidence_default']}, "
            f"allows_in_main={cfg['allows_in_main']}, "
            f"requires_url={cfg['requires_url']}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Pass D — NCES CCD entity roster verification
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_district(community_id: str, state: str):
    """Return (leaid, city_display, state_fips) for a community.

    LEAID comes from config/states.yaml `nces_district_map` (state-agnostic; may
    be None for communities with no NCES LEA, e.g. tribal-land entries). The
    state FIPS code is read from the per-state config (not hardcoded) and used to
    scope the statewide city charter pull. The city display string is derived
    from the community_id and used for the statewide filter and verification notes.
    """
    leaid = None
    state_fips = None
    try:
        with open("config/states.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        state_cfg = cfg.get(state.upper(), {}) or {}
        leaid = (state_cfg.get("nces_district_map", {}) or {}).get(community_id)
        state_fips = state_cfg.get("state_fips")
    except Exception as exc:
        logger.warning("Could not load district config from states.yaml: %s", exc)

    prefix = f"{state.lower()}-"
    city = community_id[len(prefix):] if community_id.startswith(prefix) else community_id
    city = city.replace("-", " ").strip().title()
    return leaid, city, state_fips


def _pass_d_entity_verification(
    facts: list[dict], community_id: str, state: str, config: PipelineConfig
) -> tuple[list[dict], dict]:
    """Verify named-entity facts against the live NCES CCD charter roster.

    Runs only on facts still in_main_analysis after Pass C. For each such fact
    that names a school/operator, fuzzy-match the name(s) against the district's
    CCD charter roster. Confirmed → tag entity_verified. Unconfirmed AND a
    role-assignment dimension (operator/replication/etc.) → demote and route to
    verification. The CCD module never raises; if the roster is unavailable the
    whole pass is skipped and facts are returned unchanged.
    """
    summary = {
        "roster_fetched": False,
        "roster_size": 0,
        "facts_checked": 0,
        "entities_confirmed": 0,
        "entities_flagged": 0,
        "roster_source": CCD_SOURCE_LABEL,
    }

    leaid, city, state_fips = _resolve_district(community_id, state)
    roster = fetch_charter_roster(
        city=city, state_abbr=state, cid=community_id, leaid=leaid, state_fips=state_fips
    )

    if roster is None:
        logger.warning("CCD entity verification unavailable — skipping Pass D.")
        return facts, summary

    summary["roster_fetched"] = True
    roster_names = [s["name"] for s in roster.get("charter_schools", []) if s.get("name")]
    roster_names_lc = [n.lower() for n in roster_names]
    summary["roster_size"] = len(roster_names)

    out: list[dict] = []
    for fact in facts:
        fact = dict(fact)

        if not fact.get("in_main_analysis"):
            out.append(fact)
            continue

        entities = extract_named_entities(fact.get("claim") or "")
        if not entities:
            out.append(fact)
            continue

        summary["facts_checked"] += 1

        matched = False
        unmatched: list[str] = []
        for ent in entities:
            if roster_names_lc and difflib.get_close_matches(
                ent.lower(), roster_names_lc, n=1, cutoff=ENTITY_MATCH_CUTOFF
            ):
                matched = True
            else:
                unmatched.append(ent)

        category = f"{fact.get('dimension') or ''} {fact.get('fact_key') or ''}".lower()
        is_role_claim = any(c in category for c in ENTITY_DEMOTE_CATEGORIES)

        if matched:
            fact["entity_verified"] = True
            fact["entity_verification_source"] = CCD_SOURCE_LABEL
            summary["entities_confirmed"] += 1
        elif is_role_claim:
            # No roster match on a role-assignment claim → KIPP-type risk. Demote.
            if fact.get("confidence") == "HIGH":
                fact["confidence"] = "MODERATE"
            fact["verification_status"] = "PROVISIONAL"
            fact["in_main_analysis"] = False
            fact["entity_verified"] = False
            fact["entity_verification_source"] = CCD_SOURCE_LABEL
            flagged = unmatched[0] if unmatched else (entities[0] if entities else "?")
            fact["needs_verification_reason"] = (
                f"Named entity '{flagged}' not found in NCES CCD charter roster for "
                f"{city}, {state}. Requires human confirmation before use in analysis."
            )
            summary["entities_flagged"] += 1
        else:
            # Non-role claim with no roster match: record the check, do not demote.
            fact["entity_verified"] = False
            fact["entity_verification_source"] = CCD_SOURCE_LABEL

        out.append(fact)

    return out, summary
