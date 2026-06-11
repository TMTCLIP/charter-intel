"""
pipeline/utils/urban_enrollment_fetcher.py
Multi-year district enrollment trend via the Urban Institute Education Data API.

Fetches grade-99 (district total) enrollment for a single LEAID across the
configured vintage years, returning a structured time series. Year 2021 is
intentionally included here even though it is absent from the NCES membership
parquet CSVs — the Urban API has it and fills the gap.

ENDPOINT:
  GET /api/v1/school-districts/ccd/enrollment/{year}/grade-99/?leaid={leaid}
  Filter: race=99 & sex=99 → district total (grade-99 already aggregates grades)

  NOTE: The base enrollment endpoint (/enrollment/{year}/?leaid=) returns HTTP 404.
  The grade-99 path is required. This was verified by live API probe (2026-06-07).

ERROR CONTRACT: never raises. Individual year fetch failures are skipped; if
fewer than 2 years succeed, returns None.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from pipeline.utils.data_config import URBAN_ENROLLMENT_YEARS

log = logging.getLogger(__name__)

_API = "https://educationdata.urban.org/api/v1"
SOURCE_URL = f"{_API}/school-districts/ccd/enrollment/{{year}}/grade-99/"
SOURCE_TITLE = "Urban Institute Education Data API — NCES CCD district enrollment (grade-99, multi-year)"

_CACHE_DIR = "data/cache/fetcher/urban_enrollment"
_CACHE_TTL_DAYS = 30  # enrollment data is stable; refresh monthly
_TIMEOUT = 10
_MAX_RETRIES = 3


# ── cache helpers ──────────────────────────────────────────────────────────────

def _cache_path(leaid: str) -> str:
    return os.path.join(_CACHE_DIR, f"enrollment_{leaid}.json")


def _read_cache(leaid: str) -> Optional[dict]:
    path = _cache_path(leaid)
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 86400 > _CACHE_TTL_DAYS:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("urban_enrollment_fetcher: cache read failed for %s — %s", leaid, exc)
        return None


def _write_cache(leaid: str, data: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(leaid), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("urban_enrollment_fetcher: cache write failed for %s — %s", leaid, exc)


# ── HTTP helper ────────────────────────────────────────────────────────────────

def _fetch_year(leaid: str, year: int) -> Optional[int]:
    """Return district total enrollment for one year, or None on failure."""
    url = f"{_API}/school-districts/ccd/enrollment/{year}/grade-99/?leaid={leaid}"
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
            for row in data.get("results", []):
                if (
                    str(row.get("leaid")) == str(leaid)
                    and row.get("race") == 99
                    and row.get("sex") == 99
                ):
                    val = row.get("enrollment")
                    if isinstance(val, (int, float)) and val > 0:
                        return int(val)
            return None  # no matching row — not a transient error
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None  # no data for this year
            if attempt == _MAX_RETRIES:
                log.info("urban_enrollment_fetcher: HTTP %d for year %d leaid %s", e.code, year, leaid)
                return None
        except Exception as exc:
            if attempt == _MAX_RETRIES:
                log.info("urban_enrollment_fetcher: failed year %d leaid %s — %s", year, leaid, exc)
                return None
        time.sleep(2.0 ** attempt)
    return None


# ── public interface ──────────────────────────────────────────────────────────

def get_urban_enrollment_trend(leaid: str) -> Optional[dict]:
    """Return multi-year enrollment trend for a district from the Urban API.

    Return shape:
        {
          "enrollment_trend": [{"year": 2019, "enrollment": 4200}, ...],
          "years_fetched": [2019, 2020, 2021, 2022, 2023, 2024],
          "source_url": "...",
          "source_title": "...",
          "confidence": "HIGH",
        }
    Returns None when fewer than 2 years of data are available.
    """
    if not leaid:
        return None
    leaid = str(leaid).strip().zfill(7)

    cached = _read_cache(leaid)
    if cached is not None:
        log.info("urban_enrollment_fetcher: cache hit for leaid %s", leaid)
        return cached

    trend: list[dict] = []
    years_fetched: list[int] = []

    for year in sorted(URBAN_ENROLLMENT_YEARS):
        enrollment = _fetch_year(leaid, year)
        if enrollment is not None:
            trend.append({"year": year, "enrollment": enrollment})
            years_fetched.append(year)

    if len(trend) < 2:
        log.info(
            "urban_enrollment_fetcher: leaid %s — only %d year(s) available, skipping",
            leaid, len(trend),
        )
        return None

    result = {
        "enrollment_trend": trend,
        "years_fetched": years_fetched,
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "HIGH",
    }
    log.info(
        "urban_enrollment_fetcher: leaid %s — %d years fetched (%s–%s)",
        leaid, len(trend), years_fetched[0], years_fetched[-1],
    )
    _write_cache(leaid, result)
    return result
