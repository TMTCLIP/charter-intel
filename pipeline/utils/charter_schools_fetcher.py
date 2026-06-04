"""
pipeline/utils/charter_schools_fetcher.py
Utility: read PED charter roster + proficiency CSVs to build a structured
list of charter schools operating in a given community.

Returns CSV-sourced basic info only (no API calls).
The S3 stage calls this, then enriches the result via a Claude call
(web search on standard/deep; knowledge fallback on fast).

DATA SOURCES:
  charter_roster.csv      — school name, authorizer, grades, phone, email
  proficiency_ela_*.csv   — school-level ELA proficiency (LocationCode != 0)
  proficiency_math_*.csv  — school-level Math proficiency

MATCHING:
  Roster names and proficiency CSV names are often formatted differently
  (e.g. "Monte del Sol Charter School" vs "MONTE DEL SOL CHARTER").
  The matcher tries exact → normalised → prefix matching in that order.
  Unmatched schools get ela_proficiency_pct / math_proficiency_pct = None.
"""
from __future__ import annotations

import csv
import logging
import re
from typing import Optional

log = logging.getLogger(__name__)

# TODO(S35-sweep): ROSTER_CSV, ELA_CSV, MATH_CSV, SOURCE_URL, and SOURCE_TITLE
# are all NM-specific. Derive paths from state param (data/raw/{state}/...)
# and source metadata from config/states.yaml data_sources before expanding
# beyond NM.
ROSTER_CSV    = "data/raw/nm/charter_roster.csv"
ELA_CSV       = "data/raw/nm/proficiency_ela_2024_25.csv"
MATH_CSV      = "data/raw/nm/proficiency_math_2024_25.csv"
SOURCE_URL    = "https://webnew.ped.state.nm.us/bureaus/options-for-parents/charter-schools/"
SOURCE_TITLE  = "NM PED Charter School Roster SY 2025-26"
PROFICIENCY_YEAR = 2025


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_proficiency_map(csv_path: str) -> dict[str, float]:
    """
    Return {UPPER_SCHOOL_NAME: proficiency_pct} for all school-level rows
    (LocationCode != '0', Demographic == 'All') in a PED proficiency CSV.
    """
    result: dict[str, float] = {}
    if not __import__("os").path.exists(csv_path):
        return result

    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            lines = f.readlines()
        header_idx = next(
            (i for i, ln in enumerate(lines) if "TableName" in ln), None
        )
        if header_idx is None:
            return result

        for row in csv.DictReader(lines[header_idx:]):
            if (
                row.get("Demographic", "").strip() == "All"
                and str(row.get("LocationCode", "")).strip() != "0"
            ):
                name = row.get("LocationName", "").strip().upper()
                raw  = row.get("AttenuatedProficiencyRate", "").strip()
                if name and raw:
                    try:
                        result[name] = float(raw)
                    except ValueError:
                        pass
    except Exception as exc:
        log.warning("charter_schools_fetcher: proficiency read error (%s) — %s", csv_path, exc)

    return result


def _normalise(name: str) -> str:
    """Strip common school-type words and punctuation for fuzzy matching."""
    s = name.upper()
    for token in (
        " CHARTER SCHOOL", " CHARTER", " PUBLIC ACADEMY", " ACADEMY",
        " HIGH SCHOOL", " HIGH", " ELEMENTARY", " MIDDLE SCHOOL",
        " MIDDLE", " (THE)", "THE ", r"\(|\)"
    ):
        s = re.sub(token, "", s)
    return re.sub(r"\s+", " ", s).strip()


def _match_proficiency(school_name: str, pmap: dict[str, float]) -> Optional[float]:
    """
    Try to match a roster school name to the proficiency map.
    Returns the proficiency value or None if no match found.
    """
    key = school_name.upper()

    # 1. Exact match
    if key in pmap:
        return pmap[key]

    # 2. Normalised exact
    norm = _normalise(key)
    for k, v in pmap.items():
        if _normalise(k) == norm:
            return v

    # 3. Prefix / substring match on at least 8 chars of normalised name
    if len(norm) >= 8:
        for k, v in pmap.items():
            kn = _normalise(k)
            if kn.startswith(norm[:8]) or norm.startswith(kn[:8]):
                return v

    return None


# ── Public interface ──────────────────────────────────────────────────────────

def get_community_charter_schools(
    known_schools: list[str],
    roster_file: Optional[str] = None,
    ela_csv: Optional[str] = None,
    math_csv: Optional[str] = None,
) -> list[dict]:
    """
    Return structured charter school data for all schools in known_schools.

    Parameters
    ----------
    known_schools : list of school names from S1 discovery (the community's schools)
    roster_file   : path to charter_roster.csv; falls back to module constant
    ela_csv       : path to ELA proficiency CSV; falls back to module constant
    math_csv      : path to Math proficiency CSV; falls back to module constant

    Return shape (one dict per school):
    {
        "school_name":           str,
        "authorizer":            str,
        "grades_served":         str,
        "enrollment_cap":        str | None,
        "phone":                 str | None,
        "contact_email":         str | None,
        "address":               str | None,
        "ela_proficiency_pct":   float | None,   # from PED CSV (school-level)
        "math_proficiency_pct":  float | None,
        "proficiency_year":      int | None,
        "source_class":          "PED_DATA",
        "source_url":            str,
        "source_title":          str,
        "confidence":            "HIGH",
        "contact_verification_required": True,
    }
    """
    roster_path = roster_file or ROSTER_CSV
    ela_path    = ela_csv  or ELA_CSV
    math_path   = math_csv or MATH_CSV

    if not known_schools:
        return []

    # Load proficiency maps once
    ela_map  = _load_proficiency_map(ela_path)
    math_map = _load_proficiency_map(math_path)

    known_set = set(known_schools)
    schools_out: list[dict] = []

    try:
        with open(roster_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = row.get("Charter School", "").strip()
                if name not in known_set:
                    continue

                email = row.get("Administrator Email", "").strip() or None
                # Prefer 2nd email only if primary is absent
                if not email:
                    email = row.get("2nd Administrator Email", "").strip() or None

                ela  = _match_proficiency(name, ela_map)
                math = _match_proficiency(name, math_map)

                schools_out.append({
                    "school_name":           name,
                    "authorizer":            row.get("Authorizer", "").strip() or "—",
                    "grades_served":         row.get("Grades Served 2025-26", "").strip() or "—",
                    "enrollment_cap":        row.get("Enrollment Cap", "").strip() or None,
                    "phone":                 row.get("Phone Number", "").strip() or None,
                    "contact_email":         email,
                    "address":               row.get("Street Address", "").strip() or None,
                    "ela_proficiency_pct":   ela,
                    "math_proficiency_pct":  math,
                    "proficiency_year":      PROFICIENCY_YEAR if (ela or math) else None,
                    "source_class":          "PED_DATA",
                    "source_url":            SOURCE_URL,
                    "source_title":          SOURCE_TITLE,
                    "confidence":            "HIGH",
                    "contact_verification_required": True,
                })

    except Exception as exc:
        log.warning("charter_schools_fetcher: roster read error — %s", exc)

    if schools_out:
        # Sort descending by ELA proficiency (nulls last) so Claude sees
        # the likely top performers first when selecting the top 5
        schools_out.sort(
            key=lambda s: (s["ela_proficiency_pct"] is None, -(s["ela_proficiency_pct"] or 0))
        )

    log.info(
        "charter_schools_fetcher: found %d schools (%d with proficiency data)",
        len(schools_out),
        sum(1 for s in schools_out if s["ela_proficiency_pct"] is not None),
    )
    return schools_out
