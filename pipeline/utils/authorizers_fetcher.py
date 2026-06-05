"""
pipeline/utils/authorizers_fetcher.py
Utility: read PED charter roster to extract unique authorizers active
in a given community.

Returns CSV-sourced data only (no API calls).
The S3 stage calls this, then enriches via a Claude call
(web search on standard/deep; knowledge fallback on fast).

DATA SOURCE:
  charter_roster.csv — Authorizer column, one row per school

Authorizer types found in NM:
  SEA  — Public Education Commission (statewide)
  LEA  — Local school district (e.g. Albuquerque Public Schools)
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from typing import Optional

log = logging.getLogger(__name__)

# TODO(S35-sweep): ROSTER_CSV, SOURCE_URL, SOURCE_TITLE, and _AUTHORIZER_TYPES
# are NM-specific. Derive from state param and config/states.yaml before
# expanding beyond NM.
ROSTER_CSV   = "data/raw/nm/charter_roster.csv"
SOURCE_URL   = "https://webnew.ped.state.nm.us/bureaus/options-for-parents/charter-schools/"
SOURCE_TITLE = "NM PED Charter School Roster SY 2025-26"

# Known NM authorizer types (supplement via Claude enrichment for unknowns)
_AUTHORIZER_TYPES: dict[str, str] = {
    "Public Education Commission": "SEA",
    "Albuquerque Public Schools":  "LEA",
    "Santa Fe Public Schools":     "LEA",
    "Las Cruces Public Schools":   "LEA",
    "Gallup-McKinley County Schools": "LEA",
    "Española Valley Schools":     "LEA",
    "Socorro Consolidated Schools": "LEA",
    "Truth or Consequences Municipal Schools": "LEA",
    "New Mexico Institute of Mining and Technology": "university",
    "New Mexico State University":                   "university",
    "University of New Mexico":                      "university",
}


# ── Public interface ──────────────────────────────────────────────────────────

def get_community_authorizers(
    known_schools: list[str],
    state: str = "NM",
    roster_file: Optional[str] = None,
) -> list[dict]:
    """
    Return unique authorizers for the given community's charter schools.

    Parameters
    ----------
    known_schools : list of school names from S1 discovery
    state         : two-letter state code; used to derive default CSV path
    roster_file   : path to charter_roster.csv; overrides state-derived default

    Return shape (one dict per authorizer):
    {
        "authorizer_name":                str,
        "type":                           str | None,  # SEA|LEA|university|independent
        "num_schools_in_community":       int,
        "source_class":                   "PED_DATA",
        "source_url":                     str,
        "source_title":                   str,
        "confidence":                     "HIGH",
        "contact_verification_required":  True,
    }
    """
    roster_path = roster_file or f"data/raw/{state.lower()}/charter_roster.csv"

    if not known_schools:
        return []

    known_set = set(known_schools)
    # authorizer_name → list of schools it authorizes in this community
    authorizer_schools: dict[str, list[str]] = defaultdict(list)

    try:
        with open(roster_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = row.get("Charter School", "").strip()
                auth = row.get("Authorizer", "").strip()
                if name in known_set and auth:
                    authorizer_schools[auth].append(name)

    except FileNotFoundError:
        log.debug(
            "authorizers_fetcher: roster not found at %s — returning empty for %s",
            roster_path, state,
        )
        return []
    except Exception as exc:
        log.warning("authorizers_fetcher: roster read error — %s", exc)
        return []

    result: list[dict] = []
    for auth_name, schools in authorizer_schools.items():
        auth_type = _AUTHORIZER_TYPES.get(auth_name)
        if auth_type is None:
            log.warning(
                "authorizers_fetcher: '%s' not in _AUTHORIZER_TYPES — type unknown",
                auth_name,
            )
            auth_type = "Unknown"
        result.append({
            "authorizer_name":               auth_name,
            "type":                          auth_type,
            "num_schools_in_community":      len(schools),
            "source_class":                  "PED_DATA",
            "source_url":                    SOURCE_URL,
            "source_title":                  SOURCE_TITLE,
            "confidence":                    "HIGH",
            "contact_verification_required": True,
        })

    # Sort: SEA first (PEC authorizes most schools statewide), then by count desc
    result.sort(key=lambda a: (
        0 if a["type"] == "SEA" else 1,
        -a["num_schools_in_community"],
    ))

    log.info(
        "authorizers_fetcher: found %d authorizers for community", len(result)
    )
    return result
