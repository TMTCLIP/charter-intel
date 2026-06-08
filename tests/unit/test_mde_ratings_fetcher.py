"""
tests/unit/test_mde_ratings_fetcher.py

Unit tests for pipeline/fetchers/mde_ratings_fetcher.py. Offline: reads the
committed MDE xlsx (data/raw/ms/mde_ratings_2024.xlsx) + CCD parquet; no network.
Also verifies the rating flows through the S3 injector into the S5 § 37-28-7 gate.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.fetchers.mde_ratings_fetcher as mde
import pipeline.s3_fact_extraction as s3
from pipeline.s5_scoring import statutory_barrier_check

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


class TestMdeRatingsFetcher:
    def test_oxford_is_rated_A(self):
        r = mde.get_mde_district_rating("2803450")
        assert r is not None
        assert r["district_accountability_rating"] == "A"
        assert r["district_rating_year"] == 2024
        assert r["confidence"] == "HIGH"

    @pytest.mark.parametrize("leaid,expected", [
        ("2804260", "B"),  # Tishomingo County (override)
        ("2801380", "B"),  # East Jasper Consolidated (override)
        ("2800195", "B"),  # Holmes County Consolidated (override)
        ("2801410", "C"),  # East Tallahatchie Consolidated (override)
        ("2800198", "C"),  # Greenwood-Leflore Consolidated (override)
    ])
    def test_override_districts_resolve(self, leaid, expected):
        r = mde.get_mde_district_rating(leaid)
        assert r is not None and r["district_accountability_rating"] == expected

    def test_unknown_leaid_returns_none(self):
        assert mde.get_mde_district_rating("9999999") is None

    def test_empty_leaid_returns_none(self):
        assert mde.get_mde_district_rating("") is None
        assert mde.get_mde_district_rating(None) is None

    def test_map_covers_most_districts(self):
        m = mde._build_rating_map()
        # ~136 traditional districts map; charter-school rows are excluded.
        assert len(m) >= 120
        assert all(v["rating"] in {"A", "B", "C", "D", "F"} for v in m.values())

    def test_normalize_collapses_vocabulary(self):
        assert mde._normalize("Oxford School District") == mde._normalize("OXFORD SCHOOL DIST")


class TestRatingFlowsToGate:
    def _bundle(self, rating):
        fo = {}
        if rating is not None:
            s3._inject_mde_rating_facts(
                fo, {"district_accountability_rating": rating, "district_rating_year": 2024,
                     "source_url": "u", "source_title": "t"},
                "ms-x", "MS", "X",
            )
        return {"facts": fo.get("facts", [])}

    def test_injector_emits_rating_fact(self):
        b = self._bundle("A")
        f = b["facts"][0]
        assert f["fact_key"] == "district_accountability_rating"
        assert f["value"] == "A"
        assert f["in_main_analysis"] is False  # carrier fact, read by the gate not scored

    def test_injector_none_is_noop(self):
        assert self._bundle(None)["facts"] == []

    def test_A_rating_triggers_consent_applies(self):
        b = statutory_barrier_check("MS", "ms-x", self._bundle("A"))
        assert b["severity"] == "consent_required"
        assert b["applies"] is True
        assert b["evaluable"] is True

    def test_D_rating_passes_through(self):
        assert statutory_barrier_check("MS", "ms-x", self._bundle("D")) is None

    def test_missing_rating_is_unverified_advisory(self):
        b = statutory_barrier_check("MS", "ms-x", self._bundle(None))
        assert b["evaluable"] is False
        assert b["applies"] == "unknown"
