"""
pipeline/utils/ped_fetcher.py
Utility: load real NM PED proficiency data for injection into S3 prompts.

Reads pre-downloaded CSVs from data/raw/{state}/ and returns verified
district-level proficiency figures. Returns None gracefully if files are
missing or the community has no district mapping — never raises.
"""
from __future__ import annotations
import csv
import logging
import os
from typing import Optional

import yaml

log = logging.getLogger(__name__)

ELA_CSV  = "data/raw/nm/proficiency_ela_2024_25.csv"
MATH_CSV = "data/raw/nm/proficiency_math_2024_25.csv"
STATES_YAML = "config/states.yaml"

SOURCE_URL   = "https://webnew.ped.state.nm.us/bureaus/accountability/achievement-data-by-year/"
SOURCE_TITLE = "NM PED Achievement Data SY 2024-25"
SCHOOL_YEAR  = "2024-2025"


def _load_district_map(state: str) -> dict:
    """Return the nm_district_map for the given state from states.yaml."""
    try:
        with open(STATES_YAML) as f:
            config = yaml.safe_load(f)
        return config.get(state.upper(), {}).get("nm_district_map", {})
    except Exception as exc:
        log.warning("ped_fetcher: could not load states.yaml — %s", exc)
        return {}


def _read_proficiency(csv_path: str, district_name: str) -> Optional[float]:
    """
    Return AttenuatedProficiencyRate for the district's All / district-level row.

    Handles two CSV variants found in NM PED downloads:
      - File with a metadata title line before the real header (ELA export)
      - File with a UTF-8 BOM on the first header line (Math export)
    Both are normalised by reading all lines, finding the first line that
    contains "TableName" (the real column header), and feeding only from
    that point to csv.DictReader.
    """
    if not os.path.exists(csv_path):
        log.warning("ped_fetcher: file not found — %s", csv_path)
        return None

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            lines = f.readlines()

        # Find the real header row (contains the column name "TableName")
        header_idx = next(
            (i for i, ln in enumerate(lines) if "TableName" in ln),
            None,
        )
        if header_idx is None:
            log.warning("ped_fetcher: no TableName header found in %s", csv_path)
            return None

        reader = csv.DictReader(lines[header_idx:])
        for row in reader:
            if (
                row.get("Demographic", "").strip() == "All"
                and str(row.get("LocationCode", "")).strip() == "0"
                and row.get("DistrictName", "").strip().upper() == district_name.upper()
            ):
                raw = row.get("AttenuatedProficiencyRate", "").strip()
                return float(raw) if raw else None

    except Exception as exc:
        log.warning("ped_fetcher: error reading %s — %s", csv_path, exc)

    return None


def get_district_data(community_id: str, state: str) -> Optional[dict]:
    """
    Return verified PED proficiency data for a community, or None if unavailable.

    Return shape:
        {
            "ela_proficiency_pct": 44.06,
            "math_proficiency_pct": 23.38,
            "school_year": "2024-2025",
            "source_url": "...",
            "source_title": "...",
            "confidence": "HIGH",
        }
    """
    district_map = _load_district_map(state)
    district_name = district_map.get(community_id)

    if not district_name:
        log.info("ped_fetcher: no district mapping for %s in %s", community_id, state)
        return None

    ela  = _read_proficiency(ELA_CSV,  district_name)
    math = _read_proficiency(MATH_CSV, district_name)

    if ela is None and math is None:
        log.warning(
            "ped_fetcher: no proficiency rows found for district '%s'", district_name
        )
        return None

    log.info(
        "ped_fetcher: %s — ELA=%.2f%% MATH=%.2f%%",
        district_name,
        ela  if ela  is not None else float("nan"),
        math if math is not None else float("nan"),
    )

    return {
        "ela_proficiency_pct":  ela,
        "math_proficiency_pct": math,
        "school_year":  SCHOOL_YEAR,
        "source_url":   SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence":   "HIGH",
    }
