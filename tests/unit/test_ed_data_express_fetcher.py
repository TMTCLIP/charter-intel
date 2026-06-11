"""
tests/unit/test_ed_data_express_fetcher.py

Unit tests for pipeline/fetchers/ed_data_express_fetcher.py.
Mocks all network and disk I/O — zero real HTTP calls.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.fetchers.ed_data_express_fetcher as ede

pytestmark = pytest.mark.unit

# ── helpers ───────────────────────────────────────────────────────────────────

_OXFORD_LEAID = "2803450"
_OXFORD_ROW = {
    "SCHOOL_YEAR": "2022-23",
    "STATE_NAME": "Mississippi",
    "LEAID": "2803450",
    "LEA_NAME": "Oxford School District",
    "DATA_GROUP_ID": "814",
    "DENOMINATOR": "4553",
    "NUMERATOR": "1124",
    "TEXT_VALUE": "24.7%",
    "NUMERIC_VALUE": "24.7",
    "SUBGROUP": "ALLLEA",
}
_EXTRA_SUBGROUP_ROW = dict(_OXFORD_ROW, SUBGROUP="Male", NUMERIC_VALUE="22.1")
_OTHER_DISTRICT_ROW = dict(_OXFORD_ROW, LEAID="3500060", LEA_NAME="Albuquerque")


def _make_zip(rows: list[dict], csv_name: str = "SY2223_DG814PCT_LEA_082724.csv") -> bytes:
    """Build an in-memory zip containing a CSV with the given rows."""
    if rows:
        fieldnames = list(rows[0].keys())
        buf = io.StringIO()
        import csv as _csv
        writer = _csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        csv_bytes = buf.getvalue().encode("utf-8")
    else:
        csv_bytes = b"SCHOOL_YEAR,STATE_NAME,LEAID,NUMERIC_VALUE,SUBGROUP\n"

    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as zf:
        zf.writestr(csv_name, csv_bytes)
    return zb.getvalue()


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    monkeypatch.setattr(ede, "_read_lookup_cache", lambda: None)
    monkeypatch.setattr(ede, "_write_lookup_cache", lambda lookup: None)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestFetchAndLookup:
    def test_oxford_returns_correct_pct(self, monkeypatch):
        zip_bytes = _make_zip([_OXFORD_ROW, _EXTRA_SUBGROUP_ROW, _OTHER_DISTRICT_ROW])
        monkeypatch.setattr(ede, "_fetch_zip", lambda: zip_bytes)

        result = ede.get_ed_data_express_absenteeism(_OXFORD_LEAID)

        assert result is not None
        assert result["chronic_absenteeism_pct"] == 24.7
        assert result["chronic_absent_count"] == 1124
        assert result["chronic_absent_denominator"] == 4553
        assert result["chronic_absenteeism_year"] == ede.ED_DATA_EXPRESS_CA_YEAR
        assert result["confidence"] == "HIGH"
        assert result["source"] == "ED_DATA_EXPRESS"

    def test_subgroup_rows_excluded(self, monkeypatch):
        """Only ALLLEA rows should match; other subgroups should not bleed in."""
        zip_bytes = _make_zip([_EXTRA_SUBGROUP_ROW])
        monkeypatch.setattr(ede, "_fetch_zip", lambda: zip_bytes)

        result = ede.get_ed_data_express_absenteeism(_OXFORD_LEAID)
        assert result is None

    def test_unknown_leaid_returns_none(self, monkeypatch):
        zip_bytes = _make_zip([_OXFORD_ROW])
        monkeypatch.setattr(ede, "_fetch_zip", lambda: zip_bytes)

        result = ede.get_ed_data_express_absenteeism("9999999")
        assert result is None

    def test_empty_leaid_returns_none(self):
        assert ede.get_ed_data_express_absenteeism("") is None
        assert ede.get_ed_data_express_absenteeism(None) is None

    def test_zip_fetch_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(ede, "_fetch_zip", lambda: None)
        assert ede.get_ed_data_express_absenteeism(_OXFORD_LEAID) is None

    def test_leaid_zero_padded_to_7(self, monkeypatch):
        """LEAIDs shorter than 7 digits should be zero-padded before lookup."""
        zip_bytes = _make_zip([_OXFORD_ROW])
        monkeypatch.setattr(ede, "_fetch_zip", lambda: zip_bytes)

        result = ede.get_ed_data_express_absenteeism("2803450")  # already 7
        assert result is not None

    def test_lookup_cache_hit_bypasses_fetch(self, monkeypatch):
        cached_lookup = {_OXFORD_LEAID: {"chronic_absenteeism_pct": 24.7, "chronic_absent_count": 1124, "chronic_absent_denominator": 4553}}
        monkeypatch.setattr(ede, "_read_lookup_cache", lambda: cached_lookup)
        fetch_called = []
        monkeypatch.setattr(ede, "_fetch_zip", lambda: fetch_called.append(1) or b"")

        result = ede.get_ed_data_express_absenteeism(_OXFORD_LEAID)
        assert result is not None
        assert result["chronic_absenteeism_pct"] == 24.7
        assert not fetch_called

    def test_invalid_numeric_value_skipped(self, monkeypatch):
        bad_row = dict(_OXFORD_ROW, NUMERIC_VALUE="N/A")
        zip_bytes = _make_zip([bad_row])
        monkeypatch.setattr(ede, "_fetch_zip", lambda: zip_bytes)

        result = ede.get_ed_data_express_absenteeism(_OXFORD_LEAID)
        assert result is None

    def test_column_names_preserved(self, monkeypatch):
        zip_bytes = _make_zip([_OXFORD_ROW])
        monkeypatch.setattr(ede, "_fetch_zip", lambda: zip_bytes)

        result = ede.get_ed_data_express_absenteeism(_OXFORD_LEAID)
        assert result is not None
        expected_keys = {
            "chronic_absenteeism_pct",
            "chronic_absenteeism_year",
            "chronic_absent_count",
            "chronic_absent_denominator",
            "source",
            "source_url",
            "source_title",
            "confidence",
            "data_note",
        }
        assert expected_keys == set(result.keys())
