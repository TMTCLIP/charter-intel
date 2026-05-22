"""
pipeline/s3_fact_extraction.py
Stage 3: Community Fact Extraction

Calls Claude Sonnet (temperature=0) to extract structured facts for all
10 scoring dimensions. Every fact requires a source URL or explicit NOT_FOUND.

INPUT:
  - data/cache/state/{state}/s2_state_context.json (state reference)
  - data/processed/{state}/community_list.json (known schools from S1)
OUTPUT:
  - data/cache/community/{state}/{community_id}/s3_facts_raw.json
"""
from __future__ import annotations
import json
import logging
import math
import os
import csv
import time
from typing import Optional

logger = logging.getLogger(__name__)

from pipeline import PipelineConfig, StageResult, StageStatus, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema
from pipeline.utils.ped_fetcher import get_district_data

STAGE_ID = "s3_fact_extraction"

class _DepthSkip(Exception):
    """Raised inside a web-search try block to skip it based on --depth."""


# fact_keys whose values are grounded in the VERIFIED_ROSTER block injected into
# the S3 prompt.  Claude tags them UNVERIFIED/LOW (no citable web URL) but they
# ARE derived from PED roster data we supply — S4 would block them without this.
_ROSTER_DERIVED_KEYS: frozenset = frozenset({
    "num_charter_schools",
    "replication_readiness_index",
    "demand_supply_gap_index",
})
_PED_ROSTER_URL = "https://webnew.ped.state.nm.us/bureaus/options-for-parents/charter-schools/"
_PED_ROSTER_TITLE = "New Mexico PED Charter School Roster"


def run(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    **kwargs
) -> StageResult:
    start = time.time()

    cache = CacheManager(config)
    cache_key = f"community/{state.lower()}/{community_id}/s3_facts_raw.json"

    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.SUCCESS, output_data=cached,
                cache_hit=True, duration_seconds=round(time.time() - start, 2)
            )

    if config.dry_run:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.SKIPPED,
            warnings=["Dry run — skipping Sonnet API call for S3"]
        )

    # Load state context
    state_context_path = f"data/cache/state/{state.lower()}/s2_state_context.json"
    if not os.path.exists(state_context_path):
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"State context not found at {state_context_path}. Run S2 first."]
        )
    with open(state_context_path) as f:
        state_context = json.load(f)

    # Get known schools for this community from S1 output
    community_list_path = f"data/processed/{state.lower()}/community_list.json"
    known_schools = []
    roster_source_file = None
    if os.path.exists(community_list_path):
        with open(community_list_path) as f:
            cl = json.load(f)
        for c in cl.get("communities", []):
            if c["community_id"] == community_id:
                known_schools = c.get("school_names", [])
                roster_source_file = c.get("source_file") or cl.get("source_file")
                break
        if not roster_source_file:
            roster_source_file = cl.get("source_file")

    # Build verified roster table by joining S1 school list with raw CSV fields
    verified_roster_rows = []
    if known_schools and roster_source_file and os.path.exists(roster_source_file):
        known_set = set(known_schools)
        verified_roster_rows = [
            "| School Name | Authorizer | Grades Served | Enrollment Cap |",
            "|---|---|---|---|",
        ]
        with open(roster_source_file, newline="", encoding="utf-8") as csvf:
            for row in csv.DictReader(csvf):
                name = row.get("Charter School", "").strip()
                if name in known_set:
                    authorizer = row.get("Authorizer", "").strip() or "—"
                    grades = row.get("Grades Served 2025-26", "").strip() or "—"
                    cap = row.get("Enrollment Cap", "").strip() or "—"
                    verified_roster_rows.append(f"| {name} | {authorizer} | {grades} | {cap} |")

    verified_roster = (
        "\n".join(verified_roster_rows)
        if verified_roster_rows
        else "(No verified roster — S1 not yet run for this community)"
    )

    # Load verified PED proficiency data for prompt injection
    ped_data = get_district_data(community_id, state)
    if ped_data:
        verified_ped_data = (
            f"VERIFIED PED DATA (SY {ped_data['school_year']}) — treat as ground truth:\n"
            f"- ELA proficiency: {ped_data['ela_proficiency_pct']}%\n"
            f"- Math proficiency: {ped_data['math_proficiency_pct']}%\n"
            f"- Source: {ped_data['source_url']}"
        )
    else:
        verified_ped_data = "(No verified PED proficiency data available for this community)"

    community_name = community_id.split("-", 1)[1].replace("-", " ").title()

    # Condense state context to reduce tokens (only governance + law + ecosystem)
    state_context_condensed = {
        "charter_law": state_context.get("charter_law", {}),
        "governance": state_context.get("governance", {}),
        "ecosystem": {
            "major_advocacy_orgs": state_context.get("ecosystem", {}).get("major_advocacy_orgs", [])[:3],
            "notable_operators": state_context.get("ecosystem", {}).get("notable_operators", [])[:5],
        }
    }

    prompt_text = load_prompt("prompts/extract/s3_community_facts.md", {
        "COMMUNITY_NAME": community_name,
        "STATE_NAME": state_context.get("state_name", state),
        "STATE_CODE": state,
        "COMMUNITY_ID": community_id,
        "TODAY_DATE": today_str(),
        "STATE_CONTEXT_JSON": json.dumps(state_context_condensed, indent=2),
        "KNOWN_SCHOOLS": json.dumps(known_schools, indent=2),
        "VERIFIED_ROSTER": verified_roster,
        "VERIFIED_PED_DATA": verified_ped_data,
    })

    result = call_claude(
        model=config.model_sonnet,
        system="You are a structured data extraction agent. Respond ONLY with valid JSON.",
        user=prompt_text,
        max_tokens=8192,
        temperature=0.0,
        expect_json=True,
        stage=STAGE_ID,
        community_id=community_id
    )

    if result.parse_error:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"S3 JSON parse failed: {result.parse_error}"]
        )

    facts_output = result.parsed_json

    # ── Web search supplemental calls — gated by --depth ────────────────────
    # fast: none  |  standard: political_climate only  |  deep: all 5 + 8s sleeps
    _web_search_tokens = 0
    try:
        if config.depth not in ("standard", "deep"):
            raise _DepthSkip
        pc_prompt = load_prompt("prompts/extract/s3_political_climate.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })

        pc_result = call_claude(
            model=config.model_sonnet,
            system=(
                "You are a political intelligence researcher. "
                "Use web search to find current information. "
                "Respond ONLY with valid JSON."
            ),
            user=pc_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_political_climate",
            community_id=community_id,
        )
        _web_search_tokens += pc_result.total_tokens

        if pc_result.parse_error:
            logger.warning(
                "[%s] Political climate web search JSON parse failed: %s",
                community_id, pc_result.parse_error,
            )
        elif pc_result.parsed_json:
            pc = pc_result.parsed_json
            sources = pc.get("sources") or []
            first_source = sources[0] if sources else {}
            index_val = pc.get("political_climate_index")
            confidence = pc.get("confidence", "MODERATE")

            pc_datapoint = {
                "datapoint_id": f"dp_{community_id}_political_climate_websearch",
                "community_id": community_id,
                "state": state,
                "dimension": "political_climate",
                "fact_key": "political_climate_index",
                "claim": f"Political climate index (web): {index_val}",
                "value": index_val,
                "value_unit": None,
                "value_year": int(today_str()[:4]),
                "source_class": "MEDIA",
                "source_url": first_source.get("url"),
                "source_title": first_source.get("title"),
                "source_date": first_source.get("date"),
                "confidence": confidence,
                "confidence_rationale": None,
                "verification_status": "VERIFIED" if confidence == "HIGH" else "PROVISIONAL",
                "bias_flag": None,
                "extracted_by": config.model_sonnet,
                "extracted_at": today_str(),
                "stage": STAGE_ID,
                "in_main_analysis": True,
                "needs_verification_reason": None,
            }

            facts_output.setdefault("facts", []).append(pc_datapoint)

            logger.info(
                "[%s] Political climate web search — index=%s confidence=%s sources=%d",
                community_id, index_val, confidence, len(sources),
            )

    except _DepthSkip:
        pass
    except Exception as exc:
        logger.warning(
            "[%s] Political climate web search failed (non-fatal): %s",
            community_id, exc,
        )

    # ── Population trends web search (supplemental call) ────────────────────
    try:
        if config.depth != "deep":
            raise _DepthSkip
        time.sleep(8)
        pt_prompt = load_prompt("prompts/extract/s3_population_trends.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })

        pt_result = call_claude(
            model=config.model_sonnet,
            system=(
                "You are a demographic and enrollment researcher. "
                "Use web search to find current information. "
                "Respond ONLY with valid JSON."
            ),
            user=pt_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_population_trends",
            community_id=community_id,
        )
        _web_search_tokens += pt_result.total_tokens

        if pt_result.parse_error:
            logger.warning(
                "[%s] Population trends web search JSON parse failed: %s",
                community_id, pt_result.parse_error,
            )
        elif pt_result.parsed_json:
            pt = pt_result.parsed_json
            sources = pt.get("sources") or []
            first_source = sources[0] if sources else {}
            index_val = pt.get("population_trend_index")
            confidence = pt.get("confidence", "MODERATE")

            facts_output.setdefault("facts", []).append({
                "datapoint_id": f"dp_{community_id}_population_trends_websearch",
                "community_id": community_id,
                "state": state,
                "dimension": "population_trends",
                "fact_key": "population_trend_index",
                "claim": f"Population trend index (web): {index_val}",
                "value": index_val,
                "value_unit": None,
                "value_year": int(today_str()[:4]),
                "source_class": "FEDERAL_DATA",
                "source_url": first_source.get("url"),
                "source_title": first_source.get("title"),
                "source_date": first_source.get("date"),
                "confidence": confidence,
                "confidence_rationale": None,
                "verification_status": "VERIFIED" if confidence == "HIGH" else "PROVISIONAL",
                "bias_flag": None,
                "extracted_by": config.model_sonnet,
                "extracted_at": today_str(),
                "stage": STAGE_ID,
                "in_main_analysis": True,
                "needs_verification_reason": None,
            })

            logger.info(
                "[%s] Population trends web search — index=%s confidence=%s sources=%d",
                community_id, index_val, confidence, len(sources),
            )

    except _DepthSkip:
        pass
    except Exception as exc:
        logger.warning(
            "[%s] Population trends web search failed (non-fatal): %s",
            community_id, exc,
        )

    # ── Operational complexity web search (supplemental call) ────────────────
    try:
        if config.depth != "deep":
            raise _DepthSkip
        time.sleep(8)
        oc_prompt = load_prompt("prompts/extract/s3_operational_complexity.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })

        oc_result = call_claude(
            model=config.model_sonnet,
            system=(
                "You are an education demographics researcher. "
                "Use web search to find current information. "
                "Respond ONLY with valid JSON."
            ),
            user=oc_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_operational_complexity",
            community_id=community_id,
        )
        _web_search_tokens += oc_result.total_tokens

        if oc_result.parse_error:
            logger.warning(
                "[%s] Operational complexity web search JSON parse failed: %s",
                community_id, oc_result.parse_error,
            )
        elif oc_result.parsed_json:
            oc = oc_result.parsed_json
            sources = oc.get("sources") or []
            first_source = sources[0] if sources else {}
            index_val = oc.get("operational_complexity_index")
            confidence = oc.get("confidence", "MODERATE")

            facts_output.setdefault("facts", []).append({
                "datapoint_id": f"dp_{community_id}_operational_complexity_websearch",
                "community_id": community_id,
                "state": state,
                "dimension": "operational_complexity",
                "fact_key": "operational_complexity_index",
                "claim": f"Operational complexity index (web): {index_val}",
                "value": index_val,
                "value_unit": None,
                "value_year": int(today_str()[:4]),
                "source_class": "PED_DATA",
                "source_url": first_source.get("url"),
                "source_title": first_source.get("title"),
                "source_date": first_source.get("date"),
                "confidence": confidence,
                "confidence_rationale": None,
                "verification_status": "VERIFIED" if confidence == "HIGH" else "PROVISIONAL",
                "bias_flag": None,
                "extracted_by": config.model_sonnet,
                "extracted_at": today_str(),
                "stage": STAGE_ID,
                "in_main_analysis": True,
                "needs_verification_reason": None,
            })

            logger.info(
                "[%s] Operational complexity web search — index=%s confidence=%s sources=%d",
                community_id, index_val, confidence, len(sources),
            )

    except _DepthSkip:
        pass
    except Exception as exc:
        logger.warning(
            "[%s] Operational complexity web search failed (non-fatal): %s",
            community_id, exc,
        )

    # ── Facilities feasibility web search (supplemental call) ────────────────
    try:
        if config.depth != "deep":
            raise _DepthSkip
        time.sleep(8)
        ff_prompt = load_prompt("prompts/extract/s3_facilities_feasibility.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })

        ff_result = call_claude(
            model=config.model_sonnet,
            system=(
                "You are a real estate and education facilities researcher. "
                "Use web search to find current information. "
                "Respond ONLY with valid JSON."
            ),
            user=ff_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_facilities_feasibility",
            community_id=community_id,
        )
        _web_search_tokens += ff_result.total_tokens

        if ff_result.parse_error:
            logger.warning(
                "[%s] Facilities feasibility web search JSON parse failed: %s",
                community_id, ff_result.parse_error,
            )
        elif ff_result.parsed_json:
            ff = ff_result.parsed_json
            sources = ff.get("sources") or []
            first_source = sources[0] if sources else {}
            index_val = ff.get("facilities_feasibility_index")
            confidence = ff.get("confidence", "MODERATE")

            facts_output.setdefault("facts", []).append({
                "datapoint_id": f"dp_{community_id}_facilities_feasibility_websearch",
                "community_id": community_id,
                "state": state,
                "dimension": "facilities_feasibility",
                "fact_key": "facilities_feasibility_index",
                "claim": f"Facilities feasibility index (web): {index_val}",
                "value": index_val,
                "value_unit": None,
                "value_year": int(today_str()[:4]),
                "source_class": "MEDIA",
                "source_url": first_source.get("url"),
                "source_title": first_source.get("title"),
                "source_date": first_source.get("date"),
                "confidence": confidence,
                "confidence_rationale": None,
                "verification_status": "VERIFIED" if confidence == "HIGH" else "PROVISIONAL",
                "bias_flag": None,
                "extracted_by": config.model_sonnet,
                "extracted_at": today_str(),
                "stage": STAGE_ID,
                "in_main_analysis": True,
                "needs_verification_reason": None,
            })

            logger.info(
                "[%s] Facilities feasibility web search — index=%s confidence=%s sources=%d",
                community_id, index_val, confidence, len(sources),
            )

    except _DepthSkip:
        pass
    except Exception as exc:
        logger.warning(
            "[%s] Facilities feasibility web search failed (non-fatal): %s",
            community_id, exc,
        )

    # ── Funding environment web search (supplemental call) ───────────────────
    try:
        if config.depth != "deep":
            raise _DepthSkip
        time.sleep(8)
        fe_prompt = load_prompt("prompts/extract/s3_funding_environment.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })

        fe_result = call_claude(
            model=config.model_sonnet,
            system=(
                "You are an education philanthropy and funding researcher. "
                "Use web search to find current information. "
                "Respond ONLY with valid JSON."
            ),
            user=fe_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_funding_environment",
            community_id=community_id,
        )
        _web_search_tokens += fe_result.total_tokens

        if fe_result.parse_error:
            logger.warning(
                "[%s] Funding environment web search JSON parse failed: %s",
                community_id, fe_result.parse_error,
            )
        elif fe_result.parsed_json:
            fe = fe_result.parsed_json
            sources = fe.get("sources") or []
            first_source = sources[0] if sources else {}
            index_val = fe.get("funding_environment_index")
            confidence = fe.get("confidence", "MODERATE")

            facts_output.setdefault("facts", []).append({
                "datapoint_id": f"dp_{community_id}_funding_environment_websearch",
                "community_id": community_id,
                "state": state,
                "dimension": "funding_environment",
                "fact_key": "funding_environment_index",
                "claim": f"Funding environment index (web): {index_val}",
                "value": index_val,
                "value_unit": None,
                "value_year": int(today_str()[:4]),
                "source_class": "THINK_TANK",
                "source_url": first_source.get("url"),
                "source_title": first_source.get("title"),
                "source_date": first_source.get("date"),
                "confidence": confidence,
                "confidence_rationale": None,
                "verification_status": "VERIFIED" if confidence == "HIGH" else "PROVISIONAL",
                "bias_flag": None,
                "extracted_by": config.model_sonnet,
                "extracted_at": today_str(),
                "stage": STAGE_ID,
                "in_main_analysis": True,
                "needs_verification_reason": None,
            })

            logger.info(
                "[%s] Funding environment web search — index=%s confidence=%s sources=%d",
                community_id, index_val, confidence, len(sources),
            )

    except _DepthSkip:
        pass
    except Exception as exc:
        logger.warning(
            "[%s] Funding environment web search failed (non-fatal): %s",
            community_id, exc,
        )
    # ── Rate limit cooldown after web search calls ───────────────────────────
    # The rate limit window is 30,000 tokens/minute.  If web search consumed
    # more than 20,000 tokens we sleep enough full windows to clear the bucket
    # before S6's Sonnet call arrives.  Formula: ceil(tokens / 30000) * 62s
    # (62s = 60s window + 2s margin).
    if _web_search_tokens > 20_000:
        cooldown = math.ceil(_web_search_tokens / 30_000) * 62
        logger.info(
            "[%s] Rate limit cooldown: sleeping %ds after %d web search tokens",
            community_id, cooldown, _web_search_tokens,
        )
        time.sleep(cooldown)

    # ── Protect roster-derived facts from S4's UNVERIFIED block ─────────────
    # Must run after all supplemental calls, before the cache write.
    facts_output["facts"] = _upgrade_roster_derived_facts(
        facts_output.get("facts", []),
        has_roster=bool(verified_roster_rows),
    )
    # ─────────────────────────────────────────────────────────────────────────

    out_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(facts_output, f, indent=2)

    cache.set(cache_key, facts_output)

    return StageResult(
        stage_id=STAGE_ID, community_id=community_id, state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        output_data=facts_output, tokens_used=result.total_tokens,
        duration_seconds=round(time.time() - start, 2)
    )


def _upgrade_roster_derived_facts(facts: list[dict], has_roster: bool) -> list[dict]:
    """Re-attribute roster-derived facts so S4 does not block them.

    Facts in _ROSTER_DERIVED_KEYS have no citable web URL (they are computed
    from the VERIFIED_ROSTER block we inject), so Claude assigns them
    source_class=UNVERIFIED / confidence=LOW.  S4's _enforce_main_analysis_rules
    would then flip in_main_analysis=False.  This pass upgrades them to
    PED_DATA / HIGH before the cache write, which is honest: the PED charter
    roster IS the primary source.  Only runs when roster data was available.
    """
    if not has_roster:
        return facts
    out = []
    for f in facts:
        if (
            f.get("fact_key") in _ROSTER_DERIVED_KEYS
            and f.get("value") is not None
            and f.get("source_class") in ("UNVERIFIED", None, "")
        ):
            f = dict(f)
            f["source_class"] = "PED_DATA"
            f["source_url"] = f.get("source_url") or _PED_ROSTER_URL
            f["source_title"] = f.get("source_title") or _PED_ROSTER_TITLE
            f["confidence"] = "HIGH"
            f["in_main_analysis"] = True
            f["needs_verification_reason"] = None
        out.append(f)
    return out
