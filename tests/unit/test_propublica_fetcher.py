"""
tests/unit/test_propublica_fetcher.py

Unit tests for pipeline/propublica_fetcher.py. Mocks the network and the
filesystem cache; zero real network calls.

Step-0 finding under test: search.json carries no financials, so the fetcher
makes a per-EIN detail call. Tests verify the NTEE education filter, the
national-org (high-revenue) exclusion, activity-tier computation for 0 / 1-2 /
3+ qualifying orgs, and graceful None on API failure.
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

import pipeline.propublica_fetcher as pp

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(pp, "_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(pp, "_write_cache", lambda *a, **k: None)


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


def _org_summary(ein, ntee, name="Some Education Fund"):
    return {"ein": ein, "ntee_code": ntee, "name": name}


def _detail(ein, ntee, name, assets, revenue):
    return {"organization": {
        "ein": ein, "ntee_code": ntee, "name": name,
        "asset_amount": assets, "revenue_amount": revenue,
    }}


def _install(monkeypatch, search_orgs, details):
    """Route search.json to a fixed org list and organizations/{ein}.json to
    the supplied detail map. details: {ein: detail_payload}."""
    def router(url):
        if "search.json" in url:
            # Only the first (education) search returns orgs; charter search empty.
            if "charter" in url:
                return _FakeResp({"organizations": []})
            return _FakeResp({"organizations": search_orgs})
        # organizations/{ein}.json
        for ein, payload in details.items():
            if f"/{ein}.json" in url:
                return _FakeResp(payload)
        return _FakeResp({"organization": {}})

    def _urlopen(req, timeout=30):
        url = req.full_url if hasattr(req, "full_url") else req
        return router(url)
    monkeypatch.setattr(pp.urllib.request, "urlopen", _urlopen)


# ── NTEE filter (pure unit) ──────────────────────────────────────────────────

def test_ntee_filter():
    assert pp._ntee_qualifies("B82", "Local Education Fund") is True
    assert pp._ntee_qualifies("R20", "Community Civil Rights") is True
    # Post-secondary education excluded.
    assert pp._ntee_qualifies("B43", "State University Foundation") is False
    assert pp._ntee_qualifies("B50", "Graduate School") is False
    # Non education/civil-rights NTEE excluded.
    assert pp._ntee_qualifies("E20", "General Hospital") is False
    # University/hospital name tokens excluded even with a B code.
    assert pp._ntee_qualifies("B82", "The University of Somewhere") is False
    assert pp._ntee_qualifies(None, "No Code") is False


# ── activity tiers ───────────────────────────────────────────────────────────

def test_activity_tier_active_three_plus(monkeypatch):
    orgs = [_org_summary("111", "B82"), _org_summary("222", "B11"),
            _org_summary("333", "B90")]
    details = {
        "111": _detail("111", "B82", "Ed Fund One", 1_000_000, 800_000),
        "222": _detail("222", "B11", "Ed Fund Two", 2_000_000, 900_000),
        "333": _detail("333", "B90", "Ed Fund Three", 500_000, 400_000),
    }
    _install(monkeypatch, orgs, details)
    out = pp.get_philanthropic_activity("Albuquerque", "NM", "nm-test")
    assert out is not None
    assert out["foundation_count"] == 3
    assert out["activity_tier"] == "ACTIVE"
    assert out["total_assets"] == 3_500_000
    assert out["largest_foundation"]["name"] == "Ed Fund Two"
    assert out["largest_foundation"]["asset_amount"] == 2_000_000


def test_activity_tier_active_by_assets(monkeypatch):
    """A single org above the $10M asset floor still tiers ACTIVE."""
    orgs = [_org_summary("111", "B82")]
    details = {"111": _detail("111", "B82", "Big Ed Fund", 12_000_000, 5_000_000)}
    _install(monkeypatch, orgs, details)
    out = pp.get_philanthropic_activity("Albuquerque", "NM", "nm-test")
    assert out["foundation_count"] == 1
    assert out["total_assets"] == 12_000_000
    assert out["activity_tier"] == "ACTIVE"


def test_activity_tier_moderate(monkeypatch):
    orgs = [_org_summary("111", "B82"), _org_summary("222", "B11")]
    details = {
        "111": _detail("111", "B82", "Ed Fund One", 1_000_000, 800_000),
        "222": _detail("222", "B11", "Ed Fund Two", 500_000, 400_000),
    }
    _install(monkeypatch, orgs, details)
    out = pp.get_philanthropic_activity("Albuquerque", "NM", "nm-test")
    assert out["foundation_count"] == 2
    assert out["activity_tier"] == "MODERATE"


def test_activity_tier_limited_when_none_qualify(monkeypatch):
    """Search returns orgs but none pass the NTEE filter -> LIMITED (not None)."""
    orgs = [_org_summary("111", "T20", name="Some Foundation"),
            _org_summary("222", "E20", name="A Hospital")]
    _install(monkeypatch, orgs, {})
    out = pp.get_philanthropic_activity("Albuquerque", "NM", "nm-test")
    assert out is not None
    assert out["foundation_count"] == 0
    assert out["activity_tier"] == "LIMITED"


def test_national_org_excluded_by_revenue(monkeypatch):
    """A qualifying-NTEE org above the $500M revenue ceiling is dropped."""
    orgs = [_org_summary("999", "B82", name="National Education Org")]
    details = {"999": _detail("999", "B82", "National Education Org",
                              100_000_000, 600_000_000)}
    _install(monkeypatch, orgs, details)
    out = pp.get_philanthropic_activity("Albuquerque", "NM", "nm-test")
    assert out["foundation_count"] == 0
    assert out["activity_tier"] == "LIMITED"


def test_returns_none_on_api_failure(monkeypatch):
    def _urlopen(req, timeout=30):
        raise urllib.error.URLError("network down")
    monkeypatch.setattr(pp.urllib.request, "urlopen", _urlopen)
    assert pp.get_philanthropic_activity("Albuquerque", "NM", "nm-test") is None


def test_returns_none_without_city_or_state(monkeypatch):
    _install(monkeypatch, [], {})
    assert pp.get_philanthropic_activity("", "NM", "nm-test") is None
    assert pp.get_philanthropic_activity("Albuquerque", "", "nm-test") is None
