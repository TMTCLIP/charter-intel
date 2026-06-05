"""
pipeline/utils/saipe_fetcher.py
Utility: fetch SAIPE school district poverty data from the Census API.

Census SAIPE (Small Area Income and Poverty Estimates) provides poverty rate
at school district geography — an independent federal poverty signal separate
from NCES FRL%. Used for operational_complexity and funding_environment.

DATA SOURCE:
  Census SAIPE School District, timeseries endpoint
  https://api.census.gov/data/timeseries/poverty/saipe/schdist

  Variables used:
    SAEPOVRAT5_17RV_PT — poverty rate, related children ages 5-17 (%)
    SAEPOV5_17RV_PT    — poverty count, related children ages 5-17
    SAEPOVALL_PT       — total population estimate for the district

GEOGRAPHY JOIN:
  LEAID (7 digits) = state_fips(2) + district_code(5)
  district_code = leaid[2:]   e.g. "3500060" → "00060"
  Attempts unified, elementary, and secondary district types; returns first match.

API KEY:
  Uses CENSUS_API_KEY (same environment variable as acs_fetcher.py).
  If absent, returns None with a warning — pipeline continues without SAIPE data.

CACHE:
  Responses cached at data/cache/fetcher/saipe/{leaid}.json
  TTL: 365 days (SAIPE data is released annually with ~18-month lag)
  # TODO: EDFacts — 365 days (placeholder for future EDFacts integration)
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

from pipeline.utils.fips_utils import get_state_fips

log = logging.getLogger(__name__)

# ── Census API config ─────────────────────────────────────────────────────────

SAIPE_API_BASE = "https://api.census.gov/data/timeseries/poverty/saipe/schdist"

SOURCE_TITLE   = "Census SAIPE School District Poverty Estimates"
SOURCE_URL     = SAIPE_API_BASE

# Variables to request from the API
_GET_VARS = "SAEPOVRAT5_17RV_PT,SAEPOV5_17RV_PT,SAEPOVALL_PT"

# District geography types to attempt, in order
_DISTRICT_TYPES = [
    "school district (unified)",
    "school district (elementary)",
    "school district (secondary)",
]

# ── Cache config ──────────────────────────────────────────────────────────────

_CACHE_DIR    = "data/cache/fetcher/saipe"
# TTL: 365 days — SAIPE is updated annually; annual refresh is sufficient
CACHE_TTL_DAYS = 365


# ── Internal helpers ──────────────────────────────────────────────────────────

def _api_key() -> Optional[str]:
    return os.environ.get("CENSUS_API_KEY") or None


def _cache_path(leaid: str) -> str:
    return os.path.join(_CACHE_DIR, f"{leaid}.json")


def _read_cache(leaid: str) -> Optional[dict]:
    path = _cache_path(leaid)
    if not os.path.exists(path):
        return None
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    if age_days > CACHE_TTL_DAYS:
        log.info("saipe_fetcher: cache expired for LEAID %s (%.0f days old)", leaid, age_days)
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("saipe_fetcher: cache read failed for LEAID %s — %s", leaid, exc)
        return None


def _write_cache(leaid: str, data: dict) -> None:
    path = _cache_path(leaid)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("saipe_fetcher: cache write failed for LEAID %s — %s", leaid, exc)


def _fetch_district_type(
    district_code: str,
    district_type: str,
    year: int,
    api_key: str,
    state_fips: str,
) -> Optional[dict]:
    """
    Query SAIPE for one district type and year.

    Returns a {column: value} dict for the data row, or None on no-match / failure.
    """
    params = {
        "get":    _GET_VARS,
        "for":    f"{district_type}:{district_code}",
        "in":     f"state:{state_fips}",
        "YEAR":   str(year),
        "key":    api_key,
    }
    url = SAIPE_API_BASE + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning(
            "saipe_fetcher: API request failed for district %s type=%s year=%d — %s",
            district_code, district_type, year, exc,
        )
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "saipe_fetcher: JSON parse error for district %s type=%s year=%d — %s",
            district_code, district_type, year, exc,
        )
        return None

    # Expected: [[header, ...], [value, ...]] — fewer than 2 rows means no match
    if not isinstance(data, list) or len(data) < 2:
        return None

    headers, row = data[0], data[1]
    return dict(zip(headers, row))


# ── Public interface ──────────────────────────────────────────────────────────

def get_poverty_data(leaid: str, year: Optional[int] = None, state: str = "NM") -> Optional[dict]:
    """
    Return SAIPE poverty data for a school district, or None if unavailable.

    Tries unified, elementary, and secondary district types in order.
    Tries current_year-1 first, falls back to current_year-2 if no data found.

    Parameters
    ----------
    leaid : 7-digit NCES LEAID string (e.g. "3500060")
    year  : override year; if None, uses most recent available

    Return shape:
        {
            "poverty_rate_pct":     15.2,   # SAEPOVRAT5_17RV_PT, ages 5-17
            "poverty_count_5_17":   12345,  # SAEPOV5_17RV_PT, count
            "total_population":     81234,  # SAEPOVALL_PT
            "leaid":                "3500060",
            "data_year":            2023,
            "source":               "CENSUS_SAIPE",
            "source_url":           "https://...",
            "source_title":         "Census SAIPE School District Poverty Estimates",
            "confidence":           "MODERATE",
        }

    Returns None (does not raise) if:
      - CENSUS_API_KEY not set
      - LEAID is None or malformed
      - No matching district found across all three types and both fallback years
      - API call fails or returns unexpected data
    """
    if not leaid:
        log.info("saipe_fetcher: no LEAID provided — skipping")
        return None

    state_fips = get_state_fips(state)
    if state_fips is None:
        log.warning("saipe_fetcher: unknown state '%s' — cannot derive FIPS", state)
        return None

    api_key = _api_key()
    if not api_key:
        log.warning(
            "saipe_fetcher: CENSUS_API_KEY not set — SAIPE poverty data unavailable"
        )
        return None

    # ── Cache read ────────────────────────────────────────────────────────────
    cached = _read_cache(leaid)
    if cached is not None:
        log.info("saipe_fetcher: cache hit for LEAID %s (year=%s)", leaid, cached.get("data_year"))
        return cached

    # ── Derive district code ──────────────────────────────────────────────────
    leaid_str = str(leaid)
    if len(leaid_str) != 7:
        log.warning("saipe_fetcher: unexpected LEAID length for %s — skipping", leaid_str)
        return None
    district_code = leaid_str[2:]   # "3500060" → "00060"

    # ── Determine years to try ────────────────────────────────────────────────
    current_year = datetime.date.today().year
    years = [year] if year is not None else [current_year - 1, current_year - 2]

    # ── Query: try each year × each district type; return first successful hit ─
    row = None
    matched_year = None
    for try_year in years:
        for district_type in _DISTRICT_TYPES:
            row = _fetch_district_type(district_code, district_type, try_year, api_key, state_fips)
            if row is not None:
                matched_year = try_year
                log.info(
                    "saipe_fetcher: found data for LEAID %s type=%s year=%d",
                    leaid, district_type, try_year,
                )
                break
        if row is not None:
            break

    if row is None:
        log.warning(
            "saipe_fetcher: no SAIPE match for LEAID %s across types %s and years %s",
            leaid, _DISTRICT_TYPES, years,
        )
        return None

    # ── Parse response fields ─────────────────────────────────────────────────
    def _float(key: str) -> Optional[float]:
        val = row.get(key, "")
        if not val or val in ("-", "NA", "N/A", "null", "None"):
            return None
        try:
            return float(val)
        except ValueError:
            return None

    def _int(key: str) -> Optional[int]:
        val = row.get(key, "")
        if not val or val in ("-", "NA", "N/A", "null", "None"):
            return None
        try:
            return int(float(val))
        except ValueError:
            return None

    poverty_rate   = _float("SAEPOVRAT5_17RV_PT")
    poverty_count  = _int("SAEPOV5_17RV_PT")
    total_pop      = _int("SAEPOVALL_PT")

    if poverty_rate is None and poverty_count is None:
        log.warning(
            "saipe_fetcher: no usable poverty fields for LEAID %s year=%s",
            leaid, matched_year,
        )
        return None

    result = {
        "poverty_rate_pct":    round(poverty_rate, 2) if poverty_rate is not None else None,
        "poverty_count_5_17":  poverty_count,
        "total_population":    total_pop,
        "leaid":               leaid,
        "data_year":           matched_year,
        "source":              "CENSUS_SAIPE",
        "source_url":          SOURCE_URL,
        "source_title":        SOURCE_TITLE,
        "confidence":          "MODERATE",
    }

    log.info(
        "saipe_fetcher: LEAID %s year=%d — poverty_rate=%.1f%% count=%s pop=%s",
        leaid, matched_year,
        poverty_rate if poverty_rate is not None else float("nan"),
        poverty_count, total_pop,
    )

    _write_cache(leaid, result)
    return result
