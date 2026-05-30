"""
tests/unit/test_s6_injections.py

Unit tests for the deterministic S6 honesty injections added in Session 18:
  - _inject_recommendation_gate        (Area E / BLOCKER 4b)
  - _inject_tribal_jurisdiction_flag   (Area F / BLOCKER 5a)
  - _inject_teacher_supply_warning     (Area G / BLOCKER 5b)

These functions are pure (given config/states.yaml on disk) and run AFTER the
hallucination audit, so they cannot be stripped. They read config/states.yaml
with a relative path, hence the chdir fixture.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.s6_synthesis import (
    _inject_recommendation_gate,
    _inject_teacher_supply_warning,
    _inject_tribal_jurisdiction_flag,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


# ─────────────────────────────────────────────────────────────────────────────
# Area E — recommendation confidence gate
# ─────────────────────────────────────────────────────────────────────────────

class TestRecommendationGate:
    def test_insufficient_tier_is_gated(self):
        brief = {}
        sc = {"data_coverage_tier": "INSUFFICIENT", "confidence_overall": "LOW"}
        out = _inject_recommendation_gate(brief, sc)
        assert out["data_coverage_tier"] == "INSUFFICIENT"
        gate = out["recommendation_confidence_gate"]
        assert gate["gated"] is True
        assert "default" in gate["reason"].lower()

    def test_unreliable_tier_is_gated(self):
        out = _inject_recommendation_gate({}, {"data_coverage_tier": "unreliable", "confidence_overall": "MODERATE"})
        assert out["recommendation_confidence_gate"]["gated"] is True

    def test_low_confidence_is_gated_even_when_coverage_ok(self):
        out = _inject_recommendation_gate({}, {"data_coverage_tier": "reliable", "confidence_overall": "LOW"})
        assert out["recommendation_confidence_gate"]["gated"] is True

    def test_reliable_high_confidence_is_not_gated(self):
        out = _inject_recommendation_gate({}, {"data_coverage_tier": "reliable", "confidence_overall": "HIGH"})
        gate = out["recommendation_confidence_gate"]
        assert gate["gated"] is False
        assert gate["data_coverage_tier"] == "reliable"


# ─────────────────────────────────────────────────────────────────────────────
# Area F — tribal jurisdiction flag
# ─────────────────────────────────────────────────────────────────────────────

class TestTribalJurisdictionFlag:
    def test_flagged_community_gets_flag_and_needs_verification(self):
        brief = {"needs_verification": []}
        out = _inject_tribal_jurisdiction_flag(brief, "NM", "nm-jemez-pueblo")
        assert out["tribal_jurisdiction_flag"]["status"] == "FLAGGED"
        nv = out["needs_verification"]
        assert any(e.get("impact_if_wrong") == "HIGH" for e in nv)

    def test_pending_human_review_is_injected_with_moderate_impact(self):
        out = _inject_tribal_jurisdiction_flag({"needs_verification": []}, "NM", "nm-gallup")
        flag = out["tribal_jurisdiction_flag"]
        assert flag["status"] == "PENDING_HUMAN_REVIEW"
        nv = out["needs_verification"]
        assert len(nv) == 1
        assert nv[0]["impact_if_wrong"] == "MODERATE"

    def test_pending_human_review_espanola(self):
        out = _inject_tribal_jurisdiction_flag({"needs_verification": []}, "NM", "nm-espanola")
        assert out["tribal_jurisdiction_flag"]["status"] == "PENDING_HUMAN_REVIEW"

    def test_non_tribal_community_gets_no_flag(self):
        out = _inject_tribal_jurisdiction_flag({"needs_verification": []}, "NM", "nm-albuquerque")
        assert "tribal_jurisdiction_flag" not in out
        assert out["needs_verification"] == []

    def test_state_code_case_insensitive(self):
        out = _inject_tribal_jurisdiction_flag({"needs_verification": []}, "nm", "nm-jemez-pueblo")
        assert out.get("tribal_jurisdiction_flag", {}).get("status") == "FLAGGED"


# ─────────────────────────────────────────────────────────────────────────────
# Area G — teacher supply warning
# ─────────────────────────────────────────────────────────────────────────────

class TestTeacherSupplyWarning:
    def test_nm_brief_gets_high_impact_warning(self):
        out = _inject_teacher_supply_warning({"needs_verification": []}, "NM", "nm-questa")
        nv = out["needs_verification"]
        assert len(nv) == 1
        entry = nv[0]
        assert entry["impact_if_wrong"] == "HIGH"
        assert "teacher" in entry["claim"].lower()
        assert "NMPED" in entry["resolution_path"]

    def test_unknown_state_is_noop(self):
        out = _inject_teacher_supply_warning({"needs_verification": []}, "ZZ", "zz-nowhere")
        assert out["needs_verification"] == []

    def test_creates_needs_verification_list_if_missing(self):
        out = _inject_teacher_supply_warning({}, "NM", "nm-questa")
        assert isinstance(out["needs_verification"], list)
        assert len(out["needs_verification"]) == 1
