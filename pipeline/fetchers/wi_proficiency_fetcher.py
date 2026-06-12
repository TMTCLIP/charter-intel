"""
pipeline/fetchers/wi_proficiency_fetcher.py
Wisconsin district proficiency fetcher.

Reads data/raw/wi/wi_district_proficiency.csv and returns ELA and Math
proficiency percentages for a given NCES LEAID or community_id. Mirrors the
MS adapter (pipeline/fetchers/ms_proficiency_fetcher.py) exactly in interface
and return shape. Returns None gracefully — never raises.

AUTHORITATIVE SOURCE (verified 2026-06-07):
  Wisconsin Forward Exam, WI Dept. of Public Instruction (DPI). District-level
  ELA/Math proficiency, grades 3-8 and 10.
  Public statewide download files — no login required:
    https://dpi.wi.gov/wisedash/download-files/type  ("Data Files by Topic")
  Public dashboard: https://dpi.wi.gov/wisedash  (WISEdash Public Portal).
  NOTE: "WISEdash for Districts" is login-gated, but the statewide *download
  files* and Public Portal are open — use those.

METHODOLOGY CAVEAT (operator must confirm before populating):
  Beginning Spring 2024 the Forward Exam reports four performance levels —
  Advanced, Meeting, Approaching, Developing — under new cut scores. There is
  no single "Proficient" level. To produce a "% proficient" comparable to the
  MS/NM/TN adapters, the operator must define proficient = Advanced + Meeting
  (the conventional choice) and apply it consistently. Pre-Spring-2024 files use
  the older Advanced/Proficient/Basic/Below-Basic levels (proficient = Advanced
  + Proficient). Mixing vintages without normalizing the level definition would
  corrupt the academic_need signal — do NOT do it silently.

DATA GAP (graceful degradation):
  The Forward Exam district file has NOT been downloaded/normalized into the CSV
  below yet, so this fetcher returns None for every community until an operator
  populates data/raw/wi/wi_district_proficiency.csv. Until then S5 excludes
  academic_need from the composite (used_default) rather than defaulting to 5.0.

  To populate: download the statewide district-level file, decide the proficient
  definition (see caveat above), then write a CSV with this exact header (same
  schema as data/raw/ms/ms_district_proficiency.csv):
    LEAID,DistrictName,EntityID,ELAProficiencyPct,MathProficiencyPct,SchoolYear
  where LEAID is the 7-digit NCES district id. Do NOT invent values — leave a
  district out if its number is unavailable.
"""
from __future__ import annotations
import csv
import logging
import os
from typing import Optional

import yaml

log = logging.getLogger(__name__)


def _slug(community_id: str) -> str:
    """Strip a 7-digit LEAID suffix appended by _registry_prefix_lookup."""
    parts = community_id.rsplit('-', 1)
    if len(parts) == 2 and len(parts[1]) == 7 and parts[1].isdigit():
        return parts[0]
    return community_id


CSV_PATH     = "data/raw/wi/wi_district_proficiency.csv"
STATES_YAML  = "config/states.yaml"
SCHOOL_YEAR  = "2023-2024"
SOURCE_URL   = "https://dpi.wi.gov/wisedash/download-files/type"
SOURCE_TITLE = "Wisconsin Forward Exam District Results SY 2023-24"


def _load_csv() -> dict[str, dict]:
    """Return {leaid_str: row_dict} from the WI proficiency CSV."""
    if not os.path.exists(CSV_PATH):
        log.warning("wi_proficiency_fetcher: CSV not found — %s", CSV_PATH)
        return {}
    by_leaid: dict[str, dict] = {}
    try:
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                leaid = str(row.get("LEAID", "")).strip().lstrip("0") or str(row.get("LEAID", "")).strip()
                leaid_z = str(row.get("LEAID", "")).strip()  # zero-padded form
                if leaid:
                    by_leaid[leaid] = row
                if leaid_z and leaid_z != leaid:
                    by_leaid[leaid_z] = row
    except Exception as exc:
        log.warning("wi_proficiency_fetcher: error reading CSV — %s", exc)
    return by_leaid


def _load_nces_map() -> dict[str, str]:
    """Return {community_id: leaid} from states.yaml WI nces_district_map."""
    try:
        with open(STATES_YAML) as f:
            cfg = yaml.safe_load(f)
        return {k: str(v) for k, v in cfg.get("WI", {}).get("nces_district_map", {}).items()}
    except Exception as exc:
        log.warning("wi_proficiency_fetcher: could not load states.yaml — %s", exc)
        return {}


def fetch_wi_proficiency(leaid: str) -> Optional[dict]:
    """
    Return verified WI proficiency data for a LEAID, or None if unavailable.

    Accepts LEAID as a string with or without leading zeros. Return shape matches
    the MS/NM adapters exactly:
        {
            "ela_proficiency_pct":  float,
            "math_proficiency_pct": float,
            "school_year":   "2023-2024",
            "source_url":    SOURCE_URL,
            "source_title":  SOURCE_TITLE,
            "confidence":    "HIGH",
        }
    """
    by_leaid = _load_csv()
    if not by_leaid:
        return None

    leaid_str = str(leaid).strip()
    row = by_leaid.get(leaid_str) or by_leaid.get(leaid_str.lstrip("0"))
    if row is None:
        log.info("wi_proficiency_fetcher: no row for LEAID %s", leaid_str)
        return None

    try:
        ela_raw  = row.get("ELAProficiencyPct",  "").strip()
        math_raw = row.get("MathProficiencyPct", "").strip()
        ela  = float(ela_raw)  if ela_raw  else None
        math = float(math_raw) if math_raw else None
    except ValueError as exc:
        log.warning("wi_proficiency_fetcher: bad numeric value for LEAID %s — %s", leaid_str, exc)
        return None

    if ela is None and math is None:
        log.warning("wi_proficiency_fetcher: both ELA and Math null for LEAID %s", leaid_str)
        return None

    log.info(
        "wi_proficiency_fetcher: LEAID %s — ELA=%.1f%% Math=%.1f%%",
        leaid_str,
        ela  if ela  is not None else float("nan"),
        math if math is not None else float("nan"),
    )
    return {
        "ela_proficiency_pct":  ela,
        "math_proficiency_pct": math,
        "school_year":   SCHOOL_YEAR,
        "source_url":    SOURCE_URL,
        "source_title":  SOURCE_TITLE,
        "confidence":    "HIGH",
    }


def get_wi_district_data(community_id: str) -> Optional[dict]:
    """
    Return proficiency data for a community_id (e.g. 'wi-milwaukee-5509600').

    Looks up the NCES LEAID from states.yaml, then delegates to
    fetch_wi_proficiency(). Returns None if no mapping exists.
    """
    nces_map = _load_nces_map()
    leaid = nces_map.get(community_id) or nces_map.get(_slug(community_id))
    if not leaid:
        log.info("wi_proficiency_fetcher: no NCES mapping for %s", community_id)
        return None
    return fetch_wi_proficiency(leaid)
