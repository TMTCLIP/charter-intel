"""
tests/unit/test_saipe_fetcher.py

Unit tests for pipeline/utils/saipe_fetcher.py.
Mocks urllib.request.urlopen and the filesystem cache to avoid real network calls.
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest.mock as mock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.utils.saipe_fetcher as saipe_fetcher

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setenv("CENSUS_API_KEY", "test-key-123")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_api_response(
    name: str = "Test District",
    poverty_rate: str = "18.5",
    poverty_count: str = "1234",
    total_pop: str = "6789",
    district_code: str = "00060",
    year: str = "2023",
) -> bytes:
    """Return bytes mimicking a Census SAIPE API success response."""
    payload = [
        ["NAME", "SAEPOVRAT5_17RV_PT", "SAEPOV5_17RV_PT", "SAEPOVALL_PT",
         "state", "school district (unified)", "YEAR"],
        [name, poverty_rate, poverty_count, total_pop, "35", district_code, year],
    ]
    return json.dumps(payload).encode("utf-8")


def _mock_urlopen(response_bytes: bytes):
    """Return a context-manager mock for urllib.request.urlopen."""
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = response_bytes
    cm.__exit__.return_value = False
    return cm


# ── Successful fetch ──────────────────────────────────────────────────────────

class TestGetPovertyDataSuccess:
    def test_returns_expected_fields(self):
        resp = _make_api_response(poverty_rate="18.5", poverty_count="1234", total_pop="6789")
        with (
            mock.patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            mock.patch.object(saipe_fetcher, "_read_cache", return_value=None),
            mock.patch.object(saipe_fetcher, "_write_cache"),
        ):
            result = saipe_fetcher.get_poverty_data("3500060", year=2023)

        assert result is not None
        assert result["poverty_rate_pct"] == 18.5
        assert result["poverty_count_5_17"] == 1234
        assert result["total_population"] == 6789
        assert result["leaid"] == "3500060"
        assert result["data_year"] == 2023
        assert result["source"] == "CENSUS_SAIPE"
        assert result["confidence"] == "MODERATE"

    def test_cache_is_written_on_success(self):
        resp = _make_api_response()
        with (
            mock.patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            mock.patch.object(saipe_fetcher, "_read_cache", return_value=None),
            mock.patch.object(saipe_fetcher, "_write_cache") as mock_write,
        ):
            saipe_fetcher.get_poverty_data("3500060", year=2023)

        mock_write.assert_called_once()
        _args = mock_write.call_args[0]
        assert _args[0] == "3500060"
        assert _args[1]["data_year"] == 2023


# ── Graceful None on no-match ─────────────────────────────────────────────────

class TestGetPovertyDataNoMatch:
    def test_returns_none_when_api_returns_empty(self):
        # API returns header only — no data row
        empty_response = json.dumps([
            ["NAME", "SAEPOVRAT5_17RV_PT", "SAEPOV5_17RV_PT", "SAEPOVALL_PT",
             "state", "school district (unified)", "YEAR"],
        ]).encode("utf-8")

        with (
            mock.patch("urllib.request.urlopen", return_value=_mock_urlopen(empty_response)),
            mock.patch.object(saipe_fetcher, "_read_cache", return_value=None),
        ):
            result = saipe_fetcher.get_poverty_data("3599999", year=2023)

        assert result is None

    def test_returns_none_when_api_call_raises(self):
        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=Exception("connection refused"),
            ),
            mock.patch.object(saipe_fetcher, "_read_cache", return_value=None),
        ):
            result = saipe_fetcher.get_poverty_data("3500060", year=2023)

        assert result is None

    def test_returns_none_for_null_leaid(self):
        result = saipe_fetcher.get_poverty_data(None)
        assert result is None

    def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("CENSUS_API_KEY", raising=False)
        result = saipe_fetcher.get_poverty_data("3500060", year=2023)
        assert result is None


# ── Cache hit skips network call ──────────────────────────────────────────────

class TestCacheHit:
    def test_cache_hit_skips_urlopen(self):
        cached_data = {
            "poverty_rate_pct": 15.2,
            "poverty_count_5_17": 999,
            "total_population": 5000,
            "leaid": "3500060",
            "data_year": 2023,
            "source": "CENSUS_SAIPE",
            "source_url": saipe_fetcher.SOURCE_URL,
            "source_title": saipe_fetcher.SOURCE_TITLE,
            "confidence": "MODERATE",
        }
        with (
            mock.patch.object(saipe_fetcher, "_read_cache", return_value=cached_data),
            mock.patch("urllib.request.urlopen") as mock_url,
        ):
            result = saipe_fetcher.get_poverty_data("3500060", year=2023)

        mock_url.assert_not_called()
        assert result is not None
        assert result["poverty_rate_pct"] == 15.2

    def test_expired_cache_triggers_network_call(self, tmp_path, monkeypatch):
        # Simulate expired cache by making _read_cache return None (TTL logic returns None)
        resp = _make_api_response(poverty_rate="20.0")
        with (
            mock.patch.object(saipe_fetcher, "_read_cache", return_value=None),
            mock.patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            mock.patch.object(saipe_fetcher, "_write_cache"),
        ):
            result = saipe_fetcher.get_poverty_data("3500060", year=2023)

        assert result is not None
        assert result["poverty_rate_pct"] == 20.0
