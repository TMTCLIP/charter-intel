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

from pipeline import PipelineConfig, StageResult, StageStatus, OutputMode, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema
from pipeline.utils.ped_fetcher import get_district_data
from pipeline.utils.nces_fetcher import get_district_data as get_nces_district_data
from pipeline.utils.acs_fetcher  import get_district_data as get_acs_district_data, get_total_population as get_acs_total_population
from pipeline.utils.population_trends_fetcher import get_population_trends
from pipeline.utils.charter_schools_fetcher import get_community_charter_schools
from pipeline.utils.authorizers_fetcher     import get_community_authorizers

STAGE_ID = "s3_fact_extraction"

# Charter intel: only pass the top N schools to the prompt (CSV already sorted
# by ELA proficiency descending). Claude selects 5 from this set. Passing all
# 58 Albuquerque schools inflates the base prompt to ~10k tokens.
_MAX_CHARTER_INTEL_SCHOOLS = 10

# Web result truncation — caps content/snippet fields in any list-of-dicts
# payload before it's serialized into a prompt string.
_WEB_RESULT_MAX_CHARS = 600


def _truncate_web_results(results: list[dict]) -> list[dict]:
    """Truncate each web result's content field to reduce prompt bloat.
    Preserves url and title in full — only content/snippet/text/body is capped."""
    truncated = []
    for r in results:
        r = dict(r)
        for field in ("content", "snippet", "text", "body"):
            if field in r and isinstance(r[field], str):
                if len(r[field]) > _WEB_RESULT_MAX_CHARS:
                    r[field] = r[field][:_WEB_RESULT_MAX_CHARS] + "…"
        truncated.append(r)
    return truncated


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

    # Load verified NCES district finance + FRL data for prompt injection
    nces_data = get_nces_district_data(community_id, state)
    if nces_data:
        ppr_vs_avg = nces_data.get("per_pupil_revenue_vs_state_avg_pct")
        ppe        = nces_data.get("per_pupil_expenditure")
        fed_pct    = nces_data.get("revenue_federal_pct")
        state_pct  = nces_data.get("revenue_state_pct")
        loc_pct    = nces_data.get("revenue_local_pct")
        frl_pct    = nces_data.get("frl_pct")
        verified_nces_data = (
            f"VERIFIED NCES DATA (FY {nces_data['fiscal_year']}) — treat as ground truth:\n"
            + (f"- Per-pupil revenue vs. NM state avg: {ppr_vs_avg:+.1f}%\n" if ppr_vs_avg is not None else "")
            + (f"- Per-pupil expenditure: ${ppe:,.0f}\n" if ppe is not None else "")
            + (f"- Revenue mix: {fed_pct}% federal / {state_pct}% state / {loc_pct}% local\n"
               if all(v is not None for v in (fed_pct, state_pct, loc_pct)) else "")
            + (f"- FRL%: {frl_pct}% of students qualify for free/reduced-price lunch\n"
               if frl_pct is not None else "")
            + f"- Source: {nces_data['source_url']}"
        )
    else:
        verified_nces_data = "(No verified NCES district finance data available for this community)"

    # Load verified ACS ELL data for prompt injection
    acs_data = get_acs_district_data(community_id, state)
    if acs_data:
        verified_acs_data = (
            f"VERIFIED CENSUS ACS DATA ({acs_data['data_year']}) — treat as ground truth:\n"
            f"- ELL%: {acs_data['ell_pct']}% of students ages 5–17 speak English less than very well\n"
            f"- Source: {acs_data['source_url']}"
        )
    else:
        verified_acs_data = "(No verified Census ACS ELL data available — CENSUS_API_KEY not set or no district mapping)"

    # Load NCES enrollment trend data (district-level, 2020–2024)
    pop_data = get_population_trends(community_id, state)
    if pop_data is None:
        logger.warning(
            "[%s] population_trends_fetcher returned None — no enrollment trend data for this community",
            community_id,
        )

    # Load ACS total population (secondary signal — two vintages, not scored)
    acs_pop_data = get_acs_total_population(community_id, state)

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
        "VERIFIED_NCES_DATA": verified_nces_data,
        "VERIFIED_ACS_DATA": verified_acs_data,
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

    # ── Inject NCES fact datapoints (funding_environment + frl_pct) ──────────
    # These are pre-computed from federal government files and are more
    # authoritative than anything Claude could extract.  Runs after the roster
    # upgrade so it has the final facts list to deduplicate against.
    _inject_nces_facts(facts_output, nces_data, community_id, state, config)

    # ── Inject ACS fact datapoints (ell_pct → operational_complexity) ────────
    _inject_acs_facts(facts_output, acs_data, community_id, state)

    # ── Inject NCES enrollment trend facts (population_trends dimension) ──────
    _inject_population_trends_facts(facts_output, pop_data, community_id, state)

    # ── Inject ACS total population (secondary signal, not scored) ────────────
    _inject_acs_population_facts(facts_output, acs_pop_data, community_id, state)

    # ── Charter schools + authorizer intelligence ─────────────────────────────
    # Scan mode skips both fetchers: charter_intel feeds brief narrative only and
    # does not contribute to any scoring dimension.  Cost: ~$0.05/city saved.
    if config.mode == OutputMode.SCAN:
        logger.info(
            "[%s] Scan mode — charter_intel and authorizer fetchers intentionally skipped",
            community_id,
        )
        facts_output["charter_schools"]  = []
        facts_output["local_authorizers"] = []
    else:
        csv_schools     = get_community_charter_schools(known_schools, roster_source_file)
        csv_authorizers = get_community_authorizers(known_schools, roster_source_file)

        charter_intel = _fetch_charter_intel(
            community_id=community_id,
            community_name=community_name,
            state=state,
            state_name=state_context.get("state_name", state),
            csv_schools=csv_schools,
            csv_authorizers=csv_authorizers,
            config=config,
        )
        facts_output["charter_schools"]  = charter_intel.get("top_charter_schools", csv_schools)
        facts_output["local_authorizers"] = charter_intel.get("local_authorizers", csv_authorizers)
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


# Maps NCES result keys → (scoring dimension, value_unit)
# Only keys that appear in scoring_weights.yaml fact_keys or are useful context.
_NCES_FACT_KEY_MAP: dict[str, tuple[str, str]] = {
    "per_pupil_revenue_vs_state_avg_pct": ("funding_environment",    "percent"),
    "per_pupil_expenditure":             ("funding_environment",    "USD"),
    "revenue_federal_pct":               ("funding_environment",    "percent"),
    "revenue_state_pct":                 ("funding_environment",    "percent"),
    "revenue_local_pct":                 ("funding_environment",    "percent"),
    "frl_pct":                           ("operational_complexity", "percent"),
    # Derived from frl_pct in nces_fetcher — provides a FEDERAL_DATA-sourced
    # primary_fact for s5_scoring so S4's UNVERIFIED block cannot suppress it.
    "operational_complexity_index":      ("operational_complexity", "index_1_9"),
}
# Metadata keys in the nces_data dict — not fact values, skip them
_NCES_META_KEYS: frozenset = frozenset({"fiscal_year", "source_url", "source_title", "confidence"})


def _inject_nces_facts(
    facts_output: dict,
    nces_data: Optional[dict],
    community_id: str,
    state: str,
    config: PipelineConfig,
) -> None:
    """Append NCES CCD fact datapoints to facts_output.

    Iterates over confirmed NCES keys and appends a pre-formed DataPoint for
    each one that is NOT already present in the facts array with source_class
    FEDERAL_DATA.  This guarantees funding_environment and frl_pct land in
    the facts bundle regardless of what Claude extracted.

    Called after _upgrade_roster_derived_facts so the final facts list is
    available for deduplication.
    """
    if not nces_data:
        return

    facts: list[dict] = facts_output.setdefault("facts", [])

    # Collect fact_keys already represented by a FEDERAL_DATA datapoint
    existing_federal_keys: set[str] = {
        f.get("fact_key", "")
        for f in facts
        if f.get("source_class") == "FEDERAL_DATA"
    }

    injected = 0
    for fact_key, value in nces_data.items():
        if fact_key in _NCES_META_KEYS or fact_key not in _NCES_FACT_KEY_MAP:
            continue
        if fact_key in existing_federal_keys:
            continue  # already present — defer to the existing entry

        dimension, unit = _NCES_FACT_KEY_MAP[fact_key]
        facts.append({
            "datapoint_id":         f"dp_{community_id}_nces_{fact_key}",
            "community_id":         community_id,
            "state":                state,
            "dimension":            dimension,
            "fact_key":             fact_key,
            "claim":                f"{fact_key}: {value} (NCES CCD FY2023)",
            "value":                value,
            "value_unit":           unit,
            "value_year":           2023,
            "source_class":         "FEDERAL_DATA",
            "source_url":           nces_data.get("source_url"),
            "source_title":         nces_data.get("source_title"),
            "source_date":          None,
            "confidence":           "HIGH",
            "confidence_rationale": "Directly computed from NCES CCD federal finance/lunch files",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "nces_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": None,
        })
        injected += 1

    if injected:
        logger.info(
            "[%s] Injected %d NCES fact datapoints (funding_environment + operational_complexity)",
            community_id, injected,
        )


# Maps ACS result keys → (scoring dimension, value_unit)
_ACS_FACT_KEY_MAP: dict[str, tuple[str, str]] = {
    "ell_pct": ("operational_complexity", "percent"),
}
_ACS_META_KEYS: frozenset = frozenset({"data_year", "source_url", "source_title", "confidence"})


def _inject_acs_facts(
    facts_output: dict,
    acs_data: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Append Census ACS fact datapoints to facts_output.

    Injects ell_pct as a FEDERAL_DATA-class fact so S4's deterministic rules
    pass it through to S5 for operational_complexity scoring.  Skips injection
    if acs_data is None (key not set, no district mapping, or API failure).
    """
    if not acs_data:
        facts_output["ell_data_suppressed"] = True
        return

    facts: list[dict] = facts_output.setdefault("facts", [])

    existing_federal_keys: set[str] = {
        f.get("fact_key", "")
        for f in facts
        if f.get("source_class") == "FEDERAL_DATA"
    }

    injected = 0
    for fact_key, value in acs_data.items():
        if fact_key in _ACS_META_KEYS or fact_key not in _ACS_FACT_KEY_MAP:
            continue
        if fact_key in existing_federal_keys:
            continue  # already present — defer to existing entry

        dimension, unit = _ACS_FACT_KEY_MAP[fact_key]
        facts.append({
            "datapoint_id":         f"dp_{community_id}_acs_{fact_key}",
            "community_id":         community_id,
            "state":                state,
            "dimension":            dimension,
            "fact_key":             fact_key,
            "claim":                f"{fact_key}: {value} (Census ACS 5-year, ages 5–17)",
            "value":                value,
            "value_unit":           unit,
            "value_year":           2023,
            "source_class":         "FEDERAL_DATA",
            "source_url":           acs_data.get("source_url"),
            "source_title":         acs_data.get("source_title"),
            "source_date":          None,
            "confidence":           "MODERATE",
            "confidence_rationale": "Census ACS 5-year estimate — subject to sampling error",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "acs_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": None,
        })
        injected += 1

    if injected:
        logger.info(
            "[%s] Injected %d ACS fact datapoints (ell_pct → operational_complexity)",
            community_id, injected,
        )


# Fact keys injected from population_trends_fetcher → (dimension, value_unit)
# Only scalar keys that belong in the facts array; enrollment_by_year / years_available
# are also injected for brief context but carry non-numeric values.
_POP_FACT_KEY_MAP: dict[str, tuple[str, str]] = {
    "pct_change_total":   ("population_trends", "percent"),
    "trend_direction":    ("population_trends", "category"),
    "enrollment_by_year": ("population_trends", "dict"),
    "years_available":    ("population_trends", "list"),
    "data_year":          ("population_trends", "text"),
}
_POP_META_KEYS: frozenset = frozenset({"source_title", "source_url", "confidence"})


def _inject_population_trends_facts(
    facts_output: dict,
    pop_data: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Append NCES enrollment trend fact datapoints to facts_output.

    Injects pct_change_total (primary scoring key) plus context fields so S5
    can score population_trends from real data rather than defaulting to 5.

    Skips silently when pop_data is None (caller already logged the warning).
    """
    if not pop_data:
        return

    facts: list[dict] = facts_output.setdefault("facts", [])

    # Avoid duplicating any population_trends FEDERAL_DATA fact already present
    existing_keys: set[str] = {
        f.get("fact_key", "")
        for f in facts
        if f.get("source_class") == "FEDERAL_DATA"
        and f.get("dimension") == "population_trends"
    }

    injected = 0
    for fact_key, (dimension, unit) in _POP_FACT_KEY_MAP.items():
        value = pop_data.get(fact_key)
        if value is None:
            continue
        if fact_key in existing_keys:
            continue

        facts.append({
            "datapoint_id":         f"dp_{community_id}_pop_{fact_key}",
            "community_id":         community_id,
            "state":                state,
            "dimension":            dimension,
            "fact_key":             fact_key,
            "claim":                f"{fact_key}: {value} (NCES LEA membership {pop_data.get('data_year', '2020–2024')})",
            "value":                value,
            "value_unit":           unit,
            "value_year":           2024,
            "source_class":         "FEDERAL_DATA",
            "source_url":           pop_data.get("source_url"),
            "source_title":         pop_data.get("source_title"),
            "source_date":          None,
            "confidence":           "HIGH",
            "confidence_rationale": "Directly computed from NCES CCD LEA membership files",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "population_trends_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": None,
        })
        injected += 1

    if injected:
        logger.info(
            "[%s] population_trends_fetcher — trend=%s pct_change=%+.1f%% years=%s",
            community_id,
            pop_data.get("trend_direction"),
            pop_data.get("pct_change_total", 0.0),
            pop_data.get("years_available"),
        )


def _inject_acs_population_facts(
    facts_output: dict,
    acs_pop_data: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Inject Census ACS total population estimates as supplementary facts.

    These are secondary demographic signals — not wired into scoring (S5 does not
    look for these fact_keys). in_main_analysis=False keeps them out of the scoring
    pipeline while still making them available to S6 synthesis as context.

    Skips silently when acs_pop_data is None (no CENSUS_API_KEY, no mapping,
    or API failure — all handled upstream).
    """
    if not acs_pop_data:
        return

    facts: list[dict] = facts_output.setdefault("facts", [])

    for fact_key, value in [
        ("acs_total_pop_2019", acs_pop_data.get("pop_2019")),
        ("acs_total_pop_2023", acs_pop_data.get("pop_2023")),
        ("acs_total_pop_pct_change", acs_pop_data.get("pct_change")),
    ]:
        if value is None:
            continue
        facts.append({
            "datapoint_id":         f"dp_{community_id}_acs_pop_{fact_key}",
            "community_id":         community_id,
            "state":                state,
            "dimension":            "population_trends",
            "fact_key":             fact_key,
            "claim":                f"{fact_key}: {value} (ACS B01003, unified school district)",
            "value":                value,
            "value_unit":           "population" if "pct" not in fact_key else "percent",
            "value_year":           2023,
            "source_class":         "FEDERAL_DATA",
            "source_url":           acs_pop_data.get("source_url"),
            "source_title":         acs_pop_data.get("source_title"),
            "source_date":          None,
            "confidence":           "MODERATE",
            "confidence_rationale": "Census ACS 5-year estimate — subject to sampling error",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "acs_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     False,   # secondary signal — not scored
            "needs_verification_reason": None,
        })

    logger.info(
        "[%s] ACS total population — 2019=%s  2023=%s  pct_change=%s",
        community_id,
        acs_pop_data.get("pop_2019"),
        acs_pop_data.get("pop_2023"),
        acs_pop_data.get("pct_change"),
    )


def _fetch_charter_intel(
    community_id: str,
    community_name: str,
    state: str,
    state_name: str,
    csv_schools: list[dict],
    csv_authorizers: list[dict],
    config: "PipelineConfig",
) -> dict:
    """
    Call Claude to enrich CSV-sourced charter school and authorizer data.

    Depth routing:
      fast     — no web search; Claude uses training knowledge; all items LOW confidence
      standard — web search enabled; Claude looks up websites, profiles, contacts
      deep     — same as standard (web search enabled)

    Returns dict with keys:
      top_charter_schools  — list of up to 5 enriched school dicts
      local_authorizers    — list of enriched authorizer dicts

    On any failure: returns empty dict (caller falls back to raw CSV data).
    """
    use_web = config.depth in ("standard", "deep")

    if use_web:
        depth_instruction = (
            "You have web search available. Use it to look up school websites, "
            "academic models, proficiency data, and authorizer contact information. "
            "Set source_class='WEB_SEARCH' and confidence='HIGH' or 'MODERATE' for "
            "items you verified online. Set source_class='CLAUDE_KNOWLEDGE' and "
            "confidence='LOW' for items sourced only from training knowledge."
        )
    else:
        depth_instruction = (
            "You do NOT have web search. Use your training knowledge only. "
            "Set source_class='CLAUDE_KNOWLEDGE' and confidence='LOW' on EVERY item. "
            "Do not invent contact information — set contact fields to null if unknown."
        )

    try:
        # Cap schools passed to the prompt. CSV is already sorted by ELA
        # proficiency descending, so the first N are the best candidates.
        prompt_schools = _truncate_web_results(csv_schools[:_MAX_CHARTER_INTEL_SCHOOLS])

        prompt_text = load_prompt("prompts/extract/s3_charter_intel.md", {
            "COMMUNITY_NAME":      community_name,
            "STATE_NAME":          state_name,
            "STATE_CODE":          state,
            "TODAY_DATE":          today_str(),
            "CSV_SCHOOLS_JSON":    json.dumps(prompt_schools,   indent=2),
            "CSV_AUTHORIZERS_JSON": json.dumps(csv_authorizers, indent=2),
            "DEPTH_INSTRUCTION":   depth_instruction,
        })

        system = (
            "You are a charter school market researcher. "
            "Respond ONLY with valid JSON. No markdown fences, no preamble."
        )

        result = call_claude(
            model=config.model_sonnet,
            system=system,
            user=prompt_text,
            max_tokens=4096,
            temperature=0.3,
            expect_json=True,
            use_web_search=use_web,
            stage=f"{STAGE_ID}_charter_intel",
            community_id=community_id,
        )

        if result.parse_error:
            logger.warning(
                "[%s] Charter intel JSON parse failed: %s", community_id, result.parse_error
            )
            return {}

        data = result.parsed_json or {}
        logger.info(
            "[%s] Charter intel: %d schools, %d authorizers (web_search=%s)",
            community_id,
            len(data.get("top_charter_schools", [])),
            len(data.get("local_authorizers", [])),
            use_web,
        )
        return data

    except Exception as exc:
        logger.warning("[%s] Charter intel fetch failed (non-fatal): %s", community_id, exc)
        return {}


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
