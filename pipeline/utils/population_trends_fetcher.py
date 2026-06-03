"""
pipeline/utils/population_trends_fetcher.py
Utility: load NCES LEA membership CSVs to compute district enrollment trends.

Reads four pre-downloaded national membership files and returns a structured
trend summary for a single district identified by LEAID. Returns None
gracefully if mapping is absent or data is insufficient — never raises.

DATA SOURCES (school-year ending dates):
  nces_lea_membership_2020.csv  — SY2019-2020  (school-level rows, sum to district)
  nces_lea_membership_2022.csv  — SY2021-2022  (school-level rows, sum to district)
  nces_lea_membership_2023.csv  — SY2022-2023  (school-level rows, sum to district)
  nces_lea_membership_2024.csv  — SY2024-2025  (district-level row, single row)

NOTE: 2021 is intentionally absent from the file set.

FILTER CRITERIA (confirmed against all four files):
  TOTAL_INDICATOR == "Education Unit Total"
  DMS_FLAG        == "Reported"
  LEAID           == district LEAID from config/states.yaml

SUM: STUDENT_COUNT across all matching rows per file.
  2020-2023 files: one row per school in the district → sum = district total
  2024 file:       one pre-aggregated row per district → sum = same value

ENCODING: latin-1 for all files (national NCES files use this encoding).
"""
from __future__ import annotations

import csv
import logging
import os
import subprocess
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────

STATES_YAML = "config/states.yaml"

# (year_key, file_path) — year_key is the label used in enrollment_by_year dict.
# Files are named by data-collection year; 2021 is absent by design.
# Exported as NCES_SOURCE_FILES so build_nces_cache.py can import them without
# duplicating paths. These are the national files — build_nces_cache.py filters
# to a specific state and writes data/processed/{state}/nces_membership_{state}.parquet.
NCES_SOURCE_FILES: list[tuple[int, str]] = [
    (2020, "data/raw/nm/nces_lea_membership_2020.csv"),
    (2022, "data/raw/nm/nces_lea_membership_2022.csv"),
    (2023, "data/raw/nm/nces_lea_membership_2023.csv"),
    (2024, "data/raw/nm/nces_lea_membership_2024.csv"),
]
_MEMBERSHIP_FILES = NCES_SOURCE_FILES  # internal alias kept for _SCHOOL_MEMBERSHIP_FILES below

SOURCE_URL   = "https://nces.ed.gov/ccd/files.asp"
SOURCE_TITLE = "NCES CCD LEA Membership Data"

# School-level files only — 2024 file is district-level (no NCESSCH column)
_SCHOOL_MEMBERSHIP_FILES: list[tuple[int, str]] = [
    (2020, "data/raw/nm/nces_lea_membership_2020.csv"),
    (2022, "data/raw/nm/nces_lea_membership_2022.csv"),
    (2023, "data/raw/nm/nces_lea_membership_2023.csv"),
]

# Trend thresholds: pct_change outside ±3% → growing/declining; within → stable
_GROWING_THRESHOLD  =  3.0
_DECLINING_THRESHOLD = -3.0


# ── Parquet cache helpers ─────────────────────────────────────────────────────

def _parquet_path(state_lower: str) -> str:
    return f"data/processed/{state_lower}/nces_membership_{state_lower}.parquet"


def _ensure_parquet(state: str) -> None:
    """Build the state parquet cache if it does not exist.

    Calls scripts/build_nces_cache.py as a subprocess so the build logic and
    its pandas dependency stay out of the module-level import graph.
    Blocks until the build completes. Does not raise — if the build fails,
    the subsequent pd.read_parquet call will fail with a clear FileNotFoundError.
    """
    state_lower = state.lower()
    path = _parquet_path(state_lower)
    if os.path.exists(path):
        return
    log.info(
        "nces_cache: not found for %s — building now (one-time, ~2 min)", state.upper()
    )
    try:
        subprocess.run(
            ["python3", "scripts/build_nces_cache.py", state.upper()],
            check=True,
        )
        log.info(
            "nces_cache: built and cached for %s — future runs will be instant",
            state.upper(),
        )
    except subprocess.CalledProcessError as exc:
        log.error(
            "nces_cache: build failed for %s (exit %d) — population_trends unavailable",
            state.upper(), exc.returncode,
        )


def _enrollment_from_parquet(leaid: str, df: "pd.DataFrame") -> dict[int, int]:  # type: ignore[name-defined]
    """Return {year: enrollment_count} from the pre-filtered state parquet dataframe.

    Applies the same filter criteria as the CSV reader:
      TOTAL_INDICATOR == "Education Unit Total"
      DMS_FLAG        == "Reported"
      LEAID           == leaid
    Then sums STUDENT_COUNT per year.
    """
    import pandas as pd

    mask = (
        (df["LEAID"].str.strip() == leaid)
        & (df["TOTAL_INDICATOR"].str.strip() == "Education Unit Total")
        & (df["DMS_FLAG"].str.strip() == "Reported")
    )
    filtered = df[mask].copy()
    if filtered.empty:
        return {}

    filtered["_count"] = pd.to_numeric(filtered["STUDENT_COUNT"], errors="coerce")
    valid = filtered.dropna(subset=["_count"])

    result: dict[int, int] = {}
    for year_str, group in valid.groupby("_year"):
        total = int(group["_count"].sum())
        if total > 0:
            result[int(year_str)] = total

    return result


# ── Config loader ─────────────────────────────────────────────────────────────

def _load_nces_map(state: str) -> dict:
    """Return the nces_district_map for the given state from states.yaml."""
    try:
        with open(STATES_YAML) as f:
            config = yaml.safe_load(f)
        return config.get(state.upper(), {}).get("nces_district_map", {})
    except Exception as exc:
        log.warning("population_trends_fetcher: could not load states.yaml — %s", exc)
        return {}


# ── Per-file enrollment reader ────────────────────────────────────────────────

def _read_enrollment(leaid: str, file_path: str) -> Optional[int]:
    """
    Sum STUDENT_COUNT for all rows matching the LEAID and filter criteria.

    Returns the summed enrollment count, or None if:
      - file does not exist
      - no matching rows found
      - all matching rows have blank/non-numeric STUDENT_COUNT
    """
    if not os.path.exists(file_path):
        log.warning("population_trends_fetcher: file not found — %s", file_path)
        return None

    total = 0
    found_any = False

    try:
        with open(file_path, newline="", encoding="latin-1") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("LEAID", "").strip() != leaid:
                    continue
                if row.get("TOTAL_INDICATOR", "").strip() != "Education Unit Total":
                    continue
                if row.get("DMS_FLAG", "").strip() != "Reported":
                    continue

                raw = row.get("STUDENT_COUNT", "").strip()
                if not raw:
                    continue
                try:
                    count = int(raw)
                except ValueError:
                    continue

                total += count
                found_any = True

    except Exception as exc:
        log.warning(
            "population_trends_fetcher: error reading %s — %s", file_path, exc
        )
        return None

    if not found_any:
        return None

    return total


# ── Trend classifier ──────────────────────────────────────────────────────────

def _classify_trend(pct_change: float) -> str:
    """Return 'growing', 'declining', or 'stable' based on pct_change."""
    if pct_change > _GROWING_THRESHOLD:
        return "growing"
    if pct_change < _DECLINING_THRESHOLD:
        return "declining"
    return "stable"


# ── Public interface ──────────────────────────────────────────────────────────

def get_population_trends(community_id: str, state: str) -> Optional[dict]:
    """
    Return enrollment trend data for a community, or None if unavailable.

    Return shape:
        {
            "enrollment_by_year":  {2020: 5412, 2022: 5389, 2023: 5301, 2024: 5280},
            "trend_direction":     "declining",
            "pct_change_total":    -2.4,
            "years_available":     [2020, 2022, 2023, 2024],
            "data_year":           "2020–2024",
            "source_title":        "NCES CCD LEA Membership Data",
            "source_url":          "https://nces.ed.gov/ccd/files.asp",
            "confidence":          "HIGH",
        }

    Returns None (does not raise) if:
      - community_id has no entry in nces_district_map
      - community_id maps to null
      - fewer than 2 years of data are available
    """
    nces_map = _load_nces_map(state)
    leaid = nces_map.get(community_id)

    if leaid is None:
        log.info(
            "population_trends_fetcher: no NCES mapping for %s in %s — skipping",
            community_id, state,
        )
        return None

    # ── Parquet fast path — build on first run if absent ─────────────────────
    state_lower = state.lower()
    pq_path = _parquet_path(state_lower)
    _ensure_parquet(state)

    if not os.path.exists(pq_path):
        log.warning(
            "population_trends_fetcher: parquet unavailable for %s — skipping trend data",
            state.upper(),
        )
        return None

    import pandas as pd
    log.info("nces_cache: reading from filtered parquet (%s)", state.upper())
    df = pd.read_parquet(pq_path)

    # ── Collect enrollment by year ────────────────────────────────────────────
    enrollment_by_year: dict[int, int] = _enrollment_from_parquet(leaid, df)

    years_available = sorted(enrollment_by_year.keys())

    if len(years_available) < 2:
        log.warning(
            "population_trends_fetcher: fewer than 2 years of data for %s (%d available) — skipping",
            community_id, len(years_available),
        )
        return None

    # ── Compute trend ─────────────────────────────────────────────────────────
    earliest_year = years_available[0]
    latest_year   = years_available[-1]
    earliest_count = enrollment_by_year[earliest_year]
    latest_count   = enrollment_by_year[latest_year]

    if earliest_count == 0:
        log.warning(
            "population_trends_fetcher: earliest enrollment is 0 for %s — cannot compute trend",
            community_id,
        )
        return None

    pct_change = round((latest_count - earliest_count) / earliest_count * 100, 1)
    trend_direction = _classify_trend(pct_change)
    data_year = f"{earliest_year}–{latest_year}"  # en-dash

    log.info(
        "population_trends_fetcher: %s (LEAID %s) — %s to %s: %d → %d (%.1f%% %s)",
        community_id, leaid,
        earliest_year, latest_year,
        earliest_count, latest_count,
        pct_change, trend_direction,
    )

    return {
        "enrollment_by_year": enrollment_by_year,
        "trend_direction":    trend_direction,
        "pct_change_total":   pct_change,
        "years_available":    years_available,
        "data_year":          data_year,
        "source_title":       SOURCE_TITLE,
        "source_url":         SOURCE_URL,
        "confidence":         "HIGH",
    }


# ── School-level enrollment helpers ──────────────────────────────────────────

def _read_school_enrollment_by_file(
    leaid: str, file_path: str
) -> dict[str, dict]:
    """
    Read school-level enrollment totals from one membership file.

    Filters to rows where LEAID matches, TOTAL_INDICATOR == "Education Unit Total",
    and DMS_FLAG == "Reported". Groups by NCESSCH and sums STUDENT_COUNT.

    Returns {ncessch: {"school_name": str, "count": int}}.
    Empty dict if the file is missing or no matching rows exist.
    """
    if not os.path.exists(file_path):
        log.warning("population_trends_fetcher: file not found — %s", file_path)
        return {}

    schools: dict[str, dict] = {}

    try:
        with open(file_path, newline="", encoding="latin-1") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("LEAID", "").strip() != leaid:
                    continue
                if row.get("TOTAL_INDICATOR", "").strip() != "Education Unit Total":
                    continue
                if row.get("DMS_FLAG", "").strip() != "Reported":
                    continue

                ncessch = row.get("NCESSCH", "").strip()
                if not ncessch:
                    continue  # 2024-style district-level rows have no NCESSCH

                raw = row.get("STUDENT_COUNT", "").strip()
                if not raw:
                    continue
                try:
                    count = int(raw)
                except ValueError:
                    continue

                if ncessch not in schools:
                    schools[ncessch] = {
                        "school_name": row.get("SCH_NAME", "").strip(),
                        "count": 0,
                    }
                schools[ncessch]["count"] += count

    except Exception as exc:
        log.warning(
            "population_trends_fetcher: error reading %s — %s", file_path, exc
        )
        return {}

    return schools


# NOTE (2026-06-03): The raw CSV source files read by this function
# (nces_lea_membership_2020/2022/2023.csv) were deleted from data/raw/nm/ to
# reclaim ~7.2 GB. This function is currently dead code (never called from S1–S7).
# To activate it, re-download those files from https://nces.ed.gov/ccd/files.asp.
def get_school_enrollment_trends(community_id: str, state: str) -> Optional[dict]:
    """
    Return school-level enrollment trends for a community.

    Uses only the 2020, 2022, 2023 school-level membership files.
    The 2024 file has no NCESSCH column (district-level only) and is excluded.

    Return shape:
        {
            "schools": {
                "350030000123": {
                    "school_name": "Jefferson Montessori Academy",
                    "ncessch": "350030000123",
                    "enrollment_by_year": {2020: 312, 2022: 298, 2023: 287},
                    "trend_direction": "declining",
                    "pct_change_total": -8.0
                }
            },
            "years_available": [2020, 2022, 2023],
            "source_title": "NCES CCD LEA Membership Data",
            "source_url": "https://nces.ed.gov/ccd/files.asp"
        }

    Returns None if no LEAID mapping exists for the community.
    Schools with fewer than 2 years of data are excluded with a per-school warning.
    """
    nces_map = _load_nces_map(state)
    leaid = nces_map.get(community_id)

    if leaid is None:
        log.info(
            "population_trends_fetcher: no NCES mapping for %s in %s — skipping",
            community_id, state,
        )
        return None

    # Accumulate {ncessch: {year: count}} and {ncessch: name} across all files
    school_years: dict[str, dict[int, int]] = {}
    school_names: dict[str, str] = {}

    for year_key, file_path in _SCHOOL_MEMBERSHIP_FILES:
        per_file = _read_school_enrollment_by_file(leaid, file_path)
        for ncessch, entry in per_file.items():
            if ncessch not in school_years:
                school_years[ncessch] = {}
                school_names[ncessch] = entry["school_name"]
            school_years[ncessch][year_key] = entry["count"]

    schools_out: dict[str, dict] = {}
    all_years_seen: set[int] = set()

    for ncessch, by_year in school_years.items():
        all_years_seen.update(by_year.keys())
        years = sorted(by_year.keys())

        if len(years) < 2:
            log.warning(
                "population_trends_fetcher: %s school %s has only %d year(s) of data — skipping",
                community_id, ncessch, len(years),
            )
            continue

        earliest = by_year[years[0]]
        latest   = by_year[years[-1]]

        if earliest == 0:
            log.warning(
                "population_trends_fetcher: school %s earliest enrollment is 0 — skipping",
                ncessch,
            )
            continue

        pct_change = round((latest - earliest) / earliest * 100, 1)

        schools_out[ncessch] = {
            "school_name":        school_names[ncessch],
            "ncessch":            ncessch,
            "enrollment_by_year": dict(by_year),
            "trend_direction":    _classify_trend(pct_change),
            "pct_change_total":   pct_change,
        }

    log.info(
        "population_trends_fetcher: %s — %d schools with multi-year enrollment data",
        community_id, len(schools_out),
    )

    return {
        "schools":        schools_out,
        "years_available": sorted(all_years_seen),
        "source_title":   SOURCE_TITLE,
        "source_url":     SOURCE_URL,
    }
