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
import concurrent.futures
import hashlib
import json
import logging
import os
import csv
import re
import time
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

from pipeline import PipelineConfig, StageResult, StageStatus, OutputMode, today_str
from pipeline.utils.api_client import call_claude, call_claude_cached, load_prompt
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema
from pipeline.utils.ped_fetcher import get_district_data
from pipeline.fetchers.ms_proficiency_fetcher import get_ms_district_data
from pipeline.fetchers.tn_proficiency_fetcher import get_tn_district_data
from pipeline.fetchers.wi_proficiency_fetcher import get_wi_district_data
from pipeline.fetchers.in_proficiency_fetcher import get_in_district_data
from pipeline.fetchers.il_proficiency_fetcher import get_il_district_data
from pipeline.fetchers.crdc_fetcher import get_crdc_district_data
from pipeline.fetchers.ed_data_express_fetcher import get_ed_data_express_absenteeism
from pipeline.utils.urban_enrollment_fetcher import get_urban_enrollment_trend
from pipeline.fetchers.bls_teacher_wages_fetcher import get_teacher_wages
from pipeline.fetchers.mde_ratings_fetcher import get_mde_district_rating
from pipeline.utils.nces_fetcher import get_district_data as get_nces_district_data, get_charter_enrollment_share, _slug as _nces_slug
from pipeline.utils.acs_fetcher  import get_district_data as get_acs_district_data, get_total_population as get_acs_total_population
from pipeline.utils.population_trends_fetcher import get_population_trends
from pipeline.utils.saipe_fetcher import get_poverty_data as get_saipe_poverty_data
from pipeline.utils.charter_schools_fetcher import get_community_charter_schools
from pipeline.utils.authorizers_fetcher     import get_community_authorizers
from pipeline.utils.usaspending_csp_fetcher import get_csp_awards
from pipeline.utils.ccd_closures_fetcher    import get_closed_schools
from pipeline.utils.ipeds_completers_fetcher import get_completers
from pipeline.edfacts_teacher_fetcher import get_teacher_supply
from pipeline.propublica_fetcher import get_philanthropic_activity

STAGE_ID = "s3_fact_extraction"

# ── CMO local-presence skepticism (Session 24) ──────────────────────────────
# Config-driven (config/known_cmos.yaml). No CMO names are hardcoded here.
_KNOWN_CMOS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "known_cmos.yaml")


def _load_known_cmos(path: str = _KNOWN_CMOS_PATH) -> dict:
    """Load the national-CMO skepticism config.

    Returns {"entries": [(canonical_name, [compiled_patterns]), ...],
             "dimensions": set[str], "reason": str}. Degrades to an inert
    config (no entries) if the file is missing or malformed so the gate is a
    no-op rather than a crash.
    """
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Could not load %s (CMO gate disabled): %s", path, exc)
        return {"entries": [], "dimensions": set(), "reason": ""}

    entries = []
    for c in cfg.get("national_cmos", []) or []:
        name = c.get("name")
        if not name:
            continue
        terms = [name] + list(c.get("aliases") or [])
        patterns = [re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE) for t in terms]
        entries.append((name, patterns))

    rule = cfg.get("skepticism_rule", {}) or {}
    dimensions = set(rule.get("applies_to_dimensions", []) or [])
    reason = ((rule.get("required_output", {}) or {}).get("needs_verification_reason") or "").strip()
    return {"entries": entries, "dimensions": dimensions, "reason": reason}


_CMO_CONFIG = _load_known_cmos()


def _enforce_cmo_skepticism(
    facts: list[dict], city: str, state: str, cmo_config: Optional[dict] = None
) -> tuple[list[dict], dict]:
    """Deterministic post-LLM gate: a nationally-known CMO named in a targeted
    dimension's claim cannot remain HIGH-confidence / VERIFIED for local presence.

    Caps confidence at MODERATE (only downgrades HIGH) and verification_status at
    PROVISIONAL (only downgrades VERIFIED), so facts already cautious are left
    untouched. Reads from config/known_cmos.yaml — no hardcoded names.
    """
    cfg = cmo_config or _CMO_CONFIG
    dimensions = cfg["dimensions"]
    entries = cfg["entries"]
    reason_tpl = cfg["reason"]

    checked = 0
    demoted = 0
    detected: set[str] = set()
    out: list[dict] = []

    for fact in facts:
        fact = dict(fact)
        if fact.get("dimension") in dimensions:
            checked += 1
            claim = fact.get("claim") or ""
            hit = None
            for name, patterns in entries:
                if any(p.search(claim) for p in patterns):
                    hit = name
                    break
            if hit:
                changed = False
                if fact.get("confidence") == "HIGH":
                    fact["confidence"] = "MODERATE"
                    changed = True
                if fact.get("verification_status") == "VERIFIED":
                    fact["verification_status"] = "PROVISIONAL"
                    changed = True
                if changed:
                    demoted += 1
                    detected.add(hit)
                    fact["needs_verification_reason"] = (
                        reason_tpl.format(cmo_name=hit, city=city, state=state)
                        if reason_tpl else
                        f"{hit} is a nationally-known CMO; local presence in "
                        f"{city}, {state} unconfirmed."
                    )
                    fact["cmo_skepticism_applied"] = True
                    logger.info(
                        "CMO skepticism gate: '%s' detected in %s (%s) — "
                        "downgraded to MODERATE/PROVISIONAL",
                        hit, fact.get("datapoint_id"), fact.get("dimension"),
                    )
        out.append(fact)

    summary = {
        "facts_checked": checked,
        "facts_demoted": demoted,
        "cmos_detected": sorted(detected),
    }
    return out, summary

# Charter intel: only pass the top N schools to the prompt (CSV already sorted
# by ELA proficiency descending). Claude selects 5 from this set. Passing all
# 58 Albuquerque schools inflates the base prompt to ~10k tokens.
_MAX_CHARTER_INTEL_SCHOOLS = 10

# Columns the charter_intel prompt actually uses. All other CSV columns are
# stripped before JSON injection to reduce input token count. The model copies
# these verbatim (school_name, authorizer, grades_served, phone, contact_email)
# or uses them as ranking/proficiency ground truth.
_SCHOOL_PROMPT_FIELDS = frozenset({
    "school_name", "authorizer", "grades_served",
    "phone", "contact_email",
    "ela_proficiency_pct", "math_proficiency_pct",
})

# Web result truncation — caps content/snippet fields in any list-of-dicts
# payload before it's serialized into a prompt string.
_WEB_RESULT_MAX_CHARS = 600

# LLM-response cache TTL for all S3 Claude calls.  Controls how long a cached
# API response is considered fresh before a live call is made again.  Override
# by setting S3_LLM_CACHE_TTL_DAYS in the environment or changing this constant.
S3_LLM_CACHE_TTL_DAYS: int = int(os.environ.get("S3_LLM_CACHE_TTL_DAYS", 30))


def _s3_llm_cache_key(
    state: str, community_id: str, stage_suffix: str, system: str, user: str
) -> str:
    """Return the CacheManager key for one S3 LLM call.

    Key encodes state + community + stage + a SHA-1 prefix of the full prompt
    so that any change in prompt content (injected data, dates, depth flags)
    produces a distinct key and bypasses the stale cached response.
    """
    content_hash = hashlib.sha1(f"{system}\x00{user}".encode()).hexdigest()[:12]
    return f"llm/{state.lower()}/{community_id}/s3_{stage_suffix}_{content_hash}.json"

# Audit log for S3 web-search token regression tracking.
_S3_AUDIT_PATH = "logs/s3_token_audit.csv"
_S3_AUDIT_HEADER = [
    "timestamp", "community", "depth", "call_name",
    "input_tokens", "web_search_requests", "output_tokens",
]


def _write_s3_audit(
    community_id: str,
    depth: str,
    call_name: str,
    result: "APIResult",
) -> None:
    """Append one row to logs/s3_token_audit.csv after each S3 Claude call.

    Captures input_tokens, output_tokens, and web_search_requests (from
    usage.server_tool_use — 0 for non-web-search calls or batch responses
    where raw_response is absent).  Creates the file with a header on first
    write.  Never raises — failures are logged as warnings only.
    """
    try:
        raw = getattr(result, "raw_response", None)
        stu = getattr(getattr(raw, "usage", None), "server_tool_use", None)
        web_searches = int(getattr(stu, "web_search_requests", 0) or 0)

        os.makedirs("logs", exist_ok=True)
        write_header = not os.path.exists(_S3_AUDIT_PATH)
        with open(_S3_AUDIT_PATH, "a", newline="") as fh:
            w = csv.writer(fh)
            if write_header:
                w.writerow(_S3_AUDIT_HEADER)
            w.writerow([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                community_id,
                depth,
                call_name,
                result.tokens_input,
                web_searches,
                result.tokens_output,
            ])
    except Exception as _exc:
        logger.warning("[%s] s3_token_audit write failed: %s", community_id, _exc)


def _should_run_political_climate_search(
    community_id: str,
    community_name: str,
    state: str,
    known_schools: list[str],
    pop_data: Optional[dict],
    state_context: dict,
    config: PipelineConfig,
) -> tuple[bool, str]:
    """Gate: determines whether expensive Sonnet political climate web search is warranted.

    Returns (should_run, skip_reason).  If should_run=False, skip_reason explains why
    the state baseline is inherited. If should_run=True, skip_reason is empty.

    Gate is fail-open: any ambiguous result (empty Haiku response, exception, parse
    failure) passes through to the full Sonnet search. A missed skip is better than
    a suppressed dimension.

    Rule-based skip (zero cost, no model call) — fires if BOTH:
      - Community latest-year K-12 enrollment < 2000 (from pop_data.enrollment_by_year)
      - No charter schools exist in S1 roster (known_schools empty)

    Haiku gate (if rule-based skip does NOT fire):
      - One Haiku call, no web search, max_tokens=100, temperature=0
      - Question: does this community have meaningfully distinct local charter politics?
      - YES → run Sonnet web search as normal
      - NO  → inherit state baseline (index=5, MODERATE, PROVISIONAL)
      - Empty/error → fail-open, run Sonnet search

    Never suppresses the political_climate dimension — a state-baseline datapoint
    is always injected if the gate blocks.

    Depth routing (checked first, before any model call or rule evaluation):
      --depth deep     → bypass gate entirely, return YES unconditionally
      --depth fast     → depth-skip is handled upstream; gate should never be reached
                         at fast depth, but return NO defensively if called
      --depth standard → two-tier gate logic below
    """
    # ── Depth bypass — evaluated before any rule or model call ──────────────
    if config.depth == "deep":
        logger.info(
            "[%s] Political climate: depth=deep — gate bypassed, running Sonnet search",
            community_id,
        )
        return (True, "")

    if config.depth == "fast":
        # Depth=fast should have been caught upstream by _DepthSkip, but be defensive
        logger.info(
            "[%s] Political climate: depth=fast — gate short-circuit NO",
            community_id,
        )
        return (False, "fast depth: web search disabled")

    # config.depth == "standard" — fall through to two-tier gate below

    # ── Derive enrollment from pop_data.enrollment_by_year (latest year) ─────
    enrollment: Optional[int] = None
    if pop_data:
        by_year: dict = pop_data.get("enrollment_by_year") or {}
        if by_year:
            latest_year = max(by_year.keys())
            enrollment = by_year[latest_year]

    # ── Rule-based skip (zero cost) ──────────────────────────────────────────
    # Requires BOTH conditions — either alone is insufficient to skip Sonnet.
    enrollment_small = enrollment is not None and enrollment < 2000
    # Area B (Session 18): tighten the rule-based skip to fire ONLY when the charter
    # roster is genuinely unknown (None), not merely empty. A community with zero
    # charters but real enrollment should still get the Haiku gate (which may run the
    # search) rather than cheaply inheriting a state baseline that masks the data gap.
    #
    # NOTE (Review Fix 2 — cost implication): S1 always initialises known_schools as
    # an empty list [], never None — so no_charters is always False in a normal pipeline
    # run and the rule-based skip (zero cost) is effectively retired for all real
    # communities. Every community at --depth standard now reaches the Haiku gate.
    # Haiku call cost: ~$0.0003/community × 28 NM cities ≈ $0.008/statewide scan.
    # This is intentional: a cheap honest "DATA GAP" baseline is better than silently
    # inheriting a state index with no community-level investigation at all.
    no_charters = known_schools is None

    if enrollment_small and no_charters:
        reason = (
            f"rule-based skip: enrollment {enrollment} < 2000, "
            f"no known charter schools"
        )
        logger.info(
            "[%s] Political climate: rule-based skip — inheriting state baseline (%s)",
            community_id, reason,
        )
        return (False, reason)

    # ── Haiku gate ───────────────────────────────────────────────────────────
    # FIX C: Scope constraint prepended to prevent factual reasoning about charter landscape
    school_list_str = ", ".join(known_schools) if known_schools else "none"
    haiku_prompt = (
        f"Your only job is to assess whether a web search for political climate "
        f"information is likely to return useful results for this community. "
        f"Do NOT reason about charter school counts, enrollment figures, or market conditions. "
        f"Do NOT use charter presence or absence as a reason to skip the search. Only consider:\n"
        f"- Is this a real, searchable US city or community?\n"
        f"- Is there likely to be public information about local school board posture, "
        f"political climate, or charter policy for this location?\n\n"
        f"If yes to both: return YES.\n"
        f"If the community is too small or obscure to have findable political climate data: "
        f"return NO.\n\n"
        f"Does {community_name}, {state} likely have meaningfully distinct local "
        f"charter school politics from the state baseline?\n\n"
        f"Context:\n"
        f"- K-12 enrollment (latest year): {enrollment if enrollment is not None else 'unknown'}\n"
        f"- Known charter schools ({len(known_schools) if known_schools else 0}): "
        f"{school_list_str}\n\n"
        f"Answer YES or NO with one sentence of reasoning."
    )

    _llm_cache = CacheManager(config)
    _gate_system = (
        "You are a concise policy analyst. Answer YES or NO followed by "
        "one sentence. Do not exceed two sentences."
    )
    try:
        haiku_result = call_claude_cached(
            _llm_cache,
            _s3_llm_cache_key(state, community_id, "political_climate_gate", _gate_system, haiku_prompt),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_haiku,
            system=_gate_system,
            user=haiku_prompt,
            max_tokens=100,
            temperature=0,
            expect_json=False,
            use_web_search=False,
            stage=f"{STAGE_ID}_political_climate_gate",
            community_id=community_id,
        )

        # Audit the Haiku gate call — appears as s3_political_climate_gate in logs
        _write_s3_audit(community_id, config.depth, "s3_political_climate_gate", haiku_result)

        haiku_response = (haiku_result.text or "").strip()
        resp_upper = haiku_response.upper()

        # Fail-open: default YES unless response clearly starts with NO (not "NO, but...")
        # Protects against "NO, YES would be..." edge-cases
        starts_no = resp_upper.startswith("NO") and "YES" not in resp_upper
        haiku_decision = "NO" if starts_no else "YES"

        if haiku_decision == "NO":
            reason = f"Haiku gate NO: {haiku_response[:120]}"
            logger.info(
                "[%s] Political climate: Haiku gate — NO (%s), inheriting state baseline",
                community_id, haiku_response[:80],
            )
            return (False, reason)

        logger.info(
            "[%s] Political climate: Haiku gate — YES (%s), running Sonnet web search",
            community_id, haiku_response[:80],
        )
        return (True, "")

    except Exception as exc:
        logger.warning(
            "[%s] Political climate: Haiku gate failed — fail-open, running Sonnet search: %s",
            community_id, exc,
        )
        return (True, "")


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


def _resolve_websearch_status(parsed: dict) -> tuple[str, str, Optional[str]]:
    """Honest confidence / verification_status for the 5 supplemental web-search
    dimensions (Session 18, area A).

    Distinguishes three cases the prompt can express so a "5" that means
    "we found real evidence of a middling market" is never confused with a "5"
    that means "we found nothing":

      EVIDENCE     — sources present → confidence as reported; VERIFIED/PROVISIONAL
      RURAL_THIN   — model set market_thin=true (area J): a genuinely thin rural
                     market where the signal does not exist locally. Index stays 5
                     but at MODERATE/PROVISIONAL, explicitly "availability unknown".
      NOT_FOUND    — searched but nothing usable: confidence LOW, status NOT_FOUND.
                     LOW confidence propagates through S4 → S5 used_default so the
                     dimension is excluded from the composite rather than scored.

    Returns (confidence, verification_status, confidence_rationale).
    """
    confidence = (parsed.get("confidence") or "MODERATE").upper()
    status = (parsed.get("verification_status") or "").upper()
    sources = parsed.get("sources") or []
    market_thin = bool(parsed.get("market_thin"))

    # Model explicitly reported no usable results (area A honesty).
    if status == "NOT_FOUND":
        return ("LOW", "NOT_FOUND",
                "Web search returned no usable results (NOT_FOUND); index defaulted "
                "to midpoint and is excluded from the scored composite.")

    # Documented thin rural market (area J) — distinct from no-evidence.
    if market_thin and not sources:
        return ("MODERATE", "PROVISIONAL",
                "Thin rural market: the relevant signal does not exist locally. "
                "Index 5 reflects 'not binding', NOT favorability — availability unknown.")

    # No sources and no thin-market claim → infer NOT_FOUND (honesty default).
    if not sources:
        return ("LOW", "NOT_FOUND",
                "No sources returned by web search; index defaulted to midpoint "
                "and is excluded from the scored composite.")

    # Evidence present.
    vs = "VERIFIED" if confidence == "HIGH" else "PROVISIONAL"
    return (confidence, vs, None)


# fact_keys whose values are grounded in the VERIFIED_ROSTER block injected into
# the S3 prompt.  Claude tags them UNVERIFIED/LOW (no citable web URL) but they
# ARE derived from PED roster data we supply — S4 would block them without this.
_ROSTER_DERIVED_KEYS: frozenset = frozenset({
    "num_charter_schools",
    "replication_readiness_index",
    "demand_supply_gap_index",
})
_PED_ROSTER_URL = "https://webnew.ped.state.nm.us/bureaus/options-for-parents/charter-schools/"
# TODO(S35-sweep): _PED_ROSTER_TITLE and _PED_ROSTER_URL are NM-specific.
# Derive from config/states.yaml data_sources.charter_roster per state.
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
            # source_mode absent on pre-existing cache files → treat as mode 2 (full data)
            source_mode = cached.get("source_mode", 2)
            if config.mode == OutputMode.STRATEGIC_BRIEF and source_mode == 1:
                logger.info(
                    "[%s] s3_cache: invalidating mode 1 cache for mode 2 run — re-fetching",
                    community_id,
                )
                # Fall through to full re-run
            else:
                parquet_path = f"data/processed/{state.lower()}/nces_membership_{state.lower()}.parquet"
                s3_cache_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
                if (
                    os.path.exists(parquet_path)
                    and os.path.exists(s3_cache_path)
                    and os.path.getmtime(parquet_path) > os.path.getmtime(s3_cache_path)
                ):
                    logger.info(
                        "[%s] S3 cache predates NCES parquet — invalidating to inject pct_change_total",
                        community_id,
                    )
                    # Fall through to full re-run
                else:
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

    # Load verified proficiency data for prompt injection. Each state has its own
    # authoritative source adapter (NM: PED; MS: MSRC; TN: TCAP; WI: Forward Exam).
    # Adapters return None gracefully when their data file is not yet populated.
    # States with proficiency_adapter: none in states.yaml skip all adapter calls.
    state_uc = state.upper()
    _prof_adapter = None
    try:
        with open("config/states.yaml") as _f_prof:
            _prof_adapter = (yaml.safe_load(_f_prof) or {}).get(state_uc, {}).get("proficiency_adapter")
    except Exception as _exc_prof:
        logger.warning("[%s] Could not read proficiency_adapter from states.yaml: %s", community_id, _exc_prof)

    if _prof_adapter == "none":
        logger.warning(
            "[%s] No proficiency adapter for %s — academic_need will score from SAIPE/NCES proxies only",
            community_id, state_uc,
        )
        ped_data = None
    elif state_uc == "MS":
        ped_data = get_ms_district_data(community_id)
    elif state_uc == "TN":
        ped_data = get_tn_district_data(community_id)
    elif state_uc == "WI":
        ped_data = get_wi_district_data(community_id)
    elif state_uc == "IN":
        ped_data = get_in_district_data(community_id)
    elif state_uc == "IL":
        ped_data = get_il_district_data(community_id)
    else:
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
            + (f"- Per-pupil revenue vs. {state_context.get('state_name', state)} state avg: {ppr_vs_avg:+.1f}%\n" if ppr_vs_avg is not None else "")
            + (f"- Total per-pupil expenditure (capital-inclusive, NCES TOTALEXP/enrollment): ${ppe:,.0f}\n" if ppe is not None else "")
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

    # ── Pre-fetch CSV data for charter intel (fast filesystem reads) ─────────
    # Done before the executor so csv_schools/csv_authorizers are available
    # inside fn_charter_intel without touching shared state.
    csv_schools: list[dict] = []
    csv_authorizers: list[dict] = []
    if config.mode != OutputMode.SCAN:
        csv_schools     = get_community_charter_schools(known_schools, roster_source_file)
        csv_authorizers = get_community_authorizers(known_schools, roster_source_file)

    _main_system = "You are a structured data extraction agent. Respond ONLY with valid JSON."
    _pc_max_uses = {"standard": 4, "deep": 8}.get(config.depth, 4)

    # ── Callable definitions for ThreadPoolExecutor ───────────────────────────

    def fn_main():
        r = call_claude_cached(
            cache,
            _s3_llm_cache_key(state, community_id, "main", _main_system, prompt_text),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=_main_system,
            user=prompt_text,
            max_tokens=8192,
            temperature=0.0,
            expect_json=True,
            stage=STAGE_ID,
            community_id=community_id,
        )
        _write_s3_audit(community_id, config.depth, "s3_main", r)
        return r

    def fn_political_climate():
        """Returns a fact dict to append, or None. Gate + search are sequential."""
        should_run_pc, gate_skip_reason = _should_run_political_climate_search(
            community_id=community_id,
            community_name=community_name,
            state=state,
            known_schools=known_schools,
            pop_data=pop_data,
            state_context=state_context,
            config=config,
        )

        if not should_run_pc:
            state_pc_index = (
                state_context.get("political_climate_index")
                if isinstance(state_context, dict) else None
            )
            baseline_value = state_pc_index if isinstance(state_pc_index, (int, float)) else 5
            baseline_source = "S2 state-level index" if state_pc_index is not None else "neutral state default"
            logger.info(
                "[%s] Political climate: gate skipped Sonnet search — LOW-confidence "
                "state baseline injected (data gap, value=%s)",
                community_id, baseline_value,
            )
            return {
                "datapoint_id": f"dp_{community_id}_political_climate_baseline",
                "community_id": community_id,
                "state": state,
                "dimension": "political_climate",
                "fact_key": "political_climate_index",
                "claim": f"Political climate index ({baseline_source}): {baseline_value}",
                "value": baseline_value,
                "value_unit": None,
                "value_year": int(today_str()[:4]),
                "source_class": "STATE_CONTEXT",
                "source_url": None,
                "source_title": None,
                "source_date": None,
                "confidence": "LOW",
                "confidence_rationale": (
                    f"DATA GAP: no community-level political climate evidence — "
                    f"{baseline_source} inherited because gate skipped the web search "
                    f"({gate_skip_reason})."
                ),
                "verification_status": "PROVISIONAL",
                "bias_flag": None,
                "extracted_by": "haiku_gate",
                "extracted_at": today_str(),
                "stage": STAGE_ID,
                "in_main_analysis": True,
                "needs_verification_reason": (
                    "Political climate inherited a state baseline (no community-level "
                    "evidence). Verify local board posture before strategic use."
                ),
            }

        pc_prompt = load_prompt("prompts/extract/s3_political_climate.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })
        _pc_system = (
            "You are a political intelligence researcher. "
            "Use web search to find current information. "
            "Respond ONLY with valid JSON."
        )
        pc_result = call_claude_cached(
            cache,
            _s3_llm_cache_key(state, community_id, "political_climate", _pc_system, pc_prompt),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=_pc_system,
            user=pc_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            web_search_max_uses=_pc_max_uses,
            stage=f"{STAGE_ID}_political_climate",
            community_id=community_id,
        )
        _write_s3_audit(community_id, config.depth, "s3_political_climate", pc_result)

        if pc_result.parse_error:
            logger.warning(
                "[%s] Political climate web search JSON parse failed: %s",
                community_id, pc_result.parse_error,
            )
            return None

        if not pc_result.parsed_json:
            return None

        pc = pc_result.parsed_json
        sources = pc.get("sources") or []
        first_source = sources[0] if sources else {}
        index_val = pc.get("political_climate_index")
        confidence, _vs, _vs_rationale = _resolve_websearch_status(pc)

        logger.info(
            "[%s] Political climate web search — index=%s confidence=%s sources=%d",
            community_id, index_val, confidence, len(sources),
        )
        return {
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
            "confidence_rationale": _vs_rationale,
            "verification_status": _vs,
            "bias_flag": None,
            "extracted_by": config.model_sonnet,
            "extracted_at": today_str(),
            "stage": STAGE_ID,
            "in_main_analysis": True,
            "needs_verification_reason": None,
        }

    def fn_charter_intel():
        return _fetch_charter_intel(
            community_id=community_id,
            community_name=community_name,
            state=state,
            state_name=state_context.get("state_name", state),
            csv_schools=csv_schools,
            csv_authorizers=csv_authorizers,
            config=config,
        )

    def fn_population_trends():
        pt_prompt = load_prompt("prompts/extract/s3_population_trends.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })
        _pt_system = (
            "You are a demographic and enrollment researcher. "
            "Use web search to find current information. "
            "Respond ONLY with valid JSON."
        )
        pt_result = call_claude_cached(
            cache,
            _s3_llm_cache_key(state, community_id, "population_trends", _pt_system, pt_prompt),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=_pt_system,
            user=pt_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_population_trends",
            community_id=community_id,
        )
        if pt_result.parse_error:
            logger.warning(
                "[%s] Population trends web search JSON parse failed: %s",
                community_id, pt_result.parse_error,
            )
            return None
        if not pt_result.parsed_json:
            return None
        pt = pt_result.parsed_json
        sources = pt.get("sources") or []
        first_source = sources[0] if sources else {}
        index_val = pt.get("population_trend_index")
        confidence, _vs, _vs_rationale = _resolve_websearch_status(pt)
        logger.info(
            "[%s] Population trends web search — index=%s confidence=%s sources=%d",
            community_id, index_val, confidence, len(sources),
        )
        return {
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
            "confidence_rationale": _vs_rationale,
            "verification_status": _vs,
            "bias_flag": None,
            "extracted_by": config.model_sonnet,
            "extracted_at": today_str(),
            "stage": STAGE_ID,
            "in_main_analysis": True,
            "needs_verification_reason": None,
        }

    def fn_operational_complexity():
        oc_prompt = load_prompt("prompts/extract/s3_operational_complexity.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })
        _oc_system = (
            "You are an education demographics researcher. "
            "Use web search to find current information. "
            "Respond ONLY with valid JSON."
        )
        oc_result = call_claude_cached(
            cache,
            _s3_llm_cache_key(state, community_id, "operational_complexity", _oc_system, oc_prompt),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=_oc_system,
            user=oc_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_operational_complexity",
            community_id=community_id,
        )
        if oc_result.parse_error:
            logger.warning(
                "[%s] Operational complexity web search JSON parse failed: %s",
                community_id, oc_result.parse_error,
            )
            return None
        if not oc_result.parsed_json:
            return None
        oc = oc_result.parsed_json
        sources = oc.get("sources") or []
        first_source = sources[0] if sources else {}
        index_val = oc.get("operational_complexity_index")
        confidence, _vs, _vs_rationale = _resolve_websearch_status(oc)
        logger.info(
            "[%s] Operational complexity web search — index=%s confidence=%s sources=%d",
            community_id, index_val, confidence, len(sources),
        )
        return {
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
            "confidence_rationale": _vs_rationale,
            "verification_status": _vs,
            "bias_flag": None,
            "extracted_by": config.model_sonnet,
            "extracted_at": today_str(),
            "stage": STAGE_ID,
            "in_main_analysis": True,
            "needs_verification_reason": None,
        }

    def fn_facilities_feasibility():
        ff_prompt = load_prompt("prompts/extract/s3_facilities_feasibility.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })
        _ff_system = (
            "You are a real estate and education facilities researcher. "
            "Use web search to find current information. "
            "Respond ONLY with valid JSON."
        )
        ff_result = call_claude_cached(
            cache,
            _s3_llm_cache_key(state, community_id, "facilities_feasibility", _ff_system, ff_prompt),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=_ff_system,
            user=ff_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_facilities_feasibility",
            community_id=community_id,
        )
        if ff_result.parse_error:
            logger.warning(
                "[%s] Facilities feasibility web search JSON parse failed: %s",
                community_id, ff_result.parse_error,
            )
            return None
        if not ff_result.parsed_json:
            return None
        ff = ff_result.parsed_json
        sources = ff.get("sources") or []
        first_source = sources[0] if sources else {}
        index_val = ff.get("facilities_feasibility_index")
        confidence, _vs, _vs_rationale = _resolve_websearch_status(ff)
        logger.info(
            "[%s] Facilities feasibility web search — index=%s confidence=%s sources=%d",
            community_id, index_val, confidence, len(sources),
        )
        return {
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
            "confidence_rationale": _vs_rationale,
            "verification_status": _vs,
            "bias_flag": None,
            "extracted_by": config.model_sonnet,
            "extracted_at": today_str(),
            "stage": STAGE_ID,
            "in_main_analysis": True,
            "needs_verification_reason": None,
        }

    def fn_funding_environment():
        fe_prompt = load_prompt("prompts/extract/s3_funding_environment.md", {
            "COMMUNITY_NAME": community_name,
            "STATE_NAME": state_context.get("state_name", state),
            "TODAY_DATE": today_str(),
        })
        _fe_system = (
            "You are an education philanthropy and funding researcher. "
            "Use web search to find current information. "
            "Respond ONLY with valid JSON."
        )
        fe_result = call_claude_cached(
            cache,
            _s3_llm_cache_key(state, community_id, "funding_environment", _fe_system, fe_prompt),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=_fe_system,
            user=fe_prompt,
            max_tokens=2048,
            temperature=0.3,
            expect_json=True,
            use_web_search=True,
            stage=f"{STAGE_ID}_funding_environment",
            community_id=community_id,
        )
        if fe_result.parse_error:
            logger.warning(
                "[%s] Funding environment web search JSON parse failed: %s",
                community_id, fe_result.parse_error,
            )
            return None
        if not fe_result.parsed_json:
            return None
        fe = fe_result.parsed_json
        sources = fe.get("sources") or []
        first_source = sources[0] if sources else {}
        index_val = fe.get("funding_environment_index")
        confidence, _vs, _vs_rationale = _resolve_websearch_status(fe)
        logger.info(
            "[%s] Funding environment web search — index=%s confidence=%s sources=%d",
            community_id, index_val, confidence, len(sources),
        )
        return {
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
            "confidence_rationale": _vs_rationale,
            "verification_status": _vs,
            "bias_flag": None,
            "extracted_by": config.model_sonnet,
            "extracted_at": today_str(),
            "stage": STAGE_ID,
            "in_main_analysis": True,
            "needs_verification_reason": None,
        }

    # ── Submit all independent calls concurrently ─────────────────────────────
    _run_pc = config.depth in ("standard", "deep")
    _run_ci = config.mode != OutputMode.SCAN
    _run_deep = config.depth == "deep"
    _n_calls = (1
                + (1 if _run_pc else 0)
                + (1 if _run_ci else 0)
                + (4 if _run_deep else 0))

    logger.info(
        "[%s] S3 parallelized — submitting %d concurrent calls",
        community_id, _n_calls,
    )

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(_n_calls, 3))
    try:
        _f_main = executor.submit(fn_main)
        _f_pc   = executor.submit(fn_political_climate) if _run_pc   else None
        _f_ci   = executor.submit(fn_charter_intel)     if _run_ci   else None
        _f_pt   = executor.submit(fn_population_trends)       if _run_deep else None
        _f_oc   = executor.submit(fn_operational_complexity)  if _run_deep else None
        _f_ff   = executor.submit(fn_facilities_feasibility)  if _run_deep else None
        _f_fe   = executor.submit(fn_funding_environment)     if _run_deep else None

        # fn_main is required — re-raises BudgetExceededError / RuntimeError
        result = _f_main.result()

        pc_fact = None
        if _f_pc is not None:
            try:
                pc_fact = _f_pc.result()
            except Exception as _exc:
                logger.warning(
                    "[%s] Political climate future failed (non-fatal): %s",
                    community_id, _exc,
                )

        charter_intel_result: dict = {}
        if _f_ci is not None:
            try:
                charter_intel_result = _f_ci.result() or {}
            except Exception as _exc:
                logger.warning(
                    "[%s] Charter intel future failed (non-fatal): %s",
                    community_id, _exc,
                )

        _deep_facts: list = []
        for _f_deep in [_f_pt, _f_oc, _f_ff, _f_fe]:
            if _f_deep is None:
                continue
            try:
                _deep_facts.append(_f_deep.result())
            except Exception as _exc:
                logger.warning(
                    "[%s] Deep web search future failed (non-fatal): %s",
                    community_id, _exc,
                )
    finally:
        executor.shutdown(wait=True)

    # ── Parse guard — checked after all futures complete ─────────────────────
    if result.parse_error:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"S3 JSON parse failed: {result.parse_error}"]
        )

    facts_output = result.parsed_json
    # A literal `null` / non-object top-level parses cleanly (no parse_error) but
    # is unusable downstream. Skip this community gracefully rather than crash.
    if not isinstance(facts_output, dict):
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"S3 returned non-object JSON: {type(facts_output).__name__}"]
        )

    # ── Merge supplemental facts from parallel futures (main thread only) ─────
    if pc_fact is not None:
        facts_output.setdefault("facts", []).append(pc_fact)
    for _df in _deep_facts:
        if _df is not None:
            facts_output.setdefault("facts", []).append(_df)

    # ── Protect roster-derived facts from S4's UNVERIFIED block ─────────────
    # Must run after all supplemental calls, before the cache write.
    facts_output["facts"] = _upgrade_roster_derived_facts(
        facts_output.get("facts", []),
        has_roster=bool(verified_roster_rows),
    )

    # ── Align num_charter_schools with the S1 roster the gate uses ───────────
    # The model emits num_charter_schools grounded in the injected VERIFIED_ROSTER,
    # which can drift from the authoritative S1 roster (e.g. ABQ: model 53 vs
    # roster 55). The political-climate gate already counts len(known_schools);
    # this realigns S3 to the same source so the brief and the gate agree.
    facts_output["facts"] = _align_num_charter_schools(
        facts_output["facts"], known_schools,
    )

    # ── Inject NCES fact datapoints (funding_environment + frl_pct) ──────────
    # These are pre-computed from federal government files and are more
    # authoritative than anything Claude could extract.  Runs after the roster
    # upgrade so it has the final facts list to deduplicate against.
    _inject_nces_facts(facts_output, nces_data, community_id, state, config)

    # ── Inject ACS fact datapoints (ell_pct → operational_complexity) ────────
    _inject_acs_facts(facts_output, acs_data, community_id, state)

    # ── Inject SAIPE poverty facts (operational_complexity + funding_environment)
    nces_map = {}
    try:
        import yaml as _yaml
        with open("config/states.yaml") as _f:
            _states_cfg = _yaml.safe_load(_f)
        nces_map = _states_cfg.get(state.upper(), {}).get("nces_district_map", {})
    except Exception as _exc:
        logger.warning("[%s] Could not load nces_district_map for SAIPE lookup: %s", community_id, _exc)
    saipe_leaid = nces_map.get(community_id) or nces_map.get(_nces_slug(community_id))
    saipe_data = get_saipe_poverty_data(saipe_leaid) if saipe_leaid else None
    _inject_saipe_facts(facts_output, saipe_data, community_id, state)

    # ── Inject federal facilities + replication signals (free, keyless APIs) ──
    # state FIPS is derived from the district LEAID (first two digits). When the
    # community has no NCES mapping, CCD (needs LEAID) and IPEDS (needs FIPS) are
    # skipped; the USAspending state-code signal still runs.
    _state_fips = saipe_leaid[:2] if saipe_leaid else None

    # CCD closed schools → facilities_feasibility
    ccd_data = (
        get_closed_schools(saipe_leaid, _state_fips)
        if (saipe_leaid and _state_fips) else None
    )
    _inject_ccd_closures_facts(facts_output, ccd_data, community_id, state)

    # USAspending CSP awards:
    #  - county place-of-performance → SCORED replication operator signal
    #    (discriminates by community; re-scoped from regional recipient at
    #    operator request 2026-05-30).
    #  - state place-of-performance → funding_environment evidence (non-scoring).
    #  - regional recipient (state+adjacent) → non-scoring ecosystem context.
    # The 3-digit county FIPS is the county portion of the CCD 5-digit county_code.
    _county5 = ccd_data.get("county_fips") if ccd_data else None
    _county3 = _county5[-3:] if _county5 else None

    csp_county = (
        get_csp_awards(state.upper(), scope="place_of_performance_county", county_fips=_county3)
        if _county3 else None
    )
    csp_place    = get_csp_awards(state.upper(), scope="place_of_performance")
    csp_regional = get_csp_awards(state.upper(), scope="recipient")

    # IPEDS CIP-13 (education) completers → replication_feasibility pipeline signal
    ipeds_data = get_completers(_state_fips) if _state_fips else None

    _inject_replication_facts(
        facts_output, csp_county, ipeds_data, community_id, state, csp_regional=csp_regional
    )
    _inject_funding_csp_evidence(facts_output, csp_place, community_id, state)

    # ── Inject NCES enrollment trend facts (population_trends dimension) ──────
    _inject_population_trends_facts(facts_output, pop_data, community_id, state)

    # ── Inject ACS total population (secondary signal, not scored) ────────────
    _inject_acs_population_facts(facts_output, acs_pop_data, community_id, state)

    # ── Session 27 federal signals: charter enrollment share, teacher supply, ──
    # ── philanthropic activity. All free/keyless; each degrades to a no-op when ─
    # ── its fetcher returns None. (saipe_leaid / _state_fips computed above.) ───
    charter_share = get_charter_enrollment_share(saipe_leaid) if saipe_leaid else None
    _inject_charter_enrollment_share_facts(
        facts_output, charter_share, community_id, state, community_name
    )

    teacher_supply = (
        get_teacher_supply(saipe_leaid, community_id, state, state_fips=_state_fips)
        if saipe_leaid else None
    )
    _inject_teacher_supply_facts(
        facts_output, teacher_supply, community_id, state, community_name
    )

    philanthropy = get_philanthropic_activity(community_name, state, community_id)
    _inject_philanthropic_facts(
        facts_output, philanthropy, community_id, state, community_name
    )

    # ── Session 9 federal signals: CRDC IEP%/chronic-absenteeism + BLS teacher ──
    # ── wages. Both free/keyless; each degrades to a no-op when its fetcher    ──
    # ── returns None. NOTE: these are brief-narrative facts only — no S5 scored ──
    # ── dimension consumes them today (see _inject_crdc_facts / _inject_bls_*). ──
    #
    # Chronic absenteeism: prefer ED Data Express SY2022-23 (LEA-level, annual
    # ESSA reporting) over CRDC (school-level, biennial, older vintage). CRDC is
    # still fetched for IEP%; skip_absenteeism=True suppresses its chronic
    # absenteeism injection when ED Data Express has data for this district.
    ede_absenteeism = (
        get_ed_data_express_absenteeism(saipe_leaid) if saipe_leaid else None
    )
    _inject_ede_absenteeism_facts(facts_output, ede_absenteeism, community_id, state, community_name)

    crdc_data = get_crdc_district_data(saipe_leaid) if saipe_leaid else None
    _inject_crdc_facts(
        facts_output, crdc_data, community_id, state, community_name,
        skip_absenteeism=ede_absenteeism is not None,
    )

    # ── Urban Institute multi-year enrollment trend (FIX 4) ────────────────────
    urban_enrollment = (
        get_urban_enrollment_trend(saipe_leaid) if saipe_leaid else None
    )
    _inject_urban_enrollment_facts(facts_output, urban_enrollment, community_id, state)

    teacher_wages = get_teacher_wages(_state_fips) if _state_fips else None
    _inject_teacher_wages_facts(
        facts_output, teacher_wages, community_id, state, community_name
    )

    # ── MS district A–F accountability rating → feeds the charter_law § 37-28-7 ──
    # ── consent gate (S5). MS-only; degrades to None elsewhere / when unmapped. ──
    mde_rating = (
        get_mde_district_rating(saipe_leaid)
        if (saipe_leaid and state.upper() == "MS") else None
    )
    _inject_mde_rating_facts(facts_output, mde_rating, community_id, state, community_name)

    # ── Charter schools + authorizer intelligence ─────────────────────────────
    # Scan mode skips both fetchers: charter_intel feeds brief narrative only and
    # does not contribute to any scoring dimension.  Cost: ~$0.05/city saved.
    # csv_schools / csv_authorizers were pre-fetched before the executor block.
    # charter_intel_result was populated by fn_charter_intel running concurrently.
    if config.mode == OutputMode.SCAN:
        logger.info(
            "[%s] Scan mode — charter_intel and authorizer fetchers intentionally skipped",
            community_id,
        )
        facts_output["charter_schools"]  = []
        facts_output["local_authorizers"] = []
    else:
        charter_intel = charter_intel_result
        facts_output["charter_schools"]  = charter_intel.get("top_charter_schools", csv_schools)
        facts_output["local_authorizers"] = charter_intel.get("local_authorizers", csv_authorizers)
        # LLM output omits num_schools_in_community; re-merge from roster-derived csv_authorizers.
        _csv_count = {a["authorizer_name"]: a.get("num_schools_in_community") for a in csv_authorizers}
        for _auth in facts_output["local_authorizers"]:
            if not _auth.get("num_schools_in_community"):
                _auth["num_schools_in_community"] = _csv_count.get(_auth.get("authorizer_name"))
        # Charter-adjacent schools (operating + pending; see s3_charter_intel.md RULE 5).
        # Captured as NON-SCORING context — surfaced in the brief and used by the S5
        # saturation reliability gate, but never folded into the authoritative
        # num_charter_schools count (which stays PED-roster only by design).
        _inject_charter_adjacent_facts(
            facts_output, charter_intel.get("charter_adjacent_schools", []),
            community_id, state, community_name,
        )
    # ─────────────────────────────────────────────────────────────────────────

    # ── CMO local-presence skepticism gate (deterministic, config-driven) ──
    # Caps confidence/verification_status for any national-CMO local-presence
    # claim in a targeted dimension, before caching/output.
    _facts_list, _cmo_summary = _enforce_cmo_skepticism(
        facts_output.get("facts", []), community_name, state
    )
    facts_output["facts"] = _facts_list
    facts_output["cmo_skepticism"] = _cmo_summary

    facts_output["source_mode"] = config.mode.value

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
        datapoint = {
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
            # TODO(S35): source_date is None for NCES facts — and for most other
            # fetchers in this file.  This means _derive_data_through in S6 has
            # no vintage dates to read and falls back to (today.year - 1)-12-31.
            # Fix: set source_date to the fiscal-year-end date of the dataset
            # (e.g. "2023-09-30" for NCES FY2023 CCD). Each fetcher must be
            # updated individually since vintage dates vary by data source.
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
        }
        # Carry the top-band compression flag onto the FRL datapoint so S5/S6
        # can distinguish a saturated-but-present FRL signal from a default-5.
        if fact_key == "frl_pct" and "frl_compressed" in nces_data:
            datapoint["frl_compressed"] = bool(nces_data["frl_compressed"])
        facts.append(datapoint)
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



# Maps SAIPE result keys → list of (scoring_dimension, value_unit)
# poverty_rate_pct flows into BOTH operational_complexity and funding_environment
# poverty_count_5_17 flows into funding_environment only
_SAIPE_FACT_KEY_MAP: dict[str, list[tuple[str, str]]] = {
    "poverty_rate_pct":   [("operational_complexity", "percent"), ("funding_environment", "percent")],
    "poverty_count_5_17": [("funding_environment", "count")],
}
_SAIPE_META_KEYS: frozenset = frozenset({
    "leaid", "data_year", "source", "source_url", "source_title", "confidence", "total_population",
})


def _inject_saipe_facts(
    facts_output: dict,
    saipe_data: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Append Census SAIPE poverty fact datapoints to facts_output.

    Injects saipe_poverty_rate_pct into operational_complexity and
    funding_environment dimensions, and saipe_poverty_count_5_17 into
    funding_environment. Skips silently when saipe_data is None (no API key,
    no LEAID mapping, no matching district, or API failure).
    """
    if not saipe_data:
        return

    facts: list[dict] = facts_output.setdefault("facts", [])

    existing_federal_keys: set[str] = {
        f.get("fact_key", "")
        for f in facts
        if f.get("source_class") == "FEDERAL_DATA"
    }

    injected = 0
    for fact_key, dim_unit_list in _SAIPE_FACT_KEY_MAP.items():
        value = saipe_data.get(fact_key)
        if value is None:
            continue

        for dimension, unit in dim_unit_list:
            # Build a unique fact_key for deduplication (dimension-qualified)
            qualified_key = f"saipe_{fact_key}_{dimension}"
            if qualified_key in existing_federal_keys:
                continue

            facts.append({
                "datapoint_id":         f"dp_{community_id}_saipe_{fact_key}_{dimension[:4]}",
                "community_id":         community_id,
                "state":                state,
                "dimension":            dimension,
                "fact_key":             qualified_key,
                "claim": (
                    f"saipe_{fact_key}: {value} "
                    f"(SAIPE {saipe_data.get('data_year', 'unknown')})"
                ),
                "value":                value,
                "value_unit":           unit,
                "value_year":           saipe_data.get("data_year"),
                "source_class":         "FEDERAL_DATA",
                "source_url":           saipe_data.get("source_url"),
                "source_title":         saipe_data.get("source_title"),
                "source_date":          None,
                "confidence":           "MODERATE",
                "confidence_rationale": (
                    "Census SAIPE school district estimate — "
                    "independent federal poverty signal"
                ),
                "verification_status":  "VERIFIED",
                "bias_flag":            None,
                "extracted_by":         "saipe_fetcher",
                "extracted_at":         today_str(),
                "stage":                STAGE_ID,
                "in_main_analysis":     True,
                "needs_verification_reason": None,
            })
            injected += 1

    if injected:
        logger.info(
            "[%s] Injected %d SAIPE fact datapoints (poverty_rate=%.1f%% year=%s)",
            community_id, injected,
            saipe_data.get("poverty_rate_pct", float("nan")),
            saipe_data.get("data_year"),
        )


def _inject_charter_enrollment_share_facts(
    facts_output: dict,
    charter_share: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Append charter enrollment share as a charter_saturation datapoint.

    Secondary signal (S5 primary_fact for charter_saturation is
    num_charter_schools); this resolves the brief's charter-share
    needs-verification item. Skips silently when the fetcher returns None.
    """
    if not charter_share:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "charter_enrollment_share_pct" for f in facts):
        return

    pct = charter_share["charter_enrollment_share_pct"]
    facts.append({
        "datapoint_id":         f"dp_{community_id}_charter_saturation_003",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "charter_saturation",
        "fact_key":             "charter_enrollment_share_pct",
        "claim": (
            f"Charter enrollment share in {community_name}: {pct}% of district "
            f"students attend charter schools "
            f"({charter_share['total_charter_enrollment']:,} of "
            f"{charter_share['district_total_enrollment']:,}, NCES CCD "
            f"{charter_share['year']})."
        ),
        "value":                pct,
        "value_unit":           "percent",
        "value_year":           charter_share["year"],
        "source_class":         "FEDERAL_DATA",
        "source_url":           charter_share.get("source_url"),
        "source_title":         charter_share.get("source_title"),
        "source_date":          None,
        "confidence":           "HIGH",
        "confidence_rationale": "Directly computed from NCES CCD school directory enrollment by charter status",
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "nces_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": None,
    })
    logger.info(
        "[%s] Injected charter enrollment share datapoint (%.1f%%, year %s)",
        community_id, pct, charter_share["year"],
    )


def _inject_charter_adjacent_facts(
    facts_output: dict,
    adjacent_schools: list,
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Store the charter-adjacent school list and emit a NON-SCORING count fact.

    "Charter-adjacent" = schools that compete for the same students as a charter
    but are NOT on the PED charter roster: autonomous public schools of choice
    (innovation/magnet/option schools run with charter-like autonomy), university-
    model/hybrid schools, established private micro-schools, and schools that have
    formally applied or publicly announced intent to open (status="pending"). The
    full definition (incl. exclusions) lives in prompts/extract/s3_charter_intel.md
    RULE 5 — keep the two in sync.

    Why non-scoring: the scored charter_saturation primary fact (num_charter_schools)
    is forced to the authoritative PED roster (see _align_num_charter_schools) so the
    composite never rests on drift-prone web counts. Charter-adjacent schools are web/
    knowledge-sourced and lower-confidence, so folding them into the scored count would
    reduce reliability. Instead the count is emitted with in_main_analysis=False and
    consumed by the S5 saturation reliability gate, which flags (does not silently
    inflate) a market that looks empty on the formal roster but is not. Promoting this
    count into the scored value requires operator sign-off (changes rankings).
    """
    if not isinstance(adjacent_schools, list):
        adjacent_schools = []
    # Keep only well-formed entries with a name; never fabricate.
    cleaned = [s for s in adjacent_schools if isinstance(s, dict) and (s.get("school_name") or "").strip()]
    facts_output["charter_adjacent_schools"] = cleaned

    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "num_charter_adjacent_schools" for f in facts):
        return

    count = len(cleaned)
    operating = sum(1 for s in cleaned if (s.get("status") or "").lower() == "operating")
    pending = count - operating
    # Confidence of the count = lowest confidence among contributing entries (conservative).
    _rank = {"HIGH": 3, "MODERATE": 2, "LOW": 1}
    confidences = [(_rank.get((s.get("confidence") or "LOW").upper(), 1)) for s in cleaned]
    conf = "LOW"
    if confidences:
        inv = {3: "HIGH", 2: "MODERATE", 1: "LOW"}
        conf = inv[min(confidences)]

    facts.append({
        "datapoint_id":         f"dp_{community_id}_charter_saturation_004",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "charter_saturation",
        "fact_key":             "num_charter_adjacent_schools",
        "claim": (
            f"{count} charter-adjacent school(s) identified near {community_name} "
            f"({operating} operating, {pending} pending/announced) that are not on the "
            f"PED charter roster. Non-scoring context; requires verification."
        ),
        "value":                count,
        "value_unit":           "count",
        "value_year":           None,
        "source_class":         "WEB_SEARCH",
        "source_url":           None,
        "source_title":         "Charter-adjacent school scan (s3_charter_intel)",
        "source_date":          None,
        "confidence":           conf,
        "confidence_rationale": "Web/knowledge-sourced charter-adjacent scan; lower confidence than PED roster — non-scoring",
        "verification_status":  "PROVISIONAL",
        "bias_flag":            None,
        "extracted_by":         "charter_intel",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     False,
        "needs_verification_reason": "Charter-adjacent schools are unofficial/unverified; confirm before use",
    })
    logger.info(
        "[%s] Injected num_charter_adjacent_schools=%d (non-scoring; %d operating, %d pending)",
        community_id, count, operating, pending,
    )


def _inject_teacher_supply_facts(
    facts_output: dict,
    teacher_supply: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Append the teacher-supply signal as an operational_complexity datapoint.

    PROVISIONAL/MODERATE: EDFacts/CCD staffing data lags 1-2 years. Skips
    silently when the fetcher returns None.
    """
    if not teacher_supply:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "teacher_supply_signal" for f in facts):
        return

    signal = teacher_supply["teacher_signal"]
    field_used = teacher_supply["field_used"]
    raw_value = teacher_supply["raw_value"]
    facts.append({
        "datapoint_id":         f"dp_{community_id}_operational_complexity_teacher",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "operational_complexity",
        "fact_key":             "teacher_supply_signal",
        "claim": (
            f"Teacher supply signal for {community_name}: {signal} based on "
            f"{field_used} ({raw_value})."
        ),
        "value":                raw_value,
        "value_unit":           "ratio" if field_used == "student_teacher_ratio" else "percent",
        "value_year":           teacher_supply.get("year"),
        "source_class":         "FEDERAL_DATA",
        "source_url":           teacher_supply.get("source_url"),
        "source_title":         teacher_supply.get("source_title"),
        "source_date":          None,
        "confidence":           "MODERATE",
        "confidence_rationale": "Derived from federal CCD staffing/enrollment data (reporting lag)",
        "verification_status":  "PROVISIONAL",
        "bias_flag":            None,
        "extracted_by":         "edfacts_teacher_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": (
            "EDFacts data is 1-2 years lagged; verify against current NMPED "
            "hard-to-staff designation list at https://webnew.ped.state.nm.us "
            "before external use."
        ),
    })
    logger.info(
        "[%s] Injected teacher supply datapoint (%s via %s=%s)",
        community_id, signal, field_used, raw_value,
    )


def _inject_crdc_facts(
    facts_output: dict,
    crdc: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
    skip_absenteeism: bool = False,
) -> None:
    """Append CRDC-derived IEP% and chronic-absenteeism% as operational_complexity
    datapoints (brief-narrative context).

    skip_absenteeism: when True, suppress the chronic_absenteeism_pct injection
    because ED Data Express already provided a more recent figure.

    SCORING NOTE: operational_complexity scores only from operational_complexity_index
    (it has no secondary_facts), so neither iep_pct nor chronic_absenteeism_pct is
    consumed by S5 today — these inform the brief, not the composite. CRDC lags
    (collected biennially); both carry PROVISIONAL status with the vintage note.
    Skips silently when the fetcher returns None.
    """
    if not crdc:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    note = crdc.get("data_note")

    iep_pct = crdc.get("iep_pct")
    if iep_pct is not None and not any(f.get("fact_key") == "iep_pct" for f in facts):
        facts.append({
            "datapoint_id":         f"dp_{community_id}_operational_complexity_iep",
            "community_id":         community_id,
            "state":                state,
            "dimension":            "operational_complexity",
            "fact_key":             "iep_pct",
            "claim": (
                f"Students with IEPs (IDEA) in {community_name}: {iep_pct}% "
                f"({crdc.get('iep_count')} of {crdc.get('iep_denominator')}, "
                f"CRDC {crdc.get('iep_year')})."
            ),
            "value":                iep_pct,
            "value_unit":           "percent",
            "value_year":           crdc.get("iep_year"),
            "source_class":         "FEDERAL_DATA",
            "source_url":           crdc.get("source_url"),
            "source_title":         crdc.get("source_title"),
            "source_date":          None,
            "confidence":           "MODERATE",
            "confidence_rationale": "CRDC IDEA enrollment / CCD district enrollment",
            "verification_status":  "PROVISIONAL",
            "bias_flag":            None,
            "extracted_by":         "crdc_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": note,
        })

    ca_pct = crdc.get("chronic_absenteeism_pct")
    if ca_pct is not None and not skip_absenteeism and not any(f.get("fact_key") == "chronic_absenteeism_pct" for f in facts):
        facts.append({
            "datapoint_id":         f"dp_{community_id}_operational_complexity_absenteeism",
            "community_id":         community_id,
            "state":                state,
            "dimension":            "operational_complexity",
            "fact_key":             "chronic_absenteeism_pct",
            "claim": (
                f"Chronic absenteeism in {community_name}: {ca_pct}% "
                f"({crdc.get('chronic_absent_count')} of "
                f"{crdc.get('chronic_absent_denominator')}, CRDC "
                f"{crdc.get('chronic_absenteeism_year')})."
            ),
            "value":                ca_pct,
            "value_unit":           "percent",
            "value_year":           crdc.get("chronic_absenteeism_year"),
            "source_class":         "FEDERAL_DATA",
            "source_url":           crdc.get("source_url"),
            "source_title":         crdc.get("source_title"),
            "source_date":          None,
            "confidence":           "MODERATE",
            "confidence_rationale": "CRDC chronically-absent count / CCD district enrollment",
            "verification_status":  "PROVISIONAL",
            "bias_flag":            None,
            "extracted_by":         "crdc_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": note,
        })

    logger.info(
        "[%s] Injected CRDC datapoints (IEP%%=%s, chronic_absent%%=%s)",
        community_id, iep_pct, ca_pct,
    )


def _inject_ede_absenteeism_facts(
    facts_output: dict,
    ede: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Append ED Data Express chronic absenteeism as an operational_complexity
    datapoint. Preferred over CRDC chronic absenteeism (newer vintage, LEA-level).
    Skips silently when the fetcher returns None.
    """
    if not ede:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "chronic_absenteeism_pct" for f in facts):
        return

    ca_pct = ede.get("chronic_absenteeism_pct")
    if ca_pct is None:
        return

    facts.append({
        "datapoint_id":         f"dp_{community_id}_operational_complexity_absenteeism_ede",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "operational_complexity",
        "fact_key":             "chronic_absenteeism_pct",
        "claim": (
            f"Chronic absenteeism in {community_name}: {ca_pct}% "
            f"({ede.get('chronic_absent_count')} of "
            f"{ede.get('chronic_absent_denominator')}, "
            f"ED Data Express SY{ede.get('chronic_absenteeism_year') - 1}"
            f"-{str(ede.get('chronic_absenteeism_year'))[2:]})."
        ),
        "value":                ca_pct,
        "value_unit":           "percent",
        "value_year":           ede.get("chronic_absenteeism_year"),
        "source_class":         "FEDERAL_DATA",
        "source_url":           ede.get("source_url"),
        "source_title":         ede.get("source_title"),
        "source_date":          None,
        "confidence":           "HIGH",
        "confidence_rationale": "ED Data Express ESSA/ESEA district-level reporting",
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "ed_data_express_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": ede.get("data_note"),
    })
    logger.info(
        "[%s] Injected ED Data Express chronic absenteeism: %s%%",
        community_id, ca_pct,
    )


def _inject_urban_enrollment_facts(
    facts_output: dict,
    urban: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Append Urban Institute multi-year enrollment trend as a population_trends
    datapoint. Provides a richer time series than the NCES membership parquet
    (includes 2021 and covers 2019–2024). Skips silently when fetcher returns None.
    """
    if not urban:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "enrollment_trend" for f in facts):
        return

    trend = urban.get("enrollment_trend")
    if not trend:
        return

    facts.append({
        "datapoint_id":         f"dp_{community_id}_population_trends_enrollment_trend",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "population_trends",
        "fact_key":             "enrollment_trend",
        "claim":                f"Multi-year enrollment trend ({urban['years_fetched'][0]}–{urban['years_fetched'][-1]}): {trend}",
        "value":                trend,
        "value_unit":           "dict",
        "value_year":           urban["years_fetched"][-1],
        "source_class":         "FEDERAL_DATA",
        "source_url":           urban.get("source_url"),
        "source_title":         urban.get("source_title"),
        "source_date":          None,
        "confidence":           "HIGH",
        "confidence_rationale": "Urban Institute API — NCES CCD grade-99 district enrollment",
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "urban_enrollment_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": None,
    })
    logger.info(
        "[%s] Injected urban enrollment trend: %d years (%s–%s)",
        community_id,
        len(trend),
        urban["years_fetched"][0],
        urban["years_fetched"][-1],
    )


def _inject_teacher_wages_facts(
    facts_output: dict,
    wages: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Append BLS OEWS teacher median wages as an operational_complexity datapoint
    (brief-narrative context; not consumed by S5 scoring today). Skips silently
    when the fetcher returns None.
    """
    if not wages:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "teacher_median_wage" for f in facts):
        return

    elem = wages.get("elementary_teacher_median_wage")
    sec = wages.get("secondary_teacher_median_wage")
    geo = wages.get("geography_level", "state")
    facts.append({
        "datapoint_id":         f"dp_{community_id}_operational_complexity_teacher_wage",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "operational_complexity",
        "fact_key":             "teacher_median_wage",
        "claim": (
            f"Teacher labor market ({geo}-level, BLS OEWS {wages.get('wage_year')}): "
            f"elementary median ${elem:,}/yr"
            + (f", secondary median ${sec:,}/yr" if sec is not None else "")
            + ". Lower prevailing wages can ease charter staffing-cost pressure."
        ) if elem is not None else (
            f"Teacher labor market ({geo}-level, BLS OEWS {wages.get('wage_year')}): "
            f"secondary median ${sec:,}/yr."
        ),
        "value":                elem if elem is not None else sec,
        "value_unit":           "usd_per_year",
        "value_year":           wages.get("wage_year"),
        "source_class":         "FEDERAL_DATA",
        "source_url":           wages.get("source_url"),
        "source_title":         wages.get("source_title"),
        "source_date":          None,
        "confidence":           "HIGH",
        "confidence_rationale": "BLS OEWS published annual median wage",
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "bls_teacher_wages_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": None,
    })
    logger.info(
        "[%s] Injected BLS teacher wage datapoint (%s elem=%s sec=%s)",
        community_id, geo, elem, sec,
    )


def _inject_mde_rating_facts(
    facts_output: dict,
    mde_rating: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Append the MS district A–F accountability rating as a fact.

    Emits fact_key 'district_accountability_rating' (value e.g. 'A') which the S5
    statutory_barrier_check reads for the § 37-28-7 consent gate. Skips silently
    when None (non-MS, unmapped district, or source unavailable). The gate then
    degrades to its unverified advisory.
    """
    if not mde_rating:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "district_accountability_rating" for f in facts):
        return

    grade = mde_rating["district_accountability_rating"]
    year = mde_rating.get("district_rating_year")
    facts.append({
        "datapoint_id":         f"dp_{community_id}_district_accountability_rating",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "charter_saturation",  # carrier dimension; not scored — read by the charter_law gate
        "fact_key":             "district_accountability_rating",
        "claim": (
            f"{community_name} district accountability rating: {grade} "
            f"(MDE, {year}). Drives the § 37-28-7 local-board-consent gate."
        ),
        "value":                grade,
        "value_unit":           "letter_grade",
        "value_year":           year,
        "source_class":         "PED_DATA",
        "source_url":           mde_rating.get("source_url"),
        "source_title":         mde_rating.get("source_title"),
        "source_date":          None,
        "confidence":           "HIGH",
        "confidence_rationale": "Published MDE statewide accountability letter grade",
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "mde_ratings_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     False,  # not a scored signal; consumed by the statutory gate
        "needs_verification_reason": None,
    })
    logger.info(
        "[%s] Injected MDE district rating datapoint (%s, %s)",
        community_id, grade, year,
    )


def _inject_philanthropic_facts(
    facts_output: dict,
    philanthropy: Optional[dict],
    community_id: str,
    state: str,
    community_name: str,
) -> None:
    """Append the philanthropic-activity tier as a replication_feasibility
    datapoint.

    PROVISIONAL/MODERATE: IRS 990 data lags 1-2 years. Skips silently when the
    fetcher returns None.
    """
    if not philanthropy:
        return
    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "philanthropic_activity_tier" for f in facts):
        return

    tier = philanthropy["activity_tier"]
    count = philanthropy["foundation_count"]
    assets_fmt = philanthropy["total_assets_formatted"]
    facts.append({
        "datapoint_id":         f"dp_{community_id}_replication_feasibility_phil",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "replication_feasibility",
        "fact_key":             "philanthropic_activity_tier",
        "claim": (
            f"Philanthropic activity in {community_name}: {tier} "
            f"({count} education foundations found, total assets ~${assets_fmt})."
        ),
        "value":                tier,
        "value_unit":           "tier",
        "value_year":           None,
        "source_class":         "FEDERAL_DATA",
        "source_url":           philanthropy.get("source_url"),
        "source_title":         philanthropy.get("source_title"),
        "source_date":          None,
        "source_notes":         (
            f"Largest: {philanthropy['largest_foundation']['name']} "
            f"(${philanthropy['largest_foundation']['asset_amount']:,})"
            if philanthropy.get("largest_foundation") else None
        ),
        "confidence":           "MODERATE",
        "confidence_rationale": "Aggregated from IRS Form 990 data via ProPublica Nonprofit Explorer",
        "verification_status":  "PROVISIONAL",
        "bias_flag":            None,
        "extracted_by":         "propublica_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": (
            "ProPublica 990 data may be 1-2 years lagged. Verify current "
            "foundation activity and grant focus areas before external use."
        ),
    })
    logger.info(
        "[%s] Injected philanthropic activity datapoint (%s, %d foundations, ~$%s)",
        community_id, tier, count, assets_fmt,
    )


def _inject_ccd_closures_facts(
    facts_output: dict,
    ccd_data: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Append the CCD closed-school count as a facilities_feasibility datapoint.

    Skips silently when ccd_data is None (no LEAID, no directory year, or API
    failure) — S5 then keeps the neutral default and tags it unscored. A genuine
    zero closures is injected as value=0 (a valid scored signal). The closed≠
    available real-estate caveat travels in both the claim and source_notes.
    """
    if not ccd_data:
        return

    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "facilities_closed_schools_count" for f in facts):
        return

    c = ccd_data.get("closed_count_total", 0)
    years = ccd_data.get("years_queried", [])
    facts.append({
        "datapoint_id":         f"dp_{community_id}_facilities_feasibility_001",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "facilities_feasibility",
        "fact_key":             "facilities_closed_schools_count",
        "claim": (
            f"{c} public school(s) were closed or temporarily closed in the "
            f"district and county over CCD years {years}. {ccd_data.get('caveat', '')}"
        ),
        "value":                c,
        "value_unit":           "count",
        "value_year":           max(years) if years else None,
        "source_class":         "FEDERAL_DATA",
        "source_url":           ccd_data.get("source_url"),
        "source_title":         ccd_data.get("source_title"),
        "source_date":          None,
        "source_notes":         ccd_data.get("caveat"),
        "confidence":           "MODERATE",
        "confidence_rationale": (
            "Directly counted from NCES CCD directory; closed-school count is a "
            "facilities-availability signal, not a verified real-estate inventory"
        ),
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "ccd_closures_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     True,
        "needs_verification_reason": None,
    })
    logger.info(
        "[%s] Injected CCD facilities datapoint (closed_count_total=%s, "
        "district=%s county=%s)",
        community_id, c, ccd_data.get("district_closed_count"),
        ccd_data.get("county_closed_count"),
    )


def _inject_replication_facts(
    facts_output: dict,
    csp_county: Optional[dict],
    ipeds_data: Optional[dict],
    community_id: str,
    state: str,
    csp_regional: Optional[dict] = None,
) -> None:
    """Append the replication_feasibility sub-signals when available.

    SCORED:
      csp_distinct_operators — distinct CSP operators that have performed funded
        work IN the community's county (county place-of-performance). This is
        community-specific (re-scoped from the regional view 2026-05-30).
      cip13_completers — statewide IPEDS education completers (pipeline proxy).
    NON-SCORING context:
      csp_regional_operators — distinct CSP operators across the state + adjacent
        states (ecosystem maturity), surfaced for synthesis only.

    Each is injected independently; if a fetcher returned None its datapoint is
    skipped, and S5 scores from whichever scored sub-signal is present (or keeps
    the tagged default if both are absent). A zero operator/county count is a
    VALID scored value (no CSP operator has worked in-county), not missing data.
    """
    facts: list[dict] = facts_output.setdefault("facts", [])
    existing_keys = {f.get("fact_key") for f in facts}

    if csp_county and "csp_distinct_operators" not in existing_keys:
        ops = csp_county.get("distinct_operators", 0)
        facts.append({
            "datapoint_id":         f"dp_{community_id}_replication_feasibility_001",
            "community_id":         community_id,
            "state":                state,
            "dimension":            "replication_feasibility",
            "fact_key":             "csp_distinct_operators",
            "claim": (
                f"{ops} distinct charter operator(s) performed federally CSP-funded "
                f"(CFDA {','.join(csp_county.get('matched_cfda_numbers', [])) or '84.282'}) "
                f"work in {state.upper()} county {csp_county.get('county_fips')} "
                f"over the last 10 years."
            ),
            "value":                ops,
            "value_unit":           "count",
            "value_year":           None,
            "source_class":         "FEDERAL_DATA",
            "source_url":           csp_county.get("source_url"),
            "source_title":         csp_county.get("source_title"),
            "source_date":          None,
            "source_notes":         "County place-of-performance scope; community-specific.",
            "confidence":           "MODERATE",
            "confidence_rationale": "Counted from USAspending federal award records",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "usaspending_csp_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": None,
        })

    # Non-scoring regional ecosystem context (state + adjacent).
    if csp_regional and "csp_regional_operators" not in existing_keys:
        facts.append({
            "datapoint_id":         f"dp_{community_id}_replication_feasibility_003",
            "community_id":         community_id,
            "state":                state,
            "dimension":            "replication_feasibility",
            "fact_key":             "csp_regional_operators",
            "claim": (
                f"{csp_regional.get('distinct_operators', 0)} distinct charter operators "
                f"received CSP awards across {state.upper()} and adjacent states "
                f"({', '.join(csp_regional.get('states_queried', []))}) in the last 10 years."
            ),
            "value":                csp_regional.get("distinct_operators", 0),
            "value_unit":           "count",
            "value_year":           None,
            "source_class":         "FEDERAL_DATA",
            "source_url":           csp_regional.get("source_url"),
            "source_title":         csp_regional.get("source_title"),
            "source_date":          None,
            "source_notes":         "Regional ecosystem context (state + adjacent); not scored.",
            "confidence":           "MODERATE",
            "confidence_rationale": "Counted from USAspending federal award records",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "usaspending_csp_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     False,
            "needs_verification_reason": "Regional context only — intentionally excluded from scoring",
        })

    if ipeds_data and "cip13_completers" not in existing_keys:
        comp = ipeds_data.get("completers", 0)
        facts.append({
            "datapoint_id":         f"dp_{community_id}_replication_feasibility_002",
            "community_id":         community_id,
            "state":                state,
            "dimension":            "replication_feasibility",
            "fact_key":             "cip13_completers",
            "claim": (
                f"{comp} education-program (CIP 13) completers were awarded "
                f"statewide in {ipeds_data.get('data_year')}, per IPEDS — a proxy "
                f"for the local teacher-credential pipeline."
            ),
            "value":                comp,
            "value_unit":           "count",
            "value_year":           ipeds_data.get("data_year"),
            "source_class":         "FEDERAL_DATA",
            "source_url":           ipeds_data.get("source_url"),
            "source_title":         ipeds_data.get("source_title"),
            "source_date":          None,
            "source_notes":         "Statewide signal; not community-specific.",
            "confidence":           "MODERATE",
            "confidence_rationale": "Summed from IPEDS federal completions records (aggregate rows)",
            "verification_status":  "VERIFIED",
            "bias_flag":            None,
            "extracted_by":         "ipeds_completers_fetcher",
            "extracted_at":         today_str(),
            "stage":                STAGE_ID,
            "in_main_analysis":     True,
            "needs_verification_reason": None,
        })

    logger.info(
        "[%s] Injected replication datapoints (county_operators=%s, completers=%s, regional_operators=%s)",
        community_id,
        csp_county.get("distinct_operators") if csp_county else None,
        ipeds_data.get("completers") if ipeds_data else None,
        csp_regional.get("distinct_operators") if csp_regional else None,
    )


def _inject_funding_csp_evidence(
    facts_output: dict,
    csp_place: Optional[dict],
    community_id: str,
    state: str,
) -> None:
    """Append NM place-of-performance CSP award activity as funding_environment
    EVIDENCE. NON-SCORING: in_main_analysis=False keeps it out of S5's
    funding_environment scoring (which reads its own primary/secondary fact
    keys), while still surfacing it in the fact bundle for S6 synthesis.
    """
    if not csp_place:
        return

    facts: list[dict] = facts_output.setdefault("facts", [])
    if any(f.get("fact_key") == "csp_nm_place_of_performance_evidence" for f in facts):
        return

    facts.append({
        "datapoint_id":         f"dp_{community_id}_funding_environment_001",
        "community_id":         community_id,
        "state":                state,
        "dimension":            "funding_environment",
        "fact_key":             "csp_nm_place_of_performance_evidence",
        "claim": (
            f"{csp_place.get('award_count', 0)} federal CSP awards "
            f"(${csp_place.get('total_dollars', 0):,.0f}) had a "
            f"place of performance in {state.upper()} over the last 10 years."
        ),
        "value":                csp_place.get("award_count", 0),
        "value_unit":           "count",
        "value_year":           None,
        "source_class":         "FEDERAL_DATA",
        "source_url":           csp_place.get("source_url"),
        "source_title":         csp_place.get("source_title"),
        "source_date":          None,
        "source_notes":         "Contextual CSP-in-state evidence for synthesis; not scored.",
        "confidence":           "MODERATE",
        "confidence_rationale": "Counted from USAspending federal award records",
        "verification_status":  "VERIFIED",
        "bias_flag":            None,
        "extracted_by":         "usaspending_csp_fetcher",
        "extracted_at":         today_str(),
        "stage":                STAGE_ID,
        "in_main_analysis":     False,
        "needs_verification_reason": "Contextual evidence only — intentionally excluded from scoring",
    })
    logger.info(
        "[%s] Injected funding_environment CSP evidence (non-scoring): "
        "%s NM awards, $%.0f",
        community_id, csp_place.get("award_count", 0), csp_place.get("total_dollars", 0),
    )


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
    # max_uses: standard=5 (reverted from 6 — raised search pressure), deep=10
    ci_max_uses = {"standard": 5, "deep": 10}.get(config.depth, 5)

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
        # Project to prompt-relevant fields only before slicing and serialising.
        # Strips enrollment_cap, address, proficiency_year, source_class,
        # source_url, source_title, confidence, contact_verification_required —
        # none of which the charter_intel prompt needs.
        projected = [
            {k: v for k, v in school.items() if k in _SCHOOL_PROMPT_FIELDS}
            for school in csv_schools[:_MAX_CHARTER_INTEL_SCHOOLS]
        ]

        prompt_text = load_prompt("prompts/extract/s3_charter_intel.md", {
            "COMMUNITY_NAME":      community_name,
            "STATE_NAME":          state_name,
            "STATE_CODE":          state,
            "TODAY_DATE":          today_str(),
            "CSV_SCHOOLS_JSON":    json.dumps(projected,        indent=2),
            "CSV_AUTHORIZERS_JSON": json.dumps(csv_authorizers, indent=2),
            "DEPTH_INSTRUCTION":   depth_instruction,
        })

        system = (
            "You are a charter school market researcher. "
            "Respond ONLY with valid JSON. No markdown fences, no preamble."
        )

        _ci_cache = CacheManager(config)
        result = call_claude_cached(
            _ci_cache,
            _s3_llm_cache_key(state, community_id, "charter_intel", system, prompt_text),
            ttl_days=S3_LLM_CACHE_TTL_DAYS,
            model=config.model_sonnet,
            system=system,
            user=prompt_text,
            max_tokens=4096,
            temperature=0.3,
            expect_json=True,
            use_web_search=use_web,
            web_search_max_uses=ci_max_uses,
            stage=f"{STAGE_ID}_charter_intel",
            community_id=community_id,
        )
        _write_s3_audit(community_id, config.depth, "s3_charter_intel", result)

        if result.parse_error:
            logger.warning(
                "[%s] Charter intel JSON parse failed: %s", community_id, result.parse_error
            )
            return {}

        data = result.parsed_json or {}

        # FIX A: Content guard for empty charter_schools with signals in raw text
        charter_schools = data.get("top_charter_schools", [])
        if not charter_schools:
            raw_text = result.text or ""
            raw_lower = raw_text.lower()
            # Check for charter signals per spec:
            # - "charter" mentioned more than 3 times
            # - "charter school" mentioned (school names)
            # - "charter" + "enrollment" (enrollment numbers)
            # - "charter" + "operator" (operators)
            charter_count = raw_lower.count("charter")
            has_charter_school = "charter school" in raw_lower
            has_charter_enrollment = "charter" in raw_lower and "enrollment" in raw_lower
            has_charter_operator = "charter" in raw_lower and "operator" in raw_lower

            charter_signals = [
                charter_count > 3,
                has_charter_school,
                has_charter_enrollment,
                has_charter_operator,
            ]
            if any(charter_signals):
                # Found signals but returned empty — mark as LOW confidence
                data["top_charter_schools"] = []
                data["charter_intel_confidence"] = "LOW"
                data["charter_intel_warning"] = (
                    "Extraction returned zero schools but response text suggests charter activity "
                    "was found. Manual review required before scoring."
                )
                logger.warning(
                    "[%s] Charter intel: zero schools but response suggests activity — "
                    "confidence set to LOW, manual review required",
                    community_id,
                )
            else:
                logger.info(
                    "[%s] Charter intel: zero schools confirmed — no charter signals in response",
                    community_id,
                )

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


def _align_num_charter_schools(facts: list[dict], known_schools: list[str]) -> list[dict]:
    """Override num_charter_schools with the S1 roster count (len(known_schools)).

    The S3 model emits num_charter_schools grounded in the injected VERIFIED_ROSTER
    table, which can drift from the authoritative S1 roster (e.g. Albuquerque: model
    reports 53 vs S1 roster 55). The political-climate gate already uses
    len(known_schools) as the operating count, so this aligns S3 to the same source.

    The numeric `value` is overridden, and the old value is also swapped for the
    new count inside the human-readable `claim` string (so the brief narrative and
    the scorecard agree). source_class, confidence, and every other field are left
    untouched. State-agnostic: known_schools is the universal S1 abstraction (no
    state-specific paths). No-op when the roster is empty, since there is nothing
    authoritative to align to.
    """
    if not known_schools:
        return facts
    count = len(known_schools)
    for f in facts:
        if f.get("fact_key") == "num_charter_schools":
            old_value = f.get("value")
            f["value"] = count
            claim = f.get("claim")
            if isinstance(claim, str) and old_value is not None:
                f["claim"] = claim.replace(str(old_value), str(count))
    return facts
