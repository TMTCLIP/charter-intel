#!/usr/bin/env python3
"""
scripts/trim_nces_csv.py

Trim an NCES national CSV to rows matching a single state, reducing file size
and cold-start seeding time for state-specific deployments.

Usage:
    python3 scripts/trim_nces_csv.py --state NM --file data/raw/nm/nces_sch_lunch_2024.csv
    python3 scripts/trim_nces_csv.py --state TX --file data/raw/tx/nces_sch_lunch_2024.csv --out /tmp/tx_lunch.csv

The script auto-detects the state column (ST, LSTATE, or FIPST) from the CSV
header.  --state accepts a two-letter code (e.g. NM, TX, AZ).  No state or
path is hardcoded.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

# FIPS codes for the 50 states + DC + territories — used only when the CSV
# carries FIPST instead of a two-letter abbreviation.
_FIPS_TO_ST: dict[str, str] = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR",
    "78": "VI",
}
_ST_TO_FIPS: dict[str, str] = {v: k for k, v in _FIPS_TO_ST.items()}

# Ordered preference: first match wins.
_STATE_COL_CANDIDATES = ("ST", "LSTATE", "FIPST", "STATENAME")


def _detect_state_col(fieldnames: list[str]) -> str | None:
    upper = {f.upper(): f for f in fieldnames}
    for candidate in _STATE_COL_CANDIDATES:
        if candidate in upper:
            return upper[candidate]
    return None


def _row_matches(row: dict, col: str, col_upper: str, state: str, fips: str | None) -> bool:
    val = row.get(col, "").strip().upper()
    if col_upper in ("ST", "LSTATE"):
        return val == state
    if col_upper == "FIPST":
        return val == (fips or "")
    if col_upper == "STATENAME":
        # Accept full name match for states that embed name — not a robust path
        # but serves as fallback.
        return False  # we rely on ST/LSTATE/FIPST; STATENAME is a last resort
    return False


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Trim an NCES national CSV to state-only rows."
    )
    parser.add_argument("--state", required=True,
                        help="Two-letter state code, e.g. NM")
    parser.add_argument("--file", required=True,
                        help="Path to the source CSV (will be overwritten unless --out is given)")
    parser.add_argument("--out",
                        help="Write trimmed CSV here instead of overwriting --file")
    args = parser.parse_args(argv)

    state = args.state.strip().upper()
    src_path = args.file
    dst_path = args.out or src_path

    if not os.path.isfile(src_path):
        print(f"ERROR: file not found: {src_path}", file=sys.stderr)
        return 1

    fips = _ST_TO_FIPS.get(state)

    # --- Read header to find state column ---
    with open(src_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        state_col = _detect_state_col(fieldnames)
        if state_col is None:
            print(
                f"ERROR: no recognisable state column found in {src_path}.\n"
                f"  Columns present: {fieldnames}\n"
                f"  Expected one of: {_STATE_COL_CANDIDATES}",
                file=sys.stderr,
            )
            return 1

        state_col_upper = state_col.upper()
        print(f"State column detected: {state_col!r}")

        # Count + collect matching rows
        before_rows = 0
        kept_rows: list[dict] = []
        for row in reader:
            before_rows += 1
            if _row_matches(row, state_col, state_col_upper, state, fips):
                kept_rows.append(row)

    before_bytes = os.path.getsize(src_path)
    after_rows = len(kept_rows)

    if after_rows == 0:
        print(
            f"WARNING: 0 rows matched state={state!r} in column {state_col!r}. "
            f"No output written.",
            file=sys.stderr,
        )
        return 1

    # --- Write trimmed CSV ---
    tmp_path = dst_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept_rows)

    os.replace(tmp_path, dst_path)
    after_bytes = os.path.getsize(dst_path)

    print(
        f"Trimmed {src_path!r} → {dst_path!r}\n"
        f"  Rows:  {before_rows:,} → {after_rows:,} "
        f"({after_rows / before_rows * 100:.1f}% kept)\n"
        f"  Size:  {_fmt_bytes(before_bytes)} → {_fmt_bytes(after_bytes)} "
        f"({_fmt_bytes(before_bytes - after_bytes)} saved)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
