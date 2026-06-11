"""
tests/unit/test_urban_enrollment_fetcher.py

Unit tests for pipeline/utils/urban_enrollment_fetcher.py.
Mocks all network and disk I/O — zero real HTTP calls.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.utils.urban_enrollment_fetcher as uef

pytestmark = pytest.mark.unit

# ── fixtures ──────────────────────────────────────────────────────────────────

_OXFORD_LEAID = "2803450"
_OXFORD_LEAID_RAW = "2803450"

# Simulated Urban API responses for grade-99 endpoint by year
_ENROLLMENT_BY_YEAR = {
    2019: 4100,
    2020: 4200,
    2021: 4350,
    2022: 4450,
    2023: 4553,
    2024: 4600,
}


def _make_fake_fetch_year(data: dict):
    """Return a fake _fetch_year that returns enrollment from a dict."""
    def fake(leaid: str, year: int):
        return data.get(year)
    return fake


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(uef, "_read_cache", lambda leaid: None)
    monkeypatch.setattr(uef, "_write_cache", lambda leaid, data: None)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestTrendFetch:
    def test_full_trend_returned(self, monkeypatch):
        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2019, 2020, 2021, 2022, 2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", _make_fake_fetch_year(_ENROLLMENT_BY_YEAR))

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)

        assert result is not None
        assert len(result["enrollment_trend"]) == 6
        assert result["years_fetched"] == [2019, 2020, 2021, 2022, 2023, 2024]
        assert result["enrollment_trend"][0] == {"year": 2019, "enrollment": 4100}
        assert result["enrollment_trend"][-1] == {"year": 2024, "enrollment": 4600}

    def test_2021_gap_year_included(self, monkeypatch):
        """Urban API provides 2021 even though NCES membership parquet omits it."""
        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2019, 2020, 2021, 2022, 2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", _make_fake_fetch_year(_ENROLLMENT_BY_YEAR))

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        years = [r["year"] for r in result["enrollment_trend"]]
        assert 2021 in years

    def test_partial_years_still_returns(self, monkeypatch):
        """When only some years have data, return what's available (≥2)."""
        partial = {2022: 4450, 2023: 4553}
        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2019, 2020, 2021, 2022, 2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", _make_fake_fetch_year(partial))

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        assert result is not None
        assert result["years_fetched"] == [2022, 2023]

    def test_only_one_year_returns_none(self, monkeypatch):
        """Fewer than 2 years of data → None (can't compute trend)."""
        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2019, 2020, 2021, 2022, 2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", _make_fake_fetch_year({2023: 4553}))

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        assert result is None

    def test_no_data_returns_none(self, monkeypatch):
        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2019, 2020, 2021, 2022, 2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", _make_fake_fetch_year({}))

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        assert result is None

    def test_empty_leaid_returns_none(self):
        assert uef.get_urban_enrollment_trend("") is None
        assert uef.get_urban_enrollment_trend(None) is None

    def test_cache_hit_bypasses_fetch(self, monkeypatch):
        cached = {
            "enrollment_trend": [{"year": 2023, "enrollment": 4553}, {"year": 2024, "enrollment": 4600}],
            "years_fetched": [2023, 2024],
            "source_url": uef.SOURCE_URL,
            "source_title": uef.SOURCE_TITLE,
            "confidence": "HIGH",
        }
        monkeypatch.setattr(uef, "_read_cache", lambda leaid: cached)
        fetch_called = []
        monkeypatch.setattr(uef, "_fetch_year", lambda leaid, year: fetch_called.append(1) or None)

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        assert result == cached
        assert not fetch_called

    def test_output_shape_preserved(self, monkeypatch):
        """Verify all expected output keys are present."""
        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", _make_fake_fetch_year({2023: 4553, 2024: 4600}))

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        assert result is not None
        expected_keys = {"enrollment_trend", "years_fetched", "source_url", "source_title", "confidence"}
        assert expected_keys == set(result.keys())

    def test_individual_year_failure_skipped(self, monkeypatch):
        """A year that returns None (404/timeout) is skipped, not fatal."""
        def fake_fetch(leaid, year):
            if year == 2020:
                return None  # simulate 404
            return _ENROLLMENT_BY_YEAR.get(year)

        monkeypatch.setattr(uef, "URBAN_ENROLLMENT_YEARS", [2019, 2020, 2021, 2022, 2023, 2024])
        monkeypatch.setattr(uef, "_fetch_year", fake_fetch)

        result = uef.get_urban_enrollment_trend(_OXFORD_LEAID)
        assert result is not None
        years = result["years_fetched"]
        assert 2020 not in years
        assert len(years) == 5
