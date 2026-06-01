"""
pipeline/edfacts_teacher_fetcher.py
Teacher-supply signal for a school district from the free, keyless Urban
Institute Education Data API.

DESIGN / STEP-0 FINDING:
  The originally-specified EDFacts staff endpoint
    https://educationdata.urban.org/api/v1/districts/edfacts/staff/{year}/
  returns HTTP 404 (it is not exposed by the API), and no
  teachers_uncertified_pct / vacancy field is available anywhere in the API.
  Per the documented fallback priority ("total_teachers vs enrollment ratio"),
  this fetcher derives a STUDENT-TEACHER RATIO from the CCD school directory
  endpoint, which exposes per-school `teachers_fte` and `enrollment`:
    https://educationdata.urban.org/api/v1/schools/ccd/directory/{year}/?leaid=
  A higher ratio = fewer teachers per student = greater supply pressure.

  The fetcher still attempts the EDFacts endpoint first (so it self-upgrades if
  the API ever exposes it), then falls back to the ratio.

SIGNAL THRESHOLDS (fetcher-internal classification — NOT an S5 scoring rule):
    ratio >= 18         -> HIGH_SHORTAGE
    14 <= ratio < 18    -> MODERATE
    ratio < 14          -> LOW_SHORTAGE

CACHE:  data/cache/community/{state}/{cid}/edfacts_teacher.json   (TTL 90 days)
ERROR CONTRACT:  never raises; returns None on any failure / no data.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# ── API config ────────────────────────────────────────────────────────────────
_EDFACTS_STAFF_API = "https://educationdata.urban.org/api/v1/districts/edfacts/staff"
_CCD_DIRECTORY_API = "https://educationdata.urban.org/api/v1/schools/ccd/directory"
SOURCE_TITLE = "Urban Institute Education Data API — NCES CCD school directory (teachers_fte)"

_YEARS = (2022, 2021)           # try 2022 first, then prior year
_MAX_PAGES = 30
CACHE_TTL_DAYS = 90

# Student-teacher ratio classification thresholds.
_RATIO_HIGH = 18.0
_RATIO_MODERATE = 14.0


def _cache_path(state: str, community_id: str) -> str:
    return os.path.join(
        "data", "cache", "community", state.lower(), community_id, "edfacts_teacher.json"
    )


def _read_cache(state: str, community_id: str) -> Optional[dict]:
    path = _cache_path(state, community_id)
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 86400 > CACHE_TTL_DAYS:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(state: str, community_id: str, data: dict) -> None:
    path = _cache_path(state, community_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("edfacts_teacher_fetcher: cache write failed (%s) — %s", path, exc)


def _get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("edfacts_teacher_fetcher: request failed for %s — %s", url, exc)
        return None


def _fetch_paged(first_url: str) -> Optional[list]:
    first = _get(first_url)
    if first is None:
        return None
    rows = list(first.get("results", []))
    nxt = first.get("next")
    pages = 1
    while nxt and pages < _MAX_PAGES:
        page = _get(nxt)
        if page is None:
            break
        rows.extend(page.get("results", []))
        nxt = page.get("next")
        pages += 1
    return rows


def _pos_num(val) -> float:
    try:
        n = float(val)
        return n if n > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _classify(ratio: float) -> str:
    if ratio >= _RATIO_HIGH:
        return "HIGH_SHORTAGE"
    if ratio >= _RATIO_MODERATE:
        return "MODERATE"
    return "LOW_SHORTAGE"


def _try_edfacts_staff(leaid: str, year: int) -> Optional[dict]:
    """Attempt the (currently 404) EDFacts staff endpoint. Returns a result dict
    only if a usable qualification/vacancy field is present; else None."""
    resp = _get(f"{_EDFACTS_STAFF_API}/{year}/?leaid={leaid}")
    if not resp or not resp.get("results"):
        return None
    row = resp["results"][0]
    for field in ("teachers_uncertified_pct", "teachers_uncertified",
                  "teacher_vacancy_pct", "teachers_inexperienced_pct"):
        if row.get(field) not in (None, ""):
            raw = _pos_num(row.get(field))
            # A higher uncertified/vacancy % means more shortage.
            signal = "HIGH_SHORTAGE" if raw >= 10 else "MODERATE" if raw >= 4 else "LOW_SHORTAGE"
            return {
                "teacher_signal": signal,
                "raw_value": raw,
                "field_used": field,
                "year": year,
                "source_url": f"{_EDFACTS_STAFF_API}/{year}/?leaid={leaid}",
            }
    return None


def _try_ccd_ratio(leaid: str, year: int) -> Optional[dict]:
    """Derive a student-teacher ratio from the CCD directory endpoint."""
    rows = _fetch_paged(f"{_CCD_DIRECTORY_API}/{year}/?leaid={leaid}")
    if not rows:
        return None
    enrollment = sum(_pos_num(r.get("enrollment")) for r in rows)
    teachers = sum(_pos_num(r.get("teachers_fte")) for r in rows)
    if enrollment <= 0 or teachers <= 0:
        return None
    ratio = round(enrollment / teachers, 1)
    return {
        "teacher_signal": _classify(ratio),
        "raw_value": ratio,
        "field_used": "student_teacher_ratio",
        "year": year,
        "source_url": f"{_CCD_DIRECTORY_API}/{year}/?leaid={leaid}",
    }


def get_teacher_supply(
    leaid: Optional[str],
    community_id: str,
    state: str,
    state_fips: Optional[str] = None,
    year: Optional[int] = None,
) -> Optional[dict]:
    """Return a teacher-supply signal for a district, or None.

    Parameters
    ----------
    leaid        : 7-digit NCES district id. Required (None -> None result).
    community_id : community slug, used in the cache path.
    state        : two-letter state code, used in the cache path.
    state_fips   : optional 2-digit FIPS (unused by the working CCD path; kept
                   for interface compatibility with the spec).
    year         : optional explicit year; otherwise tries _YEARS in order.

    Return shape:
        {
            "leaid": "3500060",
            "year": 2022,
            "teacher_signal": "LOW_SHORTAGE",
            "raw_value": 13.9,
            "field_used": "student_teacher_ratio",
            "source_url": "...",
            "source_title": "...",
        }
    """
    if not leaid:
        return None

    cached = _read_cache(state, community_id)
    if cached is not None:
        log.info("edfacts_teacher_fetcher: cache hit for %s", community_id)
        return cached

    years = (year,) if year else _YEARS
    for yr in years:
        # Priority 1: the spec's EDFacts staff endpoint (self-upgrades if exposed).
        found = _try_edfacts_staff(leaid, yr)
        # Priority 2: CCD directory student-teacher ratio (current working path).
        if found is None:
            found = _try_ccd_ratio(leaid, yr)
        if found is None:
            continue

        result = {
            "leaid": str(leaid),
            "year": found["year"],
            "teacher_signal": found["teacher_signal"],
            "raw_value": found["raw_value"],
            "field_used": found["field_used"],
            "source_url": found["source_url"],
            "source_title": SOURCE_TITLE,
        }
        log.info(
            "edfacts_teacher_fetcher: %s (LEAID %s, %d) — %s via %s = %s",
            community_id, leaid, result["year"], result["teacher_signal"],
            result["field_used"], result["raw_value"],
        )
        _write_cache(state, community_id, result)
        return result

    log.warning("edfacts_teacher_fetcher: no teacher data for LEAID %s", leaid)
    return None
