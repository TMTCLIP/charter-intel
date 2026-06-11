#!/usr/bin/env python3
"""
scripts/download_nces_national.py

Download national CCD data from the Urban Institute Education Data API
(https://educationdata.urban.org), which wraps the same underlying NCES CCD
files. Saves as CSVs with NCES-compatible column names to --output-dir.
Run once; subsequent runs skip existing files unless --force is passed.

Usage:
    python scripts/download_nces_national.py
    python scripts/download_nces_national.py --force
    python scripts/download_nces_national.py --dry-run

Files produced (in data/raw/national/ by default):
    nces_lea_directory_2022.csv   — district-level directory (name, city, state, enrollment)
    nces_sch_directory_2022.csv   — school-level directory (charter flag, FRL counts)
    nces_lea_membership_2020.csv  — district enrollment 2019-20
    nces_lea_membership_2022.csv  — district enrollment 2021-22
    nces_lea_membership_2023.csv  — district enrollment 2022-23
    nces_lea_membership_2024.csv  — district enrollment 2023-24
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Iterator

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.utils.data_config import NCES_CCD_YEAR  # noqa: E402

_BASE = "https://educationdata.urban.org/api/v1"
_PER_PAGE = 1000
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds; doubles on each retry

# State FIPS → 2-letter abbreviation (50 states + DC)
FIPS_TO_STABBR: dict[str, str] = {
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
    "56": "WY",
}

# Manifest: key → config for each file to build.
# "url": Urban Institute API endpoint (year embedded)
# "filename": output CSV basename
# "description": human label
# "strategy": "national" (one national batch) or "by_state" (loop per FIPS)
MANIFEST: dict[str, dict] = {
    "lea_directory": {
        "url": f"{_BASE}/school-districts/ccd/directory/{NCES_CCD_YEAR}/",
        "filename": f"nces_lea_directory_{NCES_CCD_YEAR}.csv",
        "description": f"LEA directory SY{NCES_CCD_YEAR}-{NCES_CCD_YEAR % 100 + 1:02d} (district name, city, state, enrollment)",
        "strategy": "national",
        "year": NCES_CCD_YEAR,
    },
    "school_directory": {
        "url": f"{_BASE}/schools/ccd/directory/{NCES_CCD_YEAR}/",
        "filename": f"nces_sch_directory_{NCES_CCD_YEAR}.csv",
        "description": f"School directory SY{NCES_CCD_YEAR}-{NCES_CCD_YEAR % 100 + 1:02d} (charter indicator, FRL counts)",
        "strategy": "by_state",
        "year": NCES_CCD_YEAR,
    },
    "lea_membership_2020": {
        "url": f"{_BASE}/school-districts/ccd/directory/2020/",
        "filename": "nces_lea_membership_2020.csv",
        "description": "LEA membership 2019-20 (district enrollment)",
        "strategy": "by_state",
        "year": 2020,
    },
    "lea_membership_2022": {
        "url": f"{_BASE}/school-districts/ccd/directory/2022/",
        "filename": "nces_lea_membership_2022.csv",
        "description": "LEA membership 2021-22 (district enrollment)",
        "strategy": "by_state",
        "year": 2022,
    },
    "lea_membership_2023": {
        "url": f"{_BASE}/school-districts/ccd/directory/2023/",
        "filename": "nces_lea_membership_2023.csv",
        "description": "LEA membership 2022-23 (district enrollment)",
        "strategy": "by_state",
        "year": 2023,
    },
    "lea_membership_2024": {
        "url": f"{_BASE}/school-districts/ccd/directory/2024/",
        "filename": "nces_lea_membership_2024.csv",
        "description": "LEA membership 2023-24 (district enrollment)",
        "strategy": "by_state",
        "year": 2024,
    },
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _api_get(url: str) -> dict:
    """GET url with retry/backoff. Raises RuntimeError on persistent failure."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"404 Not Found: {url}") from e
            if attempt == _MAX_RETRIES:
                raise RuntimeError(f"HTTP {e.code} after {_MAX_RETRIES} tries: {url}") from e
        except Exception as e:
            if attempt == _MAX_RETRIES:
                raise RuntimeError(f"Request failed after {_MAX_RETRIES} tries: {url} — {e}") from e
        wait = _BACKOFF_BASE ** attempt
        print(f"  retry {attempt}/{_MAX_RETRIES} in {wait:.0f}s…", flush=True)
        time.sleep(wait)
    raise RuntimeError("unreachable")


def _paginate(base_url: str) -> Iterator[dict]:
    """Yield every result dict from a paginated Urban Institute API endpoint."""
    url = f"{base_url}?per_page={_PER_PAGE}" if "?" not in base_url else f"{base_url}&per_page={_PER_PAGE}"
    page = 1
    while url:
        data = _api_get(url)
        for row in data.get("results", []):
            yield row
        nxt = data.get("next")
        if nxt:
            url = nxt if "per_page=" in nxt else f"{nxt}&per_page={_PER_PAGE}"
            page += 1
        else:
            url = None


def _count(base_url: str) -> int:
    """Return the total record count for a URL without fetching all pages."""
    url = f"{base_url}?per_page=1" if "?" not in base_url else f"{base_url}&per_page=1"
    try:
        data = _api_get(url)
        return data.get("count", 0)
    except RuntimeError:
        return 0


# ── Row transformers ──────────────────────────────────────────────────────────

def _lea_dir_row(r: dict) -> dict:
    fips = str(r.get("fips") or "").zfill(2)
    return {
        "LEAID":    str(r.get("leaid") or ""),
        "LEA_NAME": str(r.get("lea_name") or ""),
        "LCITY":    str(r.get("city_location") or ""),
        "LSTATE":   str(r.get("state_location") or ""),
        "STABBR":   str(r.get("state_location") or ""),
        "MEMBERSCH": str(r.get("enrollment") or ""),
        "FIPST":    fips,
    }


def _sch_dir_row(r: dict) -> dict:
    charter = r.get("charter")
    charter_text = "Yes" if charter == 1 else "No"
    fips = str(r.get("fips") or "").zfill(2)
    return {
        "NCESSCH":              str(r.get("ncessch") or ""),
        "LEAID":                str(r.get("leaid") or ""),
        "SCH_NAME":             str(r.get("school_name") or ""),
        "LCITY":                str(r.get("city_location") or ""),
        "LSTATE":               str(r.get("state_location") or ""),
        "STABBR":               str(r.get("state_location") or ""),
        "FIPST":                fips,
        "CHARTER_TEXT":         charter_text,
        "ENROLLMENT":           str(r.get("enrollment") or ""),
        "FREE_LUNCH":           str(r.get("free_lunch") or ""),
        "REDUCED_PRICE_LUNCH":  str(r.get("reduced_price_lunch") or ""),
        "FREE_OR_REDUCED_PRICE_LUNCH": str(r.get("free_or_reduced_price_lunch") or ""),
    }


def _membership_row(r: dict, year: int) -> dict:
    fips = str(r.get("fips") or "").zfill(2)
    stabbr = FIPS_TO_STABBR.get(fips, "")
    return {
        "LEAID":           str(r.get("leaid") or ""),
        "STABBR":          stabbr,
        "STUDENT_COUNT":   str(r.get("enrollment") or ""),
        "TOTAL_INDICATOR": "Education Unit Total",
        "DMS_FLAG":        "Reported",
        "_year":           str(year),
    }


# ── Download strategies ───────────────────────────────────────────────────────

def _download_national(key: str, cfg: dict, out_path: str, dry_run: bool) -> int:
    """Download all records from one national endpoint."""
    total = _count(cfg["url"])
    n_pages = max(1, -(-total // _PER_PAGE))
    print(f"  Downloading {cfg['description']}... (~{total:,} records, {n_pages} pages)")
    if dry_run:
        return total

    is_lea = key == "lea_directory"
    fieldnames: list[str] = []
    rows_written = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer: csv.DictWriter | None = None
        for r in _paginate(cfg["url"]):
            row = _lea_dir_row(r) if is_lea else _sch_dir_row(r)
            if writer is None:
                fieldnames = list(row.keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
            writer.writerow(row)
            rows_written += 1
        if rows_written and rows_written % 5000 == 0:
            print(f"    {rows_written:,} records…", end="\r", flush=True)

    return rows_written


def _download_by_state(key: str, cfg: dict, out_path: str, dry_run: bool) -> int:
    """Download records state-by-state (faster than national for some years)."""
    is_school = key == "school_directory"
    is_membership = key.startswith("lea_membership")
    year = cfg["year"]

    total_est = sum(
        _count(f"{cfg['url']}?fips={fips}") for fips in list(FIPS_TO_STABBR)[:3]
    ) * len(FIPS_TO_STABBR) // 3
    print(f"  Downloading {cfg['description']}... (~{len(FIPS_TO_STABBR)} states)")

    if dry_run:
        return total_est

    rows_written = 0
    writer: csv.DictWriter | None = None
    f_out = open(out_path, "w", newline="", encoding="utf-8")

    try:
        for fips, stabbr in sorted(FIPS_TO_STABBR.items()):
            state_url = f"{cfg['url']}?fips={fips}"
            try:
                for r in _paginate(state_url):
                    if is_school:
                        row = _sch_dir_row(r)
                    elif is_membership:
                        row = _membership_row(r, year)
                    else:
                        row = _lea_dir_row(r)

                    if writer is None:
                        writer = csv.DictWriter(f_out, fieldnames=list(row.keys()))
                        writer.writeheader()
                    writer.writerow(row)
                    rows_written += 1
            except RuntimeError as e:
                print(f"    WARNING: skipped {stabbr} — {e}", file=sys.stderr)
            print(f"    {stabbr} ✓ ({rows_written:,} total)", end="\r", flush=True)
    finally:
        f_out.close()

    print()  # newline after \r progress
    return rows_written


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download national CCD data from Urban Institute API."
    )
    parser.add_argument("--output-dir", default="data/raw/national",
                        help="Directory to write CSVs (default: data/raw/national)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if file already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print URLs and estimated sizes without downloading")
    parser.add_argument("--only", nargs="*", metavar="KEY",
                        help="Download only these manifest keys (e.g. lea_directory school_directory)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    keys = args.only if args.only else list(MANIFEST.keys())
    downloaded = 0
    skipped = 0
    total_rows = 0

    for key in keys:
        if key not in MANIFEST:
            print(f"WARNING: unknown key '{key}' — skipping", file=sys.stderr)
            continue

        cfg = MANIFEST[key]
        out_path = os.path.join(args.output_dir, cfg["filename"])

        if os.path.exists(out_path) and not args.force and not args.dry_run:
            size_mb = os.path.getsize(out_path) / 1_048_576
            print(f"[skip] {cfg['filename']} already exists ({size_mb:.1f} MB) — use --force to re-download")
            skipped += 1
            continue

        t0 = time.time()
        try:
            if cfg["strategy"] == "national":
                rows = _download_national(key, cfg, out_path, args.dry_run)
            else:
                rows = _download_by_state(key, cfg, out_path, args.dry_run)
        except RuntimeError as e:
            print(f"ERROR downloading {key}: {e}", file=sys.stderr)
            continue

        elapsed = round(time.time() - t0)
        if not args.dry_run and os.path.exists(out_path):
            size_mb = os.path.getsize(out_path) / 1_048_576
            print(f"[done] {cfg['filename']}  {rows:,} rows  {size_mb:.1f} MB  {elapsed}s")
            total_rows += rows
            downloaded += 1
        else:
            print(f"[dry-run] {cfg['filename']}  ~{rows:,} rows")

    print()
    if args.dry_run:
        print(f"[dry-run] Would download {len(keys)} files to {args.output_dir}/")
    else:
        print(f"Downloaded {downloaded} files to {args.output_dir}/  ({skipped} skipped)")
        print(f"Total rows: {total_rows:,}")


if __name__ == "__main__":
    main()
