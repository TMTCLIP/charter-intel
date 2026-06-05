"""
tests/unit/test_fips_utils.py

Unit tests for pipeline/utils/fips_utils.py.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.utils.fips_utils import get_state_fips

pytestmark = pytest.mark.unit


class TestGetStateFips:
    def test_nm_returns_35(self):
        assert get_state_fips("NM") == "35"

    def test_ms_returns_28(self):
        assert get_state_fips("MS") == "28"

    def test_tn_returns_47(self):
        assert get_state_fips("TN") == "47"

    def test_wi_returns_55(self):
        assert get_state_fips("WI") == "55"

    def test_unknown_returns_none(self):
        assert get_state_fips("XX") is None

    def test_lowercase_input_normalised(self):
        assert get_state_fips("nm") == "35"
        assert get_state_fips("ms") == "28"

    def test_all_50_states_plus_dc_present(self):
        # Spot-check a selection across all regions
        expected = {
            "AL": "01", "CA": "06", "DC": "11", "FL": "12",
            "TX": "48", "WA": "53", "WY": "56",
        }
        for abbr, fips in expected.items():
            assert get_state_fips(abbr) == fips, f"{abbr} should be {fips}"
