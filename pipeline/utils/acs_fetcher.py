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
import urllib.parse
import urllib.request
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# ── Census API config ─────────────────────────────────────────────────────────

ACS_API_BASE = "https://api.census.gov/data"
ACS_YEAR     = "2023"       # Most recent released ACS 5-year as of 2026
ACS_DATASET  = "acs/acs5"
STATE_FIPS   = "35"         # New Mexico

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


def _fetch_district(district_code: str, api_key: str) -> Optional[dict]:
    """
    Call the Census ACS API for one unified school district.

    district_code — 5-digit code derived from leaid[2:]
                    e.g. "00060" for Albuquerque (LEAID 3500060)

    Returns a {column_name: value} dict for the single data row,
    or None on any network/parse failure.
    """
    params = {
        "get": _GET_VARS,
        "for": f"school district (unified):{district_code}",
        "in":  f"state:{STATE_FIPS}",
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
            "acs_fetcher: API request failed for district %s — %s",
            district_code, exc
        )
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "acs_fetcher: could not parse API response for district %s — %s",
            district_code, exc
        )
        return None

    # Expected: [[header, ...], [value, ...]]
    if not isinstance(data, list) or len(data) < 2:
        log.warning(
            "acs_fetcher: unexpected response shape for district %s: %s",
            district_code, str(data)[:200]
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

    # "3500060" → "00060"
    district_code = leaid_str[2:]

    row = _fetch_district(district_code, api_key)
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

    return {
        "ell_pct":      ell_pct,
        "data_year":    DATA_YEAR,
        "source_url":   SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence":   "MODERATE",
    }
