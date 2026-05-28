"""
pipeline/zip_drill.py
ZIP Drill Mode — sub-city neighborhood opportunity ranking by zip code.

Standalone module. Does not use the 7-stage pipeline machinery.
Invoked via: python3 main.py "Albuquerque" --mode zip

OUTPUT: outputs/zip/{city_slug}/{city_slug}_zip_drill.html

DIMENSIONS (5, equal weights, uncalibrated):
  academic_need          — PED ELA proficiency per zip (inverse)
  charter_saturation     — charter school count per zip (inverse)
  school_age_population  — Census ACS B09001, ages 5-17 (direct)
  income_concentration   — Census ACS poverty rate (direct: higher need = higher score)
  ell_concentration      — Census ACS limited-English households (direct)

PROOF OF CONCEPT — Albuquerque, NM.
Weights uncalibrated. Political, authorizer, and facilities signals
not available at zip level. Do not use for real strategic decisions.
"""
from __future__ import annotations

import csv
import datetime
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

# ── Census ACS config (mirrors acs_fetcher.py constants) ─────────────────────

_ACS_BASE    = "https://api.census.gov/data"
_ACS_YEAR    = "2023"
_ACS_DATASET = "acs/acs5"
_ACS_URL     = f"{_ACS_BASE}/{_ACS_YEAR}/{_ACS_DATASET}"


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ZipInfo:
    zip_code: str
    total_schools: int
    charter_count: int
    charter_names: list[str] = field(default_factory=list)


@dataclass
class CensusData:
    school_age_pop: Optional[int]
    poverty_rate: Optional[float]       # 0.0–1.0
    median_household_income: Optional[int]
    ell_pct: Optional[float]            # 0.0–1.0


@dataclass
class ZipScore:
    zip_code: str
    rank: int
    composite: float
    academic_need: float
    charter_saturation: float
    school_age_population: float
    income_concentration: float
    ell_concentration: float
    # raw values for display
    ela_proficiency_pct: Optional[float]
    charter_count: int
    total_schools: int
    charter_names: list[str]
    school_age_pop: Optional[int]
    poverty_rate_pct: Optional[float]   # 0–100 for display
    ell_pct_display: Optional[float]    # 0–100 for display
    median_income: Optional[int]
    census_available: bool


# ── Step 1: Zip Discovery ─────────────────────────────────────────────────────

def discover_zips(city_name: str, state: str) -> list[ZipInfo]:
    """
    Read NCES CCD schools CSV. Filter by MCITY == city_name.
    Return one ZipInfo per unique MZIP with school and charter counts.
    """
    nces_path = f"data/raw/{state.lower()}/nces_ccd_schools_nm.csv"
    if not os.path.exists(nces_path):
        raise FileNotFoundError(
            f"NCES CCD file not found: {nces_path}. "
            f"Run scripts/build_nces_cache.py {state} first."
        )

    city_upper = city_name.strip().upper()
    zip_totals: dict[str, int] = {}
    zip_charters: dict[str, int] = {}
    zip_charter_names: dict[str, list[str]] = {}

    with open(nces_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("MCITY", "").strip().upper() != city_upper:
                continue
            z = row.get("MZIP", "").strip()
            if not z:
                continue
            zip_totals[z] = zip_totals.get(z, 0) + 1
            if row.get("CHARTER_TEXT", "").strip() == "Yes":
                zip_charters[z] = zip_charters.get(z, 0) + 1
                names = zip_charter_names.setdefault(z, [])
                name = row.get("SCH_NAME", "").strip()
                if name:
                    names.append(name)

    if not zip_totals:
        raise ValueError(
            f"No schools found for city '{city_name}' in {nces_path}. "
            f"Check that MCITY matches exactly (case-insensitive)."
        )

    zips = []
    for z in sorted(zip_totals):
        zips.append(ZipInfo(
            zip_code=z,
            total_schools=zip_totals[z],
            charter_count=zip_charters.get(z, 0),
            charter_names=zip_charter_names.get(z, []),
        ))

    log.info("discover_zips: %d zip codes found for %s", len(zips), city_name)
    return zips


# ── Step 2: Census ACS ZCTA fetch ─────────────────────────────────────────────

def _api_key() -> Optional[str]:
    return os.environ.get("CENSUS_API_KEY") or None


def _fetch_zcta(zip_code: str, variables: str, api_key: str) -> Optional[dict]:
    """
    Fetch Census ACS 5-year data for a single ZCTA.
    Transport pattern mirrors acs_fetcher._fetch_acs_vars exactly.
    ZCTAs do not require a state 'in' parameter.
    """
    params = {
        "get": variables,
        "for": f"zip code tabulation area:{zip_code}",
        "key": api_key,
    }
    url = _ACS_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CLIP/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("zip_drill: ACS request failed for ZCTA %s — %s", zip_code, exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("zip_drill: ACS parse failed for ZCTA %s — %s", zip_code, exc)
        return None

    if not isinstance(data, list) or len(data) < 2:
        log.warning("zip_drill: unexpected ACS shape for ZCTA %s: %s", zip_code, str(data)[:200])
        return None

    headers, row = data[0], data[1]
    return dict(zip(headers, row))


def _safe_int(val) -> Optional[int]:
    """Parse Census integer; return None for missing/suppression sentinels."""
    try:
        v = int(val)
        return v if v >= 0 else None   # negative = Census suppression sentinel
    except (TypeError, ValueError):
        return None


def fetch_census_acs(zip_codes: list[str]) -> dict[str, CensusData]:
    """
    Fetch Census ACS 5-year ZCTA data for all zip codes.
    Returns {zip_code: CensusData}. Missing zips get CensusData with all-None fields.
    Gracefully skips if CENSUS_API_KEY is not set.
    """
    api_key = _api_key()
    if not api_key:
        log.warning(
            "zip_drill: CENSUS_API_KEY not set — Census dimensions will use city averages. "
            "Set CENSUS_API_KEY for full zip-level analysis."
        )
        return {z: CensusData(None, None, None, None) for z in zip_codes}

    # Variables:
    #   B09001_001E  — school-age pop (5–17)
    #   B17001_002E  — poverty count
    #   B17001_001E  — poverty universe
    #   B19013_001E  — median household income
    #   C16002_004E,007E,010E,013E — limited-English households (speak Spanish/other not well + not at all)
    #   B11001_001E  — total households (ELL denominator)
    ell_vars = ["C16002_004E", "C16002_007E", "C16002_010E", "C16002_013E"]
    variables = ",".join([
        "B09001_001E",
        "B17001_002E", "B17001_001E",
        "B19013_001E",
        *ell_vars,
        "B11001_001E",
    ])

    results: dict[str, CensusData] = {}

    for zip_code in zip_codes:
        row = _fetch_zcta(zip_code, variables, api_key)
        if row is None:
            results[zip_code] = CensusData(None, None, None, None)
            continue

        school_age_pop = _safe_int(row.get("B09001_001E"))

        pov_count   = _safe_int(row.get("B17001_002E"))
        pov_universe = _safe_int(row.get("B17001_001E"))
        poverty_rate: Optional[float] = None
        if pov_count is not None and pov_universe and pov_universe > 0:
            poverty_rate = pov_count / pov_universe

        median_income = _safe_int(row.get("B19013_001E"))

        total_hh = _safe_int(row.get("B11001_001E"))
        ell_sum = 0
        ell_valid = True
        for var in ell_vars:
            v = _safe_int(row.get(var))
            if v is None:
                ell_valid = False
                break
            ell_sum += v
        ell_pct: Optional[float] = None
        if ell_valid and total_hh and total_hh > 0:
            ell_pct = ell_sum / total_hh

        results[zip_code] = CensusData(
            school_age_pop=school_age_pop,
            poverty_rate=poverty_rate,
            median_household_income=median_income,
            ell_pct=ell_pct,
        )
        log.info(
            "zip_drill: ZCTA %s — school_age=%s poverty=%.1f%% ell=%.1f%%",
            zip_code,
            school_age_pop,
            (poverty_rate or 0) * 100,
            (ell_pct or 0) * 100,
        )

    return results


# ── Step 3: Proficiency aggregation ──────────────────────────────────────────

def _read_proficiency_csv(path: str) -> list[dict]:
    """
    Read a PED proficiency CSV. Handles two formats:
      - ELA: row 1 is filename metadata, row 2 is real headers
      - Math: row 1 is real headers (no metadata row)
    Detects by checking if row 1 starts with 'TableName'.
    """
    with open(path, encoding="utf-8-sig") as f:
        first_line = f.readline().rstrip("\n")
        if first_line.startswith("TableName"):
            # Math format: row 1 is already the header; rewind and parse normally
            f.seek(0)
        # else: ELA format — junk row already consumed; next line is the header
        reader = csv.DictReader(f)
        return list(reader)


def aggregate_proficiency(
    zip_codes: list[str],
    state: str,
    city_name: str,
) -> dict[str, Optional[float]]:
    """
    Aggregate ELA proficiency rate per zip code by joining PED school-level
    proficiency data to NCES school zip codes via school name match.

    Returns {zip_code: avg_ela_pct_or_None}.
    Zips with no matched schools get None (caller uses city average as fallback).
    """
    ela_path = f"data/raw/{state.lower()}/proficiency_ela_2024_25.csv"
    nces_path = f"data/raw/{state.lower()}/nces_ccd_schools_nm.csv"

    if not os.path.exists(ela_path):
        log.warning("zip_drill: ELA proficiency file not found: %s — academic_need will use city average", ela_path)
        return {z: None for z in zip_codes}

    # Build NCES name → zip map for this city
    city_upper = city_name.strip().upper()
    nces_zip: dict[str, str] = {}
    with open(nces_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("MCITY", "").strip().upper() == city_upper:
                name = row.get("SCH_NAME", "").strip().upper()
                z = row.get("MZIP", "").strip()
                if name and z:
                    nces_zip[name] = z

    # Load PED ELA school-level rows (Demographic=All, LocationCode != 0)
    ela_rows = _read_proficiency_csv(ela_path)
    city_upper_ped = city_name.strip().upper()

    zip_rates: dict[str, list[float]] = {z: [] for z in zip_codes}
    city_rates: list[float] = []  # for fallback average

    for row in ela_rows:
        if row.get("DistrictName", "").strip().upper() != city_upper_ped:
            continue
        if row.get("LocationCode", "").strip() == "0":
            continue
        if row.get("Demographic", "").strip() != "All":
            continue
        rate_str = row.get("AttenuatedProficiencyRate", "").strip()
        if rate_str == "*" or not rate_str:
            continue
        try:
            rate = float(rate_str)
        except ValueError:
            continue

        city_rates.append(rate)
        ped_name = row.get("LocationName", "").strip().upper()
        z = nces_zip.get(ped_name)
        if z and z in zip_rates:
            zip_rates[z].append(rate)

    city_avg = round(sum(city_rates) / len(city_rates), 2) if city_rates else None
    log.info("zip_drill: city-avg ELA proficiency = %s%% (%d schools)", city_avg, len(city_rates))

    result: dict[str, Optional[float]] = {}
    for z in zip_codes:
        rates = zip_rates.get(z, [])
        if rates:
            result[z] = round(sum(rates) / len(rates), 2)
        else:
            result[z] = city_avg  # fallback
            if city_avg is not None:
                log.info("zip_drill: %s — no proficiency data; using city avg %.1f%%", z, city_avg)

    return result


# ── Step 4: Scoring ───────────────────────────────────────────────────────────

def _score_academic_need(ela_pct: Optional[float]) -> float:
    """
    Higher unmet need (lower proficiency) = higher score.
    Linear inverse: 0% proficiency → 10, 100% proficiency → 1.
    Returns 5.0 (midpoint) when data unavailable.
    """
    if ela_pct is None:
        return 5.0
    ela_pct = max(0.0, min(100.0, ela_pct))
    return round(10.0 - (ela_pct / 100.0) * 9.0, 2)


def _score_charter_saturation(charter_count: int, city_max_charters: int) -> float:
    """
    Fewer charters = higher score (more whitespace).
    0 charters → 10; city_max_charters → 1.
    Linear inverse against city maximum.
    """
    if city_max_charters == 0:
        return 10.0
    score = 10.0 - (charter_count / city_max_charters) * 9.0
    return round(max(1.0, score), 2)


def _score_school_age_population(pop: Optional[int], city_max_pop: int) -> float:
    """
    More school-age kids = higher score (larger addressable market).
    Normalized: zip_pop / city_max_pop → scales to 1–10.
    Returns 5.0 when data unavailable.
    """
    if pop is None or city_max_pop == 0:
        return 5.0
    score = 1.0 + (pop / city_max_pop) * 9.0
    return round(min(10.0, score), 2)


def _score_income_concentration(poverty_rate: Optional[float]) -> float:
    """
    Higher poverty rate = higher score (greatest need communities).
    Linear: 40%+ poverty → 10, 0% poverty → 1.
    Returns 5.0 when data unavailable.
    """
    if poverty_rate is None:
        return 5.0
    rate = max(0.0, min(1.0, poverty_rate))
    cap = 0.40
    score = 1.0 + (min(rate, cap) / cap) * 9.0
    return round(score, 2)


def _score_ell_concentration(ell_pct: Optional[float]) -> float:
    """
    Higher ELL concentration = higher score.
    Linear: 30%+ ELL → 10, 0% → 1.
    Returns 5.0 when data unavailable.
    """
    if ell_pct is None:
        return 5.0
    pct = max(0.0, min(1.0, ell_pct))
    cap = 0.30
    score = 1.0 + (min(pct, cap) / cap) * 9.0
    return round(score, 2)


def _census_empty(cd: CensusData) -> bool:
    """
    Return True if all three key Census fields are None or zero.
    Indicates a non-residential zip (military base, PO Box, industrial).
    These zips should be excluded from the ranked table.
    """
    def _null_or_zero(v) -> bool:
        return v is None or v == 0
    return (
        _null_or_zero(cd.school_age_pop) and
        _null_or_zero(cd.poverty_rate) and
        _null_or_zero(cd.ell_pct)
    )


def score_zips(
    zip_infos: list[ZipInfo],
    census: dict[str, CensusData],
    proficiency: dict[str, Optional[float]],
    weights: dict[str, float],
) -> list[ZipScore]:
    """
    Score all zip codes on 5 dimensions. Rank by composite descending.
    City-level maxima are computed once before the scoring loop.
    """
    # Compute city-level maxima for normalization
    city_max_charters = max((zi.charter_count for zi in zip_infos), default=0)
    school_age_pops = [
        census[zi.zip_code].school_age_pop
        for zi in zip_infos
        if census[zi.zip_code].school_age_pop is not None
    ]
    city_max_pop = max(school_age_pops, default=0)

    scored: list[ZipScore] = []

    for zi in zip_infos:
        cd = census[zi.zip_code]
        ela = proficiency.get(zi.zip_code)

        dim_academic     = _score_academic_need(ela)
        dim_saturation   = _score_charter_saturation(zi.charter_count, city_max_charters)
        dim_school_age   = _score_school_age_population(cd.school_age_pop, city_max_pop)
        dim_income       = _score_income_concentration(cd.poverty_rate)
        dim_ell          = _score_ell_concentration(cd.ell_pct)

        composite = round(
            dim_academic    * weights["academic_need"] +
            dim_saturation  * weights["charter_saturation"] +
            dim_school_age  * weights["school_age_population"] +
            dim_income      * weights["income_concentration"] +
            dim_ell         * weights["ell_concentration"],
            2
        )

        scored.append(ZipScore(
            zip_code=zi.zip_code,
            rank=0,  # assigned below
            composite=composite,
            academic_need=dim_academic,
            charter_saturation=dim_saturation,
            school_age_population=dim_school_age,
            income_concentration=dim_income,
            ell_concentration=dim_ell,
            ela_proficiency_pct=ela,
            charter_count=zi.charter_count,
            total_schools=zi.total_schools,
            charter_names=zi.charter_names,
            school_age_pop=cd.school_age_pop,
            poverty_rate_pct=round(cd.poverty_rate * 100, 1) if cd.poverty_rate is not None else None,
            ell_pct_display=round(cd.ell_pct * 100, 1) if cd.ell_pct is not None else None,
            median_income=cd.median_household_income,
            census_available=not _census_empty(cd),
        ))

    scored.sort(key=lambda s: s.composite, reverse=True)
    for i, s in enumerate(scored, 1):
        s.rank = i

    return scored


# ── Step 5: Render ────────────────────────────────────────────────────────────

def render_html(
    scores: list[ZipScore],
    excluded: list[ZipScore],
    city_name: str,
    state: str,
) -> str:
    """Render ZIP Drill results to HTML using Jinja2. Returns output path."""
    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=select_autoescape([]),
    )

    template_name = "zip_results.html.j2"
    if not os.path.exists(os.path.join("templates", template_name)):
        raise FileNotFoundError(
            f"Template not found: templates/{template_name}. "
            "Create it before running ZIP Drill."
        )

    city_slug = re.sub(r"[^a-z0-9]+", "-", city_name.lower()).strip("-")
    out_dir = f"outputs/zip/{city_slug}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = f"{out_dir}/{city_slug}_zip_drill.html"

    rendered = env.get_template(template_name).render(
        scores=scores,
        excluded=excluded,
        city_name=city_name,
        state=state,
        generated_at=datetime.date.today().isoformat(),
        zip_count=len(scores),
    )

    with open(out_path, "w") as f:
        f.write(rendered)

    return out_path


# ── Public entry point ────────────────────────────────────────────────────────

def run(city_name: str, state: str = "NM") -> str:
    """
    Full ZIP Drill pipeline for a single city.
    Returns the output HTML path.
    """
    log.info("zip_drill: starting for %s, %s", city_name, state)

    # Load weights from config
    with open("config/scoring_weights.yaml") as f:
        cfg = yaml.safe_load(f)
    weights = cfg["zip_preset"]

    # Step 1 — discover zip codes and school counts
    zip_infos = discover_zips(city_name, state)
    zip_codes = [zi.zip_code for zi in zip_infos]
    log.info("zip_drill: %d zip codes — %s", len(zip_codes), zip_codes)

    # Step 2 — fetch Census ACS ZCTA data
    census = fetch_census_acs(zip_codes)

    # Step 3 — aggregate PED proficiency per zip
    proficiency = aggregate_proficiency(zip_codes, state, city_name)

    # Step 4 — score all zips, then split ranked vs excluded
    all_scores = score_zips(zip_infos, census, proficiency, weights)

    ranked   = [s for s in all_scores if s.census_available]
    excluded = [s for s in all_scores if not s.census_available]

    # Re-rank only the included zips (already sorted by composite descending)
    for i, s in enumerate(ranked, 1):
        s.rank = i

    if excluded:
        log.info(
            "zip_drill: %d zip(s) excluded — no Census population data: %s",
            len(excluded),
            [s.zip_code for s in excluded],
        )

    # Step 5 — render HTML
    out_path = render_html(ranked, excluded, city_name, state)

    log.info("zip_drill: output written to %s", out_path)
    return out_path
