"""
tests/unit/test_regen_data_cache_invalidation.py

Pure-filesystem tests for the --regen-data selective cache invalidation.

Verifies that the regen-data logic clears ONLY the S3–S6 analysis tiers
(community/, synthesis/, llm/) for the targeted community and leaves the S2
state-context tier (state/) and the source-data fetcher caches untouched.

No API calls, no pipeline stages — the invalidation helper is exercised
directly against a temporary cache directory.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_APP = os.path.join(_ROOT, "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import main
from pipeline.utils.cache import CacheManager

pytestmark = pytest.mark.unit


class _FakeCfg:
    cache_enabled = True
    force_refresh = False


def _seed(tmp_path) -> dict:
    """Create fake cache files across all tiers. Returns {label: Path}."""
    files = {
        # S3–S6 analysis tiers for the target community — MUST be cleared
        "community_s3": tmp_path / "community" / "nm" / "nm-questa" / "s3_facts_raw.json",
        "community_s4": tmp_path / "community" / "nm" / "nm-questa" / "s4_verified.json",
        "community_s5": tmp_path / "community" / "nm" / "nm-questa" / "s5_scorecard.json",
        "synthesis_s6": tmp_path / "synthesis" / "nm" / "nm-questa" / "s6_brief_growth_mode2.json",
        "llm_s3":       tmp_path / "llm" / "nm" / "nm-questa" / "s3_charter_intel_abc123.json",
        # S2 state-context tier — MUST survive
        "state_s2":     tmp_path / "state" / "nm" / "s2_state_context.json",
        # Source-data fetcher caches — MUST survive
        "fetcher_nces": tmp_path / "fetcher" / "nces" / "3507710_enrollment.json",
        "fetcher_acs":  tmp_path / "fetcher" / "acs" / "ell_3500060.json",
        # A different community in the same state — MUST survive (scoping)
        "other_comm":   tmp_path / "community" / "nm" / "nm-santa-fe" / "s3_facts_raw.json",
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return files


def test_regen_data_clears_only_s3_to_s6_tiers(tmp_path, monkeypatch):
    files = _seed(tmp_path)

    mgr = CacheManager(_FakeCfg())
    monkeypatch.setattr(mgr, "base", str(tmp_path))

    removed = main._invalidate_regen_data_caches(mgr, "NM", ["nm-questa"])

    # 3 community + 1 synthesis + 1 llm = 5 files for nm-questa
    assert removed == 5

    # Cleared tiers
    assert not files["community_s3"].exists()
    assert not files["community_s4"].exists()
    assert not files["community_s5"].exists()
    assert not files["synthesis_s6"].exists()
    assert not files["llm_s3"].exists()

    # Preserved: S2 state context + source-data fetcher caches
    assert files["state_s2"].exists(), "S2 state cache must NOT be cleared"
    assert files["fetcher_nces"].exists(), "fetcher (source) cache must NOT be cleared"
    assert files["fetcher_acs"].exists(), "fetcher (source) cache must NOT be cleared"

    # Preserved: a different community is out of scope
    assert files["other_comm"].exists(), "other community must NOT be cleared"


def test_regen_data_tiers_exclude_state_and_fetcher():
    # Guard against accidental tier-list drift.
    assert main.REGEN_DATA_TIERS == ("community", "synthesis", "llm")
    assert "state" not in main.REGEN_DATA_TIERS
    assert "fetcher" not in main.REGEN_DATA_TIERS


def test_count_regen_data_cache_files_matches_cleared_set(tmp_path, monkeypatch):
    # The read-only count helper used for the UI feedback should report exactly
    # the files that --regen-data will clear (community + synthesis + llm).
    import cache_utils
    files = _seed(tmp_path)
    monkeypatch.setattr(cache_utils, "_CACHE_ROOT", tmp_path)

    count = cache_utils.count_regen_data_cache_files("nm-questa", "NM")
    assert count == 5, "count must include only community/synthesis/llm for the community"

    # All seeded files are still present — counting is read-only.
    for path in files.values():
        assert path.exists(), "count helper must not delete anything"
