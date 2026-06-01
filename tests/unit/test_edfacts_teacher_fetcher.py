"""
tests/unit/test_edfacts_teacher_fetcher.py

Unit tests for pipeline/edfacts_teacher_fetcher.py. Mocks the network and the
filesystem cache; zero real network calls.

Step-0 finding under test: the EDFacts staff endpoint 404s, so the fetcher
falls back to a CCD-directory student-teacher ratio. Tests verify the valid
ratio path, graceful None on failure, and the year fallback (current year's
directory empty -> prior year used).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.edfacts_teacher_fetcher as etf

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(etf, "_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(etf, "_write_cache", lambda *a, **k: None)


class _FakeResp:
    def __init__(self, payload, status=200):
        self._b = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_urlopen(monkeypatch, router):
    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        return router(url)
    monkeypatch.setattr(etf.urllib.request, "urlopen", _urlopen)


def test_returns_structured_dict_on_valid_response(monkeypatch):
    """EDFacts staff 404s -> CCD directory ratio computed from teachers_fte."""
    def router(url):
        if "edfacts/staff" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return _FakeResp({"results": [
            {"enrollment": 500, "teachers_fte": 25},
            {"enrollment": 300, "teachers_fte": 15},
        ], "next": None})
    _install_urlopen(monkeypatch, router)

    out = etf.get_teacher_supply("3500060", "nm-test", "NM", year=2022)
    assert out is not None
    assert out["field_used"] == "student_teacher_ratio"
    # (500+300) / (25+15) = 20.0 -> HIGH_SHORTAGE (>= 18)
    assert out["raw_value"] == 20.0
    assert out["teacher_signal"] == "HIGH_SHORTAGE"
    assert out["leaid"] == "3500060"
    assert out["year"] == 2022


def test_returns_none_on_api_failure(monkeypatch):
    """Every request fails -> fetcher degrades to None, never raises."""
    def router(url):
        raise urllib.error.URLError("network down")
    _install_urlopen(monkeypatch, router)

    assert etf.get_teacher_supply("3500060", "nm-test", "NM") is None


def test_returns_none_without_leaid(monkeypatch):
    _install_urlopen(monkeypatch, lambda url: _FakeResp({"results": []}))
    assert etf.get_teacher_supply(None, "nm-test", "NM") is None


def test_year_fallback(monkeypatch):
    """Current year (2022) directory empty -> falls back to 2021."""
    def router(url):
        if "edfacts/staff" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if "/2022/" in url:
            return _FakeResp({"results": [], "next": None})
        if "/2021/" in url:
            return _FakeResp({"results": [
                {"enrollment": 900, "teachers_fte": 50},
            ], "next": None})
        return _FakeResp({"results": [], "next": None})
    _install_urlopen(monkeypatch, router)

    out = etf.get_teacher_supply("3500060", "nm-test", "NM")
    assert out is not None
    assert out["year"] == 2021
    # 900 / 50 = 18.0 -> HIGH_SHORTAGE
    assert out["raw_value"] == 18.0


def test_low_shortage_classification(monkeypatch):
    def router(url):
        if "edfacts/staff" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return _FakeResp({"results": [
            {"enrollment": 1000, "teachers_fte": 100},   # ratio 10.0
        ], "next": None})
    _install_urlopen(monkeypatch, router)

    out = etf.get_teacher_supply("3500060", "nm-test", "NM", year=2022)
    assert out["raw_value"] == 10.0
    assert out["teacher_signal"] == "LOW_SHORTAGE"
