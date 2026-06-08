"""
pipeline/fetchers/mde_ratings_fetcher.py
Mississippi Department of Education district A–F accountability ratings.

These letter grades drive the charter_law consent-required gate (§ 37-28-7): a
charter in an A/B/C-rated district needs local-school-board endorsement, while a
D/F district does not. Until this fetcher existed, `district_accountability_rating`
was never populated, so the gate evaluated every MS community identically.

SOURCE (verified live 2026-06-07):
  MDE 2024 accountability media file (2023-24 school year), "Districts" sheet:
  columns ID# (MDE district code) | District Name | Grade (A–F) | points | ...
    https://mdek12.org/publicreporting/2024-accountability/
  Direct file (hardcoded below). To update for a new year: download the new
  media file, drop it at data/raw/ms/mde_ratings_{year}.xlsx, and bump
  MDE_RATINGS_YEAR / MDE_RATINGS_URL. Ratings change annually and a human should
  review the new file before it affects board-facing briefs — so this is a
  deliberate one-line update, not auto-fetched silently.

LEAID CROSSWALK: the MDE file keys on district NAME + a state district code, not
NCES LEAID. We map MDE district names to LEAID by normalized-name match against
the national CCD LEA directory parquet (data/raw/national/nces_lea_directory.parquet,
indexed by LEAID), plus an explicit override map for a handful of districts whose
CCD names abbreviate differently (verified individually). Charter-school rows in
the accountability file are intentionally ignored — the gate needs the rating of
the *district* a prospective charter would locate in, not a charter's own grade.

ERROR CONTRACT: never raises; returns None when the district has no mapped rating.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.request
import zipfile
from typing import Optional
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

MDE_RATINGS_YEAR = 2024  # 2023-24 school year
MDE_RATINGS_URL = (
    "https://mdek12.org/publicreporting/wp-content/uploads/sites/33/2026/01/"
    "2024_accountability_media_file_updated_2026_01.xlsx"
)
_LOCAL_XLSX = f"data/raw/ms/mde_ratings_{MDE_RATINGS_YEAR}.xlsx"
_CCD_PARQUET = "data/raw/national/nces_lea_directory.parquet"
SOURCE_URL = "https://mdek12.org/publicreporting/2024-accountability/"
SOURCE_TITLE = f"Mississippi Dept. of Education District Accountability Ratings SY {MDE_RATINGS_YEAR-1}-{str(MDE_RATINGS_YEAR)[2:]}"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_VALID_GRADES = frozenset({"A", "B", "C", "D", "F"})

# MDE district name -> LEAID, for districts whose CCD directory name abbreviates
# too differently for normalized-name matching (verified against the CCD parquet
# 2026-06-07). Keep this list tiny and explicit.
_LEAID_OVERRIDE = {
    "Tishomingo County School District":            "2804260",
    "East Jasper Consolidated School District":     "2801380",
    "Holmes County Consolidated School District":   "2800195",
    "East Tallahatchie Consolidated School District": "2801410",
    "Greenwood-Leflore Consolidated School District": "2800198",
}

# Word/abbreviation tokens stripped from both MDE and CCD names before matching.
_STRIP_TOKENS = [
    " SCHOOL DISTRICT", " SCHOOL DIST", " PUBLIC SCHOOLS", " PUBLIC SCHOOL",
    " PUBLIC", " CONSOLIDATED", " CONSOL", " CONS", " SCHOOLS", " SCHOOL",
    " SCH", " DISTRICT", " DIST", " SD", " COUNTY", " CTY", " CO",
    " MUNICIPAL SEPARATE", " MUNICIPAL SEP", " MUN SEP", " MUN", " SP",
    " SEPARATE", " SEP", " AGRICULTURAL HIGH", " AGRICULTURAL",
]

# module-level memo: built once per process from the committed xlsx + parquet
_RATING_BY_LEAID: Optional[dict] = None


def _normalize(name: str) -> str:
    s = " " + re.sub(r"[^A-Z0-9 ]", " ", str(name).upper()) + " "
    s = re.sub(r"\s+", " ", s)
    changed = True
    while changed:
        changed = False
        for tok in _STRIP_TOKENS:
            if tok + " " in s or s.rstrip() == s.rstrip().removesuffix(tok):
                new = s.replace(tok + " ", " ")
                if new != s:
                    s = new
                    changed = True
    return re.sub(r"\s+", " ", s).strip()


def _download_if_missing() -> bool:
    """Ensure the year's xlsx is present locally. Returns True if available."""
    if os.path.exists(_LOCAL_XLSX):
        return True
    try:
        os.makedirs(os.path.dirname(_LOCAL_XLSX), exist_ok=True)
        req = urllib.request.Request(MDE_RATINGS_URL, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        with open(_LOCAL_XLSX, "wb") as f:
            f.write(data)
        log.info("mde_ratings_fetcher: downloaded %s (%d bytes)", _LOCAL_XLSX, len(data))
        return True
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        log.warning("mde_ratings_fetcher: could not download MDE ratings file — %s", exc)
        return False


def _parse_districts_sheet(path: str) -> list[tuple[str, str]]:
    """Return [(district_name, grade), ...] from the 'Districts' sheet."""
    z = zipfile.ZipFile(path)
    shared = [
        "".join(t.text or "" for t in si.iter(_NS + "t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(_NS + "si")
    ]
    # Resolve the workbook sheet order -> worksheet XML for 'Districts'.
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    names = [s.get("name") for s in wb.iter(_NS + "sheet")]
    try:
        sheet_idx = names.index("Districts") + 1
    except ValueError:
        sheet_idx = 2  # observed position
    sheet = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet_idx}.xml"))

    def col_letter(ref: str) -> str:
        return "".join(ch for ch in (ref or "") if ch.isalpha())

    def cell_val(c) -> str:
        v = c.find(_NS + "v")
        if v is None:
            return ""
        return shared[int(v.text)] if c.get("t") == "s" else (v.text or "")

    out = []
    for i, row in enumerate(sheet.iter(_NS + "row")):
        if i == 0:
            continue  # header
        cells = {col_letter(c.get("r")): cell_val(c) for c in row.findall(_NS + "c")}
        name = (cells.get("B") or "").strip()
        grade = (cells.get("C") or "").strip().upper()
        if name and grade in _VALID_GRADES:
            out.append((name, grade))
    return out


def _build_rating_map() -> dict:
    """Build {leaid: {'rating': 'A', 'year': 2024}} from the xlsx + CCD crosswalk."""
    if not _download_if_missing():
        return {}
    try:
        districts = _parse_districts_sheet(_LOCAL_XLSX)
    except Exception as exc:  # noqa: BLE001
        log.warning("mde_ratings_fetcher: failed to parse %s — %s", _LOCAL_XLSX, exc)
        return {}

    # CCD name -> LEAID (MS only), normalized.
    ccd_by_name: dict = {}
    try:
        import pandas as pd
        df = pd.read_parquet(_CCD_PARQUET)
        ms = df[df["STABBR"] == "MS"]
        for leaid, r in ms.iterrows():
            ccd_by_name.setdefault(_normalize(r["LEA_NAME"]), str(leaid))
    except Exception as exc:  # noqa: BLE001
        log.warning("mde_ratings_fetcher: could not load CCD parquet crosswalk — %s", exc)

    override_norm = {k: v for k, v in _LEAID_OVERRIDE.items()}
    rating_map: dict = {}
    matched = unmatched = 0
    for name, grade in districts:
        leaid = override_norm.get(name) or ccd_by_name.get(_normalize(name))
        if leaid:
            rating_map[str(leaid)] = {"rating": grade, "year": MDE_RATINGS_YEAR}
            matched += 1
        else:
            unmatched += 1
    log.info(
        "mde_ratings_fetcher: built rating map — %d districts mapped, %d unmatched "
        "(unmatched are mostly charter-school rows, intentionally excluded)",
        matched, unmatched,
    )
    return rating_map


def get_mde_district_rating(leaid: str) -> Optional[dict]:
    """Return the district A–F accountability rating for a LEAID, or None.

    Return shape:
        {"district_accountability_rating": "A", "district_rating_year": 2024,
         "source_url": ..., "source_title": ..., "confidence": "HIGH"}
    """
    global _RATING_BY_LEAID
    if not leaid:
        return None
    if _RATING_BY_LEAID is None:
        _RATING_BY_LEAID = _build_rating_map()

    entry = _RATING_BY_LEAID.get(str(leaid).strip())
    if not entry:
        return None
    return {
        "district_accountability_rating": entry["rating"],
        "district_rating_year": entry["year"],
        "source_url": SOURCE_URL,
        "source_title": SOURCE_TITLE,
        "confidence": "HIGH",
    }
