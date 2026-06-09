"""
pipeline/utils/acs_fetcher.py
Utility: fetch ELL data from Census ACS API for injection into S3 prompts.

Calls the Census Bureau's ACS 5-year API at runtime (no flat file required).
Returns ELL (English Language Learner) figures for a school district.

DATA SOURCE:
  Census ACS 5-Year Estimates, Table B16004
  "Age by Language Spoken at Home by Ability to Speak English"
  Geography: unified school districts, NM (state FIPS 35)

  Denominator : B16004_002E — total population, ages 5–17
  Numerator   : ages 5–17 who speak English "less than very well"
                (all "not well" + "not at all" cells in the 5–17 band)

  Variables used:
    B16004_007E  Spanish,            "not well"
    B16004_008E  Spanish,            "not at all"
    B16004_012E  Other Indo-European,"not well"
    B16004_013E  Other Indo-European,"not at all"
    B16004_017E  Asian/Pacific,      "not well"
    B16004_018E  Asian/Pacific,      "not at all"
    B16004_022E  Other languages,    "not well"
    B16004_023E  Other languages,    "not at all"

GEOGRAPHY JOIN:
  LEAID (7 digits) = state_fips(2) + district_code(5)
  Census API returns state and school_district as separate fields.
  district_code = leaid[2:]   e.g. "3500060" → "00060"

API KEY:
  Census API requires a free key (sign up: https://api.census.gov/data/key_signup.html).
  Set CENSUS_API_KEY in your environment before running.
  If the key is absent, get_district_data() returns None and logs a warning —
  the pipeline continues without ell_pct rather than crashing.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

import yaml

from pipeline.utils.fips_utils import get_state_fips

log = logging.getLogger(__name__)

# ── File cache config ─────────────────────────────────────────────────────────
# ACS 5-year data is updated annually each December; 180 days gives one refresh
# per release cycle without hitting the API on every pipeline run.
_CACHE_DIR_ACS     = "data/cache/fetcher/acs"
ACS_CACHE_TTL_DAYS = 180   # see also CACHE_TTL_DAYS["acs_district"] in cache.py


def _acs_cache_read(key: str) -> Optional[dict]:
    """Read a JSON file from the ACS fetcher cache; return None if missing or expired."""
    path = os.path.join(_CACHE_DIR_ACS, f"{key}.json")
    if not os.path.exists(path):
        return None
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    if age_days > ACS_CACHE_TTL_DAYS:
        log.info("acs_fetcher: cache expired for key %s (%.0f days old)", key, age_days)
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("acs_fetcher: cache read failed for key %s — %s", key, exc)
        return None


def _acs_cache_write(key: str, data: dict) -> None:
    """Write data to the ACS fetcher cache."""
    path = os.path.join(_CACHE_DIR_ACS, f"{key}.json")
    try:
        os.makedirs(_CACHE_DIR_ACS, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("acs_fetcher: cache write failed for key %s — %s", key, exc)

# ── Census API config ─────────────────────────────────────────────────────────

ACS_API_BASE = "https://api.census.gov/data"
ACS_YEAR     = "2023"       # Most recent released ACS 5-year as of 2026
ACS_DATASET  = "acs/acs5"

STATES_YAML  = "config/states.yaml"

SOURCE_URL   = f"{ACS_API_BASE}/{ACS_YEAR}/{ACS_DATASET}"
SOURCE_TITLE = "Census ACS 5-Year Estimates, Table B16004 (2019–2023)"
DATA_YEAR    = "2019-2023"

# ── B16004 variable lists ─────────────────────────────────────────────────────

# Total school-age population (denominator)
_DENOM_VAR = "B16004_002E"

# Ages 5–17 who speak English "less than very well" (numerator)
# = "not well" + "not at all" across all four language groups
_LTVW_VARS = [
    "B16004_007E",   # Spanish            — "not well"
    "B16004_008E",   # Spanish            — "not at all"
    "B16004_012E",   # Other Indo-European— "not well"
    "B16004_013E",   # Other Indo-European— "not at all"
    "B16004_017E",   # Asian/Pacific      — "not well"
    "B16004_018E",   # Asian/Pacific      — "not at all"
    "B16004_022E",   # Other languages    — "not well"
    "B16004_023E",   # Other languages    — "not at all"
]

_GET_VARS = ",".join(["NAME", _DENOM_VAR] + _LTVW_VARS)

# District geography types to try in order — mirrors SAIPE's _DISTRICT_TYPES fallback
_ACS_DISTRICT_TYPES = [
    "school district (unified)",
    "school district (elementary)",
    "school district (secondary)",
]

# ── Total population query config ─────────────────────────────────────────────
# B01003_001E: total population for the district geography.
# Two ACS 5-year vintages give a ~4-year population trend.
_TOTAL_POP_VAR     = "B01003_001E"
_POP_VINTAGE_EARLY = "2019"   # ACS 5-year 2015–2019
_POP_VINTAGE_LATE  = "2023"   # ACS 5-year 2019–2023


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_nces_map(state: str) -> dict:
    """Return the nces_district_map for the given state from states.yaml."""
    try:
        with open(STATES_YAML) as f:
            config = yaml.safe_load(f)
        return config.get(state.upper(), {}).get("nces_district_map", {})
    except Exception as exc:
        log.warning("acs_fetcher: could not load states.yaml — %s", exc)
        return {}


def _api_key() -> Optional[str]:
    """Return CENSUS_API_KEY from the environment, or None."""
    return os.environ.get("CENSUS_API_KEY") or None


def _fetch_district(district_code: str, api_key: str, state_fips: str) -> Optional[dict]:
    """
    Call the Census ACS API for a school district, trying unified → elementary → secondary.

    district_code — 5-digit code derived from leaid[2:]
                    e.g. "00060" for Albuquerque (LEAID 3500060)

    Returns a {column_name: value} dict for the first matching district type,
    or None if all three types return no data or fail.
    """
    for district_type in _ACS_DISTRICT_TYPES:
        params = {
            "get": _GET_VARS,
            "for": f"{district_type}:{district_code}",
            "in":  f"state:{state_fips}",
            "key": api_key,
        }
        # quote_via=urllib.parse.quote encodes spaces as %20 (not +)
        url = (
            f"{ACS_API_BASE}/{ACS_YEAR}/{ACS_DATASET}?"
            + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
        except Exception as exc:
            log.warning(
                "acs_fetcher: API request failed for district %s type=%s — %s",
                district_code, district_type, exc
            )
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning(
                "acs_fetcher: could not parse API response for district %s type=%s — %s",
                district_code, district_type, exc
            )
            continue

        # Expected: [[header, ...], [value, ...]] — fewer than 2 rows means no match
        if not isinstance(data, list) or len(data) < 2:
            continue

        if district_type != "school district (unified)":
            log.debug(
                "acs_fetcher: district %s ELL resolved via '%s' (not unified)",
                district_code, district_type,
            )

        headers, row = data[0], data[1]
        return dict(zip(headers, row))

    return None


def _fetch_acs_vars(
    district_code: str,
    variables: str,
    year: str,
    api_key: str,
    state_fips: str,
    district_type: str = "school district (unified)",
) -> Optional[dict]:
    """
    Generic ACS API query for any variable set at the given school district level.

    Identical transport logic to _fetch_district but accepts arbitrary variables,
    vintage year, and district type — used by get_total_population to query
    B01003_001E across two vintages without touching the ELL pipeline.
    """
    params = {
        "get": variables,
        "for": f"{district_type}:{district_code}",
        "in":  f"state:{state_fips}",
        "key": api_key,
    }
    url = (
        f"{ACS_API_BASE}/{year}/{ACS_DATASET}?"
        + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning(
            "acs_fetcher: API request failed for district %s vintage %s — %s",
            district_code, year, exc,
        )
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "acs_fetcher: could not parse response for district %s vintage %s — %s",
            district_code, year, exc,
        )
        return None

    if not isinstance(data, list) or len(data) < 2:
        log.warning(
            "acs_fetcher: unexpected response shape for district %s vintage %s: %s",
            district_code, year, str(data)[:200],
        )
        return None

    headers, row = data[0], data[1]
    return dict(zip(headers, row))


# ── Public interface ──────────────────────────────────────────────────────────

def get_district_data(community_id: str, state: str) -> Optional[dict]:
    """
    Return Census ACS ELL data for a community, or None if unavailable.

    Return shape:
        {
            "ell_pct":      18.5,             # ages 5-17, English less than very well
            "data_year":    "2019-2023",
            "source_url":   "https://...",
            "source_title": "Census ACS 5-Year Estimates, Table B16004 (2019-2023)",
            "confidence":   "MODERATE",       # ACS 5-year has sampling error
        }

    Returns None (does not raise) if:
      - CENSUS_API_KEY is not set in the environment
      - community_id has no entry in nces_district_map
      - community_id maps to null (e.g. Navajo Nation PO Box addresses)
      - API request fails or returns unexpected data
      - Denominator (total ages 5-17) is zero or missing
    """
    api_key = _api_key()
    if not api_key:
        log.warning(
            "acs_fetcher: CENSUS_API_KEY not set — ell_pct will not be available. "
            "Get a free key at https://api.census.gov/data/key_signup.html"
        )
        return None

    nces_map = _load_nces_map(state)
    leaid = nces_map.get(community_id)

    if leaid is None:
        log.info(
            "acs_fetcher: no NCES mapping for %s in %s — skipping",
            community_id, state
        )
        return None

    leaid_str = str(leaid)
    if len(leaid_str) != 7:
        log.warning(
            "acs_fetcher: unexpected LEAID length for %s (%s) — skipping",
            community_id, leaid_str
        )
        return None

    state_fips = get_state_fips(state)
    if state_fips is None:
        log.warning("acs_fetcher: unknown state '%s' — cannot derive FIPS", state)
        return None

    # ── Cache read ────────────────────────────────────────────────────────────
    _cache_key = f"ell_{leaid_str}"
    cached = _acs_cache_read(_cache_key)
    if cached is not None:
        log.info("acs_fetcher: cache hit for ELL %s (LEAID %s)", community_id, leaid_str)
        return cached

    # "3500060" → "00060"
    district_code = leaid_str[2:]

    row = _fetch_district(district_code, api_key, state_fips)
    if row is None:
        return None

    # Parse denominator — total ages 5-17
    try:
        denominator = int(row[_DENOM_VAR])
    except (KeyError, ValueError, TypeError) as exc:
        log.warning(
            "acs_fetcher: missing or invalid denominator (%s) for %s — %s",
            _DENOM_VAR, community_id, exc
        )
        return None

    if denominator <= 0:
        log.warning(
            "acs_fetcher: zero denominator for %s — skipping", community_id
        )
        return None

    # Sum numerator — all LTVW cells for ages 5-17
    numerator = 0
    suppressed_count = 0
    for var in _LTVW_VARS:
        try:
            val = int(row[var])
            if val > 0:
                numerator += val
            elif val < 0:
                # Census suppression sentinel (e.g. -666666666 = "N/A",
                # -555555555 = "suppressed"). Do not add to numerator.
                suppressed_count += 1
        except (KeyError, ValueError, TypeError):
            log.warning(
                "acs_fetcher: missing variable %s for %s — treating as 0",
                var, community_id
            )
    # If the majority of ELL variables were suppressed, the result is
    # unreliable — return None rather than a misleading 0.0%.
    # Threshold: 4 of 8 variables suppressed triggers the guard.
    _SUPPRESSION_THRESHOLD = len(_LTVW_VARS) // 2
    if suppressed_count > _SUPPRESSION_THRESHOLD:
        log.warning(
            "acs_fetcher: %d/%d ELL variables suppressed for %s "
            "(likely small-district sample) — returning None instead of 0.0%%",
            suppressed_count, len(_LTVW_VARS), community_id,
        )
        return None

    # Guard 2: all LTVW vars returned 0 in a small district.
    # ACS B16004 cell-level rounding can produce all-zero rows for small
    # districts even when ELL population is nonzero. If numerator is 0
    # AND total enrollment (denominator) is below a plausibility threshold,
    # treat result as unreliable.
    _SMALL_DISTRICT_THRESHOLD = 1500  # total ACS-reported enrollment
    if numerator == 0 and denominator < _SMALL_DISTRICT_THRESHOLD:
        log.warning(
            "acs_fetcher: all LTVW vars returned 0 for %s "
            "(denominator=%d, likely small-district cell rounding) "
            "— returning None instead of 0.0%%",
            community_id, denominator,
        )
        return None

    ell_pct = round(numerator / denominator * 100, 1)

    log.info(
        "acs_fetcher: %s (district %s) — ELL=%.1f%% (%d / %d)",
        community_id, district_code, ell_pct, numerator, denominator
    )

    result = {
        "ell_pct":      ell_pct,
        "data_year":    DATA_YEAR,
        "source_url":   SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence":   "MODERATE",
    }
    _acs_cache_write(_cache_key, result)
    return result


def get_total_population(community_id: str, state: str) -> Optional[dict]:
    """
    Return Census ACS total population estimates for two vintages.

    Queries B01003_001E (total population) at the school district level for ACS
    5-year vintages 2019 (2015–2019) and 2023 (2019–2023). Computes pct change
    between vintages as a secondary demographic signal.

    Tries unified → elementary → secondary district types in order, mirroring the
    fallback behavior of get_district_data and SAIPE.  State FIPS is derived via
    get_state_fips() — works for any state in fips_utils.py.

    Return shape:
        {
            "pop_2019":    8234,
            "pop_2023":    7891,
            "pct_change":  -4.2,
            "data_year":   "2019–2023",
            "source_title": "Census ACS 5-Year Estimates, B01003 Total Population",
            "source_url":  "https://api.census.gov/data",
            "confidence":  "MODERATE",
        }

    Returns None (does not raise) if: CENSUS_API_KEY not set, unknown state,
    no LEAID mapping, either vintage API call fails, or population values are invalid.
    """
    api_key = _api_key()
    if not api_key:
        return None  # warning already emitted by get_district_data caller

    state_fips = get_state_fips(state)
    if state_fips is None:
        log.warning(
            "acs_fetcher: unknown state '%s' — cannot derive FIPS for total population",
            state,
        )
        return None

    nces_map = _load_nces_map(state)
    leaid = nces_map.get(community_id)

    if leaid is None:
        log.info(
            "acs_fetcher: no NCES mapping for %s — skipping total population query",
            community_id,
        )
        return None

    leaid_str = str(leaid)
    if len(leaid_str) != 7:
        log.warning(
            "acs_fetcher: unexpected LEAID length for %s (%s) — skipping",
            community_id, leaid_str,
        )
        return None

    district_code = leaid_str[2:]

    # ── Cache read ────────────────────────────────────────────────────────────
    _pop_cache_key = f"pop_{leaid_str}"
    cached_pop = _acs_cache_read(_pop_cache_key)
    if cached_pop is not None:
        log.info("acs_fetcher: cache hit for total population %s (LEAID %s)", community_id, leaid_str)
        return cached_pop

    variables = f"NAME,{_TOTAL_POP_VAR}"

    pop_by_vintage: dict[str, int] = {}
    for vintage in (_POP_VINTAGE_EARLY, _POP_VINTAGE_LATE):
        row = None
        for dt in _ACS_DISTRICT_TYPES:
            row = _fetch_acs_vars(district_code, variables, vintage, api_key, state_fips, district_type=dt)
            if row is not None:
                if dt != "school district (unified)":
                    log.debug(
                        "acs_fetcher: total population for %s vintage %s resolved via '%s'",
                        community_id, vintage, dt,
                    )
                break

        if row is None:
            log.warning(
                "acs_fetcher: total population query failed for %s vintage %s across all district types",
                community_id, vintage,
            )
            return None

        try:
            pop = int(row[_TOTAL_POP_VAR])
        except (KeyError, ValueError, TypeError) as exc:
            log.warning(
                "acs_fetcher: invalid population value for %s vintage %s — %s",
                community_id, vintage, exc,
            )
            return None

        if pop <= 0:
            log.warning(
                "acs_fetcher: zero/negative population for %s vintage %s — skipping",
                community_id, vintage,
            )
            return None

        pop_by_vintage[vintage] = pop

    pop_early = pop_by_vintage[_POP_VINTAGE_EARLY]
    pop_late  = pop_by_vintage[_POP_VINTAGE_LATE]
    pct_change = round((pop_late - pop_early) / pop_early * 100, 1)

    log.info(
        "acs_fetcher: %s (district %s) — total pop: %s=%d  %s=%d  Δ=%.1f%%",
        community_id, district_code,
        _POP_VINTAGE_EARLY, pop_early,
        _POP_VINTAGE_LATE,  pop_late,
        pct_change,
    )

    pop_result = {
        "pop_2019":    pop_early,
        "pop_2023":    pop_late,
        "pct_change":  pct_change,
        "data_year":   f"{_POP_VINTAGE_EARLY}–{_POP_VINTAGE_LATE}",
        "source_title": "Census ACS 5-Year Estimates, B01003 Total Population",
        "source_url":  ACS_API_BASE,
        "confidence":  "MODERATE",
    }
    _acs_cache_write(_pop_cache_key, pop_result)
    return pop_result
