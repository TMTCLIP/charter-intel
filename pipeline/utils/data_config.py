"""
Central data vintage configuration loader.

Reads data_vintages from config/pipeline.yaml once at import time and exposes
typed constants. All fetchers import from here so vintage upgrades are
one-line changes in pipeline.yaml.

Falls back to hardcoded defaults if config/pipeline.yaml is missing or the
data_vintages section is absent — safe for unit tests that chdir to tmp_path.
"""
import os

import yaml


def _load_vintages() -> dict:
    for path in (
        "config/pipeline.yaml",
        os.path.join(os.path.dirname(__file__), "..", "..", "config", "pipeline.yaml"),
    ):
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f)
            return cfg.get("data_vintages", {})
        except FileNotFoundError:
            continue
        except Exception:
            break
    return {}


_v = _load_vintages()

NCES_CCD_YEAR: int = _v.get("nces_ccd_year", 2024)
NCES_FINANCE_FISCAL_YEAR: str = _v.get("nces_finance_fiscal_year", "2022-2023")
NCES_MEMBERSHIP_YEARS: list = _v.get("nces_ccd_membership_years", [2020, 2022, 2023, 2024])
ACS_YEAR: str = _v.get("acs_year", "2024")
ACS_POP_VINTAGE_EARLY: str = _v.get("acs_pop_vintage_early", "2019")
ACS_POP_VINTAGE_LATE: str = _v.get("acs_pop_vintage_late", "2024")
ED_DATA_EXPRESS_CA_YEAR: int = _v.get("ed_data_express_ca_year", 2023)
ED_DATA_EXPRESS_CA_ZIP_URL: str = _v.get(
    "ed_data_express_ca_zip_url",
    "https://eddataexpress.ed.gov/sites/default/files/2024-11/SY2223%20Chronic%20Absenteeism%20EDE%20110724.zip",
)
ED_DATA_EXPRESS_CA_CSV: str = _v.get(
    "ed_data_express_ca_csv", "SY2223_DG814PCT_LEA_082724.csv"
)
URBAN_ENROLLMENT_YEARS: list = _v.get(
    "urban_enrollment_years", [2019, 2020, 2021, 2022, 2023, 2024]
)
