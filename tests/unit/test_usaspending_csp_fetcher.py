"""
tests/unit/test_usaspending_csp_fetcher.py

Unit tests for pipeline/utils/usaspending_csp_fetcher.py.
Mocks the network (urllib / _post / _fetch_all_awards) and the filesystem cache;
zero real network calls. Fixtures are trimmed from REAL USAspending responses
for NM CSP (CFDA 84.282) awards.
"""
from __future__ import annotations

import json
import os
import sys
import unittest.mock as mock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.utils.usaspending_csp_fetcher as usp

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(usp, "_read_cache", lambda key: None)
    monkeypatch.setattr(usp, "_write_cache", lambda key, data: None)


# ── Trimmed REAL award rows (NM CSP, CFDA 84.282) ─────────────────────────────

def _row(name, amount, cfda="84.282", state="NM", award_id="U282X000001"):
    return {
        "internal_id": 1,
        "Award ID": award_id,
        "Recipient Name": name,
        "Award Amount": amount,
        "Start Date": "2018-10-01",
        "Awarding Agency": "Department of Education",
        "Place of Performance State Code": state,
        "CFDA Number": cfda,
    }


# Two awards to the same operator (one with punctuation/case variance) + a
# second distinct operator -> distinct_operators must be 2.
REAL_RECIPIENT_ROWS = [
    _row("NACA-INSPIRED SCHOOLS NETWORK", 1810451.05, award_id="U282T180018"),
    _row("NACA Inspired Schools Network", 2275109.10, award_id="U282M160022"),
    _row("IDEA PUBLIC SCHOOLS", 31895398.0, state="TX", award_id="S282A230009"),
]


def _resp(body: dict, status: int = 200):
    cm = mock.MagicMock()
    inner = cm.__enter__.return_value
    inner.status = status
    inner.read.return_value = json.dumps(body).encode("utf-8")
    cm.__exit__.return_value = False
    return cm


# ── LocationObject construction + adjacency expansion ─────────────────────────

class TestLocationObject:
    def test_recipient_scope_expands_to_adjacent_states(self):
        assert usp._states_for_scope("recipient", "NM") == ["NM", "TX", "AZ", "CO", "OK", "UT"]

    def test_place_of_performance_scope_is_state_only(self):
        assert usp._states_for_scope("place_of_performance", "NM") == ["NM"]

    def test_recipient_location_filter_shape(self):
        key, locs = usp._build_location_filter("recipient", "NM")
        assert key == "recipient_locations"
        assert locs[0] == {"country": "USA", "state": "NM"}
        assert {l["state"] for l in locs} == {"NM", "TX", "AZ", "CO", "OK", "UT"}

    def test_place_of_performance_location_filter_shape(self):
        key, locs = usp._build_location_filter("place_of_performance", "NM")
        assert key == "place_of_performance_locations"
        assert locs == [{"country": "USA", "state": "NM"}]

    def test_county_scope_is_state_only_for_states(self):
        assert usp._states_for_scope("place_of_performance_county", "NM") == ["NM"]

    def test_county_location_filter_includes_3digit_county(self):
        key, locs = usp._build_location_filter("place_of_performance_county", "NM", county="015")
        assert key == "place_of_performance_locations"
        assert locs == [{"country": "USA", "state": "NM", "county": "015"}]

    def test_county_scope_without_county_omits_county_key(self):
        # defensive: no county supplied -> falls back to state-level shape
        key, locs = usp._build_location_filter("place_of_performance_county", "NM", county=None)
        assert "county" not in locs[0]

    def test_filters_include_program_and_award_types(self):
        f = usp._build_filters("recipient", "NM", usp.CSP_PROGRAM_NUMBERS)
        assert f["program_numbers"] == ["84.282"]
        assert f["award_type_codes"] == ["02", "03", "04", "05"]
        assert "recipient_locations" in f
        assert f["time_period"][0]["start_date"] >= usp._EARLIEST_START


# ── Operator normalization ────────────────────────────────────────────────────

class TestNormalizeOperator:
    def test_case_and_punctuation_collapse(self):
        assert usp._normalize_operator("NACA-INSPIRED SCHOOLS NETWORK") == \
               usp._normalize_operator("NACA Inspired Schools Network")

    def test_none_and_blank(self):
        assert usp._normalize_operator(None) is None
        assert usp._normalize_operator("   ") is None


# ── _post: status handling ────────────────────────────────────────────────────

class TestPost:
    def test_200_returns_dict(self):
        with mock.patch("urllib.request.urlopen", return_value=_resp({"results": []})):
            assert usp._post({"x": 1}) == {"results": []}

    def test_non_200_returns_none(self):
        with mock.patch("urllib.request.urlopen", return_value=_resp({}, status=503)):
            assert usp._post({}) is None

    def test_exception_returns_none(self):
        with mock.patch("urllib.request.urlopen", side_effect=Exception("HTTP 500")):
            assert usp._post({}) is None

    def test_malformed_body_returns_none(self):
        cm = mock.MagicMock()
        cm.__enter__.return_value.status = 200
        cm.__enter__.return_value.read.return_value = b"not json{"
        cm.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=cm):
            assert usp._post({}) is None


# ── _fetch_all_awards: pagination ─────────────────────────────────────────────

class TestPagination:
    def test_follows_hasnext_across_pages(self):
        page1 = {"results": [_row("OP A", 100)], "page_metadata": {"hasNext": True}}
        page2 = {"results": [_row("OP B", 200)], "page_metadata": {"hasNext": False}}
        with mock.patch.object(usp, "_post", side_effect=[page1, page2]) as m:
            rows = usp._fetch_all_awards({"any": "filter"})
        assert len(rows) == 2
        assert m.call_count == 2

    def test_page_failure_returns_none(self):
        with mock.patch.object(usp, "_post", side_effect=[None]):
            assert usp._fetch_all_awards({}) is None


# ── get_csp_awards: derivation, valid-zero, fallback, None ────────────────────

class TestGetCspAwards:
    def test_populated_recipient_derivation(self):
        with mock.patch.object(usp, "_fetch_all_awards", return_value=REAL_RECIPIENT_ROWS):
            r = usp.get_csp_awards("NM", scope="recipient")
        assert r["award_count"] == 3
        assert r["distinct_operators"] == 2          # NACA variants collapse
        assert r["total_dollars"] == pytest.approx(35980958.15)
        assert r["matched_cfda_numbers"] == ["84.282"]
        assert r["matched_program_numbers"] == ["84.282"]
        assert r["states_queried"] == ["NM", "TX", "AZ", "CO", "OK", "UT"]
        assert r["scope"] == "recipient"

    def test_empty_result_is_valid_zero_not_none(self):
        # primary empty AND fallback empty -> a valid zero result
        with mock.patch.object(usp, "_fetch_all_awards", side_effect=[[], []]):
            r = usp.get_csp_awards("NM", scope="recipient")
        assert r is not None
        assert r["award_count"] == 0
        assert r["distinct_operators"] == 0
        assert r["total_dollars"] == 0.0

    def test_zero_primary_triggers_cfda_fallback(self):
        # primary 84.282 returns nothing; fallback family returns rows
        fallback_rows = [_row("SOME OPERATOR", 500.0, cfda="84.282M")]
        with mock.patch.object(usp, "_fetch_all_awards", side_effect=[[], fallback_rows]):
            r = usp.get_csp_awards("NM", scope="recipient")
        assert r["award_count"] == 1
        assert r["matched_program_numbers"] == usp.CSP_PROGRAM_NUMBERS_FALLBACK
        assert r["matched_cfda_numbers"] == ["84.282M"]

    def test_http_failure_returns_none(self):
        with mock.patch.object(usp, "_fetch_all_awards", return_value=None):
            assert usp.get_csp_awards("NM", scope="recipient") is None

    def test_no_state_returns_none(self):
        assert usp.get_csp_awards("", scope="recipient") is None

    def test_cache_hit_skips_fetch(self, monkeypatch):
        canned = {"award_count": 7, "distinct_operators": 4}
        monkeypatch.setattr(usp, "_read_cache", lambda key: canned)
        with mock.patch.object(usp, "_fetch_all_awards") as m:
            r = usp.get_csp_awards("NM", scope="recipient")
        m.assert_not_called()
        assert r == canned


# ── ADJACENT_STATES expansion (MS, TN, WI) ───────────────────────────────────

class TestAdjacentStatesExpansion:
    def test_ms_in_adjacent_states(self):
        assert "MS" in usp.ADJACENT_STATES

    def test_tn_in_adjacent_states(self):
        assert "TN" in usp.ADJACENT_STATES

    def test_wi_in_adjacent_states(self):
        assert "WI" in usp.ADJACENT_STATES

    def test_ms_adjacent_states_content(self):
        assert usp.ADJACENT_STATES["MS"] == ["MS", "AL", "AR", "LA", "TN"]

    def test_tn_adjacent_states_content(self):
        assert usp.ADJACENT_STATES["TN"] == ["TN", "AL", "AR", "GA", "KY", "MO", "MS", "NC", "VA"]

    def test_wi_adjacent_states_content(self):
        assert usp.ADJACENT_STATES["WI"] == ["WI", "IL", "IA", "MI", "MN"]

    def test_unknown_state_does_not_raise_keyerror(self):
        """get_csp_awards with an unknown state must not raise KeyError."""
        with mock.patch.object(usp, "_fetch_all_awards", return_value=[]):
            result = usp.get_csp_awards("ZZ", scope="recipient")
        # A zero result (valid ZERO) — no KeyError, no crash
        assert result is not None
        assert result["award_count"] == 0

    def test_unknown_state_fallback_is_state_only(self):
        """_states_for_scope with an unknown state returns [state] only."""
        assert usp._states_for_scope("recipient", "ZZ") == ["ZZ"]


class TestCountyScope:
    def test_populated_county_signal(self):
        rows = [_row("NACA INSPIRED SCHOOLS NETWORK", 100.0, state="NM")]
        with mock.patch.object(usp, "_fetch_all_awards", return_value=rows):
            r = usp.get_csp_awards("NM", scope="place_of_performance_county", county_fips="001")
        assert r["distinct_operators"] == 1
        assert r["county_fips"] == "001"
        assert r["scope"] == "place_of_performance_county"

    def test_zero_in_county_is_valid_zero(self):
        # rural county with no CSP performance -> a real 0, not None
        with mock.patch.object(usp, "_fetch_all_awards", side_effect=[[], []]):
            r = usp.get_csp_awards("NM", scope="place_of_performance_county", county_fips="015")
        assert r is not None
        assert r["distinct_operators"] == 0
        assert r["county_fips"] == "015"

    def test_county_scope_requires_county_fips(self):
        assert usp.get_csp_awards("NM", scope="place_of_performance_county") is None
