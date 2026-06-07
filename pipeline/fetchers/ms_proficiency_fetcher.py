"""
pipeline/fetchers/ms_proficiency_fetcher.py
Mississippi district proficiency fetcher.

Reads data/raw/ms/ms_district_proficiency.csv (scraped from MSRC SY 2022-23)
and returns ELA and Math proficiency percentages for a given NCES LEAID or
community_id. Returns None gracefully — never raises.
"""
from __future__ import annotations
import csv
import logging
import os
from typing import Optional

import yaml

log = logging.getLogger(__name__)

CSV_PATH    = "data/raw/ms/ms_district_proficiency.csv"
STATES_YAML = "config/states.yaml"
SCHOOL_YEAR = "2022-2023"
SOURCE_URL   = "https://msrc.mdek12.org/"
SOURCE_TITLE = "Mississippi Succeeds Report Card SY 2022-23"


def _load_csv() -> dict[str, dict]:
    """Return {leaid_str: row_dict} from the MS proficiency CSV."""
    if not os.path.exists(CSV_PATH):
        log.warning("ms_proficiency_fetcher: CSV not found — %s", CSV_PATH)
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
        log.warning("ms_proficiency_fetcher: error reading CSV — %s", exc)
    return by_leaid


def _load_nces_map() -> dict[str, str]:
    """Return {community_id: leaid} from states.yaml MS nces_district_map."""
    try:
        with open(STATES_YAML) as f:
            cfg = yaml.safe_load(f)
        return {k: str(v) for k, v in cfg.get("MS", {}).get("nces_district_map", {}).items()}
    except Exception as exc:
        log.warning("ms_proficiency_fetcher: could not load states.yaml — %s", exc)
        return {}


def fetch_ms_proficiency(leaid: str) -> Optional[dict]:
    """
    Return verified MS proficiency data for a LEAID, or None if unavailable.

    Accepts LEAID as a string with or without leading zeros.

    Return shape:
        {
            "ela_proficiency_pct":  62.1,
            "math_proficiency_pct": 69.4,
            "school_year":   "2022-2023",
            "source_url":    "https://msrc.mdek12.org/",
            "source_title":  "Mississippi Succeeds Report Card SY 2022-23",
            "confidence":    "HIGH",
        }
    """
    by_leaid = _load_csv()
    if not by_leaid:
        return None

    leaid_str = str(leaid).strip()
    row = by_leaid.get(leaid_str) or by_leaid.get(leaid_str.lstrip("0"))
    if row is None:
        log.info("ms_proficiency_fetcher: no row for LEAID %s", leaid_str)
        return None

    try:
        ela_raw  = row.get("ELAProficiencyPct",  "").strip()
        math_raw = row.get("MathProficiencyPct", "").strip()
        ela  = float(ela_raw)  if ela_raw  else None
        math = float(math_raw) if math_raw else None
    except ValueError as exc:
        log.warning("ms_proficiency_fetcher: bad numeric value for LEAID %s — %s", leaid_str, exc)
        return None

    if ela is None and math is None:
        log.warning("ms_proficiency_fetcher: both ELA and Math null for LEAID %s", leaid_str)
        return None

    log.info(
        "ms_proficiency_fetcher: LEAID %s — ELA=%.1f%% Math=%.1f%%",
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


def get_ms_district_data(community_id: str) -> Optional[dict]:
    """
    Return proficiency data for a community_id (e.g. 'ms-oxford-2803450').

    Looks up the NCES LEAID from states.yaml, then delegates to
    fetch_ms_proficiency().  Returns None if no mapping exists.
    """
    nces_map = _load_nces_map()
    leaid = nces_map.get(community_id)
    if not leaid:
        log.info("ms_proficiency_fetcher: no NCES mapping for %s", community_id)
        return None
    return fetch_ms_proficiency(leaid)
