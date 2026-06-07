"""
tests/unit/test_s2_cache_signal.py

Tests for:
  a) states.yaml: MS, TN, WI entries exist with required keys
  b) S2 _should_regenerate logic
  c) nces_fetcher per_pupil_revenue_avg loading
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)


# ─────────────────────────────────────────────────────────────────────────────
# a) states.yaml — MS, TN, WI entries
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def states_cfg():
    cfg_path = os.path.join(_ROOT, "config", "states.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


REQUIRED_TOP_LEVEL_KEYS = {"state_fips", "charter_law", "nces_district_map"}
REQUIRED_CHARTER_LAW_KEYS = {
    "cap_on_charters", "facilities_provisions", "funding_parity_provisions",
    "statute_citation", "statute_url",
}


@pytest.mark.parametrize("state", ["MS", "TN", "WI"])
def test_state_entry_exists(states_cfg, state):
    assert state in states_cfg, f"{state} missing from states.yaml"


@pytest.mark.parametrize("state", ["MS", "TN", "WI"])
def test_state_has_required_keys(states_cfg, state):
    entry = states_cfg[state]
    for key in REQUIRED_TOP_LEVEL_KEYS:
        assert key in entry, f"{state} missing required key: {key}"


@pytest.mark.parametrize("state", ["MS", "TN", "WI"])
def test_state_fips(states_cfg, state):
    expected = {"MS": 28, "TN": 47, "WI": 55}
    assert states_cfg[state]["state_fips"] == expected[state]


@pytest.mark.parametrize("state", ["MS", "TN", "WI"])
def test_charter_law_has_required_keys(states_cfg, state):
    charter_law = states_cfg[state]["charter_law"]
    for key in REQUIRED_CHARTER_LAW_KEYS:
        assert key in charter_law, f"{state}.charter_law missing key: {key}"


@pytest.mark.parametrize("state", ["MS", "TN", "WI"])
def test_nces_district_map_nonempty(states_cfg, state):
    nmap = states_cfg[state]["nces_district_map"]
    assert isinstance(nmap, dict), f"{state}.nces_district_map is not a dict"
    assert len(nmap) > 0, f"{state}.nces_district_map is empty"


@pytest.mark.parametrize("state,min_entries", [
    ("MS", 140),
    ("TN", 130),
    ("WI", 300),
])
def test_nces_district_map_count(states_cfg, state, min_entries):
    count = len(states_cfg[state]["nces_district_map"])
    assert count >= min_entries, (
        f"{state}.nces_district_map has {count} entries, expected >= {min_entries}"
    )


def test_nm_per_pupil_revenue_avg_present(states_cfg):
    """NM per_pupil_revenue_avg should be present after Task 5 migration."""
    nm = states_cfg["NM"]
    assert "per_pupil_revenue_avg" in nm
    assert nm["per_pupil_revenue_avg"] == pytest.approx(24356.0)


@pytest.mark.parametrize("state", ["MS", "TN", "WI"])
def test_new_state_per_pupil_revenue_avg_present(states_cfg, state):
    """New states should have per_pupil_revenue_avg (may be null/approximate)."""
    entry = states_cfg[state]
    assert "per_pupil_revenue_avg" in entry


# ─────────────────────────────────────────────────────────────────────────────
# b) S2 _should_regenerate cache logic
# ─────────────────────────────────────────────────────────────────────────────

from pipeline.s2_state_context import _should_regenerate


def test_should_regenerate_when_cache_missing(tmp_path):
    """No cache file → regenerate."""
    cache_path = str(tmp_path / "nonexistent.json")
    regen, sig_id = _should_regenerate("NM", cache_path, force=False)
    assert regen is True
    assert sig_id is None


def test_should_not_regenerate_when_cache_exists_no_signal(tmp_path):
    """Cache present + no signal → use cache."""
    cache_path = tmp_path / "s2.json"
    cache_path.write_text('{"state": "NM"}')

    with patch("pipeline.layer2.notion_client.NotionSignalStore") as MockStore:
        instance = MagicMock()
        instance.get_s2_cache_bust_signals.return_value = []
        MockStore.return_value = instance

        regen, sig_id = _should_regenerate("NM", str(cache_path), force=False)

    assert regen is False
    assert sig_id is None


def test_should_regenerate_when_signal_present(tmp_path):
    """Cache present + active s2_cache_bust signal → regenerate + return signal_id."""
    cache_path = tmp_path / "s2.json"
    cache_path.write_text('{"state": "MS"}')

    fake_sig_id = "abc-123"

    with patch("pipeline.layer2.notion_client.NotionSignalStore") as MockStore:
        instance = MagicMock()
        instance.get_s2_cache_bust_signals.return_value = [
            {"signal_id": fake_sig_id, "state": "MS"}
        ]
        MockStore.return_value = instance

        regen, sig_id = _should_regenerate("MS", str(cache_path), force=False)

    assert regen is True
    assert sig_id == fake_sig_id


def test_force_flag_bypasses_cache_and_signal(tmp_path):
    """--force → regenerate regardless, no Notion query needed."""
    cache_path = tmp_path / "s2.json"
    cache_path.write_text('{"state": "TN"}')

    with patch("pipeline.layer2.notion_client.NotionSignalStore") as MockStore:
        regen, sig_id = _should_regenerate("TN", str(cache_path), force=True)
        MockStore.assert_not_called()

    assert regen is True
    assert sig_id is None


def test_notion_failure_falls_back_to_cache(tmp_path):
    """Notion query exception → log warning, use cache (return False)."""
    cache_path = tmp_path / "s2.json"
    cache_path.write_text('{}')

    with patch("pipeline.layer2.notion_client.NotionSignalStore") as MockStore:
        MockStore.side_effect = RuntimeError("NOTION_API_KEY not set")

        regen, sig_id = _should_regenerate("WI", str(cache_path), force=False)

    assert regen is False
    assert sig_id is None


# ─────────────────────────────────────────────────────────────────────────────
# c) nces_fetcher per_pupil_revenue_avg loading
# ─────────────────────────────────────────────────────────────────────────────

import pandas as pd
import pipeline.utils.nces_fetcher as nf


def test_get_state_avg_ppr_loads_from_states_yaml(monkeypatch, tmp_path):
    """NM per_pupil_revenue_avg is loaded from states.yaml (no parquet needed)."""
    monkeypatch.chdir(tmp_path)
    # Write a minimal states.yaml with NM value
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "states.yaml").write_text(
        "NM:\n  per_pupil_revenue_avg: 24356.0\n  nces_district_map: {}\n"
    )
    result = nf._get_state_avg_ppr("NM")
    assert result == pytest.approx(24356.0)


def test_get_state_avg_ppr_returns_float_for_ms_via_parquet(monkeypatch, tmp_path):
    """MS with null per_pupil_revenue_avg falls back to parquet computation."""
    monkeypatch.chdir(tmp_path)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "states.yaml").write_text(
        "MS:\n"
        "  per_pupil_revenue_avg: null\n"
        "  nces_district_map:\n"
        "    ms-jackson-2802190: '2802190'\n"
    )

    nat_dir = tmp_path / "data" / "raw" / "national"
    nat_dir.mkdir(parents=True)
    df = pd.DataFrame([
        {"LEAID": "2802190", "MEMBERSCH": 18000, "TOTALREV": 200_000_000,
         "TFEDREV": 30_000_000, "TSTREV": 140_000_000, "TLOCREV": 30_000_000,
         "TOTALEXP": 190_000_000},
    ]).set_index("LEAID")
    df.to_parquet(nat_dir / "nces_lea_finance.parquet", compression="snappy")

    result = nf._get_state_avg_ppr("MS")
    assert result is not None
    assert isinstance(result, float)
    # 200_000_000 / 18000 ≈ 11111.11
    assert result == pytest.approx(200_000_000 / 18000, rel=1e-3)


def test_get_state_avg_ppr_returns_none_when_parquet_missing(monkeypatch, tmp_path):
    """Null in states.yaml + no parquet → return None, no crash."""
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "states.yaml").write_text(
        "TX:\n  per_pupil_revenue_avg: null\n  nces_district_map: {}\n"
    )
    result = nf._get_state_avg_ppr("TX")
    assert result is None


def test_get_district_data_uses_state_ppr_not_nm_hardcode(monkeypatch, tmp_path):
    """get_district_data should NOT use the old NM_STATE_AVG_PPR for non-NM states."""
    monkeypatch.chdir(tmp_path)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    # MS PPR set to 10000 (different from NM's 24356)
    (cfg_dir / "states.yaml").write_text(
        "MS:\n"
        "  per_pupil_revenue_avg: 10000.0\n"
        "  nces_district_map:\n"
        "    ms-test: '9999999'\n"
    )

    nat_dir = tmp_path / "data" / "raw" / "national"
    nat_dir.mkdir(parents=True)
    df = pd.DataFrame([
        {"LEAID": "9999999", "MEMBERSCH": 1000, "TOTALREV": 12_000_000,
         "TFEDREV": 2_000_000, "TSTREV": 8_000_000, "TLOCREV": 2_000_000,
         "TOTALEXP": 11_000_000},
    ]).set_index("LEAID")
    df.to_parquet(nat_dir / "nces_lea_finance.parquet", compression="snappy")

    result = nf.get_district_data("ms-test", "MS")
    assert result is not None
    # PPR = 12_000_000 / 1000 = 12000
    # vs state avg = 10000 → +20%
    assert "per_pupil_revenue_vs_state_avg_pct" in result
    assert result["per_pupil_revenue_vs_state_avg_pct"] == pytest.approx(20.0, rel=0.01)
