"""
pipeline/fetchers/bls_teacher_wages_fetcher.py
Teacher labor-market signal: BLS OEWS annual median wages for elementary and
secondary school teachers, by state (with optional MSA), from the free BLS
Public Data API.

SERIES ID (verified live 2026-06-07):
  Format  OE U {areatype} {area_code:7} {industry:6} {occupation:6} {datatype:2}
    OE          OEWS prefix
    U           seasonal-adjustment code (always U for OEWS)
    areatype    'S' = statewide, 'M' = metropolitan area
    area_code   7 chars. State = 2-digit FIPS + '00000' (MS -> '2800000').
                MSA = the OEWS 7-digit metro area code (caller-supplied).
    industry    '000000' = cross-industry (all ownerships)
    occupation  '252021' = elementary teachers; '252031' = secondary teachers
    datatype    '11' = annual MEDIAN wage; '13' = annual MEAN wage
  Confirmed: OEUS280000000000025202111 -> MS elementary median $46,020 (2025);
             OEUS280000000000025203111 -> MS secondary  median $45,760 (2025).

API: POST https://api.bls.gov/publicAPI/v2/timeseries/data/  (JSON body).
  No key required, but unregistered access is limited to ~25 requests/day. Set the
  BLS_API_KEY env var to send a registrationkey and raise the limit to 500/day.

SCORING NOTE (2026-06-07): there is no teacher-wage fact key or teacher_supply
dimension in scoring_weights.yaml, and operational_complexity has no
secondary_facts, so these wages are NOT consumed by any S5 scored value today.
They feed the S6 brief narrative. Wiring into scoring needs operator sign-off.

ERROR CONTRACT: never raises; returns None when BLS yields no data for the
requested geography.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SOURCE_URL = "https://www.bls.gov/oes/"
SOURCE_TITLE = "BLS Occupational Employment and Wage Statistics (OEWS)"

_OCC_ELEMENTARY = "252021"
_OCC_SECONDARY = "252031"
_DATATYPE_MEDIAN = "11"
_INDUSTRY_ALL = "000000"

_CACHE_DIR = "data/cache/fetcher/bls_teacher_wages"
CACHE_TTL_DAYS = 180  # OEWS publishes annually


def _series_id(area_type: str, area_code: str, occupation: str, datatype: str = _DATATYPE_MEDIAN) -> str:
    return f"OEU{area_type}{area_code}{_INDUSTRY_ALL}{occupation}{datatype}"


def _state_area_code(state_fips: str) -> str:
    return f"{str(state_fips).zfill(2)}00000"


# ── cache ────────────────────────────────────────────────────────────────────────

def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.json")


def _read_cache(key: str) -> Optional[dict]:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 86400 > CACHE_TTL_DAYS:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("bls_teacher_wages_fetcher: cache read failed for %s — %s", key, exc)
        return None


def _write_cache(key: str, data: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(key), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("bls_teacher_wages_fetcher: cache write failed for %s — %s", key, exc)


# ── BLS request ────────────────────────────────────────────────────────────────────

def _post_series(series_ids: list) -> dict:
    """POST series to BLS; return {series_id: latest_value_dict}. {} on failure."""
    body = {"seriesid": series_ids}
    api_key = os.environ.get("BLS_API_KEY")
    if api_key:
        body["registrationkey"] = api_key
    req = urllib.request.Request(
        _BLS_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "CLIP/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("bls_teacher_wages_fetcher: request failed — %s", exc)
        return {}
    if payload.get("status") != "REQUEST_SUCCEEDED":
        log.warning("bls_teacher_wages_fetcher: BLS status %s — %s",
                    payload.get("status"), payload.get("message"))
        return {}
    out = {}
    for s in payload.get("Results", {}).get("series", []):
        data = s.get("data", [])
        if data:
            out[s.get("seriesID")] = data[0]  # most recent datapoint
    return out


def _wage(latest: Optional[dict]) -> Optional[int]:
    if not latest:
        return None
    try:
        return int(round(float(latest.get("value"))))
    except (TypeError, ValueError):
        return None


def _fetch_for_area(area_type: str, area_code: str) -> Optional[dict]:
    elem_sid = _series_id(area_type, area_code, _OCC_ELEMENTARY)
    sec_sid = _series_id(area_type, area_code, _OCC_SECONDARY)
    res = _post_series([elem_sid, sec_sid])
    elem = _wage(res.get(elem_sid))
    sec = _wage(res.get(sec_sid))
    if elem is None and sec is None:
        return None
    # Wage year: take from whichever series returned data.
    yr = None
    for sid in (elem_sid, sec_sid):
        if res.get(sid) and res[sid].get("year"):
            yr = int(res[sid]["year"])
            break
    return {
        "elementary_teacher_median_wage": elem,
        "secondary_teacher_median_wage": sec,
        "wage_year": yr,
        "geography_level": "msa" if area_type == "M" else "state",
        "area_code": area_code,
        "series_ids": [elem_sid, sec_sid],
    }


# ── public interface ────────────────────────────────────────────────────────────────

def get_teacher_wages(state_fips: str, msa_code: Optional[str] = None) -> Optional[dict]:
    """Return annual median wages for elementary + secondary teachers, or None.

    Tries MSA level first when msa_code is given, falling back to state level.
    Return shape:
        {
          "elementary_teacher_median_wage": 46020,
          "secondary_teacher_median_wage": 45760,
          "wage_year": 2025, "geography_level": "state", "area_code": "2800000",
          "series_ids": [...], "source_url": ..., "source_title": ...,
          "confidence": "HIGH",
        }
    """
    if not state_fips:
        return None

    cache_key = f"wages_{state_fips}_{msa_code or 'state'}"
    cached = _read_cache(cache_key)
    if cached is not None:
        log.info("bls_teacher_wages_fetcher: cache hit for %s", cache_key)
        return cached

    result = None
    if msa_code:
        result = _fetch_for_area("M", str(msa_code))
        if result is None:
            log.info("bls_teacher_wages_fetcher: no MSA data for %s — falling back to state", msa_code)
    if result is None:
        result = _fetch_for_area("S", _state_area_code(state_fips))
    if result is None:
        log.info("bls_teacher_wages_fetcher: no BLS wage data for state %s", state_fips)
        return None

    result.update({
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "HIGH",
    })
    log.info(
        "bls_teacher_wages_fetcher: state %s (%s) — elem=%s sec=%s year=%s",
        state_fips, result["geography_level"],
        result["elementary_teacher_median_wage"], result["secondary_teacher_median_wage"],
        result["wage_year"],
    )
    _write_cache(cache_key, result)
    return result
