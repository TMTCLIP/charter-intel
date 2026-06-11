"""
tests/unit/test_registry_routing.py

Verifies that _registry_prefix_lookup() routes community_ids to the
correct state registry based on the community_id prefix, not the
caller-supplied state argument.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


class TestRegistryPrefixLookup:
    def test_ms_community_id_routes_to_ms_registry_regardless_of_state_arg(self):
        """ms-oxford-2803450 must resolve against ms.yaml even when state='NM'."""
        from main import _registry_prefix_lookup
        result = _registry_prefix_lookup("ms-oxford-2803450", "NM")
        assert result == "ms-oxford-2803450", (
            f"Expected 'ms-oxford-2803450', got {result!r}"
        )

    def test_ms_community_id_not_found_in_nm_registry(self):
        """ms-oxford-2803450 must never appear in nm.yaml."""
        import yaml
        nm_path = os.path.join(_ROOT, "config", "community_registry", "nm.yaml")
        with open(nm_path) as f:
            nm_registry = yaml.safe_load(f) or {}
        assert "ms-oxford-2803450" not in nm_registry

    def test_nm_community_id_still_routes_to_nm_registry(self):
        """nm-santa-fe must still resolve against nm.yaml when state='NM'."""
        from main import _registry_prefix_lookup
        result = _registry_prefix_lookup("nm-santa-fe", "NM")
        assert result == "nm-santa-fe"

    def test_ms_plain_slug_prefix_match_returns_disambiguated_id(self):
        """ms-oxford (no NCES suffix) must expand to the highest-enrollment entry in ms.yaml."""
        from main import _registry_prefix_lookup
        result = _registry_prefix_lookup("ms-oxford", "NM")
        assert result.startswith("ms-oxford-"), (
            f"Expected an ms-oxford-* disambiguated id, got {result!r}"
        )

    def test_community_id_without_state_prefix_falls_back_to_caller_state(self):
        """A bare slug with no two-letter prefix must use the caller-supplied state."""
        from main import _registry_prefix_lookup
        # "oxford" has no state prefix; should fall back to caller state NM and
        # return the id unchanged (no match in nm.yaml).
        result = _registry_prefix_lookup("oxford", "NM")
        assert result == "oxford"
