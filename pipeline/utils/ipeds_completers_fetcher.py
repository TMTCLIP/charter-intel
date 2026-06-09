"""
pipeline/utils/ipeds_completers_fetcher.py
Utility: fetch the count of education-program completers (CIP 13) for a state
from the free, keyless Urban Institute Education Data API (IPEDS).

CIP 13 ("Education") completers are a proxy for the local teacher-credential
pipeline — the supply of newly-trained educators an operator could hire when
replicating a model. It feeds the pipeline sub-signal of replication_feasibility
scoring in S5. Note: this is a STATE-level signal (no per-community geography in
IPEDS), so it is uniform across communities within a state.

DATA SOURCE:
  GET https://educationdata.urban.org/api/v1/college-university/ipeds/
      completions-cip-2/{year}/?fips={state_fips}&cipcode=130000   (no auth)

VERIFIED (live probe, 2026-05-30):
  - cipcode values are 6-digit zero-padded: CIP 13 is "130000", NOT "13".
    Querying cipcode=13 returns zero rows.
  - Rows are broken out by award_level, majornum, race, and sex. Summing every
    row triple-counts. The aggregate ("total") rows are race=99 (all races),
    sex=99 (all sexes), majornum=1 (first major). We sum `awards` over those
    rows across all award_level values.
  - The latest loaded completions year is 2022 (2023/2024 return HTTP 500), so
    the year is discovered by descending from the current year past non-200s.

CACHE:
  Responses cached at data/cache/fetcher/ipeds/{key}.json keyed by state FIPS +
  cipcode. TTL: 90 days.

ERROR CONTRACT:
  - Non-200 / exception on the chosen year's query -> return None.
  - Zero completers is a VALID ZERO result, not "no data".
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# ── API config ──────────────────────────────────────────────────────────────

IPEDS_API_BASE = (
    "https://educationdata.urban.org/api/v1/college-university/ipeds/completions-cip-2"
)
SOURCE_TITLE = "Urban Institute Education Data API — IPEDS completions (CIP 13, Education)"
SOURCE_URL = (
    "https://educationdata.urban.org/api/v1/college-university/ipeds/completions-cip-2/"
)

CIP_EDUCATION = "130000"          # 6-digit padded CIP 13 — see module docstring
_TOTAL_RACE = 99                  # "all races" aggregate row
_TOTAL_SEX = 99                   # "all sexes" aggregate row
_FIRST_MAJOR = 1                  # majornum=1 avoids double-counting second majors

_MAX_YEAR_LOOKBACK = 12
_MAX_PAGES = 50

# ── Cache config ──────────────────────────────────────────────────────────────

_CACHE_DIR = "data/cache/fetcher/ipeds"
CACHE_TTL_DAYS = 90


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.json")


def _read_cache(key: str) -> Optional[dict]:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    age_days = (time.time() - os.path.getmtime(path)) / 86400
    if age_days > CACHE_TTL_DAYS:
        log.info("ipeds_completers_fetcher: cache expired for key %s (%.0f days old)", key, age_days)
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("ipeds_completers_fetcher: cache read failed for key %s — %s", key, exc)
        return None


def _write_cache(key: str, data: dict) -> None:
    path = _cache_path(key)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("ipeds_completers_fetcher: cache write failed for key %s — %s", key, exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("ipeds_completers_fetcher: request failed for %s — %s", url, exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("ipeds_completers_fetcher: JSON parse error for %s — %s", url, exc)
        return None


def _fetch_paged(first_url: str) -> Optional[list[dict]]:
    """Follow the `next` chain. Returns rows, [] if none, None if first GET fails."""
    resp = _get(first_url)
    if resp is None:
        return None
    rows: list[dict] = list(resp.get("results", []))
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


def _query_url(year: int, state_fips: str, cipcode: str) -> str:
    return f"{IPEDS_API_BASE}/{year}/?fips={state_fips}&cipcode={cipcode}"


def _sum_completers(rows: list[dict]) -> int:
    """Sum `awards` over the aggregate (race=99, sex=99, majornum=1) rows only,
    across all award levels. This is the un-double-counted state total."""
    total = 0
    for r in rows:
        if (
            r.get("race") == _TOTAL_RACE
            and r.get("sex") == _TOTAL_SEX
            and r.get("majornum") == _FIRST_MAJOR
        ):
            awards = r.get("awards")
            if isinstance(awards, (int, float)):
                total += int(awards)
    return total


# ── Public interface ──────────────────────────────────────────────────────────

def get_completers(state_fips: str, cipcode: str = CIP_EDUCATION) -> Optional[dict]:
    """Return CIP-13 (education) completer count for a state, or None.

    Discovers the latest loaded completions year by descending from the current
    year past non-200 responses, then sums awards over the aggregate rows.

    Return shape:
        {
            "completers": 2351,
            "cipcode": "130000",
            "cip_label": "Education (CIP 13)",
            "data_year": 2022,
            "state_fips": "<caller-supplied>",   # e.g. "35" NM, "28" MS, "47" TN, "55" WI
            "source": "IPEDS",
            "source_url": ..., "source_title": ...,
            "confidence": "MODERATE",
        }

    Returns None on HTTP/parse failure for every candidate year. A genuine zero
    completer count is returned as completers=0 (a VALID value).
    """
    if not state_fips:
        return None

    cache_key = f"completers_{state_fips}_cip{cipcode}"
    cached = _read_cache(cache_key)
    if cached is not None:
        log.info("ipeds_completers_fetcher: cache hit for %s", cache_key)
        return cached

    current_year = datetime.date.today().year
    floor = current_year - _MAX_YEAR_LOOKBACK

    chosen_year: Optional[int] = None
    chosen_rows: Optional[list[dict]] = None
    empty_year: Optional[int] = None  # first 200 year that returned no rows

    year = current_year
    while year > floor:
        rows = _fetch_paged(_query_url(year, state_fips, cipcode))
        if rows is None:
            year -= 1
            continue  # year not loaded (e.g. HTTP 500) — descend
        if rows:
            chosen_year, chosen_rows = year, rows
            break
        if empty_year is None:
            empty_year = year  # remember as a valid-zero fallback
        year -= 1

    if chosen_rows is None:
        if empty_year is None:
            log.warning("ipeds_completers_fetcher: no available year for fips %s", state_fips)
            return None
        chosen_year, chosen_rows = empty_year, []

    completers = _sum_completers(chosen_rows)
    result = {
        "completers": completers,
        "cipcode": cipcode,
        "cip_label": "Education (CIP 13)",
        "data_year": chosen_year,
        "state_fips": str(state_fips),
        "source": "IPEDS",
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "MODERATE",
    }
    log.info(
        "ipeds_completers_fetcher: fips %s — %d CIP-13 completers (year %s)",
        state_fips, completers, chosen_year,
    )
    _write_cache(cache_key, result)
    return result
