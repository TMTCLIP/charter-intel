"""
tests/unit/test_nces_fetcher.py

Unit tests for nces_fetcher helpers — focused on state-agnostic path
derivation and graceful fallback when files are absent.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _ROOT)

import pipeline.utils.nces_fetcher as nf


# ─────────────────────────────────────────────────────────────────────────────
# _read_finance — path derivation
# ─────────────────────────────────────────────────────────────────────────────

def test_read_finance_returns_none_when_nm_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No finance CSV present anywhere — must return None, not raise.
    result = nf._read_finance("3500060", "NM")
    assert result is None


def test_read_finance_returns_none_when_tx_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = nf._read_finance("4800001", "TX")
    assert result is None


def test_read_finance_reads_state_specific_path(tmp_path, monkeypatch):
    """Finance CSV is read from data/raw/{state}/, not a hardcoded nm/ path."""
    monkeypatch.chdir(tmp_path)
    tx_dir = tmp_path / "data" / "raw" / "tx"
    tx_dir.mkdir(parents=True)
    csv_path = tx_dir / "nces_lea_finance_2024.csv"
    csv_path.write_text(
        "LEAID,MEMBERSCH,TOTALREV,TFEDREV,TSTREV,TLOCREV,TOTALEXP\n"
        "4800001,5000,50000000,5000000,20000000,25000000,48000000\n"
    )
    result = nf._read_finance("4800001", "TX")
    assert result is not None
    assert result["MEMBERSCH"] == 5000


def test_read_finance_nm_path_not_used_for_tx(tmp_path, monkeypatch):
    """A file at data/raw/nm/ must NOT satisfy a TX query."""
    monkeypatch.chdir(tmp_path)
    nm_dir = tmp_path / "data" / "raw" / "nm"
    nm_dir.mkdir(parents=True)
    csv_path = nm_dir / "nces_lea_finance_2024.csv"
    csv_path.write_text(
        "LEAID,MEMBERSCH,TOTALREV,TFEDREV,TSTREV,TLOCREV,TOTALEXP\n"
        "4800001,5000,50000000,5000000,20000000,25000000,48000000\n"
    )
    # TX query — must look in data/raw/tx/, not data/raw/nm/
    result = nf._read_finance("4800001", "TX")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# get_district_data — state param threads into _read_finance
# ─────────────────────────────────────────────────────────────────────────────

def test_get_district_data_passes_state_to_read_finance(monkeypatch):
    """get_district_data must pass state to _read_finance (not hardcode 'nm')."""
    calls = []

    def fake_load_nces_map(state):
        return {"tx-austin": "4800001"}

    def fake_read_finance(leaid, state):
        calls.append(state)
        return None  # trigger early return — we only care that state was passed

    monkeypatch.setattr(nf, "_load_nces_map", fake_load_nces_map)
    monkeypatch.setattr(nf, "_read_finance", fake_read_finance)

    nf.get_district_data("tx-austin", "TX")

    assert calls == ["TX"]
