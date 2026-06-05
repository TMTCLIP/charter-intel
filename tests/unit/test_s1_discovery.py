"""
tests/unit/test_s1_discovery.py

Unit tests for s1_discovery helpers: _city_from_address() and _clean_city().
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)

from pipeline.s1_discovery import _city_from_address, _clean_city, _strip_address_artifacts


# ─────────────────────────────────────────────────────────────────────────────
# _clean_city
# ─────────────────────────────────────────────────────────────────────────────

def test_clean_city_strips_state_code_nm():
    assert _clean_city("albuquerque, NM", "NM") == "Albuquerque"


def test_clean_city_strips_state_code_tx():
    assert _clean_city("austin, TX", "TX") == "Austin"


def test_clean_city_title_cases():
    assert _clean_city("santa fe", "NM") == "Santa Fe"


def test_clean_city_no_state_leaves_suffix():
    # Without state arg, trailing ", NM" is not stripped
    assert _clean_city("albuquerque, NM") == "Albuquerque, Nm"


def test_clean_city_strips_leading_trailing_whitespace():
    assert _clean_city("  Taos  ", "NM") == "Taos"


# ─────────────────────────────────────────────────────────────────────────────
# _city_from_address — NM addresses (regression)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("address,expected", [
    ("4300 Cutler Ave NE Albuquerque, NM 87110",     "Albuquerque"),
    ("74 A Van Nu Po Road Santa Fe, NM 87508",        "Santa Fe"),
    ("123 Main St Las Cruces, NM 88001",              "Las Cruces"),
    ("456 Rio Rancho Blvd Rio Rancho, NM 87124",      "Rio Rancho"),
    ("850 N. Telshor Blvd, Las Cruces, NM 88011",    "Las Cruces"),
    ("7300 Old Santa Fe Trail, Santa Fe, NM 87505",  "Santa Fe"),
    ("515 Calle Arbolera Española, NM 87532",         "Española"),
    ("4386 Shiprock, NM 87420",                       "Shiprock"),
])
def test_city_from_address_nm(address, expected):
    assert _city_from_address(address, "NM") == expected


# ─────────────────────────────────────────────────────────────────────────────
# _city_from_address — non-NM states
# ─────────────────────────────────────────────────────────────────────────────

def test_city_from_address_tx():
    assert _city_from_address("1234 Main St Austin, TX 78701", "TX") == "Austin"


def test_city_from_address_co():
    assert _city_from_address("500 Colfax Ave Denver, CO 80203", "CO") == "Denver"


def test_city_from_address_wrong_state_returns_empty():
    # Address is NM but we query as TX — separator not found → returns ""
    assert _city_from_address("4300 Cutler Ave NE Albuquerque, NM 87110", "TX") == ""


def test_city_from_address_multiword_city_tx():
    assert _city_from_address("100 Oak St San Antonio, TX 78205", "TX") == "San Antonio"


def test_city_from_address_no_state_separator_returns_empty():
    assert _city_from_address("No state separator here", "NM") == ""


# ─────────────────────────────────────────────────────────────────────────────
# _strip_address_artifacts — highway prefix regex (multi-state)
# ─────────────────────────────────────────────────────────────────────────────
# Task 2: The highway-prefix regex now matches any 2-letter state abbreviation
# (changed from hardcoded NM to [A-Z]{2}).  These tests guard against regression
# to the NM-only pattern.

@pytest.mark.parametrize("inp,expected", [
    ("MS-82 Canton",    "Canton"),
    ("TN-64 Nashville", "Nashville"),
    ("WI-35 Madison",   "Madison"),
    ("NM-14 Taos",      "Taos"),
])
def test_strip_highway_prefix_multistate(inp, expected):
    assert _strip_address_artifacts(inp) == expected


@pytest.mark.parametrize("address,state,expected", [
    ("MS-82 Jackson, MS 39201", "MS", "Jackson"),
    ("TN-64 Nashville, TN 37201", "TN", "Nashville"),
    ("WI-35 Madison, WI 53701", "WI", "Madison"),
    ("NM-14 Taos, NM 87571", "NM", "Taos"),
])
def test_city_from_address_highway_prefix_multistate(address, state, expected):
    assert _city_from_address(address, state) == expected
