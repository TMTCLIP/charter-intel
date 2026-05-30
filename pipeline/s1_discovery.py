"""
pipeline/s1_discovery.py
Stage 1: Community Discovery

PURPOSE:
  Parse the state's charter school roster and identify all charter-active
  communities (grouped by municipality). No LLM is used. Pure data processing.

INPUT:
  - State charter school roster (downloaded CSV/Excel from state PED)
  - states.yaml (community boundary rules)

OUTPUT:
  - data/processed/{state}/community_list.json
  - A list of CommunityStub objects, each identifying a unique charter-active municipality

NOTES:
  - This stage is idempotent. Re-running it with the same source data produces
    identical output.
  - The roster must be downloaded manually (or via a state-specific scraper)
    and placed in data/raw/{state}/charter_roster.csv before running.
  - This stage does NOT fetch the roster from the internet.
"""

from __future__ import annotations
import csv
import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Optional

from pipeline import (
    PipelineConfig, StageResult, StageStatus, ValidationResult,
    build_community_id, today_str
)
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema


STAGE_ID = "s1_discovery"


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class SchoolStub:
    """Minimal school record from the PED roster."""
    school_name: str
    state: str
    city: str
    county: Optional[str]
    authorizer_name: Optional[str]
    grades: Optional[str]
    school_type: str          # "CHARTER" always for this pipeline
    status: str               # "ACTIVE" | "PENDING" | "CLOSED"
    source_row: int           # row number in source CSV for debugging
    nces_id: Optional[str] = None  # NCES school ID if available in roster


@dataclass
class CommunityStub:
    """Minimal community record — expanded by later stages."""
    community_id: str
    name: str
    state: str
    school_count: int
    school_names: list[str]
    county: Optional[str]
    boundary_type: str        # from states.yaml
    discovered_at: str
    source_file: str


# ─────────────────────────────────────────────
# FIELD MAPPING
# ─────────────────────────────────────────────
# Maps state-specific column names to canonical field names.
# Add a new entry when onboarding a new state.

ROSTER_FIELD_MAPS: dict[str, dict[str, str]] = {
    "NM": {
        "school_name":    "Charter School",
        "authorizer":     "Authorizer",
        "grades":         "Grades Served 2025-26",
        # nces_id: absent in NM roster — NCES data joined separately via nces_ccd_schools_nm.csv
        # city: absent — parsed from "Street Address" by _city_from_address()
        # county: absent — defaults to ""
        # status: absent — defaults to ACTIVE
    },
    # Add additional states here
}


# ─────────────────────────────────────────────
# MAIN STAGE FUNCTION
# ─────────────────────────────────────────────

def run(
    community_id: str,  # Not used in S1 — discovery runs at state level
    state: str,
    config: PipelineConfig,
    **kwargs
) -> StageResult:
    """
    Discover all charter-active communities in the given state.
    
    Returns a StageResult where output_data contains a list of CommunityStub dicts.
    Also writes community_list.json to data/processed/{state}/.
    """
    start = _now()
    roster_path = _roster_path(state)

    # --- Validate prerequisites ---
    validation = validate_prerequisites(state, roster_path)
    if not validation:
        return StageResult(
            stage_id=STAGE_ID,
            community_id="ALL",
            state=state,
            status=StageStatus.ERROR,
            errors=validation.errors
        )

    # --- Check cache ---
    cache = CacheManager(config)
    cache_key = f"state/{state}/s1_community_list_{today_str()}.json"
    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            return StageResult(
                stage_id=STAGE_ID, community_id="ALL", state=state,
                status=StageStatus.SUCCESS, output_data=cached,
                cache_hit=True
            )

    # --- Parse roster ---
    schools = parse_roster(roster_path, state)
    communities = group_by_community(schools, state)

    # --- Write and cache the FULL unfiltered community list ---
    # The filter must NOT touch the disk file or cache.  If it did, a
    # subsequent --all run would read a filtered (possibly empty) list.
    # The in-memory filter below is applied only to the returned output_data.
    full_output = {
        "state": state,
        "discovered_at": today_str(),
        "source_file": roster_path,
        "total_schools": len(schools),
        "total_communities": len(communities),
        "communities": [asdict(c) for c in communities]
    }

    out_path = _output_path(state)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(full_output, f, indent=2)

    cache.set(cache_key, full_output)

    # --- Apply community filter in-memory only (does not affect disk or cache) ---
    if config.communities:
        communities = [c for c in communities if c.community_id in config.communities]

    output = {
        **full_output,
        "total_communities": len(communities),
        "communities": [asdict(c) for c in communities],
    }

    duration = _elapsed(start)
    return StageResult(
        stage_id=STAGE_ID,
        community_id="ALL",
        state=state,
        status=StageStatus.SUCCESS,
        output_path=out_path,
        output_data=output,
        duration_seconds=duration
    )


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def parse_roster(roster_path: str, state: str) -> list[SchoolStub]:
    """Parse the state CSV roster into SchoolStub objects."""
    field_map = ROSTER_FIELD_MAPS.get(state, {})
    schools = []

    with open(roster_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # Row 1 = header
            city_col = field_map.get("city")
            if city_col:
                city = _clean_city(row.get(city_col, "").strip())
            else:
                city = _city_from_address(row.get("Street Address", ""))
            status_col = field_map.get("status")
            status = row.get(status_col, "ACTIVE").strip().upper() if status_col else "ACTIVE"

            if status == "CLOSED":
                continue  # Exclude closed schools from active analysis

            if not city:
                # TODO: log and route to manual review
                continue

            nces_id_col = field_map.get("nces_id")
            nces_id = row.get(nces_id_col, "").strip() if nces_id_col else None

            schools.append(SchoolStub(
                school_name=row.get(field_map.get("school_name", "School Name"), "").strip(),
                state=state,
                city=city,
                county=row.get(field_map.get("county", "County"), "").strip() or None,
                authorizer_name=row.get(field_map.get("authorizer", "Authorizer"), "").strip() or None,
                grades=row.get(field_map.get("grades", "Grades"), "").strip() or None,
                school_type="CHARTER",
                status=status,
                source_row=i,
                nces_id=nces_id or None,
            ))

    return schools


def group_by_community(schools: list[SchoolStub], state: str) -> list[CommunityStub]:
    """Group schools by municipality and return CommunityStub list."""
    grouped: dict[str, list[SchoolStub]] = {}

    for school in schools:
        cid = build_community_id(school.city, state)
        if cid not in grouped:
            grouped[cid] = []
        grouped[cid].append(school)

    communities = []
    for cid, school_list in sorted(grouped.items()):
        school_list = _dedup_school_list(school_list)
        sample = school_list[0]
        communities.append(CommunityStub(
            community_id=cid,
            name=sample.city,
            state=state,
            school_count=len(school_list),
            school_names=[s.school_name for s in school_list],
            county=sample.county,
            boundary_type="MUNICIPALITY",
            discovered_at=today_str(),
            source_file=_roster_path(state)
        ))

    return communities


def _dedup_school_list(schools: list[SchoolStub]) -> list[SchoolStub]:
    """Remove duplicate school rows within a per-city group.

    Primary key: nces_id — two rows with the same non-empty NCES ID are the
    same physical school (e.g. duplicate CSV rows from a PED export artifact).
    Fallback: normalized (lowercase, stripped) name — catches re-listings of
    the same school with minor formatting differences.

    City is already identical for every row in the list (they share a community
    group), so name alone is sufficient as the fallback key.

    Keeps first occurrence; preserves order. Does NOT collapse schools in
    different cities — that separation is enforced upstream by grouping.
    """
    seen_nces: set[str] = set()
    seen_names: set[str] = set()
    deduped = []
    for school in schools:
        if school.nces_id:
            if school.nces_id in seen_nces:
                continue
            seen_nces.add(school.nces_id)
        norm_name = school.school_name.lower().strip()
        if norm_name in seen_names:
            continue
        seen_names.add(norm_name)
        deduped.append(school)
    return deduped


def validate_prerequisites(state: str, roster_path: str) -> ValidationResult:
    """Check that inputs are present before running."""
    errors = []
    if not os.path.exists(roster_path):
        errors.append(
            f"Charter roster not found at {roster_path}. "
            f"Download from the state PED site (see config/states.yaml) "
            f"and place at this path before running S1."
        )
    if state not in ROSTER_FIELD_MAPS:
        errors.append(
            f"No field mapping defined for state '{state}' in ROSTER_FIELD_MAPS. "
            f"Add an entry in s1_discovery.py."
        )
    return ValidationResult(valid=len(errors) == 0, errors=errors)


def _clean_city(city: str) -> str:
    """Normalize city strings — strip whitespace, title-case, remove suffixes."""
    city = city.strip().title()
    # Remove common suffixes that appear in PED data
    city = re.sub(r"\s*(,\s*NM|,\s*New Mexico)\s*$", "", city, flags=re.IGNORECASE)
    return city


def _strip_address_artifacts(city: str) -> str:
    """Remove leading address artifacts from a parsed city string.

    The NM PED address parser sometimes leaves behind street numbers,
    highway identifiers, or building/suite codes between a recognized
    street suffix and the true city name.  This pass strips those tokens
    so the community_id resolves correctly.

    Examples (before → after):
        "344 Edgewood"          → "Edgewood"
        "4386 Shiprock"         → "Shiprock"
        "156 Navajo"            → "Navajo"
        "US-70 Alamogordo"      → "Alamogordo"
        "NM Hwy 564 Gallup"     → "Gallup"
        "NM 602 Gallup"         → "Gallup"
        "Bld. 2B Albuquerque"   → "Albuquerque"
        "Bldg. 2 Albuquerque"   → "Albuquerque"
        "Ste C, Albuquerque"    → "Albuquerque"
        "2B Albuquerque"        → "Albuquerque"
        "4 Jemez Pueblo"        → "Jemez Pueblo"
    """
    # 1. Strip leading building / suite codes — must run before number strip.
    #    Pattern: keyword (Bld., Bldg., Ste., Suite, Apt., Unit) followed by
    #    a code token (e.g., "2B", "C,", "100") and optional trailing comma.
    city = re.sub(
        r"(?i)^\s*(?:Bld\.?|Bldg\.?|Ste\.?|Suite|Apt\.?|Unit)\s+\S+\s*,?\s*",
        "", city,
    )

    # 2. Strip leading highway / route identifiers.
    #    Covers: "US-70", "NM Hwy 564", "NM 602", "State Hwy 9", "Hwy 344"
    city = re.sub(
        r"(?i)^\s*(?:US|NM|State)\s*-?\s*(?:Hwy\.?|Highway|Route|Rte\.?)?\s*\d+\s*",
        "", city,
    )
    city = re.sub(
        r"(?i)^\s*(?:Hwy\.?|Highway|Route|Rte\.?)\s*\d+\s*",
        "", city,
    )

    # 3. Strip leading numbers or number+letter codes (e.g., "344", "4386", "2B").
    #    One digit-sequence with an optional trailing letter, then whitespace.
    city = re.sub(r"^\s*\d+[A-Za-z]?\s*,?\s*", "", city)

    # 4. Strip lone single-letter suite code left after prior passes
    #    (e.g., "C, Albuquerque" → "Albuquerque").
    city = re.sub(r"^\s*[A-Za-z]\s*,\s*", "", city)

    return city.strip()


# Word-level set used by _city_from_address() — compared after stripping
# all non-alpha characters so punctuated tokens like "Blvd," or "St." match.
_STREET_SUFFIXES: frozenset = frozenset({
    "ave", "avenue", "st", "street", "rd", "road",
    "blvd", "boulevard", "dr", "drive", "ln", "lane",
    "way", "ct", "court", "pl", "place", "hwy", "highway",
    "trail", "trl", "circle", "cir", "loop", "pass",
    "run", "pike", "pkwy", "parkway",
    # Compass directionals
    "ne", "nw", "se", "sw",
    # Additional common tokens
    "path", "row",
    # NM Spanish address tokens — street-name words that appear immediately
    # before the city name in NM PED roster addresses. "Arbolera" is the
    # street name in "515 Calle Arbolera Española" (McCurdy Charter School);
    # without this entry the fallback heuristic produces "Arbolera Española"
    # instead of "Española".
    "arbolera",
})


def _city_from_address(address: str) -> str:
    """Extract city from NM PED address format.

    Works backwards through the pre-state address words and stops at the
    first street suffix or compass directional. Everything after that word
    is the city name. Non-alpha characters (commas, periods) are stripped
    from each word before the set lookup so that tokens like 'Blvd,' or
    'St.' match correctly.

    Examples:
        '4300 Cutler Ave NE Albuquerque, NM 87110'    → 'Albuquerque'
        '74 A Van Nu Po Road Santa Fe, NM 87508'      → 'Santa Fe'
        '123 Main St Las Cruces, NM 88001'             → 'Las Cruces'
        '456 Rio Rancho Blvd Rio Rancho, NM 87124'    → 'Rio Rancho'
        '850 N. Telshor Blvd, Las Cruces, NM 88011'  → 'Las Cruces'
        '7300 Old Santa Fe Trail, Santa Fe, NM 87505' → 'Santa Fe'
        '515 Calle Arbolera Española, NM 87532'       → 'Española'
    """
    parts = re.split(r",\s*NM\b", address, maxsplit=1)
    if len(parts) < 2:
        return ""
    pre_state = parts[0].strip()
    words = pre_state.split()

    # Scan right-to-left; stop at the rightmost suffix/directional word.
    # Strip all non-alpha characters before the set lookup so punctuated
    # tokens ("Blvd,", "St.", "Rd.") match the same as clean tokens.
    for i in range(len(words) - 1, -1, -1):
        token = re.sub(r"[^a-zA-Z]", "", words[i]).lower()
        if token in _STREET_SUFFIXES:
            city_words = words[i + 1:]
            if city_words:
                return _clean_city(_strip_address_artifacts(" ".join(city_words)))
            break  # suffix at tail with nothing after — fall through

    # Fallback: no recognizable suffix found.
    # Last two words are the best heuristic for NM cities (most are 1–2 words).
    raw = " ".join(words[-2:]) if len(words) >= 2 else pre_state
    return _clean_city(_strip_address_artifacts(raw))


def _roster_path(state: str) -> str:
    return f"data/raw/{state.lower()}/charter_roster.csv"


def _output_path(state: str) -> str:
    return f"data/processed/{state.lower()}/community_list.json"


def _now() -> float:
    import time
    return time.time()


def _elapsed(start: float) -> float:
    import time
    return round(time.time() - start, 2)
