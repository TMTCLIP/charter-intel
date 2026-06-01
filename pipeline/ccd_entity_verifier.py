"""
pipeline/ccd_entity_verifier.py
Stage 4 helper: live NCES CCD charter-roster verification for named-entity facts.

Called by pipeline/s4_verification.py (Pass D) ONLY. It validates any fact that
names a specific school/operator against the actual federal charter registry, so
that hallucinated "local operators" (e.g. a national CMO asserted to operate
locally with no campus on the roster) can be demoted.

DESIGN
  - State-agnostic: no hardcoded school or operator lists. The district LEAID
    comes from config/states.yaml (per-state `nces_district_map`); if absent we
    fall back to a city/state name search against the same API.
  - Free + keyless: Urban Institute Education Data Portal, which wraps NCES CCD.
      GET https://educationdata.urban.org/api/v1/schools/ccd/directory/{year}/
    (Same endpoint family already used by ccd_closures_fetcher.py. The `leaid`
    and `charter` query params are honored by the endpoint.)
  - Graceful degradation: this module NEVER raises to its caller. On any network
    failure it returns None so S4 can skip Pass D and leave facts untouched.

CACHE
  data/cache/community/{state_lower}/{cid}/ccd_charter_roster.json  (TTL 30 days)
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

# ── API config ───────────────────────────────────────────────────────────────
CCD_DIR_BASE = "https://educationdata.urban.org/api/v1/schools/ccd/directory"
SOURCE_LABEL = "NCES CCD via Urban Institute API"
ROSTER_TTL_DAYS = 30
# Most-recent-first; the directory dataset trails real time. We stop at the first
# year that returns rows so a single live year suffices.
ROSTER_YEARS = [2023, 2022, 2021]
_HTTP_TIMEOUT = 30
_PAGE_CAP = 10000  # safety: never spin forever following `next`

# Standard US state FIPS — universal reference data, not an entity list. Used only
# for the name-search fallback when a community has no LEAID in states.yaml.
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}

# Generic education phrases that are NOT distinctive entity names. Compared by
# exact (case-insensitive) match so real names like "East Mountain High School"
# are still extracted.
_GENERIC_TERMS = {
    "charter school", "public school", "school district", "education commission",
    "public education", "high school", "elementary school", "middle school",
    "community school",
}

# Words that, when they precede a capitalized token, signal an operator/CMO name.
_CMO_SIGNALS = ("operators", "operator", "cmo", "network", "managed by")


# ─────────────────────────────────────────────────────────────────────────────
# Entity extraction
# ─────────────────────────────────────────────────────────────────────────────
def extract_named_entities(claim: str) -> list[str]:
    """Return candidate school/operator names mentioned in a claim string.

    Heuristics:
      1. Title-case multi-word runs (2+ capitalized words), excluding generic
         education phrases by exact match.
      2. A capitalized token (incl. all-caps acronyms like KIPP) immediately
         following a CMO signal word ("operator(s)", "CMO", "network",
         "managed by"), skipping intervening punctuation/whitespace.
    Returns [] when nothing matches.
    """
    if not claim:
        return []
    candidates: list[str] = []

    # 1. Title-case multi-word runs. Every word must start with a letter (so
    #    grant/program codes like "CFDA 84.282" or "CIP 13" are NOT treated as
    #    school names).
    for m in re.finditer(r"\b([A-Z][A-Za-z'&.]+(?:\s+[A-Z][A-Za-z'&.]+)+)", claim):
        cand = m.group(1).strip()
        if cand.lower() not in _GENERIC_TERMS:
            candidates.append(cand)

    # 2. Capitalized token following a CMO signal word. The signal is matched
    #    case-insensitively, but the captured token must be genuinely
    #    capitalized in the source (guards against "no CMO presence" → "presence").
    for sig in _CMO_SIGNALS:
        for m in re.finditer(
            r"\b" + re.escape(sig) + r"\b[\s:,\-–—]*([A-Za-z][A-Za-z'&.]+)",
            claim,
            flags=re.IGNORECASE,
        ):
            tok = m.group(1).strip()
            if tok[:1].isupper() and tok.lower() not in _GENERIC_TERMS:
                candidates.append(tok)

    # Dedup, preserve order (case-insensitive).
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Roster fetch
# ─────────────────────────────────────────────────────────────────────────────
def _roster_cache_path(state_abbr: str, cid: str) -> str:
    return f"data/cache/community/{state_abbr.lower()}/{cid}/ccd_charter_roster.json"


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "clip-ccd-entity-verifier"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _fetch_pages(url: Optional[str]) -> list[dict]:
    """Follow the API's `next` pagination. Raises on network/HTTP error."""
    rows: list[dict] = []
    while url:
        data = _http_get_json(url)
        rows.extend(data.get("results", []))
        url = data.get("next")
        if len(rows) > _PAGE_CAP:
            log.warning("CCD roster: page cap (%d) hit — truncating", _PAGE_CAP)
            break
    return rows


def _fetch_rows(leaid: Optional[str], state_abbr: str, city: Optional[str]):
    """Return (rows, year, mode) or None on network failure.

    rows may be an empty list (valid 'no charter schools' result).
    mode is 'leaid' (Option B) or 'name' (Option A fallback).
    """
    if leaid:
        for year in ROSTER_YEARS:
            url = f"{CCD_DIR_BASE}/{year}/?leaid={leaid}&charter=1"
            try:
                rows = _fetch_pages(url)
            except Exception as exc:  # network/HTTP — treat as unreachable
                log.warning("CCD roster fetch failed (leaid=%s year=%s): %s", leaid, year, exc)
                return None
            if rows:
                return rows, year, "leaid"
        return [], ROSTER_YEARS[0], "leaid"

    # Fallback: city/state name search (Option A).
    fips = STATE_FIPS.get((state_abbr or "").upper())
    if not fips:
        log.warning("No FIPS for state %r — cannot run CCD name-search fallback", state_abbr)
        return [], ROSTER_YEARS[0], "name"
    city_norm = (city or "").strip().lower()
    for year in ROSTER_YEARS:
        url = f"{CCD_DIR_BASE}/{year}/?fips={fips}&charter=1"
        try:
            rows = _fetch_pages(url)
        except Exception as exc:
            log.warning("CCD roster name-search failed (fips=%s year=%s): %s", fips, year, exc)
            return None
        if rows:
            if city_norm:
                rows = [r for r in rows if (r.get("city_location") or "").strip().lower() == city_norm]
            return rows, year, "name"
    return [], ROSTER_YEARS[0], "name"


def _fetch_statewide_city_charters(city: str, state_fips, year: int) -> list[dict]:
    """Statewide charter pull filtered to a city's location.

    This catches state-authorized (e.g. NM PEC) charters that live under their
    own LEAIDs and are therefore invisible to a single-district roster. Same
    error contract as the rest of the module: never raises, returns [] on
    failure.
    """
    if not state_fips or not city:
        return []
    city_norm = str(city).strip().lower()
    url = f"{CCD_DIR_BASE}/{year}/?fips={state_fips}&charter=1"
    try:
        rows = _fetch_pages(url)
    except Exception as exc:
        log.warning("CCD statewide city pull failed (fips=%s year=%s): %s", state_fips, year, exc)
        return []
    out: list[dict] = []
    for r in rows:
        if r.get("charter") != 1:
            continue
        if (r.get("city_location") or "").strip().lower() != city_norm:
            continue
        name = r.get("school_name")
        if not name:
            continue
        out.append({
            "name": name,
            "ncessch": r.get("ncessch"),
            "leaid": r.get("leaid"),
            "enrollment": r.get("enrollment"),
        })
    return out


def fetch_charter_roster(
    city: Optional[str],
    state_abbr: str,
    cid: str,
    leaid: Optional[str] = None,
    state_fips=None,
    force_refresh: bool = False,
) -> Optional[dict]:
    """Fetch (or load cached) charter roster for a community.

    The roster is the UNION of:
      - district-LEAID charters (Option B), and
      - statewide charters whose city_location matches the community city
        (catches PEC / state-authorized charters under other LEAIDs).
    Deduplicated by ncessch.

    Returns a roster dict, or None if the API is unreachable (caller must then
    skip entity verification). An empty roster (no charter schools) is a valid,
    non-None result.
    """
    cache_path = _roster_cache_path(state_abbr, cid)

    # Cache read (TTL 30 days).
    if not force_refresh and os.path.exists(cache_path):
        age_days = (time.time() - os.path.getmtime(cache_path)) / 86400.0
        if age_days < ROSTER_TTL_DAYS:
            try:
                with open(cache_path) as f:
                    return json.load(f)
            except Exception as exc:
                log.warning("CCD roster cache unreadable (%s) — refetching: %s", cache_path, exc)

    fips = state_fips or STATE_FIPS.get((state_abbr or "").upper())

    fetched = _fetch_rows(leaid, state_abbr, city)
    if fetched is None:
        return None  # unreachable → caller skips Pass D
    rows, year, mode = fetched

    # Merge district + statewide-city charters, dedup by ncessch.
    by_key: dict = {}
    order: list = []
    district_name = None

    def _add(entry: dict) -> None:
        key = entry.get("ncessch") or (entry.get("name") or "").lower()
        if key and key not in by_key:
            by_key[key] = entry
            order.append(key)

    for r in rows:
        if r.get("charter") != 1:
            continue
        name = r.get("school_name")
        if not name:
            continue
        _add({
            "name": name,
            "ncessch": r.get("ncessch"),
            "leaid": r.get("leaid"),
            "enrollment": r.get("enrollment"),
        })
        if district_name is None:
            district_name = r.get("lea_name")

    lookup_modes = [mode]
    # Augment with statewide-by-city only when the district path ran ('leaid').
    # In 'name' mode, _fetch_rows already performed a city-filtered statewide pull.
    if mode == "leaid" and fips and city:
        for entry in _fetch_statewide_city_charters(city, fips, year):
            _add(entry)
        lookup_modes.append("statewide_city")

    schools = [by_key[k] for k in order]

    if not schools:
        log.warning(
            "CCD roster: no charter schools returned for %s (leaid=%s, modes=%s, year=%s)",
            cid, leaid, lookup_modes, year,
        )

    roster = {
        "leaid": leaid,
        "state_fips": fips,
        "district_name": district_name,
        "charter_schools": schools,
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "year": year,
        "lookup_modes": lookup_modes,
        "source": SOURCE_LABEL,
    }

    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(roster, f, indent=2)
    except Exception as exc:
        log.warning("Could not write CCD roster cache %s: %s", cache_path, exc)

    return roster
