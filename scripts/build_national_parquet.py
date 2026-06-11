#!/usr/bin/env python3
"""
scripts/build_national_parquet.py

Build national parquet files from CSVs downloaded by download_nces_national.py.
Also converts the existing (already-national) nces_lea_finance_2024.csv.

Usage:
    python scripts/build_national_parquet.py
    python scripts/build_national_parquet.py --force

Parquet files produced in --output-dir (default: data/raw/national/):
    nces_lea_directory.parquet  — district directory with has_charter_schools column
    nces_lea_finance.parquet    — district finance / per-pupil revenue data
    nces_sch_lunch.parquet      — school-level FRL in NCES filter-compatible format
    nces_lea_membership.parquet — district enrollment by year (2020/2022/2023/2024)
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

from pipeline.utils.data_config import NCES_CCD_YEAR, NCES_MEMBERSHIP_YEARS  # noqa: E402

_FINANCE_SRC = "data/raw/national/nces_lea_finance_2024.csv"
_MEMBERSHIP_YEARS = NCES_MEMBERSHIP_YEARS


# ── Builders ──────────────────────────────────────────────────────────────────

def _build_lea_directory(input_dir: str, output_dir: str, force: bool) -> None:
    """Build nces_lea_directory.parquet with has_charter_schools column."""
    out_path = os.path.join(output_dir, "nces_lea_directory.parquet")
    if os.path.exists(out_path) and not force:
        print(f"[skip] nces_lea_directory.parquet exists — use --force to rebuild")
        return

    lea_csv = os.path.join(input_dir, f"nces_lea_directory_{NCES_CCD_YEAR}.csv")
    sch_csv = os.path.join(input_dir, f"nces_sch_directory_{NCES_CCD_YEAR}.csv")

    if not os.path.exists(lea_csv):
        print(f"ERROR: {lea_csv} not found. Run download_nces_national.py first.", file=sys.stderr)
        return

    print(f"Building nces_lea_directory.parquet from {lea_csv}…")

    # Read LEA directory — LEAID must be string to preserve leading zeros
    str_cols = {"LEAID": str, "FIPST": str}
    df = pd.read_csv(lea_csv, dtype=str_cols, low_memory=False)
    df["STABBR"] = df["STABBR"].str.upper().str.strip()
    df["LSTATE"] = df["LSTATE"].str.upper().str.strip()
    df["LEAID"] = df["LEAID"].str.strip().str.zfill(7)

    # Convert enrollment to numeric (keep as int where possible)
    df["MEMBERSCH"] = pd.to_numeric(df["MEMBERSCH"], errors="coerce").fillna(0).astype(int)

    # ── Derive has_charter_schools from school directory ──────────────────────
    if os.path.exists(sch_csv):
        print(f"  Loading school charter data from {sch_csv}…")
        sch_df = pd.read_csv(sch_csv, dtype={"LEAID": str, "NCESSCH": str}, low_memory=False,
                             usecols=["LEAID", "CHARTER_TEXT"])
        sch_df["LEAID"] = sch_df["LEAID"].str.strip().str.zfill(7)
        charter_leaids = set(
            sch_df.loc[sch_df["CHARTER_TEXT"] == "Yes", "LEAID"].unique()
        )
        df["has_charter_schools"] = df["LEAID"].isin(charter_leaids)
        print(f"  {len(charter_leaids):,} districts with ≥1 charter school")
    else:
        print(f"  WARNING: {sch_csv} not found — has_charter_schools will be False", file=sys.stderr)
        df["has_charter_schools"] = False

    df = df.set_index("LEAID")
    df.to_parquet(out_path, compression="snappy")

    n_states = df["STABBR"].nunique()
    print(f"Built nces_lea_directory.parquet: {len(df):,} rows, {n_states} states")

    # ── NM validation ─────────────────────────────────────────────────────────
    _validate_nm(df, "nces_lea_directory.parquet")


def _build_lea_finance(output_dir: str, force: bool) -> None:
    """Convert the already-national finance CSV to parquet."""
    out_path = os.path.join(output_dir, "nces_lea_finance.parquet")
    if os.path.exists(out_path) and not force:
        print(f"[skip] nces_lea_finance.parquet exists — use --force to rebuild")
        return

    if not os.path.exists(_FINANCE_SRC):
        print(f"ERROR: {_FINANCE_SRC} not found.", file=sys.stderr)
        print("  Run: mv data/raw/nm/nces_lea_finance_2024.csv data/raw/national/", file=sys.stderr)
        return

    print(f"Building nces_lea_finance.parquet from {_FINANCE_SRC}…")
    df = pd.read_csv(_FINANCE_SRC, dtype={"LEAID": str}, encoding="utf-8-sig",
                     low_memory=False)
    df["LEAID"] = df["LEAID"].str.strip().str.zfill(7)
    df["STABBR"] = df["STABBR"].str.upper().str.strip()
    df = df.set_index("LEAID")
    df.to_parquet(out_path, compression="snappy")

    n_states = df["STABBR"].nunique()
    print(f"Built nces_lea_finance.parquet: {len(df):,} rows, {n_states} states")
    _validate_nm(df.reset_index(), "nces_lea_finance.parquet")


def _build_sch_lunch(input_dir: str, output_dir: str, force: bool) -> None:
    """Build nces_sch_lunch.parquet in NCES filter-compatible row format."""
    out_path = os.path.join(output_dir, "nces_sch_lunch.parquet")
    if os.path.exists(out_path) and not force:
        print(f"[skip] nces_sch_lunch.parquet exists — use --force to rebuild")
        return

    sch_csv = os.path.join(input_dir, f"nces_sch_directory_{NCES_CCD_YEAR}.csv")
    if not os.path.exists(sch_csv):
        print(f"ERROR: {sch_csv} not found. Run download_nces_national.py first.", file=sys.stderr)
        return

    print(f"Building nces_sch_lunch.parquet from {sch_csv}…")
    src = pd.read_csv(sch_csv, dtype={"LEAID": str, "NCESSCH": str}, low_memory=False,
                      usecols=["NCESSCH", "LEAID", "SCH_NAME", "STABBR",
                               "FREE_LUNCH", "REDUCED_PRICE_LUNCH"])
    src["LEAID"] = src["LEAID"].str.strip().str.zfill(7)
    src["STABBR"] = src["STABBR"].str.upper().str.strip()

    # Explode into two rows per school — one for free lunch, one for reduced-price —
    # matching the NCES nces_sch_lunch_2024.csv filter format used by nces_fetcher.py.
    rows: list[dict] = []
    for _, r in src.iterrows():
        for prog, col in [
            ("Free lunch qualified", "FREE_LUNCH"),
            ("Reduced-price lunch qualified", "REDUCED_PRICE_LUNCH"),
        ]:
            try:
                count = int(float(r[col] or 0))
            except (ValueError, TypeError):
                count = 0
            if count > 0:
                rows.append({
                    "LEAID":            r["LEAID"],
                    "ST":               r["STABBR"],
                    "NCESSCH":          r["NCESSCH"],
                    "SCH_NAME":         r["SCH_NAME"],
                    "DATA_GROUP":       "Free and Reduced-price Lunch Table",
                    "LUNCH_PROGRAM":    prog,
                    "STUDENT_COUNT":    str(count),
                    "TOTAL_INDICATOR":  "Category Set A",
                    "DMS_FLAG":         "Reported",
                })

    if not rows:
        print("  WARNING: no FRL rows built (all zero?)", file=sys.stderr)
        return

    out_df = pd.DataFrame(rows)
    out_df["LEAID"] = out_df["LEAID"].astype(str)
    out_df.to_parquet(out_path, compression="snappy", index=False)
    n_states = out_df["ST"].nunique()
    print(f"Built nces_sch_lunch.parquet: {len(out_df):,} rows, {n_states} states")


def _build_lea_membership(input_dir: str, output_dir: str, force: bool) -> None:
    """Combine per-year LEA membership CSVs into one national parquet."""
    out_path = os.path.join(output_dir, "nces_lea_membership.parquet")
    if os.path.exists(out_path) and not force:
        print(f"[skip] nces_lea_membership.parquet exists — use --force to rebuild")
        return

    frames: list[pd.DataFrame] = []
    missing: list[int] = []
    for year in _MEMBERSHIP_YEARS:
        csv_path = os.path.join(input_dir, f"nces_lea_membership_{year}.csv")
        if not os.path.exists(csv_path):
            missing.append(year)
            continue
        df = pd.read_csv(csv_path, dtype={"LEAID": str}, low_memory=False)
        df["LEAID"] = df["LEAID"].str.strip().str.zfill(7)
        df["_year"] = str(year)
        frames.append(df)

    if missing:
        print(f"  WARNING: missing membership years {missing} — "
              "run download_nces_national.py to fetch them", file=sys.stderr)

    if not frames:
        print(f"ERROR: no membership CSV files found in {input_dir}", file=sys.stderr)
        return

    combined = pd.concat(frames, ignore_index=True)
    combined["STABBR"] = combined["STABBR"].str.upper().str.strip()
    combined.to_parquet(out_path, compression="snappy", index=False)

    years_present = sorted(combined["_year"].unique())
    n_states = combined["STABBR"].nunique()
    print(f"Built nces_lea_membership.parquet: {len(combined):,} rows, "
          f"{n_states} states, years {years_present}")

    # NM validation: compare row count to existing NM state parquet
    nm_pq = "data/processed/nm/nces_membership_nm.parquet"
    if os.path.exists(nm_pq):
        nm_old = pd.read_parquet(nm_pq)
        nm_old_total = nm_old[
            (nm_old["TOTAL_INDICATOR"] == "Education Unit Total") &
            (nm_old["DMS_FLAG"] == "Reported")
        ]["LEAID"].nunique()
        nm_new = combined[combined["STABBR"] == "NM"]["LEAID"].nunique()
        diff_pct = abs(nm_new - nm_old_total) / max(nm_old_total, 1) * 100
        if diff_pct > 10:
            print(f"  WARNING: NM district count mismatch: "
                  f"new={nm_new}, old={nm_old_total} ({diff_pct:.0f}% diff)")
        else:
            print(f"  NM validation OK: {nm_new} districts (was {nm_old_total} in old parquet)")


def _validate_nm(df: pd.DataFrame, label: str) -> None:
    """Compare NM row count in national parquet vs local NM CSV baseline."""
    nm_csv = "data/raw/nm/nces_ccd_schools_nm.csv"
    if not os.path.exists(nm_csv):
        return
    import subprocess
    result = subprocess.run(["wc", "-l", nm_csv], capture_output=True, text=True)
    nm_csv_rows = int(result.stdout.strip().split()[0]) - 1  # minus header
    stabbr_col = "STABBR" if "STABBR" in df.columns else "ST"
    if stabbr_col not in df.columns:
        return
    nm_rows = (df[stabbr_col] == "NM").sum()
    diff_pct = abs(nm_rows - nm_csv_rows) / max(nm_csv_rows, 1) * 100
    if diff_pct > 10:
        print(f"  WARNING [{label}]: NM rows = {nm_rows}, "
              f"nm_csv rows = {nm_csv_rows} ({diff_pct:.0f}% diff)")
    else:
        print(f"  NM validation OK [{label}]: {nm_rows} rows")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build national CCD parquet files from downloaded CSVs."
    )
    parser.add_argument("--input-dir", default="data/raw/national",
                        help="Directory containing downloaded CSVs (default: data/raw/national)")
    parser.add_argument("--output-dir", default="data/raw/national",
                        help="Directory for output parquet files (default: data/raw/national)")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild even if parquet already exists")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    _build_lea_directory(args.input_dir, args.output_dir, args.force)
    _build_lea_finance(args.output_dir, args.force)
    _build_sch_lunch(args.input_dir, args.output_dir, args.force)
    _build_lea_membership(args.input_dir, args.output_dir, args.force)

    print("\nDone.")


if __name__ == "__main__":
    main()
