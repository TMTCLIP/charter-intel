"""
pipeline/utils/usaspending_csp_fetcher.py
Utility: fetch federal Charter Schools Program (CSP) award activity from the
free, keyless USAspending API.

CSP (Charter Schools Program, CFDA/Assistance Listing 84.282) is the federal
grant program that seeds and replicates charter operators. Three views are
fetched:

  - place_of_performance_county (state + 3-digit county): the count of distinct
    CSP operators that have performed funded work IN the community's county. This
    is the SCORED operator sub-signal of replication_feasibility in S5 — it
    discriminates by community (re-scoped from the original NM+adjacent recipient
    view at operator request, 2026-05-30, to make replication community-specific).
  - place_of_performance (state only): a NON-SCORING evidence signal surfaced for
    synthesis (funding_environment).
  - recipient (state + adjacent states): the regional CSP-operator ecosystem
    count, retained as NON-SCORING context for synthesis.

DATA SOURCE:
  POST https://api.usaspending.gov/api/v2/search/spending_by_award/  (no auth)

VERIFIED (live probe, 2026-05-30):
  - program_numbers=["84.282"] returns CSP awards; every returned record carried
    cfda_number "84.282", so the single primary code is sufficient. The expanded
    fallback list (84.282A/B/E/M) is retried ONLY if the primary returns zero
    awards (it did not in NM, but the retry guards other states/years).
  - The field labels in FIELDS are all accepted by the endpoint, including
    "CFDA Number" (used to record exactly which assistance listing matched).
  - USAspending limits time_period to an earliest start of 2007-10-01.
  - Pagination is page/limit with page_metadata.hasNext.

CACHE:
  Responses cached at data/cache/fetcher/usaspending/{key}.json
  The cache key encodes scope + the full set of states queried + the program
  set so that a place-of-performance query can never collide with a recipient
  query (or an NM query with an NM+adjacent query). TTL: 30 days.

ERROR CONTRACT:
  - Any non-200 response or exception during fetch -> return None.
  - A successful query that legitimately returns no awards -> a ZERO result
    (award_count=0, distinct_operators=0), which is a VALID scored value, not
    "no data".
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# ── API config ──────────────────────────────────────────────────────────────

USASPENDING_API_URL = (
    "https://api.usaspending.gov/api/v2/search/spending_by_award/"
)
SOURCE_TITLE = "USAspending.gov — Charter Schools Program (CFDA 84.282) awards"
SOURCE_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# CSP assistance listing. See module docstring for the live verification note.
CSP_PROGRAM_NUMBERS = ["84.282"]
CSP_PROGRAM_NUMBERS_FALLBACK = ["84.282", "84.282A", "84.282B", "84.282E", "84.282M"]

# 02-05 = block/formula and project grant assistance types (the CSP award types).
AWARD_TYPE_CODES = ["02", "03", "04", "05"]

LOOKBACK_YEARS = 10
# USAspending rejects time_period start dates earlier than this.
_EARLIEST_START = "2007-10-01"

# Award detail fields to request. If the endpoint ever rejects a label, probe
# the endpoint for the accepted name and fix it here.
FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Start Date",
    "Awarding Agency",
    "Place of Performance State Code",
    "CFDA Number",
]

# Replication scope = the state plus its adjacent states.
ADJACENT_STATES: dict[str, list[str]] = {
    "NM": ["NM", "TX", "AZ", "CO", "OK", "UT"],
    "MS": ["MS", "AL", "AR", "LA", "TN"],
    "TN": ["TN", "AL", "AR", "GA", "KY", "MO", "MS", "NC", "VA"],
    "WI": ["WI", "IL", "IA", "MI", "MN"],
}

_PAGE_LIMIT = 100
_MAX_PAGES = 50  # safety cap; CSP nationwide is far below this at limit=100

# ── Cache config ──────────────────────────────────────────────────────────────

_CACHE_DIR = "data/cache/fetcher/usaspending"
CACHE_TTL_DAYS = 30   # see also CACHE_TTL_DAYS hint in cache.py


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.json")


def _read_cache(key: str) -> Optional[dict]:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    if age_days > CACHE_TTL_DAYS:
        log.info("usaspending_fetcher: cache expired for key %s (%.0f days old)", key, age_days)
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("usaspending_fetcher: cache read failed for key %s — %s", key, exc)
        return None


def _write_cache(key: str, data: dict) -> None:
    path = _cache_path(key)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("usaspending_fetcher: cache write failed for key %s — %s", key, exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _states_for_scope(scope: str, state: str) -> list[str]:
    """Return the list of state codes queried for a given scope."""
    if scope == "recipient":
        states = ADJACENT_STATES.get(state.upper())
        if states is None:
            log.warning(
                "ADJACENT_STATES not defined for %s, using state-only fallback",
                state.upper(),
            )
            return [state.upper()]
        return states
    return [state.upper()]


def _build_location_filter(
    scope: str, state: str, county: Optional[str] = None
) -> tuple[str, list[dict]]:
    """Construct the USAspending LocationObject filter for a scope.

    Returns (filter_key, location_objects):
      - "recipient"                  -> ("recipient_locations", [{NM},{TX},...])
      - "place_of_performance"       -> ("place_of_performance_locations", [{NM}])
      - "place_of_performance_county"-> ("place_of_performance_locations",
                                          [{NM, county=015}])  (county scoped)

    `county` is the 3-digit county FIPS (the county portion only, e.g. "015"),
    which is the form USAspending's LocationObject expects.
    """
    if scope == "recipient":
        states = _states_for_scope(scope, state)
        return "recipient_locations", [{"country": "USA", "state": s} for s in states]

    loc = {"country": "USA", "state": state.upper()}
    if scope == "place_of_performance_county" and county:
        loc["county"] = county
    return "place_of_performance_locations", [loc]


def _time_period() -> list[dict]:
    """Last LOOKBACK_YEARS years, clamped to the USAspending earliest start."""
    today = datetime.date.today()
    start = today.replace(year=today.year - LOOKBACK_YEARS)
    start_str = max(start.isoformat(), _EARLIEST_START)
    return [{"start_date": start_str, "end_date": today.isoformat()}]


def _build_filters(
    scope: str, state: str, program_numbers: list[str], county: Optional[str] = None
) -> dict:
    loc_key, locations = _build_location_filter(scope, state, county)
    return {
        "program_numbers": list(program_numbers),
        "award_type_codes": list(AWARD_TYPE_CODES),
        "time_period": _time_period(),
        loc_key: locations,
    }


def _post(body: dict) -> Optional[dict]:
    """POST a JSON body; return the parsed response dict, or None on any failure."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        USASPENDING_API_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "CLIP/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                log.warning("usaspending_fetcher: HTTP %s", resp.status)
                return None
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("usaspending_fetcher: request failed — %s", exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("usaspending_fetcher: JSON parse error — %s", exc)
        return None


def _fetch_all_awards(filters: dict) -> Optional[list[dict]]:
    """Page through spending_by_award. Returns all result rows, [] if none,
    or None if any page request fails (so the caller keeps the neutral default).
    """
    results: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        body = {
            "filters": filters,
            "fields": list(FIELDS),
            "page": page,
            "limit": _PAGE_LIMIT,
        }
        resp = _post(body)
        if resp is None or "results" not in resp:
            return None
        results.extend(resp["results"])
        if not resp.get("page_metadata", {}).get("hasNext"):
            break
        page += 1
    return results


def _normalize_operator(name: Optional[str]) -> Optional[str]:
    """Normalize a recipient name for distinct-operator counting."""
    if not name:
        return None
    norm = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm or None


def _derive(rows: list[dict], scope: str, state: str, program_numbers: list[str],
            county: Optional[str] = None) -> dict:
    """Derive aggregate signals from award rows."""
    total_dollars = 0.0
    for r in rows:
        amt = r.get("Award Amount")
        if isinstance(amt, (int, float)):
            total_dollars += float(amt)
    operators = sorted({
        n for n in (_normalize_operator(r.get("Recipient Name")) for r in rows)
        if n
    })
    matched_cfda = sorted({
        str(r.get("CFDA Number")) for r in rows if r.get("CFDA Number")
    })
    return {
        "scope": scope,
        "state": state.upper(),
        "county_fips": county,
        "states_queried": _states_for_scope(scope, state),
        "award_count": len(rows),
        "total_dollars": round(total_dollars, 2),
        "distinct_operators": len(operators),
        "operators": operators,
        "matched_program_numbers": list(program_numbers),
        "matched_cfda_numbers": matched_cfda,
        "source": "USASPENDING",
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "MODERATE",
    }


# ── Public interface ──────────────────────────────────────────────────────────

def get_csp_awards(
    state: str, scope: str = "recipient", county_fips: Optional[str] = None
) -> Optional[dict]:
    """Return CSP award aggregates for a state and scope, or None if unavailable.

    Parameters
    ----------
    state : two-letter state code (e.g. "NM")
    scope : "place_of_performance_county" -> awards physically performed in the
                                    community's county (3-digit county_fips). This
                                    is the SCORED replication operator signal — it
                                    discriminates by community.
            "place_of_performance" -> state only (funding_environment evidence;
                                    NON-SCORING).
            "recipient"          -> state + adjacent states (regional CSP-operator
                                    ecosystem; surfaced as non-scoring context).
    county_fips : 3-digit county FIPS (county portion only, e.g. "015"). Required
                  for the place_of_performance_county scope; ignored otherwise.

    Returns None (does not raise) on any HTTP/parse failure. Returns a populated
    dict on success; a query that legitimately matches zero awards returns a
    valid ZERO result (award_count=0, distinct_operators=0).
    """
    if not state:
        return None
    if scope == "place_of_performance_county" and not county_fips:
        return None  # county scope requires a county

    states = _states_for_scope(scope, state)
    program_tag = "primary"  # may flip to "fallback" below; recorded in cache key
    county_tag = county_fips or "all"
    cache_key = f"csp_{scope}_{'-'.join(states)}_{county_tag}_{program_tag}"

    cached = _read_cache(cache_key)
    if cached is not None:
        log.info("usaspending_fetcher: cache hit for %s", cache_key)
        return cached

    # Primary attempt with the single CSP assistance listing.
    rows = _fetch_all_awards(_build_filters(scope, state, CSP_PROGRAM_NUMBERS, county_fips))
    if rows is None:
        return None  # hard failure — keep neutral default upstream

    program_numbers = CSP_PROGRAM_NUMBERS
    if len(rows) == 0:
        # Zero awards under the primary code: retry with the expanded family in
        # case this state/year files under a sub-listing. Live NM data needed
        # only "84.282" (see module docstring); this guards other geographies.
        fallback_rows = _fetch_all_awards(
            _build_filters(scope, state, CSP_PROGRAM_NUMBERS_FALLBACK, county_fips)
        )
        if fallback_rows is None:
            return None
        if len(fallback_rows) > 0:
            rows = fallback_rows
            program_numbers = CSP_PROGRAM_NUMBERS_FALLBACK
            cache_key = f"csp_{scope}_{'-'.join(states)}_{county_tag}_fallback"

    result = _derive(rows, scope, state, program_numbers, county_fips)
    log.info(
        "usaspending_fetcher: %s scope=%s county=%s — %d awards, %d distinct operators, $%.0f (cfda=%s)",
        state, scope, county_fips or "-", result["award_count"], result["distinct_operators"],
        result["total_dollars"], ",".join(result["matched_cfda_numbers"]) or "none",
    )
    _write_cache(cache_key, result)
    return result
