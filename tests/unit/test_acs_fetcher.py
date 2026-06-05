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
