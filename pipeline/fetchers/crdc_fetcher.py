"""
pipeline/fetchers/crdc_fetcher.py
District-level IEP% and chronic-absenteeism rate from the free, keyless Urban
Institute Education Data API.

WHY CRDC (not EDFacts): the originally-scoped EDFacts district endpoints for
special-education / chronic-absenteeism DO NOT EXIST (verified 2026-06-07 against
Urban's /api/v1/api-endpoints/ metadata — EDFacts school-district topics are only
`assessments` and `grad-rates`). The real sources are the Civil Rights Data
Collection (CRDC), which is SCHOOL-level and published only for 2011, 2013, 2015,
2017, 2020, 2021/22. We therefore aggregate CRDC school rows to the district by
leaid and divide by the matching-year CCD district enrollment to produce rates.

VINTAGE NOTE (surfaced into the injected datapoint): CRDC is collected every ~2
years and lags; the latest available year for a given district is typically
2017–2020, NOT the current year. Treat these as structural baselines, not
current-year figures.

DATA SOURCES (no auth):
  Chronic absenteeism (school):  /api/v1/schools/crdc/chronic-absenteeism/{year}/race/sex/?leaid=
    → per-school row with race=99 & sex=99 is the school grand total
      (field: students_chronically_absent). Summed across the district's schools.
  IEP / disability (school):     /api/v1/schools/crdc/enrollment/{year}/disability/sex/?leaid=
    → disability code 1 = IDEA (the IEP population), 2 = Section 504 only,
      99 = total. We take disability=1 & sex=99 (field: enrollment_crdc), summed.
  Denominator (district):        /api/v1/school-districts/ccd/enrollment/{year}/grade-99/?leaid=
    → grade=99, race=99, sex=99 row (field: enrollment) = district total enrollment.

SCORING NOTE: as of 2026-06-07 neither iep_pct nor chronic_absenteeism_pct is
consumed by any S5 scored dimension (operational_complexity scores only from the
LLM-derived operational_complexity_index; it has no secondary_facts). These facts
feed the S6 brief narrative. Wiring them into scoring requires operator sign-off.

ERROR CONTRACT: never raises. Each signal degrades to None independently when no
CRDC year has data or the CCD denominator is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_API = "https://educationdata.urban.org/api/v1"
SOURCE_URL = "https://educationdata.urban.org/"
SOURCE_TITLE = "Urban Institute Education Data API — CRDC (school) + CCD enrollment"

# CRDC collection years, newest first. We probe descending and take the first
# year that yields data for the district.
_CRDC_YEARS = (2021, 2020, 2017, 2015, 2013)

_CACHE_DIR = "data/cache/fetcher/crdc"
CACHE_TTL_DAYS = 90
_MAX_PAGES = 50

VINTAGE_CAVEAT = (
    "CRDC is collected biennially and lags; this reflects the most recent available "
    "collection year (typically 2017–2020), not the current school year."
)


# ── cache (mirrors ccd_closures_fetcher) ────────────────────────────────────────

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
        log.warning("crdc_fetcher: cache read failed for %s — %s", key, exc)
        return None


def _write_cache(key: str, data: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(key), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("crdc_fetcher: cache write failed for %s — %s", key, exc)


# ── http ────────────────────────────────────────────────────────────────────────

def _get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.info("crdc_fetcher: request failed for %s — %s", url, exc)
        return None


def _fetch_all_rows(first_url: str) -> Optional[list]:
    """Follow the `next` chain. Returns rows, [] if none, or None if the first
    request fails entirely."""
    resp = _get(first_url)
    if resp is None:
        return None
    rows = list(resp.get("results", []))
    nxt = resp.get("next")
    pages = 1
    while nxt and pages < _MAX_PAGES:
        resp = _get(nxt)
        if resp is None:
            break
        rows.extend(resp.get("results", []))
        nxt = resp.get("next")
        pages += 1
    return rows


# ── per-signal aggregation ───────────────────────────────────────────────────────

def _ccd_district_enrollment(leaid: str, year: int) -> Optional[int]:
    """District total enrollment (grade 99, race 99, sex 99) for the denominator."""
    rows = _fetch_all_rows(f"{_API}/school-districts/ccd/enrollment/{year}/grade-99/?leaid={leaid}")
    if not rows:
        return None
    for r in rows:
        if str(r.get("leaid")) == str(leaid) and r.get("race") == 99 and r.get("sex") == 99:
            val = r.get("enrollment")
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
    return None


def _sum_by_school(rows: list, value_field: str, keep) -> Optional[int]:
    """Sum a value_field across schools, ONE row per ncessch.

    The Urban CRDC tables emit duplicate per-school rows in some years (verified:
    2021 repeats each school's disability=1/sex=99 row twice). Deduplicating by
    ncessch before summing prevents the resulting district total from doubling.
    """
    by_school: dict = {}
    for r in rows:
        if not keep(r):
            continue
        v = r.get(value_field)
        if isinstance(v, (int, float)) and v >= 0:
            by_school[r.get("ncessch")] = int(v)  # identical dup rows collapse to one
    if not by_school:
        return None
    return sum(by_school.values())


def _chronic_absentee_count(leaid: str, year: int) -> Optional[int]:
    """District chronically-absent students (per-school race=99 & sex=99 totals)."""
    rows = _fetch_all_rows(f"{_API}/schools/crdc/chronic-absenteeism/{year}/race/sex/?leaid={leaid}")
    if not rows:
        return None
    return _sum_by_school(
        rows, "students_chronically_absent",
        lambda r: r.get("race") == 99 and r.get("sex") == 99,
    )


def _idea_count(leaid: str, year: int) -> Optional[int]:
    """District IDEA-served (IEP) enrollment (disability=1 & sex=99 per school)."""
    rows = _fetch_all_rows(f"{_API}/schools/crdc/enrollment/{year}/disability/sex/?leaid={leaid}")
    if not rows:
        return None
    return _sum_by_school(
        rows, "enrollment_crdc",
        lambda r: r.get("disability") == 1 and r.get("sex") == 99,
    )


def _rate(numerator: Optional[int], denom: Optional[int]) -> Optional[float]:
    if numerator is None or not denom:
        return None
    return round(numerator / denom * 100, 1)


# ── public interface ──────────────────────────────────────────────────────────────

def get_crdc_district_data(leaid: str) -> Optional[dict]:
    """Return district IEP% and chronic-absenteeism rate from CRDC+CCD, or None.

    Return shape (each signal independently None when unavailable):
        {
          "iep_pct": 9.3, "iep_year": 2017, "iep_count": 422, "iep_denominator": 4528,
          "chronic_absenteeism_pct": 1.9, "chronic_absenteeism_year": 2020,
          "chronic_absent_count": 84, "chronic_absent_denominator": 4528,
          "leaid": "2803450", "source_url": ..., "source_title": ...,
          "data_note": VINTAGE_CAVEAT, "confidence": "MODERATE",
        }
    Returns None only when BOTH signals are unavailable.
    """
    if not leaid:
        return None
    leaid = str(leaid).strip()

    cache_key = f"crdc_{leaid}"
    cached = _read_cache(cache_key)
    if cached is not None:
        log.info("crdc_fetcher: cache hit for %s", cache_key)
        return cached

    iep_pct = iep_year = iep_count = iep_denom = None
    ca_pct = ca_year = ca_count = ca_denom = None

    # IEP%: first CRDC year with IDEA data + a CCD denominator.
    for yr in _CRDC_YEARS:
        idea = _idea_count(leaid, yr)
        if idea is None:
            continue
        denom = _ccd_district_enrollment(leaid, yr)
        if denom:
            iep_pct, iep_year, iep_count, iep_denom = _rate(idea, denom), yr, idea, denom
            break

    # Chronic absenteeism: first CRDC year with data + a CCD denominator.
    for yr in _CRDC_YEARS:
        ca = _chronic_absentee_count(leaid, yr)
        if ca is None:
            continue
        denom = _ccd_district_enrollment(leaid, yr)
        if denom:
            ca_pct, ca_year, ca_count, ca_denom = _rate(ca, denom), yr, ca, denom
            break

    if iep_pct is None and ca_pct is None:
        log.info("crdc_fetcher: no CRDC IEP or absenteeism data for leaid %s", leaid)
        return None

    result = {
        "iep_pct": iep_pct,
        "iep_year": iep_year,
        "iep_count": iep_count,
        "iep_denominator": iep_denom,
        "chronic_absenteeism_pct": ca_pct,
        "chronic_absenteeism_year": ca_year,
        "chronic_absent_count": ca_count,
        "chronic_absent_denominator": ca_denom,
        "leaid": leaid,
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "data_note": VINTAGE_CAVEAT,
        "confidence": "MODERATE",
    }
    log.info(
        "crdc_fetcher: leaid %s — IEP%%=%s (%s) chronic_absent%%=%s (%s)",
        leaid, iep_pct, iep_year, ca_pct, ca_year,
    )
    _write_cache(cache_key, result)
    return result
