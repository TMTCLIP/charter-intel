"""
tests/unit/test_bls_teacher_wages_fetcher.py

Unit tests for pipeline/fetchers/bls_teacher_wages_fetcher.py. Mocks the BLS
network call (_post_series) and disk cache; zero real network calls.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.fetchers.bls_teacher_wages_fetcher as bls

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(bls, "_read_cache", lambda key: None)
    monkeypatch.setattr(bls, "_write_cache", lambda key, data: None)


class TestSeriesConstruction:
    def test_state_series_id_matches_verified_format(self):
        # Verified live: MS elementary median = OEUS280000000000025202111
        assert bls._series_id("S", bls._state_area_code("28"), "252021") == "OEUS280000000000025202111"
        assert bls._series_id("S", bls._state_area_code("28"), "252031") == "OEUS280000000000025203111"

    def test_state_area_code_pads_fips(self):
        assert bls._state_area_code("28") == "2800000"
        assert bls._state_area_code("6") == "0600000"


class TestGetTeacherWages:
    def _resp(self, elem=46020, sec=45760, year="2025"):
        elem_sid = bls._series_id("S", "2800000", "252021")
        sec_sid = bls._series_id("S", "2800000", "252031")
        out = {}
        if elem is not None:
            out[elem_sid] = {"year": year, "value": str(elem)}
        if sec is not None:
            out[sec_sid] = {"year": year, "value": str(sec)}
        return out

    def test_state_level_wages(self, monkeypatch):
        monkeypatch.setattr(bls, "_post_series", lambda sids: self._resp())
        r = bls.get_teacher_wages("28")
        assert r["elementary_teacher_median_wage"] == 46020
        assert r["secondary_teacher_median_wage"] == 45760
        assert r["geography_level"] == "state"
        assert r["wage_year"] == 2025
        assert r["area_code"] == "2800000"

    def test_missing_state_fips_returns_none(self):
        assert bls.get_teacher_wages("") is None
        assert bls.get_teacher_wages(None) is None

    def test_no_data_returns_none(self, monkeypatch):
        monkeypatch.setattr(bls, "_post_series", lambda sids: {})
        assert bls.get_teacher_wages("28") is None

    def test_msa_falls_back_to_state(self, monkeypatch):
        calls = []
        def fake_post(sids):
            calls.append(sids)
            # MSA series (area_type M) return nothing; state series return data.
            if any(s.startswith("OEUM") for s in sids):
                return {}
            return self._resp()
        monkeypatch.setattr(bls, "_post_series", fake_post)
        r = bls.get_teacher_wages("28", msa_code="1234567")
        assert r["geography_level"] == "state"   # fell back
        assert len(calls) == 2                    # tried MSA, then state

    def test_partial_data_one_occupation(self, monkeypatch):
        monkeypatch.setattr(bls, "_post_series", lambda sids: self._resp(elem=46020, sec=None))
        r = bls.get_teacher_wages("28")
        assert r["elementary_teacher_median_wage"] == 46020
        assert r["secondary_teacher_median_wage"] is None
