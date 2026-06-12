"""
pipeline/fetchers/il_proficiency_fetcher.py
Illinois district proficiency fetcher.

Reads data/raw/il/iar_district_proficiency.csv (Illinois Assessment of
Readiness SY 2023-24, district-level "All Students" rows, IAR only).
Matches by district name derived from community_id — LEAID/RCDTS lookup
is not used for v1 because the IL nces_district_map in states.yaml is
not yet populated.

AUTHORITATIVE SOURCE (verified 2026-06-12):
  Illinois Report Card Public Data Set (SY 2023-24), ISBE.
  IAR ELA / Math Proficiency Rate - Total (district-level, All Students).
  Public download — no login required:
    https://www.isbe.net/ilreportcarddata
  Latest released data set: 2024 Report Card Public Data Set (version 2,
  published 2024-11-01).

MATCHING STRATEGY (v1):
  Extract city slug from community_id (e.g. "il-chicago" → "chicago"),
  find rows where DistrictName contains the slug.  Among multiple hits,
  prefer rows whose DistrictName starts with the slug, then prefer
  "Public Schools" or "School District" in the name, then shortest match.
  Returns None on no match or if CSV is missing — never raises.

UPGRADE PATH:
  When IL nces_district_map is populated in states.yaml, add LEAID or
  RCDTS-based primary lookup and keep name match as fallback.
"""
from __future__ import annotations
import csv
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

CSV_PATH     = "data/raw/il/iar_district_proficiency.csv"
SCHOOL_YEAR  = "2023-2024"
SOURCE_URL   = "https://www.isbe.net/ilreportcarddata"
SOURCE_TITLE = "Illinois IAR District Proficiency Rate SY 2023-24"


def _load_csv() -> list[dict]:
    if not os.path.exists(CSV_PATH):
        log.warning("il_proficiency_fetcher: CSV not found — %s", CSV_PATH)
        return []
    try:
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        log.warning("il_proficiency_fetcher: error reading CSV — %s", exc)
        return []


def _city_slug(community_id: str) -> str:
    """'il-chicago' → 'chicago', 'il-north-chicago' → 'north chicago'"""
    slug = community_id.lower()
    slug = re.sub(r'^il-', '', slug)
    slug = re.sub(r'-\d+$', '', slug)
    return slug.replace('-', ' ').strip()


def _best_match(rows: list[dict], city: str) -> Optional[dict]:
    city_l = city.lower()
    hits = [r for r in rows if city_l in r.get('DistrictName', '').lower()]
    if not hits:
        return None
    starts = [r for r in hits if r['DistrictName'].lower().startswith(city_l)]
    pool = starts if starts else hits
    # Prefer "Public Schools" or "School District" in name
    pub = [r for r in pool
           if 'public schools' in r['DistrictName'].lower()
           or 'school district' in r['DistrictName'].lower()]
    return (pub or pool)[0]


def fetch_il_proficiency(community_id: str) -> Optional[dict]:
    """
    Return verified IL proficiency data for a community_id, or None if unavailable.

    Return shape:
        {
            "ela_proficiency_pct":  float,
            "math_proficiency_pct": float,
            "school_year":   "2023-2024",
            "source_url":    SOURCE_URL,
            "source_title":  SOURCE_TITLE,
            "confidence":    "HIGH",
        }
    """
    rows = _load_csv()
    if not rows:
        return None

    city = _city_slug(community_id)
    row  = _best_match(rows, city)
    if row is None:
        log.info("il_proficiency_fetcher: no match for community_id %s (city='%s')",
                 community_id, city)
        return None

    try:
        ela_raw  = row.get("ELAProficiencyPct",  "")
        math_raw = row.get("MathProficiencyPct", "")
        ela  = float(ela_raw)  if ela_raw  else None
        math = float(math_raw) if math_raw else None
    except ValueError as exc:
        log.warning("il_proficiency_fetcher: bad numeric value for %s — %s", community_id, exc)
        return None

    if ela is None and math is None:
        log.warning("il_proficiency_fetcher: both ELA and Math null for %s", community_id)
        return None

    log.info("il_proficiency_fetcher: %s → '%s' ELA=%.1f%% Math=%.1f%%",
             community_id, row['DistrictName'],
             ela  if ela  is not None else float('nan'),
             math if math is not None else float('nan'))

    return {
        "ela_proficiency_pct":  ela,
        "math_proficiency_pct": math,
        "school_year":   SCHOOL_YEAR,
        "source_url":    SOURCE_URL,
        "source_title":  SOURCE_TITLE,
        "confidence":    "HIGH",
    }


def get_il_district_data(community_id: str) -> Optional[dict]:
    """Public entry point called by S3. Delegates to fetch_il_proficiency."""
    return fetch_il_proficiency(community_id)
