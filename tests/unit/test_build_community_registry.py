"""
tests/unit/test_build_community_registry.py

Tests for scripts/build_community_registry.py.
Exercises via subprocess to respect the "no pipeline imports" contract.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# Absolute path to the script under test
SCRIPT = str(Path(__file__).parent.parent.parent / "scripts" / "build_community_registry.py")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def write_ccd(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["LEAID", "LEA_NAME", "LCITY", "LSTATE", "ENROLLMENT"])
        writer.writeheader()
        writer.writerows(rows)


def write_roster(path: str, cities: list[str]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["Charter School", "City"])
        writer.writeheader()
        for city in cities:
            writer.writerow({"Charter School": f"{city} Academy", "City": city})


def run_script(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True,
        text=True,
    )


# ─────────────────────────────────────────────
# SYNTHETIC DATA
# ─────────────────────────────────────────────

THREE_DISTRICTS = [
    {"LEAID": "2801230", "LEA_NAME": "Jackson Public Schools",    "LCITY": "Jackson",     "LSTATE": "MS", "ENROLLMENT": "24000"},
    {"LEAID": "2802100", "LEA_NAME": "Hattiesburg Municipal SD",  "LCITY": "Hattiesburg", "LSTATE": "MS", "ENROLLMENT": "6500"},
    {"LEAID": "2800900", "LEA_NAME": "Tiny Valley School Dist",   "LCITY": "Tiny Valley", "LSTATE": "MS", "ENROLLMENT": "200"},
]


# ─────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────

def test_floor_excludes_small_district(tmp_path):
    """3 districts in CCD, 1 below default floor (500) → registry has 2 entries."""
    ccd = str(tmp_path / "ccd.csv")
    out = str(tmp_path / "ms.yaml")
    write_ccd(ccd, THREE_DISTRICTS)

    result = run_script(["--state", "MS", "--ccd-file", ccd, "--output", out])

    assert result.returncode == 0, result.stderr
    with open(out) as f:
        reg = yaml.safe_load(f)
    assert len(reg) == 2
    keys = list(reg.keys())
    assert "ms-tiny-valley" not in keys
    assert "ms-jackson" in keys
    assert "ms-hattiesburg" in keys


def test_floor_applied_correctly_with_lower_value(tmp_path):
    """Lower floor (100) → all 3 districts pass."""
    ccd = str(tmp_path / "ccd.csv")
    out = str(tmp_path / "ms.yaml")
    write_ccd(ccd, THREE_DISTRICTS)

    run_script(["--state", "MS", "--ccd-file", ccd, "--output", out, "--floor", "100"])

    with open(out) as f:
        reg = yaml.safe_load(f)
    assert len(reg) == 3


def test_dry_run_does_not_write_file(tmp_path):
    """--dry-run prints to stdout but must not create the output file."""
    ccd = str(tmp_path / "ccd.csv")
    out = str(tmp_path / "ms.yaml")
    write_ccd(ccd, THREE_DISTRICTS)

    result = run_script(["--state", "MS", "--ccd-file", ccd, "--output", out, "--dry-run"])

    assert result.returncode == 0
    assert not os.path.exists(out)
    assert "dry-run" in result.stdout.lower()


def test_has_charters_set_from_roster(tmp_path):
    """has_charters=True for Jackson (in roster), False for Hattiesburg (not in roster)."""
    ccd = str(tmp_path / "ccd.csv")
    roster = str(tmp_path / "roster.csv")
    out = str(tmp_path / "ms.yaml")
    write_ccd(ccd, THREE_DISTRICTS)
    write_roster(roster, ["Jackson"])

    run_script(["--state", "MS", "--ccd-file", ccd, "--output", out, "--roster", roster])

    with open(out) as f:
        reg = yaml.safe_load(f)
    assert reg["ms-jackson"]["has_charters"] is True
    assert reg["ms-hattiesburg"]["has_charters"] is False


def test_duplicate_city_names_disambiguated(tmp_path):
    """Two districts share city name 'Springfield' → slugs get NCES ID suffix."""
    rows = [
        {"LEAID": "2801001", "LEA_NAME": "Springfield Public Schools", "LCITY": "Springfield", "LSTATE": "MS", "ENROLLMENT": "5000"},
        {"LEAID": "2801002", "LEA_NAME": "Springfield Charter SD",     "LCITY": "Springfield", "LSTATE": "MS", "ENROLLMENT": "3000"},
    ]
    ccd = str(tmp_path / "ccd.csv")
    out = str(tmp_path / "ms.yaml")
    write_ccd(ccd, rows)

    run_script(["--state", "MS", "--ccd-file", ccd, "--output", out])

    with open(out) as f:
        reg = yaml.safe_load(f)
    keys = list(reg.keys())
    assert len(keys) == 2
    assert "ms-springfield" not in keys
    assert "ms-springfield-2801001" in keys
    assert "ms-springfield-2801002" in keys


# ─────────────────────────────────────────────
# EXPANSION STATE: SC (not in the original four)
# ─────────────────────────────────────────────

SC_DISTRICTS = [
    {"LEAID": "4500002", "LEA_NAME": "Dorchester 04",  "LCITY": "St. George",  "LSTATE": "SC", "ENROLLMENT": "2027"},
    {"LEAID": "4500270", "LEA_NAME": "Greenville 01",  "LCITY": "Greenville",  "LSTATE": "SC", "ENROLLMENT": "77978"},
    {"LEAID": "4500510", "LEA_NAME": "Tiny School SD", "LCITY": "Tiny Town",   "LSTATE": "SC", "ENROLLMENT": "50"},
]


def test_sc_registry_produced_for_new_state(tmp_path):
    """Registry builder works for an expansion state (SC) not in the original four.

    Uses a synthetic CSV. Verifies the output file is created, has the correct
    number of entries after enrollment filtering, and carries the correct state code.
    """
    ccd = str(tmp_path / "sc_ccd.csv")
    out = str(tmp_path / "sc.yaml")
    write_ccd(ccd, SC_DISTRICTS)

    result = run_script(["--state", "SC", "--ccd-file", ccd, "--output", out])

    assert result.returncode == 0, result.stderr
    assert os.path.exists(out)
    with open(out) as f:
        reg = yaml.safe_load(f)

    # Two districts pass the default floor (500); Tiny Town (50) is excluded.
    assert len(reg) == 2
    assert "sc-st-george" in reg
    assert "sc-greenville" in reg
    assert "sc-tiny-town" not in reg
    # State code is uppercased correctly
    for entry in reg.values():
        assert entry["state"] == "SC"
        assert entry["source"] == "ccd_auto"


def test_sc_registry_state_filter_excludes_other_states(tmp_path):
    """CCD file with mixed states only produces SC entries when --state SC."""
    rows = SC_DISTRICTS + [
        {"LEAID": "2801230", "LEA_NAME": "Jackson Public Schools", "LCITY": "Jackson", "LSTATE": "MS", "ENROLLMENT": "24000"},
    ]
    ccd = str(tmp_path / "mixed.csv")
    out = str(tmp_path / "sc.yaml")
    write_ccd(ccd, rows)

    run_script(["--state", "SC", "--ccd-file", ccd, "--output", out])

    with open(out) as f:
        reg = yaml.safe_load(f)
    for key in reg:
        assert key.startswith("sc-"), f"Found non-SC key: {key}"
