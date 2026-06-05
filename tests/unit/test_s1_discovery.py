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


# ─────────────────────────────────────────────────────────────────────────────
# Registry discovery path
# ─────────────────────────────────────────────────────────────────────────────

import logging
import yaml as _yaml

from pipeline.s1_discovery import (
    CommunityStub,
    _registry_path,
    _select_source,
    load_from_registry,
)

_SAMPLE_REGISTRY = {
    "ms-jackson": {
        "display_name": "Jackson",
        "state": "MS",
        "district_nces_id": "2801230",
        "district_name": "Jackson Public Schools",
        "has_charters": True,
        "enrollment": 24000,
        "source": "ccd_auto",
    },
    "ms-hattiesburg": {
        "display_name": "Hattiesburg",
        "state": "MS",
        "district_nces_id": "2802100",
        "district_name": "Hattiesburg Municipal SD",
        "has_charters": False,
        "enrollment": 6500,
        "source": "ccd_auto",
    },
}


def test_registry_path_format():
    assert _registry_path("MS") == "config/community_registry/ms.yaml"
    assert _registry_path("TN") == "config/community_registry/tn.yaml"
    assert _registry_path("WI") == "config/community_registry/wi.yaml"


def test_load_from_registry_returns_sorted_stubs(tmp_path):
    f = tmp_path / "ms.yaml"
    f.write_text(_yaml.dump(_SAMPLE_REGISTRY))
    stubs = load_from_registry(str(f), "MS")
    assert len(stubs) == 2
    assert all(isinstance(s, CommunityStub) for s in stubs)
    # Sorted alphabetically by community_id
    assert stubs[0].community_id == "ms-hattiesburg"
    assert stubs[1].community_id == "ms-jackson"
    assert stubs[0].name == "Hattiesburg"
    assert stubs[1].name == "Jackson"
    assert stubs[0].state == "MS"


def test_load_from_registry_stub_fields(tmp_path):
    f = tmp_path / "ms.yaml"
    f.write_text(_yaml.dump(_SAMPLE_REGISTRY))
    stubs = load_from_registry(str(f), "MS")
    stub = next(s for s in stubs if s.community_id == "ms-jackson")
    assert stub.school_count == 0
    assert stub.school_names == []
    assert stub.county is None
    assert stub.boundary_type == "DISTRICT"
    assert stub.source_file == str(f)


def test_select_source_uses_registry_when_present(tmp_path, monkeypatch):
    """_select_source returns 'registry' when the registry file exists."""
    import pipeline.s1_discovery as s1
    f = tmp_path / "ms.yaml"
    f.write_text(_yaml.dump(_SAMPLE_REGISTRY))
    monkeypatch.setattr(s1, "_registry_path", lambda state: str(f))

    source_type, path = s1._select_source("MS")
    assert source_type == "registry"
    assert path == str(f)


def test_select_source_logs_registry_message(tmp_path, monkeypatch, caplog):
    """Registry discovery logs the expected message."""
    import pipeline.s1_discovery as s1
    f = tmp_path / "ms.yaml"
    f.write_text(_yaml.dump(_SAMPLE_REGISTRY))
    monkeypatch.setattr(s1, "_registry_path", lambda state: str(f))

    with caplog.at_level(logging.INFO, logger="pipeline.s1_discovery"):
        s1._select_source("MS")

    assert "using community registry for MS" in caplog.text


def test_select_source_falls_back_to_roster_when_no_registry(monkeypatch):
    """_select_source returns 'roster' when no registry file exists."""
    import pipeline.s1_discovery as s1
    monkeypatch.setattr(s1, "_registry_path", lambda state: "/nonexistent/ms.yaml")

    source_type, path = s1._select_source("MS")
    assert source_type == "roster"
    assert "charter_roster.csv" in path


def test_select_source_logs_fallback_message(monkeypatch, caplog):
    """Roster fallback logs the expected message."""
    import pipeline.s1_discovery as s1
    monkeypatch.setattr(s1, "_registry_path", lambda state: "/nonexistent/ms.yaml")

    with caplog.at_level(logging.INFO, logger="pipeline.s1_discovery"):
        s1._select_source("MS")

    assert "falling back to roster CSV" in caplog.text


def test_nm_has_no_registry_file():
    """NM does not have a registry file — it uses roster discovery."""
    assert not os.path.exists(_registry_path("NM"))
