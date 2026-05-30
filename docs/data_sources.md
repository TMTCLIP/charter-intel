# CLIP Data Sources

Charter Community Landscape Intelligence Platform — data source inventory for The Mind Trust's New Mexico charter strategy pipeline.

## Overview

CLIP ingests data from five categories of sources: static configuration files (states.yaml, scoring YAML), downloaded NM PED and NCES CSV datasets, U.S. Census API calls (ACS), Anthropic web-search calls (Sonnet with `web_search_20250305` tool), and supplemental geospatial data for the zip-drill feature. Every fact is tagged with a `source_class` and `confidence` level, flows through the S4 hallucination audit, and is subject to the anti-fabrication rules enforced throughout S3–S6. Where a source is absent or returns no usable data, the pipeline produces a verified `NOT_FOUND` or `PROVISIONAL` fact rather than inventing a value.

---

## Data Source Inventory

| Source | Integration type | Stage(s) | Key fields produced | TTL | Known gaps / limits |
|---|---|---|---|---|---|
| **NM PED Charter School Roster** (CSV) | Downloaded CSV | S1, S3, zip_drill | school_name, authorizer_name, authorizer_type, grades_served, enrollment_cap, contact_phone, contact_email | 90 days | Authorizer type defaults to `LEA` if not in `_AUTHORIZER_TYPES` dict (logged as warning — Review Fix L). Contact info requires human verification before external use. |
| **NM PED Achievement Data** (proficiency CSVs) | Downloaded CSV | S3, zip_drill | ela_proficiency_pct, math_proficiency_pct (district-level and school-level) | 365 days | Two format variants (ELA has metadata row; Math has UTF-8 BOM). District-level only for community scoring. School-level matching uses exact → normalised → prefix heuristic; unmatched schools get null. |
| **NCES CCD LEA Finance / Lunch** (CSV) | Downloaded CSV | S3 | per_pupil_expenditure, per_pupil_revenue_vs_state_avg_pct, revenue_local/federal/state_pct, frl_pct, frl_compressed (flag ≥80%) | 365 days | ELL % not in NCES finance files. FRL saturates at high percentages — `frl_compressed=True` flagged at ≥80% (Session 18 Area H). Returns null gracefully if community has no NCES mapping. |
| **NCES CCD LEA Membership / Enrollment** (CSV + Parquet cache) | Downloaded CSV; Parquet fast-path built on first run | S3, population_trends_fetcher | enrollment_by_year, trend_direction, pct_change_total, years_available | 365 days | Four files: 2020, 2022, 2023 (school-level), 2024 (district-level). 2021 intentionally absent. Requires ≥2 years; earliest enrollment must be >0. Parquet cache built via `scripts/build_nces_cache.py`. |
| **NCES CCD Schools master** (CSV) | Downloaded CSV | zip_drill, zip_drill_v2 | school names and charter counts by ZIP | 365 days | Filtered by MCITY; unmatched cities raise ValueError. |
| **U.S. Census ACS 5-Year, Table B16004** (ELL) | API (`api.census.gov`) | S3, zip_drill, zip_drill_v2 | ell_pct | 180 days | Requires `CENSUS_API_KEY` env var. Suppression sentinels (negative values) trigger guard. All-zero rows for small districts (<1 500 enrollment). Sampling error → confidence MODERATE. If >4/8 ELL cells are suppressed, returns `ell_data_suppressed=True` (disclosed in brief via `_inject_ell_gap_disclosure`). |
| **U.S. Census ACS 5-Year, Table B01003 / population** | API (`api.census.gov`) | zip_drill, zip_drill_v2 | pop_2019, pop_2023, pct_change | 180 days | NM only (ACS population trend function). Requires same API key. |
| **Census SAIPE School District Poverty Estimates** | API (`api.census.gov/data/timeseries/poverty/saipe/schdist`) | S3 | saipe_poverty_rate_pct, saipe_poverty_count_5_17, total_population | 365 days | Requires `CENSUS_API_KEY`. Attempts unified → elementary → secondary district types. Tries current_year-1 then current_year-2. Communities with null NCES mappings (nm-156-navajo, nm-4386-shiprock) return None. Confidence MODERATE (model-based estimates). Integrated Session 18 Task 1. |
| **Census TIGER/Line 2023 ZCTA Shapefile** | Pre-downloaded Shapefile (geopandas) | zip_drill_v2 | zcta_geometries, centroid_lat/lon, neighboring ZIPs | N/A (static) | Must be pre-downloaded via `scripts/download_zcta_shapefile.sh`. Requires geopandas/shapely. Non-residential ZCTAs absent with warning. Simplified to 0.004° for SVG rendering. |
| **Transitland API v2 (GTFS)** | API (`transit.land`) | zip_drill_v2 | transit_stop_count, transit_route_count | 30 days | Requires `TRANSITLAND_API_KEY`. Cached to `data/cache/zip/transit/{zip}.json`. 0.5-mile radius around ZCTA centroid. Returns (null, null) if key absent. |
| **Anthropic Claude web search** (`web_search_20250305`) | Anthropic API + web search tool | S2 (state context), S3 (5 supplemental dimensions) | political_climate_index, population_trend_index, operational_complexity_index, funding_environment_index, facilities_feasibility_index | 30 days (political_climate); 90 days (S2 state context) | Model must search before scoring (Session 18 Area A). NOT_FOUND → LOW/excluded; thin rural market → MODERATE/PROVISIONAL. Model may return stale or incomplete web results. |
| **Anthropic Claude synthesis** (Haiku/Sonnet/Opus) | Anthropic API | S2, S3 (political Haiku gate + Sonnet search), S6 (brief + audit) | State context pack, brief text, hallucination audit | 90 days (S2); 30 days (S3 political climate) | Haiku gate runs for every community at `--depth standard` (rule-based tier effectively retired — Review Fix 2 comment). ~$0.0003/community × 28 NM cities ≈ $0.008/statewide scan just for gate calls. |
| **`config/states.yaml`** | Static YAML config | S1, S3, S5, S6, all fetchers | nces_district_map, nm_district_map, charter_law, community_metadata, tribal_jurisdiction_flags, teacher_supply_warning, excluded_communities | N/A (static) | Tribal flags require human determination. Teacher supply warning is UNVERIFIED statewide; per-community designations not yet populated (open TODO). |
| **`config/scoring_weights.yaml` + `scoring_presets.yaml`** | Static YAML config | S5 | Dimension weights, thresholds, preset adjustments | N/A (static) | Weights flagged as uncalibrated — do not use for real decisions until operator profile conversation is completed. |

---

## Known Gaps

| Dimension | Missing source | Severity | Status |
|---|---|---|---|
| Teacher supply / staffing feasibility | NMPED hard-to-staff / shortage-area list (per-community designations) | HIGH | Open TODO — statewide warning injected by S6; per-community status unverified. Do not invent designations. |
| Tribal charter jurisdiction | Legal/policy determination for `nm-gallup` and `nm-espanola` | HIGH | `PENDING_HUMAN_REVIEW` flag set (Review Fix 3); `nm-jemez-pueblo`, `nm-156-navajo`, `nm-4386-shiprock` are `FLAGGED`. No assertion of sovereignty from any flag. |
| PEC authorization geography | PEC historical authorization pattern by region | MODERATE | PROVISIONAL per Session 18 Area K; needs_verification injected in S2 output. Resolution: review PEC approved-school roster by region. |
| Population income / poverty | SAIPE (Small Area Income and Poverty Estimates) | MODERATE | **Integrated — Session 18 Task 1.** `saipe_fetcher.py` injects `saipe_poverty_rate_pct` into `operational_complexity` + `funding_environment` and `saipe_poverty_count_5_17` into `funding_environment`. Cached 365 days. Communities with null NCES mappings (Navajo Nation) return None gracefully. |
| Disability / IEP rate | EDFacts (IDEA Child Count) | MODERATE | Pending download approval. Would improve operational_complexity_index. Currently sourced via web search only (PROVISIONAL). |
| ELL rate (small districts) | ACS B16004 suppressed for districts with <~1 500 enrollment | LOW–MODERATE | Disclosed via `_inject_ell_gap_disclosure` in brief needs_verification when `ell_data_suppressed=True`. Resolution: NM PED state report card or district profile. |
| Political climate (baseline) | Community-level political index source when Haiku gate returns NO | LOW | DATA GAP baseline injected with PROVISIONAL status; needs_verification surfaced. Resolution: Sonnet web-search at `--depth deep`. |

---

*Generated with Claude assistance. Data source details derived from reading the pipeline source code as of 2026-05-29. Requires human review — source coverage may have changed since this document was written.*
