"""
tests/unit/test_acs_fetcher.py

Unit tests for pipeline/utils/acs_fetcher.py.
Verifies that the NM-only guard in get_total_population has been removed
and that FIPS codes are derived dynamically from state.
"""
from __future__ import annotations

import os
import sys
import unittest.mock as mock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.utils.acs_fetcher as acs_fetcher

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


# ── NM guard removed ──────────────────────────────────────────────────────────

class TestNMGuardRemoved:
    def test_ms_reaches_leaid_lookup_not_early_none(self, monkeypatch):
        """With NM guard removed, state='MS' must proceed to _load_nces_map."""
        lookup_called = []

        def fake_load(state):
            lookup_called.append(state)
            return {}  # no mapping → leaid is None → returns None gracefully

        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map", fake_load)

        result = acs_fetcher.get_total_population("any_community", "MS")

        # The NM guard would have returned None before _load_nces_map was called.
        assert "MS" in lookup_called, "MS must reach _load_nces_map (guard gone)"
        assert result is None  # None because no LEAID mapping, not because of guard

    def test_ms_returns_data_when_api_succeeds(self, monkeypatch):
        """MS returns real data once the guard is gone and the API is mocked."""
        fake_row = {"NAME": "Test District", "B01003_001E": "5000"}

        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map",
                            lambda s: {"ms_community": "2800001"})
        monkeypatch.setattr(acs_fetcher, "_acs_cache_read", lambda key: None)
        monkeypatch.setattr(acs_fetcher, "_acs_cache_write", lambda key, data: None)
        monkeypatch.setattr(acs_fetcher, "_fetch_acs_vars",
                            lambda *args, **kwargs: fake_row)

        result = acs_fetcher.get_total_population("ms_community", "MS")

        assert result is not None
        assert result["pop_2019"] == 5000
        assert result["pop_2023"] == 5000

    def test_nm_still_works_after_guard_removal(self, monkeypatch):
        """NM behaviour is unchanged: proceeds to API call, returns data."""
        fake_row = {"NAME": "APS District", "B01003_001E": "80000"}

        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map",
                            lambda s: {"nm_community": "3500060"})
        monkeypatch.setattr(acs_fetcher, "_acs_cache_read", lambda key: None)
        monkeypatch.setattr(acs_fetcher, "_acs_cache_write", lambda key, data: None)
        monkeypatch.setattr(acs_fetcher, "_fetch_acs_vars",
                            lambda *args, **kwargs: fake_row)

        result = acs_fetcher.get_total_population("nm_community", "NM")

        assert result is not None
        assert result["pop_2019"] == 80000

    def test_unknown_state_returns_none(self, monkeypatch):
        """Unknown state 'XX' cannot derive FIPS — returns None gracefully."""
        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        result = acs_fetcher.get_total_population("any_community", "XX")
        assert result is None


# ── F-002: ACS district-type fallback ────────────────────────────────────────

class TestFetchDistrictTypeFallback:
    """
    _fetch_district must try unified → elementary → secondary in order and return
    the first non-None result. Mirrors SAIPE's _DISTRICT_TYPES loop.
    """

    def _make_row(self, district_type: str = "school district (unified)") -> dict:
        """Minimal row dict that _fetch_district would return on success."""
        return {
            "NAME": "Test District",
            "B16004_002E": "1000",
            **{v: "10" for v in acs_fetcher._LTVW_VARS},
        }

    def test_unified_succeeds_on_first_try(self, monkeypatch):
        """If unified type returns data, loop stops there — no fallback calls."""
        call_log = []

        def fake_urlopen(req, timeout=15):
            call_log.append(req.full_url)
            cm = mock.MagicMock()
            import json
            headers = ["NAME", "B16004_002E"] + acs_fetcher._LTVW_VARS
            row = ["Test District", "2000"] + ["20"] * len(acs_fetcher._LTVW_VARS)
            cm.__enter__.return_value.read.return_value = json.dumps([headers, row]).encode()
            cm.__exit__.return_value = False
            return cm

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = acs_fetcher._fetch_district("00060", "fake-key", "35")

        assert result is not None
        assert len(call_log) == 1, (
            "Expected exactly one API call (unified succeeded), "
            f"but made {len(call_log)} calls."
        )
        assert "school+district+%28unified%29" in call_log[0] or "unified" in call_log[0]

    def test_falls_back_to_elementary_when_unified_returns_no_data(self, monkeypatch):
        """When unified returns an empty response, elementary is tried next."""
        import json
        call_types = []

        def fake_urlopen(req, timeout=15):
            url = req.full_url
            # Detect which district type is being queried from the URL
            if "unified" in url:
                call_types.append("unified")
                # Return header-only (no match)
                payload = [["NAME", "B16004_002E"] + acs_fetcher._LTVW_VARS]
            else:
                call_types.append("other")
                headers = ["NAME", "B16004_002E"] + acs_fetcher._LTVW_VARS
                row = ["Test District", "3000"] + ["30"] * len(acs_fetcher._LTVW_VARS)
                payload = [headers, row]

            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            cm.__exit__.return_value = False
            return cm

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = acs_fetcher._fetch_district("00060", "fake-key", "35")

        assert result is not None, "Should have resolved via elementary/secondary fallback"
        assert "unified" in call_types, "Unified must have been tried first"
        assert "other" in call_types, "A fallback type must have been tried"

    def test_all_types_tried_before_returning_none(self, monkeypatch):
        """If all three types return empty, _fetch_district returns None."""
        import json
        call_count = [0]

        def fake_urlopen(req, timeout=15):
            call_count[0] += 1
            cm = mock.MagicMock()
            # Always header-only — no match for any type
            payload = [["NAME", "B16004_002E"] + acs_fetcher._LTVW_VARS]
            cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            cm.__exit__.return_value = False
            return cm

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = acs_fetcher._fetch_district("99999", "fake-key", "35")

        assert result is None
        assert call_count[0] == len(acs_fetcher._ACS_DISTRICT_TYPES), (
            f"Expected {len(acs_fetcher._ACS_DISTRICT_TYPES)} API calls (one per type), "
            f"got {call_count[0]}."
        )

    def test_district_types_constant_matches_saipe_pattern(self):
        """_ACS_DISTRICT_TYPES must include unified, elementary, and secondary."""
        types = acs_fetcher._ACS_DISTRICT_TYPES
        assert "school district (unified)" in types
        assert "school district (elementary)" in types
        assert "school district (secondary)" in types
        assert types[0] == "school district (unified)", "Unified must be tried first"


class TestGetDistrictDataFallback:
    """End-to-end: get_district_data must resolve non-unified districts."""

    def test_elementary_district_resolves_ell(self, monkeypatch):
        """A non-unified district that returns data via elementary type must resolve."""
        import json

        def fake_fetch_district(district_code, api_key, state_fips):
            # Return a valid row (simulating elementary type resolved)
            headers = ["NAME", "B16004_002E"] + acs_fetcher._LTVW_VARS
            row = ["Test District", "2000"] + ["50"] * len(acs_fetcher._LTVW_VARS)
            return dict(zip(headers, row))

        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map",
                            lambda s: {"test-community": "2800001"})
        monkeypatch.setattr(acs_fetcher, "_acs_cache_read", lambda key: None)
        monkeypatch.setattr(acs_fetcher, "_acs_cache_write", lambda key, data: None)
        monkeypatch.setattr(acs_fetcher, "_fetch_district", fake_fetch_district)

        result = acs_fetcher.get_district_data("test-community", "MS")

        assert result is not None
        assert result["ell_pct"] > 0

    def test_unified_regression_unaffected(self, monkeypatch):
        """Existing unified district path is unchanged (no regression)."""
        import json

        headers = ["NAME", "B16004_002E"] + acs_fetcher._LTVW_VARS
        row = ["APS", "80000"] + ["400"] * len(acs_fetcher._LTVW_VARS)
        fake_row = dict(zip(headers, row))

        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map",
                            lambda s: {"nm-albuquerque": "3500060"})
        monkeypatch.setattr(acs_fetcher, "_acs_cache_read", lambda key: None)
        monkeypatch.setattr(acs_fetcher, "_acs_cache_write", lambda key, data: None)
        monkeypatch.setattr(acs_fetcher, "_fetch_district", lambda *a: fake_row)

        result = acs_fetcher.get_district_data("nm-albuquerque", "NM")

        assert result is not None
        assert "ell_pct" in result


class TestGetTotalPopulationFallback:
    """get_total_population must also try all district types via _fetch_acs_vars."""

    def test_fallback_type_used_when_unified_fails_for_population(self, monkeypatch):
        """If unified returns None for total pop, elementary/secondary is tried."""
        call_types = []

        def fake_fetch_acs_vars(district_code, variables, vintage, api_key,
                                state_fips, district_type="school district (unified)"):
            call_types.append(district_type)
            if district_type == "school district (unified)":
                return None  # unified not found
            return {"NAME": "Test District", "B01003_001E": "5000"}

        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map",
                            lambda s: {"ms-test": "2800001"})
        monkeypatch.setattr(acs_fetcher, "_acs_cache_read", lambda key: None)
        monkeypatch.setattr(acs_fetcher, "_acs_cache_write", lambda key, data: None)
        monkeypatch.setattr(acs_fetcher, "_fetch_acs_vars", fake_fetch_acs_vars)

        result = acs_fetcher.get_total_population("ms-test", "MS")

        assert result is not None
        assert result["pop_2019"] == 5000
        assert "school district (unified)" in call_types
        # At least one non-unified type must have been tried
        non_unified = [t for t in call_types if t != "school district (unified)"]
        assert len(non_unified) > 0, "No fallback type was tried for total population"


# ── F-005: ms-choctaw BIA LEAID returns None gracefully ──────────────────────

class TestMsChoctawBiaLeaid:
    """
    ms-choctaw LEAID 5900031 has a BIA prefix (59) that is not a Census state FIPS.
    All Census/ACS/SAIPE fetches must return None gracefully — not raise.
    """

    def test_saipe_bia_leaid_returns_none(self, monkeypatch):
        """SAIPE must return None for LEAID 5900031 — non-numeric... wait, 59 is numeric,
        but SAIPE tries to query with state_fips='59' which has no match."""
        import pipeline.utils.saipe_fetcher as saipe_fetcher_mod
        monkeypatch.setenv("CENSUS_API_KEY", "test-key")
        monkeypatch.setattr(saipe_fetcher_mod, "_read_cache", lambda leaid: None)

        def fake_fetch_type(district_code, district_type, year, api_key, state_fips):
            # BIA FIPS 59 — Census returns empty (no state 59)
            return None

        monkeypatch.setattr(saipe_fetcher_mod, "_fetch_district_type", fake_fetch_type)

        result = saipe_fetcher_mod.get_poverty_data("5900031")
        assert result is None  # None gracefully, not an exception

    def test_acs_bia_leaid_returns_none(self, monkeypatch):
        """ACS must return None for ms-choctaw — BIA LEAID maps to LEAID '5900031'."""
        # Simulate what happens: _load_nces_map returns the BIA LEAID,
        # state_fips resolves to "28" (MS) but district code "00031" won't match
        # any Census school district. The fetcher should return None gracefully.
        monkeypatch.setattr(acs_fetcher, "_api_key", lambda: "fake-key")
        monkeypatch.setattr(acs_fetcher, "_load_nces_map",
                            lambda s: {"ms-choctaw": "5900031"})
        monkeypatch.setattr(acs_fetcher, "_acs_cache_read", lambda key: None)
        monkeypatch.setattr(acs_fetcher, "_fetch_district", lambda *a: None)

        result = acs_fetcher.get_district_data("ms-choctaw", "MS")
        assert result is None  # None gracefully — data structurally unavailable
