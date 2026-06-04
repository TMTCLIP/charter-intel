"""
tests/unit/test_s6_data_through.py

Unit tests for _derive_data_through() and the data_through/generated_at
population logic in s6_synthesis.py.
"""
from __future__ import annotations

import datetime
import os
import sys
from unittest.mock import patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)

from pipeline.s6_synthesis import _derive_data_through


# ─────────────────────────────────────────────────────────────────────────────
# _derive_data_through — return type and format
# ─────────────────────────────────────────────────────────────────────────────
def test_returns_valid_iso_date_string():
    brief = {
        "sources": [{"source_class": "FEDERAL_DATA", "date": "2023-07-01"}],
    }
    result = _derive_data_through(brief)
    datetime.date.fromisoformat(result)  # must not raise


def test_result_ends_with_dec_31():
    brief = {
        "sources": [{"source_class": "FEDERAL_DATA", "date": "2022-04-15"}],
    }
    result = _derive_data_through(brief)
    assert result.endswith("-12-31")


# ─────────────────────────────────────────────────────────────────────────────
# Uses oldest vintage when multiple sources present
# ─────────────────────────────────────────────────────────────────────────────
def test_uses_oldest_when_multiple_sources():
    brief = {
        "sources": [
            {"source_class": "FEDERAL_DATA", "date": "2024-01-01"},  # 2024
            {"source_class": "FEDERAL_DATA", "date": "2022-07-01"},  # 2022 ← oldest
            {"source_class": "PED_DATA",     "date": "2025-09-01"},  # 2025
        ],
    }
    assert _derive_data_through(brief) == "2022-12-31"


def test_proficiency_year_included_in_comparison():
    """proficiency_year=2021 is older than any source date → must be selected."""
    brief = {
        "sources": [
            {"source_class": "FEDERAL_DATA", "date": "2023-01-01"},
        ],
        "top_charter_schools": [
            {"proficiency_year": 2021},
        ],
    }
    assert _derive_data_through(brief) == "2021-12-31"


def test_multiple_proficiency_years_oldest_wins():
    brief = {
        "sources": [],
        "top_charter_schools": [
            {"proficiency_year": 2024},
            {"proficiency_year": 2019},
            {"proficiency_year": 2022},
        ],
    }
    assert _derive_data_through(brief) == "2019-12-31"


# ─────────────────────────────────────────────────────────────────────────────
# Ephemeral / media sources excluded
# ─────────────────────────────────────────────────────────────────────────────
def test_media_source_date_excluded():
    """A Wikipedia article date must not count as a data vintage."""
    brief = {
        "sources": [
            {"source_class": "MEDIA",        "date": "2026-01-26"},  # excluded
            {"source_class": "FEDERAL_DATA", "date": "2023-07-01"},  # included
        ],
    }
    assert _derive_data_through(brief) == "2023-12-31"


def test_web_search_source_date_excluded():
    brief = {
        "sources": [
            {"source_class": "WEB_SEARCH", "date": "2025-12-01"},  # excluded
        ],
        "top_charter_schools": [{"proficiency_year": 2024}],
    }
    assert _derive_data_through(brief) == "2024-12-31"


def test_none_date_sources_skipped():
    brief = {
        "sources": [
            {"source_class": "FEDERAL_DATA", "date": None},
            {"source_class": "FEDERAL_DATA", "date": "2023-01-01"},
        ],
    }
    assert _derive_data_through(brief) == "2023-12-31"


# ─────────────────────────────────────────────────────────────────────────────
# Fallback when no vintage fields exist
# ─────────────────────────────────────────────────────────────────────────────
def test_fallback_when_no_vintage_fields():
    """No sources, no proficiency years → fallback to (today.year - 1)-12-31."""
    brief = {}
    result = _derive_data_through(brief)
    expected_year = datetime.date.today().year - 1
    assert result == f"{expected_year}-12-31"


def test_fallback_when_all_sources_have_none_date():
    brief = {
        "sources": [
            {"source_class": "FEDERAL_DATA", "date": None},
            {"source_class": "FEDERAL_DATA", "date": None},
        ],
    }
    result = _derive_data_through(brief)
    expected_year = datetime.date.today().year - 1
    assert result == f"{expected_year}-12-31"


def test_fallback_when_only_media_sources():
    brief = {
        "sources": [{"source_class": "MEDIA", "date": "2025-06-01"}],
    }
    result = _derive_data_through(brief)
    expected_year = datetime.date.today().year - 1
    assert result == f"{expected_year}-12-31"


# ─────────────────────────────────────────────────────────────────────────────
# data_through must not be today's date (the original bug)
# ─────────────────────────────────────────────────────────────────────────────
def test_data_through_is_not_todays_date():
    """data_through must reflect data vintage, never today's date."""
    brief = {
        "sources": [
            {"source_class": "FEDERAL_DATA", "date": "2023-07-01"},
        ],
    }
    result = _derive_data_through(brief)
    today = datetime.date.today().isoformat()
    assert result != today, f"data_through must not equal today ({today})"


def test_data_through_is_not_today_even_when_source_date_is_today():
    """If a federal source date happens to be today, data_through is {today.year}-12-31,
    which differs from the raw today ISO string — still semantically a year-end date."""
    today_iso = datetime.date.today().isoformat()
    brief = {
        "sources": [
            {"source_class": "FEDERAL_DATA", "date": today_iso},
        ],
    }
    result = _derive_data_through(brief)
    assert result.endswith("-12-31")
    assert result != today_iso  # year-end date ≠ raw today


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────
def test_unreasonable_proficiency_year_ignored():
    """Years outside 1990–2100 are ignored (guard against bad data)."""
    brief = {
        "sources": [{"source_class": "FEDERAL_DATA", "date": "2022-01-01"}],
        "top_charter_schools": [
            {"proficiency_year": 1800},  # ignored
            {"proficiency_year": 2500},  # ignored
            {"proficiency_year": 2023},  # included
        ],
    }
    assert _derive_data_through(brief) == "2022-12-31"


def test_missing_source_class_treated_as_included():
    """Sources without a source_class should NOT be excluded (don't crash)."""
    brief = {
        "sources": [
            {"date": "2021-03-01"},  # no source_class — included by default
        ],
    }
    assert _derive_data_through(brief) == "2021-12-31"
