"""
tests/unit/test_cache_ttl.py

Unit tests for the TTL enforcement added to CacheManager (Task 4).
Verifies that:
  - A cache file within TTL is returned as a hit
  - A cache file older than TTL is treated as a miss (returns None)
  - No TTL param preserves original always-return behaviour
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

from pipeline import PipelineConfig
from pipeline.utils.cache import CacheManager, CACHE_TTL_DAYS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


def _make_cache_manager(tmp_path, monkeypatch) -> CacheManager:
    """Return a CacheManager with base pointing to a temp directory."""

    class _FakeCfg:
        cache_enabled = True
        force_refresh = False

    mgr = CacheManager(_FakeCfg())
    monkeypatch.setattr(mgr, "base", str(tmp_path))
    return mgr


# ── TTL constants sanity ──────────────────────────────────────────────────────

class TestCacheTTLConstants:
    def test_all_expected_keys_present(self):
        expected = {
            "saipe", "nces_finance", "nces_lunch", "nces_membership",
            "acs_district", "acs_population",
            "s2_state_context", "s1_ped_roster",
            "s3_political_climate",
        }
        for key in expected:
            assert key in CACHE_TTL_DAYS, f"TTL not defined for {key!r}"

    def test_ttl_values_are_positive_ints(self):
        for key, val in CACHE_TTL_DAYS.items():
            assert isinstance(val, int) and val > 0, (
                f"CACHE_TTL_DAYS[{key!r}] = {val!r} is not a positive int"
            )

    def test_saipe_365(self):
        assert CACHE_TTL_DAYS["saipe"] == 365

    def test_s2_state_context_90(self):
        assert CACHE_TTL_DAYS["s2_state_context"] == 90

    def test_acs_district_180(self):
        assert CACHE_TTL_DAYS["acs_district"] == 180


# ── CacheManager TTL behaviour ────────────────────────────────────────────────

class TestCacheManagerTTL:
    def _write_test_file(self, tmp_path, key: str, data: dict) -> str:
        path = tmp_path / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return str(path)

    def test_within_ttl_returns_cached_value(self, tmp_path, monkeypatch):
        mgr = _make_cache_manager(tmp_path, monkeypatch)
        self._write_test_file(tmp_path, "state/nm/s2_state_context.json", {"v": 1})

        # mtime = 10 days ago; TTL = 90 days → hit
        fake_mtime = time.time() - (10 * 86400)
        with mock.patch("os.path.getmtime", return_value=fake_mtime):
            result = mgr.get("state/nm/s2_state_context.json", ttl_days=90)

        assert result == {"v": 1}

    def test_expired_ttl_returns_none(self, tmp_path, monkeypatch):
        mgr = _make_cache_manager(tmp_path, monkeypatch)
        self._write_test_file(tmp_path, "state/nm/s2_state_context.json", {"v": 1})

        # mtime = 100 days ago; TTL = 90 days → miss
        fake_mtime = time.time() - (100 * 86400)
        with mock.patch("os.path.getmtime", return_value=fake_mtime):
            result = mgr.get("state/nm/s2_state_context.json", ttl_days=90)

        assert result is None

    def test_no_ttl_returns_value_regardless_of_age(self, tmp_path, monkeypatch):
        mgr = _make_cache_manager(tmp_path, monkeypatch)
        self._write_test_file(tmp_path, "community/nm/test/s3.json", {"x": 42})

        # mtime = 365+ days ago; no TTL param → still returns value
        fake_mtime = time.time() - (400 * 86400)
        with mock.patch("os.path.getmtime", return_value=fake_mtime):
            result = mgr.get("community/nm/test/s3.json")   # no ttl_days

        assert result == {"x": 42}

    def test_expired_cache_is_overwritten_by_set(self, tmp_path, monkeypatch):
        mgr = _make_cache_manager(tmp_path, monkeypatch)
        self._write_test_file(tmp_path, "state/nm/s2_state_context.json", {"old": True})

        # Confirm expiry
        fake_old = time.time() - (200 * 86400)
        with mock.patch("os.path.getmtime", return_value=fake_old):
            miss = mgr.get("state/nm/s2_state_context.json", ttl_days=90)
        assert miss is None

        # set() overwrites — no delete needed
        mgr.set("state/nm/s2_state_context.json", {"new": True})
        fresh = mgr.get("state/nm/s2_state_context.json", ttl_days=90)
        assert fresh == {"new": True}

    def test_force_refresh_always_returns_none(self, tmp_path, monkeypatch):
        class _ForceCfg:
            cache_enabled = True
            force_refresh = True

        mgr = CacheManager(_ForceCfg())
        monkeypatch.setattr(mgr, "base", str(tmp_path))
        self._write_test_file(tmp_path, "state/nm/s2_state_context.json", {"v": 1})

        result = mgr.get("state/nm/s2_state_context.json", ttl_days=90)
        assert result is None


# ── ACS fetcher cache TTL ─────────────────────────────────────────────────────

class TestACSFetcherCacheTTL:
    def test_acs_cache_ttl_days_is_180(self):
        import pipeline.utils.acs_fetcher as af
        assert af.ACS_CACHE_TTL_DAYS == 180

    def test_expired_acs_cache_returns_none(self, tmp_path, monkeypatch):
        import pipeline.utils.acs_fetcher as af
        monkeypatch.setattr(af, "_CACHE_DIR_ACS", str(tmp_path))

        # Write a cache file
        cache_file = tmp_path / "ell_3500060.json"
        cache_file.write_text(json.dumps({"ell_pct": 18.5}))

        # Simulate expired mtime
        fake_mtime = time.time() - (200 * 86400)
        with mock.patch("os.path.getmtime", return_value=fake_mtime):
            result = af._acs_cache_read("ell_3500060")

        assert result is None

    def test_fresh_acs_cache_returns_value(self, tmp_path, monkeypatch):
        import pipeline.utils.acs_fetcher as af
        monkeypatch.setattr(af, "_CACHE_DIR_ACS", str(tmp_path))

        cache_file = tmp_path / "ell_3500060.json"
        cache_file.write_text(json.dumps({"ell_pct": 18.5}))

        # Simulate recent mtime (1 day old)
        fake_mtime = time.time() - (1 * 86400)
        with mock.patch("os.path.getmtime", return_value=fake_mtime):
            result = af._acs_cache_read("ell_3500060")

        assert result == {"ell_pct": 18.5}
