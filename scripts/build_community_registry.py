#!/usr/bin/env python3
"""
scripts/build_community_registry.py

Build a per-state community registry YAML from an NCES CCD LEA directory CSV.

Usage:
    python3 scripts/build_community_registry.py \
        --state MS \
        --ccd-file data/raw/ms/nces_lea_dir_2024.csv \
        --roster data/raw/ms/charter_roster.csv \
        --floor 500

See config/community_registry/README.md for schema documentation.

This script must NOT import from the pipeline/ directory.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import unicodedata
from typing import Optional

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────
# COLUMN NAME CONSTANTS
# Expected NCES CCD LEA directory column names, in preference order.
# The script warns if a column cannot be found and skips that field.
# ─────────────────────────────────────────────

_LEAID_CANDIDATES    = ("LEAID",)
_NAME_CANDIDATES     = ("LEA_NAME", "NAME")
_CITY_CANDIDATES     = ("LCITY", "MCITY", "CITY")
_STATE_CANDIDATES    = ("LSTATE", "ST", "STABBR")
_ENROLL_CANDIDATES   = ("ENROLLMENT", "TOTAL_STUDENTS", "MEMBERSCH", "V33")

DEFAULT_FLOOR = 500


# ─────────────────────────────────────────────
# SLUG HELPERS
# ─────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convert a city name to a URL-safe, ASCII lowercase slug."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_str = normalized.encode("ascii", "ignore").decode("ascii")
    slug = ascii_str.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _community_id(state: str, city: str, suffix: Optional[str] = None) -> str:
    slug = _slugify(city)
    cid = f"{state.lower()}-{slug}"
    if suffix:
        cid = f"{cid}-{suffix}"
    return cid


# ─────────────────────────────────────────────
# CSV HELPERS
# ─────────────────────────────────────────────

def _detect_col(fieldnames: list[str], candidates: tuple[str, ...], label: str) -> Optional[str]:
    """Return the first matching column name (case-insensitive). Warn if none found."""
    upper_map = {f.upper(): f for f in fieldnames}
    for c in candidates:
        if c.upper() in upper_map:
            return upper_map[c.upper()]
    print(f"WARNING: could not find {label} column (tried: {list(candidates)})", file=sys.stderr)
    return None


# ─────────────────────────────────────────────
# CCD LOADER
# ─────────────────────────────────────────────

def load_ccd(ccd_path: str, state: str, floor: int) -> tuple[list[dict], int]:
    """
    Read NCES CCD LEA directory CSV, filter to state and floor.

    Returns (district_rows, excluded_count).
    Each row dict has keys: leaid, lea_name, city, enrollment.
    """
    with open(ccd_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        col_leaid  = _detect_col(fieldnames, _LEAID_CANDIDATES,  "NCES LEA ID")
        col_name   = _detect_col(fieldnames, _NAME_CANDIDATES,   "district name")
        col_city   = _detect_col(fieldnames, _CITY_CANDIDATES,   "city")
        col_state  = _detect_col(fieldnames, _STATE_CANDIDATES,  "state")
        col_enroll = _detect_col(fieldnames, _ENROLL_CANDIDATES, "enrollment")

        rows = []
        excluded = 0

        for row in reader:
            # Filter to the target state
            if col_state:
                row_state = row.get(col_state, "").strip().upper()
                if row_state != state.upper():
                    continue

            leaid    = row.get(col_leaid, "").strip()  if col_leaid  else ""
            lea_name = row.get(col_name,  "").strip()  if col_name   else ""
            city     = row.get(col_city,  "").strip()  if col_city   else ""

            enrollment = 0
            if col_enroll:
                raw = row.get(col_enroll, "") or ""
                try:
                    enrollment = int(str(raw).strip())
                except (ValueError, TypeError):
                    enrollment = 0

            if not leaid or not city:
                continue

            if enrollment < floor:
                excluded += 1
                continue

            rows.append({
                "leaid":    leaid,
                "lea_name": lea_name,
                "city":     city,
                "enrollment": enrollment,
            })

    return rows, excluded


# ─────────────────────────────────────────────
# ROSTER LOADER
# ─────────────────────────────────────────────

def load_roster_signals(roster_path: str) -> tuple[set[str], set[str]]:
    """
    Parse a charter roster CSV and return two sets for has_charters matching:
      - district_names_lower: normalized district names
      - cities_lower: normalized city names

    Looks for common column names across known state roster formats.
    """
    district_names: set[str] = set()
    cities: set[str] = set()

    _DISTRICT_COLS = ("District Name", "District", "LEA Name", "LEA_NAME", "LEANAME")
    _CITY_COLS     = ("City", "CITY", "LCITY", "MCITY", "Municipality")

    try:
        with open(roster_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []

            col_district = _detect_col(fieldnames, _DISTRICT_COLS, "district column in roster")
            col_city     = _detect_col(fieldnames, _CITY_COLS,     "city column in roster")

            # NM roster: extract city from Street Address as fallback
            has_address_col = "Street Address" in fieldnames

            for row in reader:
                if col_district:
                    val = row.get(col_district, "").strip().lower()
                    if val:
                        district_names.add(val)

                if col_city:
                    val = row.get(col_city, "").strip().lower()
                    if val:
                        cities.add(val)
                elif has_address_col:
                    # Parse city from NM-style "123 Main St City, NM 87000"
                    addr = row.get("Street Address", "")
                    parts = re.split(r",\s*[A-Z]{2}\b", addr, maxsplit=1)
                    if len(parts) >= 1:
                        words = parts[0].strip().split()
                        if words:
                            cities.add(words[-1].lower())

    except Exception as e:
        print(f"WARNING: could not fully parse roster {roster_path}: {e}", file=sys.stderr)

    return district_names, cities


# ─────────────────────────────────────────────
# REGISTRY BUILDER
# ─────────────────────────────────────────────

def build_registry(
    districts: list[dict],
    state: str,
    district_names: set[str],
    cities: set[str],
) -> dict:
    """
    Build registry dict from district rows.

    Disambiguates duplicate city names by appending the NCES LEA ID suffix.
    Sets has_charters=True for any district whose city or district name
    appears in the roster signals.
    """
    # Count city slugs to detect duplicates
    city_slug_count: dict[str, int] = {}
    for d in districts:
        slug = _slugify(d["city"])
        city_slug_count[slug] = city_slug_count.get(slug, 0) + 1

    registry: dict = {}
    for d in districts:
        slug = _slugify(d["city"])
        if city_slug_count[slug] > 1:
            cid = _community_id(state, d["city"], d["leaid"])
        else:
            cid = _community_id(state, d["city"])

        # Determine has_charters from roster signals
        has_charters = False
        if district_names or cities:
            if (
                d["city"].lower() in cities
                or d["lea_name"].lower() in district_names
            ):
                has_charters = True

        registry[cid] = {
            "display_name":    d["city"].title(),
            "state":           state.upper(),
            "district_nces_id": d["leaid"],
            "district_name":   d["lea_name"],
            "has_charters":    has_charters,
            "enrollment":      d["enrollment"],
            "source":          "ccd_auto",
        }

    return registry


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build community registry YAML from NCES CCD LEA directory CSV."
    )
    parser.add_argument("--state",    required=True,       help="2-letter state abbreviation, e.g. MS")
    parser.add_argument("--ccd-file", required=True,       help="Path to NCES CCD LEA directory CSV")
    parser.add_argument("--roster",   default=None,        help="Charter roster CSV (sets has_charters)")
    parser.add_argument("--floor",    type=int,
                        default=DEFAULT_FLOOR,             help=f"Min enrollment (default: {DEFAULT_FLOOR})")
    parser.add_argument("--output",   default=None,        help="Output YAML path (default: config/community_registry/{state_lower}.yaml)")
    parser.add_argument("--dry-run",  action="store_true", help="Print to stdout; do not write file")
    args = parser.parse_args()

    state = args.state.upper()
    output_path = args.output or f"config/community_registry/{state.lower()}.yaml"

    print(f"Building registry for {state}")
    print(f"  CCD file:         {args.ccd_file}")
    print(f"  Enrollment floor: {args.floor}")

    districts, excluded = load_ccd(args.ccd_file, state, args.floor)

    district_names: set[str] = set()
    cities: set[str] = set()
    if args.roster:
        print(f"  Roster:           {args.roster}")
        district_names, cities = load_roster_signals(args.roster)

    registry = build_registry(districts, state, district_names, cities)
    sorted_registry = dict(sorted(registry.items()))

    has_charters_count = sum(1 for v in sorted_registry.values() if v["has_charters"])
    total_found = len(districts) + excluded

    print(f"\nSummary:")
    print(f"  Districts found:          {total_found}")
    print(f"  Excluded by floor ({args.floor:>4}): {excluded}")
    print(f"  Included:                 {len(sorted_registry)}")
    print(f"  Has charters:             {has_charters_count}")

    if args.roster and cities:
        print(f"  Roster cities loaded:     {len(cities)}")

    if args.dry_run:
        print(f"\n[dry-run] Would write to: {output_path}")
        print(yaml.dump(sorted_registry, default_flow_style=False, sort_keys=True))
        return

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(sorted_registry, f, default_flow_style=False, sort_keys=True)

    print(f"\nWritten to: {output_path}")


if __name__ == "__main__":
    main()
