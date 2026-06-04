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

from pipeline.s1_discovery import _city_from_address, _clean_city


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
