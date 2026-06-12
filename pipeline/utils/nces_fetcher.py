"""
pipeline/utils/nces_fetcher.py
Utility: load NCES CCD data for injection into S3 prompts.

Reads pre-downloaded CSVs from data/raw/{state}/ and returns verified
district-level finance and FRL figures. Returns None gracefully if files
are missing or the community has no NCES mapping — never raises.

DATA SOURCES (all FY2023 / SY2024-25):
  nces_lea_finance_2024.csv       — per-pupil revenue, expenditure, revenue mix
  nces_sch_lunch_2024.csv         — school-level FRL counts (aggregated to district)

COLUMNS NOT PRESENT in these files (confirmed Task 1b — do not add):
  ell_pct                         — not in any of the 5 NCES files
  enrollment_5yr_trend_pct        — single year only; trend not computable

PER-STATE AVERAGE PER-PUPIL REVENUE:
  Loaded at runtime from config/states.yaml per_pupil_revenue_avg.
  Falls back to dynamic computation from the national finance parquet when
  the value is absent or null in states.yaml.
  NM verified value: $24,356 (computed from 143 valid NM LEA rows).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
import urllib.request
from typing import Optional

import pandas as pd
import yaml

from pipeline.utils.data_config import NCES_CCD_YEAR  # noqa: E402

log = logging.getLogger(__name__)


def _slug(community_id: str) -> str:
    """Strip a 7-digit LEAID suffix appended by _registry_prefix_lookup.

    'tn-memphis-4700148' → 'tn-memphis'
    'tn-memphis'         → 'tn-memphis'  (no-op when suffix absent)
    """
    parts = community_id.rsplit('-', 1)
    if len(parts) == 2 and len(parts[1]) == 7 and parts[1].isdigit():
        return parts[0]
    return community_id


# ── File paths ────────────────────────────────────────────────────────────────

STATES_YAML    = "config/states.yaml"
# Finance and lunch CSV paths are derived at runtime from the state code:
#   data/raw/{state}/nces_lea_finance_2024.csv
#   data/raw/{state}/nces_sch_lunch_2024.csv

# Source metadata for fact injection
SOURCE_URL     = "https://nces.ed.gov/ccd/files.asp"
SOURCE_TITLE   = "NCES CCD Local Education Agency Finance / Lunch Data FY2023"
FISCAL_YEAR    = "2022-2023"

# Sentinel: NCES uses -2 for missing/not-applicable numeric fields
_MISSING_SENTINEL = -2

# ── Urban Institute CCD directory API (charter enrollment share) ──────────────
# The school-level CCD directory endpoint returns, per school, a `charter` flag
# (0/1), a total `enrollment` figure, and `teachers_fte`. Summing `enrollment`
# across charter vs. all schools yields charter enrollment share — a secondary
# charter_saturation signal that complements the primary num_charter_schools.
#
# NOTE (verified by live probe 2026): the `charter=1` query param is NOT honored
# on the schools/ccd/enrollment endpoint and `charter` is not a field there, so
# the directory endpoint is used instead (one call yields charter flag +
# enrollment together). The enrollment endpoint URL is retained as the cited
# source_url because it is the canonical public reference for the figure.
_CCD_DIRECTORY_API = "https://educationdata.urban.org/api/v1/schools/ccd/directory"
_CHARTER_SOURCE_URL_TMPL = (
    "https://educationdata.urban.org/api/v1/schools/ccd/enrollment/{year}/?leaid={leaid}&charter=1"
)
_CHARTER_SOURCE_TITLE = "Urban Institute Education Data API — NCES CCD school directory (enrollment by charter status)"
_CHARTER_ENROLLMENT_YEARS = (NCES_CCD_YEAR, NCES_CCD_YEAR - 1)   # try most recent first, then fall back
_CHARTER_CACHE_DIR = "data/cache/fetcher/ccd_enrollment"
_CHARTER_CACHE_TTL_DAYS = 90               # matches the existing CCD cache TTL
_CCD_MAX_PAGES = 30


def _ccd_cache_path(key: str) -> str:
    return os.path.join(_CHARTER_CACHE_DIR, f"{key}.json")


def _ccd_read_cache(key: str) -> Optional[dict]:
    path = _ccd_cache_path(key)
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 86400 > _CHARTER_CACHE_TTL_DAYS:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _ccd_write_cache(key: str, data: dict) -> None:
    try:
        os.makedirs(_CHARTER_CACHE_DIR, exist_ok=True)
        with open(_ccd_cache_path(key), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.warning("nces_fetcher: charter cache write failed for %s — %s", key, exc)


def _ccd_get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("nces_fetcher: CCD directory request failed for %s — %s", url, exc)
        return None


def _ccd_fetch_directory(leaid: str, year: int) -> Optional[list]:
    """Return all school directory rows for a LEAID/year, following pagination.
    None on first-request failure; [] when the year is not yet loaded."""
    first = _ccd_get(f"{_CCD_DIRECTORY_API}/{year}/?leaid={leaid}")
    if first is None:
        return None
    rows = list(first.get("results", []))
    nxt = first.get("next")
    pages = 1
    while nxt and pages < _CCD_MAX_PAGES:
        page = _ccd_get(nxt)
        if page is None:
            break
        rows.extend(page.get("results", []))
        nxt = page.get("next")
        pages += 1
    return rows


def _as_pos_int(val) -> int:
    try:
        n = int(val)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def get_charter_enrollment_share(leaid: Optional[str]) -> Optional[dict]:
    """Compute charter enrollment share for a district from the Urban CCD
    directory API. Never raises; returns None on missing leaid / no data.

    Return shape:
        {
            "district_total_enrollment":    79805,
            "total_charter_enrollment":     9247,
            "charter_enrollment_share_pct": 11.6,
            "year":                         2022,
            "source_url":                   "...",
            "source_title":                 "...",
            "confidence":                   "HIGH",
        }
    """
    if not leaid:
        return None

    cache_key = f"charter_share_{leaid}"
    cached = _ccd_read_cache(cache_key)
    if cached is not None:
        log.info("nces_fetcher: charter-share cache hit for %s", leaid)
        return cached

    for year in _CHARTER_ENROLLMENT_YEARS:
        rows = _ccd_fetch_directory(leaid, year)
        if not rows:
            continue   # None (request failed) or [] (year not loaded) — try next

        district_total = sum(_as_pos_int(r.get("enrollment")) for r in rows)
        charter_total = sum(
            _as_pos_int(r.get("enrollment")) for r in rows if r.get("charter") == 1
        )
        if district_total <= 0:
            continue   # no usable enrollment for this year

        result = {
            "district_total_enrollment":    district_total,
            "total_charter_enrollment":     charter_total,
            "charter_enrollment_share_pct": round(charter_total / district_total * 100, 1),
            "year":                         year,
            "source_url":   _CHARTER_SOURCE_URL_TMPL.format(year=year, leaid=leaid),
            "source_title": _CHARTER_SOURCE_TITLE,
            "confidence":   "HIGH",
        }
        log.info(
            "nces_fetcher: charter share for LEAID %s (%d) — %d/%d = %.1f%%",
            leaid, year, charter_total, district_total,
            result["charter_enrollment_share_pct"],
        )
        _ccd_write_cache(cache_key, result)
        return result

    log.warning("nces_fetcher: no CCD directory enrollment available for LEAID %s", leaid)
    return None


def _frl_to_complexity_index(frl_pct: float) -> int:
    """Convert FRL% to operational complexity index (1–9 scale).

    Implements the operational_complexity_rubric documented in
    config/scoring_weights.yaml.  Injected as a FEDERAL_DATA fact so S4's
    deterministic rules always pass it through to S5 regardless of how the
    Haiku LLM classifies the Claude-generated index for the same dimension.

    Thresholds (FRL % → rubric label → index value):
        < 30%  → MINIMAL    → 2
        < 50%  → LOW        → 3
        < 65%  → MODERATE   → 5
        < 80%  → HIGH       → 7
        < 95%  → VERY_HIGH  → 8
        >= 95% → EXTREME    → 9
    """
    if frl_pct >= 95:
        return 9   # EXTREME
    if frl_pct >= 80:
        return 8   # VERY_HIGH
    if frl_pct >= 65:
        return 7   # HIGH
    if frl_pct >= 50:
        return 5   # MODERATE
    if frl_pct >= 30:
        return 3   # LOW
    return 2       # MINIMAL


# ── Config loader ─────────────────────────────────────────────────────────────

def _load_nces_map(state: str) -> dict:
    """Return the nces_district_map for the given state from states.yaml."""
    try:
        with open(STATES_YAML) as f:
            config = yaml.safe_load(f)
        return config.get(state.upper(), {}).get("nces_district_map", {})
    except Exception as exc:
        log.warning("nces_fetcher: could not load states.yaml — %s", exc)
        return {}


def _get_state_avg_ppr(state: str) -> Optional[float]:
    """Return the per-pupil revenue average for a state.

    Load order:
      1. config/states.yaml per_pupil_revenue_avg (trusted static value)
      2. Compute from national finance parquet (mean TOTALREV/MEMBERSCH for
         the state's NCES districts — requires the parquet to be present)
      3. None with a warning if both sources are unavailable

    The dynamic parquet path (2) is the preferred source of truth — it uses
    the same TOTALREV/MEMBERSCH basis as the district value, ensuring a
    like-for-like total-revenue comparison. States.yaml overrides should only
    be used when a verified, vintage-annotated value is available (e.g.
    "per_pupil_revenue_avg: 24356.0  # NCES F-33 SY2022-23, 143 NM LEAs").
    A null or absent entry activates the parquet fallback automatically.
    """
    # 1. states.yaml static value
    try:
        with open(STATES_YAML) as f:
            states_cfg = yaml.safe_load(f)
        val = states_cfg.get(state.upper(), {}).get("per_pupil_revenue_avg")
        if val is not None:
            return float(val)
    except Exception as exc:
        log.warning("nces_fetcher: could not read per_pupil_revenue_avg from states.yaml — %s", exc)

    # 2. Dynamic computation from national finance parquet
    finance_pq = "data/raw/national/nces_lea_finance.parquet"
    if not os.path.exists(finance_pq):
        log.warning(
            "nces_fetcher: per_pupil_revenue_avg not in states.yaml for %s "
            "and national parquet not found — returning None",
            state,
        )
        return None

    try:
        nces_map = _load_nces_map(state)
        leaids = set(v for v in nces_map.values() if v)
        if not leaids:
            log.warning(
                "nces_fetcher: no NCES district IDs for %s — cannot compute state avg PPR",
                state,
            )
            return None
        df = pd.read_parquet(finance_pq)
        state_rows = df[df.index.isin(leaids)]
        valid = state_rows[
            (state_rows["MEMBERSCH"].notna()) & (state_rows["MEMBERSCH"] > 0)
            & (state_rows["TOTALREV"].notna()) & (state_rows["TOTALREV"] > 0)
            & (state_rows["MEMBERSCH"] != _MISSING_SENTINEL)
            & (state_rows["TOTALREV"] != _MISSING_SENTINEL)
        ]
        if valid.empty:
            log.warning(
                "nces_fetcher: no valid finance rows for %s — cannot compute avg PPR",
                state,
            )
            return None
        avg = (valid["TOTALREV"] / valid["MEMBERSCH"]).mean()
        log.info(
            "nces_fetcher: computed state avg PPR for %s = $%.0f (%d districts)",
            state, avg, len(valid),
        )
        return float(avg)
    except Exception as exc:
        log.warning(
            "nces_fetcher: error computing state avg PPR for %s — %s", state, exc
        )
        return None


# ── Finance reader ────────────────────────────────────────────────────────────

def _read_finance(leaid: str, state: str) -> Optional[dict]:
    """
    Return raw finance fields for a single LEAID, or None if not found / invalid.

    Filters out rows where key columns carry the -2 missing sentinel before
    any arithmetic. Returns a dict of raw numeric values.
    """
    finance_pq = "data/raw/national/nces_lea_finance.parquet"
    if not os.path.exists(finance_pq):
        log.warning(
            "nces_fetcher: national finance parquet not found. "
            "Run scripts/build_national_parquet.py"
        )
        return None

    try:
        df = pd.read_parquet(finance_pq)
        if leaid not in df.index:
            return None
        row = df.loc[leaid]

        def _int(col: str) -> Optional[int]:
            if col not in df.columns:
                return None
            raw = row[col]
            if pd.isna(raw):
                return None
            try:
                val = int(raw)
                return None if val == _MISSING_SENTINEL else val
            except (ValueError, TypeError):
                return None

        return {
            "MEMBERSCH": _int("MEMBERSCH"),
            "TOTALREV":  _int("TOTALREV"),
            "TFEDREV":   _int("TFEDREV"),
            "TSTREV":    _int("TSTREV"),
            "TLOCREV":   _int("TLOCREV"),
            "TOTALEXP":  _int("TOTALEXP"),
        }
    except Exception as exc:
        log.warning("nces_fetcher: error reading finance parquet — %s", exc)

    return None


# ── Lunch aggregator ──────────────────────────────────────────────────────────

def _aggregate_frl(leaid: str, state: str) -> Optional[int]:
    """
    Sum free + reduced-price lunch counts across all schools in the district.

    Filter criteria (confirmed Task 1b):
      DATA_GROUP   == "Free and Reduced-price Lunch Table"
      LUNCH_PROGRAM in {"Free lunch qualified", "Reduced-price lunch qualified"}
      TOTAL_INDICATOR == "Category Set A"
      DMS_FLAG     == "Reported"
      STUDENT_COUNT > 0

    Returns total FRL-eligible student count, or None if the file is missing.
    Note: denominator (total enrollment) comes from finance MEMBERSCH, not here.
    """
    lunch_pq = "data/raw/national/nces_sch_lunch.parquet"
    if not os.path.exists(lunch_pq):
        log.warning(
            "nces_fetcher: national lunch parquet not found. "
            "Run scripts/build_national_parquet.py"
        )
        return None

    try:
        df = pd.read_parquet(lunch_pq)
        mask = (
            (df["LEAID"] == leaid)
            & (df["DATA_GROUP"] == "Free and Reduced-price Lunch Table")
            & (df["TOTAL_INDICATOR"] == "Category Set A")
            & (df["DMS_FLAG"] == "Reported")
            & (df["LUNCH_PROGRAM"].isin(
                ["Free lunch qualified", "Reduced-price lunch qualified"]
            ))
        )
        rows = df[mask]
        if rows.empty:
            return None
        total = pd.to_numeric(rows["STUDENT_COUNT"], errors="coerce").fillna(0).astype(int).sum()
        return int(total) if total > 0 else None
    except Exception as exc:
        log.warning("nces_fetcher: error reading lunch parquet — %s", exc)
        return None


# ── Public interface ──────────────────────────────────────────────────────────

def get_district_data(community_id: str, state: str) -> Optional[dict]:
    """
    Return verified NCES data for a community, or None if unavailable.

    Return shape (all keys present only when data is valid):
        {
            "per_pupil_expenditure":              14326.0,
            "per_pupil_revenue_vs_state_avg_pct": -36.0,
            "revenue_local_pct":                  15.8,
            "revenue_federal_pct":                10.0,
            "revenue_state_pct":                  74.2,
            "frl_pct":                            86.3,   # if lunch data present
            "fiscal_year":                        "2022-2023",
            "source_url":                         "...",
            "source_title":                       "...",
            "confidence":                         "HIGH",
        }

    Returns None (does not raise) if:
      - community_id has no entry in nces_district_map
      - community_id maps to null (e.g., Navajo Nation communities)
      - required numeric fields are missing or carry the -2 sentinel
    """
    nces_map = _load_nces_map(state)
    leaid = nces_map.get(community_id) or nces_map.get(_slug(community_id))

    if leaid is None:
        log.info(
            "nces_fetcher: no NCES mapping for %s in %s — skipping",
            community_id, state
        )
        return None

    # ── Finance fields ────────────────────────────────────────────────────────
    finance = _read_finance(leaid, state)
    if finance is None:
        log.warning("nces_fetcher: no finance row found for LEAID %s (%s)", leaid, community_id)
        return None

    membersch = finance["MEMBERSCH"]
    totalrev  = finance["TOTALREV"]
    tfedrev   = finance["TFEDREV"]
    tstrev    = finance["TSTREV"]
    tlocrev   = finance["TLOCREV"]
    totalexp  = finance["TOTALEXP"]

    # Need at minimum membership + total revenue to produce useful data
    if not membersch or membersch <= 0:
        log.warning(
            "nces_fetcher: MEMBERSCH missing or zero for LEAID %s (%s) — skipping",
            leaid, community_id
        )
        return None

    result: dict = {
        "fiscal_year":  FISCAL_YEAR,
        "source_url":   SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence":   "HIGH",
    }

    # Per-pupil expenditure
    if totalexp is not None and totalexp > 0:
        result["per_pupil_expenditure"] = round(totalexp / membersch, 2)

    # Per-pupil revenue vs. state average
    if totalrev is not None and totalrev > 0:
        ppr = totalrev / membersch
        state_avg_ppr = _get_state_avg_ppr(state)
        if state_avg_ppr is not None and state_avg_ppr > 0:
            result["per_pupil_revenue_vs_state_avg_pct"] = round(
                (ppr - state_avg_ppr) / state_avg_ppr * 100, 1
            )
        else:
            log.warning(
                "nces_fetcher: no state avg PPR for %s — skipping per_pupil_revenue_vs_state_avg_pct",
                state,
            )

        # Revenue breakdown percentages
        if tfedrev is not None:
            result["revenue_federal_pct"] = round(tfedrev / totalrev * 100, 1)
        if tstrev is not None:
            result["revenue_state_pct"]   = round(tstrev  / totalrev * 100, 1)
        if tlocrev is not None:
            result["revenue_local_pct"]   = round(tlocrev / totalrev * 100, 1)

    # ── FRL from lunch file ───────────────────────────────────────────────────
    frl_count = _aggregate_frl(leaid, state)
    if frl_count is not None and membersch > 0:
        frl_pct = round(frl_count / membersch * 100, 1)
        result["frl_pct"] = frl_pct
        # Top-band compression: at very high FRL% the metric saturates and can no
        # longer discriminate need. Flag so downstream consumers treat a high-FRL
        # "present" datapoint differently from a default-5 exclusion.
        result["frl_compressed"] = frl_pct >= 80
        # Derive a FEDERAL_DATA-sourced operational_complexity_index from FRL%.
        # This ensures S4's hard rules (which block UNVERIFIED source_class) do
        # not prevent S5 from scoring operational_complexity when the
        # Claude-generated index happens to be left as UNVERIFIED.
        result["operational_complexity_index"] = _frl_to_complexity_index(frl_pct)

    if len(result) <= 4:
        # Only metadata keys — no actual data was usable
        log.warning(
            "nces_fetcher: all numeric fields missing for LEAID %s (%s) — skipping",
            leaid, community_id
        )
        return None

    log.info(
        "nces_fetcher: %s (LEAID %s) — PPE=$%.0f  PPR_vs_avg=%.1f%%  FRL=%.1f%%",
        community_id, leaid,
        result.get("per_pupil_expenditure", float("nan")),
        result.get("per_pupil_revenue_vs_state_avg_pct", float("nan")),
        result.get("frl_pct", float("nan")),
    )

    return result
