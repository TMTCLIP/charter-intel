"""
tests/unit/test_nm_registry.py

Tests for config/community_registry/nm.yaml and NM community resolution via S1.
"""
from __future__ import annotations

import os
import sys

import pytest
import yaml

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.unit

_NM_REGISTRY_PATH = os.path.join(_ROOT, "config", "community_registry", "nm.yaml")

_REQUIRED_FIELDS = [
    "display_name",
    "district_name",
    "district_nces_id",
    "enrollment",
    "has_charters",
    "source",
    "state",
]


# ── Registry loads ─────────────────────────────────────────────────────────────

class TestNmRegistryLoads:
    def test_file_exists(self):
        assert os.path.exists(_NM_REGISTRY_PATH), (
            f"nm.yaml not found at {_NM_REGISTRY_PATH}"
        )

    def test_loads_as_dict(self):
        with open(_NM_REGISTRY_PATH) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert len(data) > 0

    def test_all_entries_have_required_fields(self):
        with open(_NM_REGISTRY_PATH) as f:
            data = yaml.safe_load(f)
        for cid, entry in data.items():
            for field in _REQUIRED_FIELDS:
                assert field in entry, (
                    f"nm.yaml entry '{cid}' missing required field '{field}'"
                )

    def test_all_entries_have_nm_state(self):
        with open(_NM_REGISTRY_PATH) as f:
            data = yaml.safe_load(f)
        for cid, entry in data.items():
            assert entry.get("state") == "NM", (
                f"Entry '{cid}' has state='{entry.get('state')}', expected 'NM'"
            )

    def test_all_keys_start_with_nm_prefix(self):
        with open(_NM_REGISTRY_PATH) as f:
            data = yaml.safe_load(f)
        for cid in data:
            assert cid.startswith("nm-"), f"Entry key '{cid}' does not start with 'nm-'"

    def test_all_leaids_are_7_digit_strings(self):
        with open(_NM_REGISTRY_PATH) as f:
            data = yaml.safe_load(f)
        for cid, entry in data.items():
            nces_id = str(entry.get("district_nces_id", ""))
            assert len(nces_id) == 7, (
                f"Entry '{cid}' has district_nces_id='{nces_id}' (expected 7 digits)"
            )
            assert nces_id.isdigit(), (
                f"Entry '{cid}' district_nces_id '{nces_id}' is not numeric"
            )

    def test_all_leaids_start_with_nm_fips_35(self):
        with open(_NM_REGISTRY_PATH) as f:
            data = yaml.safe_load(f)
        for cid, entry in data.items():
            nces_id = str(entry.get("district_nces_id", ""))
            assert nces_id.startswith("35"), (
                f"Entry '{cid}' district_nces_id '{nces_id}' does not start with '35' (NM FIPS)"
            )


# ── Known community spot-checks ────────────────────────────────────────────────

class TestNmKnownCommunities:
    @pytest.fixture(autouse=True)
    def _load(self):
        with open(_NM_REGISTRY_PATH) as f:
            self.data = yaml.safe_load(f)

    def test_santa_fe_present(self):
        assert "nm-santa-fe" in self.data

    def test_santa_fe_leaid(self):
        assert self.data["nm-santa-fe"]["district_nces_id"] == "3502370"

    def test_santa_fe_display_name(self):
        assert self.data["nm-santa-fe"]["display_name"] == "Santa Fe"

    def test_albuquerque_present(self):
        assert "nm-albuquerque" in self.data

    def test_albuquerque_has_charters(self):
        assert self.data["nm-albuquerque"]["has_charters"] is True

    def test_gallup_present(self):
        assert "nm-gallup" in self.data

    def test_truth_or_consequences_present(self):
        assert "nm-truth-or-consequences" in self.data

    def test_truth_or_consequences_display_name(self):
        assert self.data["nm-truth-or-consequences"]["display_name"] == "Truth or Consequences"

    def test_navajo_excluded(self):
        """nm-navajo has no valid LEAID and must not appear in the registry."""
        assert "nm-navajo" not in self.data

    def test_shiprock_excluded(self):
        """nm-shiprock has no valid LEAID and must not appear in the registry."""
        assert "nm-shiprock" not in self.data


# ── S1 registry-first discovery ───────────────────────────────────────────────

class TestNmRegistryDiscovery:
    @pytest.fixture(autouse=True)
    def _chdir_root(self, monkeypatch):
        monkeypatch.chdir(_ROOT)

    def test_nm_uses_registry_not_roster(self):
        """With nm.yaml present, S1 must choose the registry path, not the CSV roster."""
        from pipeline.s1_discovery import _select_source
        source_type, path = _select_source("NM")
        assert source_type == "registry", (
            "S1 is not using the registry for NM — nm.yaml may not be found"
        )
        assert "nm.yaml" in path

    def test_nm_registry_returns_community_stubs(self):
        """load_from_registry must return non-empty CommunityStub list for NM."""
        from pipeline.s1_discovery import load_from_registry
        _, path = _ROOT, os.path.join(_ROOT, "config", "community_registry", "nm.yaml")
        stubs = load_from_registry(path, "NM")
        assert len(stubs) > 0

    def test_nm_registry_stub_for_santa_fe(self):
        """Santa Fe must appear in the stub list with correct community_id."""
        from pipeline.s1_discovery import load_from_registry
        path = os.path.join(_ROOT, "config", "community_registry", "nm.yaml")
        stubs = load_from_registry(path, "NM")
        ids = [s.community_id for s in stubs]
        assert "nm-santa-fe" in ids
