"""
tests/unit/test_crdc_fetcher.py

Unit tests for pipeline/fetchers/crdc_fetcher.py. Mocks the network (_get) and
disk cache; zero real network calls. Fixtures mirror REAL Urban CRDC/CCD shapes,
including the 2021 duplicate-row quirk that must be deduped by ncessch.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.fetchers.crdc_fetcher as crdc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(crdc, "_read_cache", lambda key: None)
    monkeypatch.setattr(crdc, "_write_cache", lambda key, data: None)


def _page(results):
    return {"results": results, "next": None}


def _route(mapping):
    """Return a fake _get that maps URL substrings to response dicts."""
    def fake_get(url):
        for frag, resp in mapping.items():
            if frag in url:
                return resp
        return None
    return fake_get


# Oxford-like fixtures (2021), with the disability=1 row DUPLICATED per school.
_ABSENT_2021 = _page([
    {"ncessch": "A", "race": 99, "sex": 99, "students_chronically_absent": 600},
    {"ncessch": "A", "race": 1, "sex": 99, "students_chronically_absent": 100},  # subgroup, ignored
    {"ncessch": "B", "race": 99, "sex": 99, "students_chronically_absent": 500},
])
_DISABILITY_2021 = _page([
    {"ncessch": "A", "disability": 1, "sex": 99, "enrollment_crdc": 400},
    {"ncessch": "A", "disability": 1, "sex": 99, "enrollment_crdc": 400},  # DUP — must collapse
    {"ncessch": "B", "disability": 1, "sex": 99, "enrollment_crdc": 256},
    {"ncessch": "A", "disability": 99, "sex": 99, "enrollment_crdc": 3000},  # total, ignored
])
_CCD_2021 = _page([
    {"leaid": "2803450", "grade": 99, "race": 99, "sex": 99, "enrollment": 4729},
])


class TestAggregation:
    def test_dedup_prevents_iep_double_count(self, monkeypatch):
        monkeypatch.setattr(crdc, "_CRDC_YEARS", (2021,))
        monkeypatch.setattr(crdc, "_get", _route({
            "chronic-absenteeism/2021": _ABSENT_2021,
            "crdc/enrollment/2021": _DISABILITY_2021,
            "ccd/enrollment/2021": _CCD_2021,
        }))
        r = crdc.get_crdc_district_data("2803450")
        # IDEA = 400 (A, deduped) + 256 (B) = 656, NOT 1056
        assert r["iep_count"] == 656
        assert r["iep_pct"] == round(656 / 4729 * 100, 1)  # 13.9
        # Chronic absent = 600 + 500 = 1100 (subgroup rows excluded)
        assert r["chronic_absent_count"] == 1100
        assert r["chronic_absenteeism_pct"] == round(1100 / 4729 * 100, 1)
        assert r["iep_year"] == 2021

    def test_missing_leaid_returns_none(self):
        assert crdc.get_crdc_district_data("") is None
        assert crdc.get_crdc_district_data(None) is None

    def test_returns_none_when_no_data(self, monkeypatch):
        monkeypatch.setattr(crdc, "_CRDC_YEARS", (2021,))
        monkeypatch.setattr(crdc, "_get", lambda url: _page([]))
        assert crdc.get_crdc_district_data("2803450") is None

    def test_absenteeism_only_when_no_disability_rows(self, monkeypatch):
        monkeypatch.setattr(crdc, "_CRDC_YEARS", (2021,))
        monkeypatch.setattr(crdc, "_get", _route({
            "chronic-absenteeism/2021": _ABSENT_2021,
            "crdc/enrollment/2021": _page([]),   # no disability data
            "ccd/enrollment/2021": _CCD_2021,
        }))
        r = crdc.get_crdc_district_data("2803450")
        assert r is not None
        assert r["iep_pct"] is None
        assert r["chronic_absenteeism_pct"] is not None

    def test_no_denominator_yields_no_rate(self, monkeypatch):
        monkeypatch.setattr(crdc, "_CRDC_YEARS", (2021,))
        monkeypatch.setattr(crdc, "_get", _route({
            "chronic-absenteeism/2021": _ABSENT_2021,
            "crdc/enrollment/2021": _DISABILITY_2021,
            "ccd/enrollment/2021": _page([]),    # no CCD denominator
        }))
        # Both signals need the CCD denominator → both None → overall None
        assert crdc.get_crdc_district_data("2803450") is None

    def test_carries_vintage_note(self, monkeypatch):
        monkeypatch.setattr(crdc, "_CRDC_YEARS", (2021,))
        monkeypatch.setattr(crdc, "_get", _route({
            "chronic-absenteeism/2021": _ABSENT_2021,
            "crdc/enrollment/2021": _DISABILITY_2021,
            "ccd/enrollment/2021": _CCD_2021,
        }))
        r = crdc.get_crdc_district_data("2803450")
        assert "biennially" in r["data_note"]
        assert r["confidence"] == "MODERATE"
