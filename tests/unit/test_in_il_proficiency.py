"""
tests/unit/test_in_il_proficiency.py

Unit tests for the IN (ILEARN) and IL (IAR) proficiency adapters.

Both fetchers use name-based city-slug matching (no LEAID lookup) because
nces_district_map is empty for IN and IL in v1. Tests exercise the slug
extraction, name matching, CSV parsing, and graceful-degradation paths.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.fetchers.in_proficiency_fetcher as inf
import pipeline.fetchers.il_proficiency_fetcher as ilf

pytestmark = pytest.mark.unit

_REQUIRED_KEYS = {
    "ela_proficiency_pct", "math_proficiency_pct",
    "school_year", "source_url", "source_title", "confidence",
}

_CSV_HEADER = "LEAID,DistrictName,EntityID,ELAProficiencyPct,MathProficiencyPct,SchoolYear\n"


def _write_csv(tmp_path, rows: str):
    p = tmp_path / "prof.csv"
    p.write_text(_CSV_HEADER + rows, encoding="utf-8")
    return str(p)


# ── graceful degradation ───────────────────────────────────────────────────────

class TestGracefulDegradation:
    def test_in_returns_none_without_csv(self, monkeypatch):
        monkeypatch.setattr(inf, "CSV_PATH", "data/raw/in/does_not_exist.csv")
        assert inf.fetch_in_proficiency("in-indianapolis") is None
        assert inf.get_in_district_data("in-indianapolis") is None

    def test_il_returns_none_without_csv(self, monkeypatch):
        monkeypatch.setattr(ilf, "CSV_PATH", "data/raw/il/does_not_exist.csv")
        assert ilf.fetch_il_proficiency("il-chicago") is None
        assert ilf.get_il_district_data("il-chicago") is None


# ── city slug extraction ───────────────────────────────────────────────────────

class TestCitySlug:
    def test_in_simple(self):
        assert inf._city_slug("in-indianapolis") == "indianapolis"

    def test_in_hyphenated(self):
        assert inf._city_slug("in-fort-wayne") == "fort wayne"

    def test_in_strips_leaid_suffix(self):
        assert inf._city_slug("in-indianapolis-1804770") == "indianapolis"

    def test_il_simple(self):
        assert ilf._city_slug("il-chicago") == "chicago"

    def test_il_hyphenated(self):
        assert ilf._city_slug("il-north-chicago") == "north chicago"

    def test_il_strips_leaid_suffix(self):
        assert ilf._city_slug("il-chicago-1709930") == "chicago"


# ── happy path: IN adapter ─────────────────────────────────────────────────────

class TestINAdapter:
    def test_parses_row(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Indianapolis Public Schools,,19.1,27.2,2023-2024\n",
        )
        monkeypatch.setattr(inf, "CSV_PATH", csv_path)
        r = inf.fetch_in_proficiency("in-indianapolis")
        assert r is not None
        assert _REQUIRED_KEYS.issubset(r.keys())
        assert r["ela_proficiency_pct"] == 19.1
        assert r["math_proficiency_pct"] == 27.2
        assert r["school_year"] == "2023-2024"
        assert "in.gov" in r["source_url"]
        assert r["confidence"] == "HIGH"

    def test_no_match_returns_none(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Indianapolis Public Schools,,19.1,27.2,2023-2024\n",
        )
        monkeypatch.setattr(inf, "CSV_PATH", csv_path)
        assert inf.fetch_in_proficiency("in-gary") is None

    def test_prefers_public_schools_in_name(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Fort Wayne Community Schools,,31.0,28.0,2023-2024\n"
            ",Fort Wayne Public Schools,,35.0,33.0,2023-2024\n",
        )
        monkeypatch.setattr(inf, "CSV_PATH", csv_path)
        r = inf.fetch_in_proficiency("in-fort-wayne")
        assert r is not None
        assert r["ela_proficiency_pct"] == 35.0

    def test_get_in_district_data_delegates(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Indianapolis Public Schools,,19.1,27.2,2023-2024\n",
        )
        monkeypatch.setattr(inf, "CSV_PATH", csv_path)
        r = inf.get_in_district_data("in-indianapolis-1804770")
        assert r is not None
        assert r["ela_proficiency_pct"] == 19.1

    def test_both_null_returns_none(self, tmp_path, monkeypatch):
        csv_path = _write_csv(tmp_path, ",Indianapolis Public Schools,,,\n")
        monkeypatch.setattr(inf, "CSV_PATH", csv_path)
        assert inf.fetch_in_proficiency("in-indianapolis") is None


# ── happy path: IL adapter ─────────────────────────────────────────────────────

class TestILAdapter:
    def test_parses_row(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Chicago Public Schools District 299,,30.5,18.3,2023-2024\n",
        )
        monkeypatch.setattr(ilf, "CSV_PATH", csv_path)
        r = ilf.fetch_il_proficiency("il-chicago")
        assert r is not None
        assert _REQUIRED_KEYS.issubset(r.keys())
        assert r["ela_proficiency_pct"] == 30.5
        assert r["math_proficiency_pct"] == 18.3
        assert r["school_year"] == "2023-2024"
        assert "isbe.net" in r["source_url"]
        assert r["confidence"] == "HIGH"

    def test_no_match_returns_none(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Chicago Public Schools District 299,,30.5,18.3,2023-2024\n",
        )
        monkeypatch.setattr(ilf, "CSV_PATH", csv_path)
        assert ilf.fetch_il_proficiency("il-springfield") is None

    def test_prefers_school_district_in_name(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Springfield Academy,,40.0,38.0,2023-2024\n"
            ",Springfield School District 186,,45.0,42.0,2023-2024\n",
        )
        monkeypatch.setattr(ilf, "CSV_PATH", csv_path)
        r = ilf.fetch_il_proficiency("il-springfield")
        assert r is not None
        assert r["ela_proficiency_pct"] == 45.0

    def test_get_il_district_data_delegates(self, tmp_path, monkeypatch):
        csv_path = _write_csv(
            tmp_path,
            ",Chicago Public Schools District 299,,30.5,18.3,2023-2024\n",
        )
        monkeypatch.setattr(ilf, "CSV_PATH", csv_path)
        r = ilf.get_il_district_data("il-chicago-1709930")
        assert r is not None
        assert r["math_proficiency_pct"] == 18.3

    def test_both_null_returns_none(self, tmp_path, monkeypatch):
        csv_path = _write_csv(tmp_path, ",Chicago Public Schools District 299,,,\n")
        monkeypatch.setattr(ilf, "CSV_PATH", csv_path)
        assert ilf.fetch_il_proficiency("il-chicago") is None


# ── interface parity with MS adapter ──────────────────────────────────────────

def test_adapter_return_shape_matches_ms(tmp_path, monkeypatch):
    from pipeline.fetchers.ms_proficiency_fetcher import fetch_ms_proficiency
    ms = fetch_ms_proficiency("2803450")  # Oxford — real committed data
    assert ms is not None
    assert _REQUIRED_KEYS.issubset(ms.keys())

    in_csv = _write_csv(tmp_path, ",Indianapolis Public Schools,,19.1,27.2,2023-2024\n")
    monkeypatch.setattr(inf, "CSV_PATH", in_csv)
    r_in = inf.fetch_in_proficiency("in-indianapolis")
    assert set(r_in.keys()) == set(ms.keys())

    il_csv = _write_csv(tmp_path, ",Chicago Public Schools District 299,,30.5,18.3,2023-2024\n")
    monkeypatch.setattr(ilf, "CSV_PATH", il_csv)
    r_il = ilf.fetch_il_proficiency("il-chicago")
    assert set(r_il.keys()) == set(ms.keys())
