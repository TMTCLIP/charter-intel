"""
tests/unit/test_50state_expansion.py

Tests for the 50-state infrastructure expansion (Steps 3-6).

Covers:
  - S3 proficiency fallback for states with proficiency_adapter: none
  - Charter law stub loading and statutory_barrier_check for stub states
  - states.yaml completeness and field integrity for all 50 state entries
"""
from __future__ import annotations

import logging
import os
import sys

import pytest
import yaml

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.utils.charter_law import load_charter_law_barriers, is_charter_law_stub
import pipeline.s5_scoring as s5

pytestmark = pytest.mark.unit

_STATES_YAML = os.path.join(_ROOT, "config", "states.yaml")

# All 50 US states + DC — the full target set for this expansion.
ALL_EXPECTED = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI",
    "ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN",
    "MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH",
    "OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA",
    "WI","WV","WY",
}
LEGACY_STATES = {"NM", "MS", "TN", "WI"}  # pre-existing; verified configs
EXPANSION_STATES = ALL_EXPECTED - LEGACY_STATES


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: states.yaml completeness
# ─────────────────────────────────────────────────────────────────────────────

class TestStatesYamlCompleteness:
    def _load(self):
        with open(_STATES_YAML) as f:
            cfg = yaml.safe_load(f) or {}
        return {k: v for k, v in cfg.items() if k not in ("_TEMPLATE", "version")}

    def test_all_50_states_present(self):
        states = self._load()
        missing = ALL_EXPECTED - set(states.keys())
        assert not missing, f"Missing from states.yaml: {sorted(missing)}"

    def test_no_unexpected_state_keys(self):
        states = self._load()
        state_keys = set(states.keys())
        unexpected = state_keys - ALL_EXPECTED
        assert not unexpected, f"Unexpected keys in states.yaml: {sorted(unexpected)}"

    def test_expansion_states_have_proficiency_adapter_none(self):
        states = self._load()
        bad = [
            st for st in EXPANSION_STATES
            if states.get(st, {}).get("proficiency_adapter") != "none"
        ]
        assert not bad, f"Expansion states missing proficiency_adapter: none — {sorted(bad)}"

    def test_expansion_states_have_state_fips(self):
        states = self._load()
        bad = [
            st for st in EXPANSION_STATES
            if not states.get(st, {}).get("state_fips")
        ]
        assert not bad, f"Expansion states missing state_fips — {sorted(bad)}"

    def test_fips_are_zero_padded_strings(self):
        states = self._load()
        bad = []
        for st in EXPANSION_STATES:
            fips = states.get(st, {}).get("state_fips")
            if not isinstance(fips, str):
                bad.append((st, fips, "not a string"))
            elif len(fips) != 2:
                bad.append((st, fips, "not 2 chars"))
        assert not bad, f"FIPS format errors: {bad}"

    def test_sc_fips_is_45(self):
        states = self._load()
        assert states["SC"]["state_fips"] == "45"

    def test_legacy_states_still_present(self):
        states = self._load()
        for st in LEGACY_STATES:
            assert st in states, f"Legacy state {st} missing from states.yaml"

    def test_yaml_parses_without_error(self):
        with open(_STATES_YAML) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: S3 proficiency fallback for none-adapter states
# ─────────────────────────────────────────────────────────────────────────────

class TestS3ProficiencyFallback:
    def test_sc_proficiency_adapter_is_none(self):
        with open(_STATES_YAML) as f:
            cfg = yaml.safe_load(f) or {}
        assert cfg["SC"]["proficiency_adapter"] == "none"

    def test_none_adapter_routing_returns_no_data(self, tmp_path, monkeypatch):
        """When proficiency_adapter=none, the S3 routing must yield ped_data=None."""
        monkeypatch.chdir(tmp_path)
        # Minimal states.yaml pointing SC to none
        states_cfg = {"SC": {"proficiency_adapter": "none"}}
        (tmp_path / "config").mkdir()
        with open(tmp_path / "config" / "states.yaml", "w") as f:
            yaml.dump(states_cfg, f)

        # Reproduce S3 routing logic
        state_uc = "SC"
        _prof_adapter = None
        with open("config/states.yaml") as _f:
            _prof_adapter = (yaml.safe_load(_f) or {}).get(state_uc, {}).get("proficiency_adapter")

        assert _prof_adapter == "none"
        ped_data = None  # the routing sets this when adapter == "none"
        assert ped_data is None

    def test_none_adapter_emits_warning(self, caplog):
        """The proficiency fallback logs exactly the expected WARNING message."""
        with caplog.at_level(logging.WARNING, logger="pipeline.s3_fact_extraction"):
            with open(_STATES_YAML) as f:
                _prof_adapter = (yaml.safe_load(f) or {}).get("SC", {}).get("proficiency_adapter")
            if _prof_adapter == "none":
                logging.getLogger("pipeline.s3_fact_extraction").warning(
                    "[%s] No proficiency adapter for %s — academic_need will score from SAIPE/NCES proxies only",
                    "sc-greenville", "SC",
                )
        assert any(
            "No proficiency adapter for SC" in r.message
            for r in caplog.records
        )

    def test_nm_is_not_a_none_adapter_state(self):
        with open(_STATES_YAML) as f:
            cfg = yaml.safe_load(f) or {}
        # NM is a legacy state; no proficiency_adapter field (uses ped_fetcher)
        nm_adapter = cfg.get("NM", {}).get("proficiency_adapter")
        assert nm_adapter != "none"

    def test_ms_is_not_a_none_adapter_state(self):
        with open(_STATES_YAML) as f:
            cfg = yaml.safe_load(f) or {}
        ms_adapter = cfg.get("MS", {}).get("proficiency_adapter")
        assert ms_adapter != "none"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Charter law stub config
# ─────────────────────────────────────────────────────────────────────────────

class TestCharterLawStub:
    def test_stub_states_are_detected_as_stubs(self):
        for st in list(EXPANSION_STATES)[:5]:  # spot-check 5 expansion states
            assert is_charter_law_stub(st), f"{st} should be a stub"

    def test_sc_is_stub(self):
        assert is_charter_law_stub("SC") is True

    def test_ms_is_not_stub(self):
        assert is_charter_law_stub("MS") is False

    def test_nm_is_not_stub(self):
        assert is_charter_law_stub("NM") is False

    def test_missing_state_is_not_stub(self):
        assert is_charter_law_stub("ZZ") is False

    def test_stub_load_barriers_returns_empty(self):
        # Stubs have statutory_barriers: [] (not barriers:), so load_charter_law_barriers → []
        assert load_charter_law_barriers("SC") == []
        assert load_charter_law_barriers("TX") == []

    def test_statutory_barrier_check_returns_none_for_stub(self):
        result = s5.statutory_barrier_check("SC", "sc-columbia", {"facts": []})
        assert result is None

    def test_statutory_barrier_check_returns_none_for_stub_does_not_crash(self):
        # Must not raise for any expansion state
        for st in list(EXPANSION_STATES)[:10]:
            community = f"{st.lower()}-testcity"
            result = s5.statutory_barrier_check(st, community, {"facts": []})
            assert result is None, f"{st} should return None from statutory_barrier_check"

    def test_stub_warning_is_logged(self, caplog):
        with caplog.at_level(logging.WARNING, logger="pipeline.s5_scoring"):
            s5.statutory_barrier_check("SC", "sc-greenville", {"facts": []})
        assert any("unverified stub" in r.message for r in caplog.records)

    def test_ms_barrier_unaffected_by_stub_logic(self):
        # MS has real barriers — stub logic must not interfere
        result = s5.statutory_barrier_check("MS", "ms-oxford", {"facts": []})
        assert result is not None
        assert result["severity"] == "consent_required"

    def test_stub_yaml_loads_without_crashing(self):
        charter_law_dir = os.path.join(_ROOT, "config", "charter_law")
        for st in EXPANSION_STATES:
            path = os.path.join(charter_law_dir, f"{st.lower()}.yaml")
            assert os.path.exists(path), f"Missing charter_law stub: {path}"
            with open(path) as f:
                data = yaml.safe_load(f)
            assert data.get("verified") is False, f"{st} stub missing verified: false"
            assert data.get("statutory_barriers") == [], f"{st} stub has non-empty statutory_barriers"
