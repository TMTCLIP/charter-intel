"""
pipeline/fetchers/ed_data_express_fetcher.py
District-level chronic absenteeism rate from ED Data Express (SY2022-23).

Supersedes CRDC chronic absenteeism for districts where the LEA-level file has
data. CRDC chronic absenteeism remains the fallback when this source has no
matching LEAID.

DATA SOURCE:
  ED Data Express — ESEA/ESSA reporting, SY2022-23
  URL: https://eddataexpress.ed.gov/
  File: SY2223_DG814PCT_LEA_082724.csv (inside zip, downloaded on first run)
  License: public domain (U.S. Department of Education)

COLUMNS:
  SCHOOL_YEAR, STATE_NAME, LEAID, LEA_NAME, DATA_GROUP_ID,
  DENOMINATOR, NUMERATOR, TEXT_VALUE, NUMERIC_VALUE, SUBGROUP

FILTER: SUBGROUP == "ALLLEA" gives the district total.
LEAID: 7-digit string; file contains 7-digit values directly.

ERROR CONTRACT: never raises. Returns None when the district is not in the
file, the zip cannot be fetched, or any parse error occurs.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
import urllib.request
import zipfile
from typing import Optional

from pipeline.utils.data_config import (
    ED_DATA_EXPRESS_CA_CSV,
    ED_DATA_EXPRESS_CA_YEAR,
    ED_DATA_EXPRESS_CA_ZIP_URL,
)

log = logging.getLogger(__name__)

SOURCE_TITLE = f"ED Data Express — Chronic Absenteeism SY{ED_DATA_EXPRESS_CA_YEAR - 1}-{str(ED_DATA_EXPRESS_CA_YEAR)[2:]}"
SOURCE_URL = "https://eddataexpress.ed.gov/"

_CACHE_DIR = "data/cache/fetcher/ed_data_express"
_ZIP_CACHE_PATH = os.path.join(_CACHE_DIR, "ede_chronic_absenteeism.zip")
_LOOKUP_CACHE_PATH = os.path.join(_CACHE_DIR, "ede_leaid_lookup.json")
_CACHE_TTL_DAYS = 365  # annual release; refresh once per year


# ── cache helpers ──────────────────────────────────────────────────────────────

def _cache_age_days(path: str) -> float:
    if not os.path.exists(path):
        return float("inf")
    return (time.time() - os.path.getmtime(path)) / 86400


def _read_lookup_cache() -> Optional[dict]:
    if _cache_age_days(_LOOKUP_CACHE_PATH) > _CACHE_TTL_DAYS:
        return None
    try:
        with open(_LOOKUP_CACHE_PATH) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("ed_data_express_fetcher: lookup cache read failed — %s", exc)
        return None


def _write_lookup_cache(lookup: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_LOOKUP_CACHE_PATH, "w") as f:
            json.dump(lookup, f, indent=2)
    except Exception as exc:
        log.warning("ed_data_express_fetcher: lookup cache write failed — %s", exc)


# ── zip fetch ─────────────────────────────────────────────────────────────────

def _fetch_zip() -> Optional[bytes]:
    """Download the ED Data Express zip. Returns raw bytes or None on failure."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        if _cache_age_days(_ZIP_CACHE_PATH) <= _CACHE_TTL_DAYS:
            with open(_ZIP_CACHE_PATH, "rb") as f:
                return f.read()
    except Exception:
        pass

    log.info("ed_data_express_fetcher: downloading zip from %s", ED_DATA_EXPRESS_CA_ZIP_URL)
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                ED_DATA_EXPRESS_CA_ZIP_URL,
                headers={"User-Agent": "CLIP/1.0"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
            with open(_ZIP_CACHE_PATH, "wb") as f:
                f.write(data)
            log.info("ed_data_express_fetcher: zip downloaded (%d bytes)", len(data))
            return data
        except Exception as exc:
            if attempt == 3:
                log.warning("ed_data_express_fetcher: zip download failed after 3 tries — %s", exc)
                return None
            wait = 2.0 ** attempt
            log.info("ed_data_express_fetcher: retry %d in %.0fs — %s", attempt, wait, exc)
            time.sleep(wait)
    return None


# ── lookup builder ─────────────────────────────────────────────────────────────

def _build_lookup(zip_bytes: bytes) -> Optional[dict]:
    """Parse the CSV from the zip and build a {leaid: row_dict} lookup.

    Filters to SUBGROUP == "ALLLEA" (district totals).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            members = zf.namelist()
            target = next((m for m in members if ED_DATA_EXPRESS_CA_CSV in m), None)
            if target is None:
                log.warning(
                    "ed_data_express_fetcher: %s not found in zip (members: %s)",
                    ED_DATA_EXPRESS_CA_CSV,
                    members[:5],
                )
                return None
            with zf.open(target) as raw:
                text = raw.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:
        log.warning("ed_data_express_fetcher: zip parse failed — %s", exc)
        return None

    lookup: dict = {}
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            if (row.get("SUBGROUP") or "").strip().upper() != "ALLLEA":
                continue
            leaid = str(row.get("LEAID") or "").strip().zfill(7)
            if not leaid or leaid == "0000000":
                continue
            try:
                numeric = float(row.get("NUMERIC_VALUE") or "nan")
            except ValueError:
                numeric = None
            try:
                numerator = int(float(row.get("NUMERATOR") or 0))
            except ValueError:
                numerator = None
            try:
                denominator = int(float(row.get("DENOMINATOR") or 0))
            except ValueError:
                denominator = None
            if numeric is not None and not (numeric != numeric):  # not NaN
                lookup[leaid] = {
                    "chronic_absenteeism_pct": round(numeric, 1),
                    "chronic_absent_count": numerator,
                    "chronic_absent_denominator": denominator,
                }
    except Exception as exc:
        log.warning("ed_data_express_fetcher: CSV parse failed — %s", exc)
        return None

    log.info("ed_data_express_fetcher: built lookup with %d LEA entries", len(lookup))
    return lookup


# ── public interface ──────────────────────────────────────────────────────────

def get_ed_data_express_absenteeism(leaid: str) -> Optional[dict]:
    """Return chronic absenteeism figures for a district from ED Data Express.

    Return shape:
        {
          "chronic_absenteeism_pct": 24.7,
          "chronic_absenteeism_year": 2023,
          "chronic_absent_count": 1124,
          "chronic_absent_denominator": 4553,
          "source": "ED_DATA_EXPRESS",
          "source_url": "https://eddataexpress.ed.gov/",
          "source_title": "ED Data Express — Chronic Absenteeism SY2022-23",
          "confidence": "HIGH",
          "data_note": "...",
        }
    Returns None when the district is not in the file or data is unavailable.
    """
    if not leaid:
        return None
    leaid = str(leaid).strip().zfill(7)

    lookup = _read_lookup_cache()
    if lookup is None:
        zip_bytes = _fetch_zip()
        if zip_bytes is None:
            return None
        lookup = _build_lookup(zip_bytes)
        if lookup is None:
            return None
        _write_lookup_cache(lookup)

    entry = lookup.get(leaid)
    if entry is None:
        log.info("ed_data_express_fetcher: leaid %s not found in lookup", leaid)
        return None

    return {
        "chronic_absenteeism_pct": entry["chronic_absenteeism_pct"],
        "chronic_absenteeism_year": ED_DATA_EXPRESS_CA_YEAR,
        "chronic_absent_count": entry.get("chronic_absent_count"),
        "chronic_absent_denominator": entry.get("chronic_absent_denominator"),
        "source": "ED_DATA_EXPRESS",
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "HIGH",
        "data_note": (
            f"ED Data Express ESSA/ESEA reporting SY{ED_DATA_EXPRESS_CA_YEAR - 1}"
            f"-{str(ED_DATA_EXPRESS_CA_YEAR)[2:]} (LEA total, all students). "
            "More recent than CRDC; verify against state accountability portal for current year."
        ),
    }
