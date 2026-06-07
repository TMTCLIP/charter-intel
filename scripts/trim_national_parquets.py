#!/usr/bin/env python3
"""
scripts/trim_national_parquets.py

Trim national NCES parquets to only the columns used by CLIP.

Reads each parquet from data/raw/national/, drops unused columns, and writes
back with snappy compression.  Run once after build_national_parquet.py;
the trimmed files are safe to commit (~2–5 MB total).

Usage:
    python scripts/trim_national_parquets.py
    python scripts/trim_national_parquets.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas pyarrow", file=sys.stderr)
    sys.exit(1)

_DIR = "data/raw/national"

# ── Column keep-lists per parquet ─────────────────────────────────────────────
# Finance: indexed by LEAID (preserved as index)
#   Used by nces_fetcher._read_finance and _get_state_avg_ppr
_FINANCE_KEEP = {"STABBR", "MEMBERSCH", "TOTALREV", "TFEDREV", "TSTREV", "TLOCREV", "TOTALEXP"}

# Directory: indexed by LEAID (preserved as index)
#   Used by build_community_registry.py (load_ccd with .parquet input)
_DIRECTORY_KEEP = {"LEA_NAME", "LCITY", "LSTATE", "STABBR", "MEMBERSCH", "has_charter_schools"}

# Lunch: no index
#   Used by nces_fetcher._aggregate_frl
_LUNCH_KEEP = {"LEAID", "DATA_GROUP", "TOTAL_INDICATOR", "DMS_FLAG", "LUNCH_PROGRAM", "STUDENT_COUNT"}

# Membership: no index
#   Used by population_trends_fetcher via build_nces_cache.py
_MEMBERSHIP_KEEP = {"LEAID", "STABBR", "TOTAL_INDICATOR", "DMS_FLAG", "STUDENT_COUNT", "_year"}


def _file_size_kb(path: str) -> int:
    return round(os.path.getsize(path) / 1024)


def _trim(
    name: str,
    keep: set[str],
    dry_run: bool,
) -> None:
    path = os.path.join(_DIR, f"{name}.parquet")
    if not os.path.exists(path):
        print(f"[SKIP] {name}.parquet — file not found")
        return

    size_before = _file_size_kb(path)
    df = pd.read_parquet(path)

    cols_before = list(df.columns)
    n_before = len(cols_before)

    # For index-keyed parquets (LEAID is index), columns does not include the index.
    # We only filter df.columns — the index is always preserved.
    present = set(df.columns)
    missing = keep - present
    if missing:
        print(f"  WARNING [{name}]: requested keep-cols not found in file — {sorted(missing)}")

    drop = present - keep
    trimmed = df.drop(columns=sorted(drop))

    cols_after = list(trimmed.columns)
    n_after = len(cols_after)

    if dry_run:
        dropped_list = sorted(drop)
        print(f"\n[dry-run] {name}.parquet")
        print(f"  Cols: {n_before} → {n_after}  (dropping {n_before - n_after})")
        if dropped_list:
            print(f"  Dropping: {dropped_list}")
        print(f"  Keeping:  {sorted(cols_after)}")
        return

    trimmed.to_parquet(path, compression="snappy")
    size_after = _file_size_kb(path)

    print(
        f"  {name}.parquet  "
        f"cols {n_before} → {n_after}  "
        f"size {size_before:,} KB → {size_after:,} KB"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Trim national NCES parquets to used columns.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change; do not write files")
    args = parser.parse_args()

    mode = "DRY RUN" if args.dry_run else "TRIMMING"
    print(f"{mode} national parquets in {_DIR}/\n")

    _trim("nces_lea_finance",    _FINANCE_KEEP,    args.dry_run)
    _trim("nces_lea_directory",  _DIRECTORY_KEEP,  args.dry_run)
    _trim("nces_sch_lunch",      _LUNCH_KEEP,      args.dry_run)
    _trim("nces_lea_membership", _MEMBERSHIP_KEEP, args.dry_run)

    if not args.dry_run:
        print("\nDone. Verify with: python scripts/trim_national_parquets.py --dry-run")


if __name__ == "__main__":
    main()
