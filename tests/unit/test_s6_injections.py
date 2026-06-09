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
    _inject_consistency_check_banner,
    _inject_recommendation_gate,
    _inject_teacher_supply_warning,
    _inject_tribal_jurisdiction_flag,
    _sanitize_needs_verification,
    _truncate_to_schema_limits,
    _cap_str,
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

    def test_ms_warning_reason_is_never_none(self):
        # MS config scaffolds reason/resolution_path as explicit null; the injection
        # must fall back to defaults rather than leak None (would render "None").
        out = _inject_teacher_supply_warning({"needs_verification": []}, "MS", "ms-oxford")
        entry = out["needs_verification"][0]
        assert entry["reason"], "reason must be a non-empty string"
        assert entry["reason"] is not None
        assert entry["resolution_path"]
        assert entry["impact_if_wrong"] == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# needs_verification sanitizer guard
# ─────────────────────────────────────────────────────────────────────────────

class TestSanitizeNeedsVerification:
    def test_none_reason_replaced_with_placeholder(self):
        brief = {"needs_verification": [
            {"claim": "Teacher supply may constrain charter staffing", "reason": None},
        ]}
        out = _sanitize_needs_verification(brief, "ms-oxford")
        item = out["needs_verification"][0]
        assert item["reason"]  # non-empty
        assert item["reason"] != "None"

    def test_blank_reason_replaced(self):
        brief = {"needs_verification": [{"claim": "X", "reason": "   "}]}
        out = _sanitize_needs_verification(brief, "c")
        assert out["needs_verification"][0]["reason"].strip()

    def test_item_with_no_claim_is_dropped(self):
        brief = {"needs_verification": [
            {"claim": "", "reason": "orphan"},
            {"claim": None, "reason": "orphan2"},
            {"reason": "orphan3"},
            {"claim": "Real claim", "reason": "ok"},
        ]}
        out = _sanitize_needs_verification(brief, "c")
        assert len(out["needs_verification"]) == 1
        assert out["needs_verification"][0]["claim"] == "Real claim"

    def test_good_reason_preserved(self):
        brief = {"needs_verification": [{"claim": "X", "reason": "solid reason"}]}
        out = _sanitize_needs_verification(brief, "c")
        assert out["needs_verification"][0]["reason"] == "solid reason"

    def test_non_dict_items_dropped(self):
        brief = {"needs_verification": ["a string", 42, {"claim": "keep", "reason": "r"}]}
        out = _sanitize_needs_verification(brief, "c")
        assert out["needs_verification"] == [{"claim": "keep", "reason": "r"}]

    def test_missing_or_empty_list_is_noop(self):
        assert _sanitize_needs_verification({}, "c") == {}
        assert _sanitize_needs_verification({"needs_verification": []}, "c")["needs_verification"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Schema length cap — _cap_str and _truncate_to_schema_limits
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaLengthCap:
    def test_cap_str_short_string_unchanged(self):
        notices = []
        result = _cap_str("hello", 10, "field", notices)
        assert result == "hello"
        assert notices == []

    def test_cap_str_exact_limit_unchanged(self):
        notices = []
        s = "a" * 200
        result = _cap_str(s, 200, "field", notices)
        assert result == s
        assert notices == []

    def test_cap_str_over_limit_truncates_to_max_len(self):
        notices = []
        s = "word " * 60  # 300 chars, limit 200
        result = _cap_str(s, 200, "field", notices)
        assert len(result) <= 200
        assert result.endswith("…")
        assert len(notices) == 1

    def test_cap_str_truncates_at_word_boundary(self):
        notices = []
        s = "alpha beta gamma " * 20  # well over limit
        result = _cap_str(s, 50, "field", notices)
        assert result.endswith("…")
        assert len(result) <= 50
        # The body before the ellipsis must be a prefix of the original
        body = result[:-1]
        assert s.startswith(body)

    def test_cap_str_non_string_passthrough(self):
        notices = []
        result = _cap_str(None, 100, "field", notices)
        assert result is None
        assert notices == []

    def test_truncate_executive_snapshot_over_limit(self):
        long_snapshot = "Charter schools in this community " * 20  # >> 600
        brief = {"executive_snapshot": long_snapshot}
        out, notices = _truncate_to_schema_limits(brief)
        assert len(out["executive_snapshot"]) <= 600
        assert out["executive_snapshot"].endswith("…")
        assert any("executive_snapshot" in n for n in notices)

    def test_truncate_recommendation_rationale(self):
        long_rationale = "Evidence suggests strong growth potential " * 12  # >> 400
        brief = {
            "recommendations": [
                {"action": "Act now", "rationale": long_rationale, "confidence": "HIGH", "supporting_fact_ids": []}
            ]
        }
        out, notices = _truncate_to_schema_limits(brief)
        assert len(out["recommendations"][0]["rationale"]) <= 400
        assert any("rationale" in n for n in notices)

    def test_truncate_driver_in_top_drivers(self):
        long_driver = "Strong demographic momentum driven by population growth " * 5  # >> 200
        brief = {
            "scorecard_summary": {
                "top_drivers": [{"dimension": "growth", "score": 7.5, "driver": long_driver}]
            }
        }
        out, notices = _truncate_to_schema_limits(brief)
        assert len(out["scorecard_summary"]["top_drivers"][0]["driver"]) <= 200
        assert any("top_drivers" in n for n in notices)

    def test_truncate_quick_reads_section(self):
        long_text = "Facilities landscape is complex with many competing interests " * 6  # >> 300
        brief = {"quick_reads": {"facilities": long_text, "political": "short"}}
        out, notices = _truncate_to_schema_limits(brief)
        assert len(out["quick_reads"]["facilities"]) <= 300
        assert out["quick_reads"]["political"] == "short"

    def test_truncate_schools_to_watch_reason(self):
        long_reason = "This school has demonstrated consistently high performance " * 5  # >> 200
        brief = {
            "schools_to_watch": [
                {"school_name": "ABC Charter", "flag_type": "HIGH_PERFORMER", "reason": long_reason}
            ]
        }
        out, notices = _truncate_to_schema_limits(brief)
        assert len(out["schools_to_watch"][0]["reason"]) <= 200

    def test_truncate_no_changes_when_all_within_limits(self):
        brief = {
            "executive_snapshot": "Short snapshot.",
            "recommendations": [
                {"action": "Do something", "rationale": "Because reasons.", "confidence": "LOW", "supporting_fact_ids": []}
            ],
        }
        out, notices = _truncate_to_schema_limits(brief)
        assert notices == []
        assert out["executive_snapshot"] == "Short snapshot."


# ─────────────────────────────────────────────────────────────────────────────
# Consistency-check banner injection
# ─────────────────────────────────────────────────────────────────────────────

class TestConsistencyCheckBannerInjection:
    """_inject_consistency_check_banner collects consistency_check_triggered
    dimensions from the scorecard and injects them onto the brief for template rendering."""

    def _scorecard(self, dims: dict) -> dict:
        return {"dimensions": dims}

    def test_injects_corrections_when_check_triggered(self):
        sc = self._scorecard({
            "political_climate": {
                "score": 5.0, "weight": 0.10, "used_default": False,
                "consistency_check_triggered": True,
                "consistency_check_id": "CHECK-002",
                "consistency_check_reason": "Score derived from ineligibility premise.",
            },
            "authorizer_friendliness": {
                "score": 6.0, "weight": 0.15, "used_default": False,
            },
        })
        out = _inject_consistency_check_banner({}, sc)
        assert "consistency_check_corrections" in out
        corrections = out["consistency_check_corrections"]
        assert len(corrections) == 1
        assert corrections[0]["dimension"] == "political_climate"
        assert corrections[0]["consistency_check_id"] == "CHECK-002"
        assert corrections[0]["consistency_check_reason"] is not None

    def test_no_key_when_no_checks_triggered(self):
        """When no dimensions have consistency_check_triggered, the key must be absent
        so the template banner does not render."""
        sc = self._scorecard({
            "political_climate": {"score": 7.0, "weight": 0.10, "used_default": False},
            "authorizer_friendliness": {"score": 6.0, "weight": 0.15, "used_default": False},
        })
        out = _inject_consistency_check_banner({}, sc)
        assert "consistency_check_corrections" not in out

    def test_multiple_triggered_dimensions_all_collected(self):
        sc = self._scorecard({
            "political_climate": {
                "score": 5.0, "weight": 0.10, "used_default": False,
                "consistency_check_triggered": True,
                "consistency_check_id": "CHECK-002",
                "consistency_check_reason": "Ineligibility premise.",
            },
            "authorizer_friendliness": {
                "score": 5.0, "weight": 0.15, "used_default": False,
                "consistency_check_triggered": True,
                "consistency_check_id": "CHECK-001",
                "consistency_check_reason": "CSAB is accessible.",
            },
        })
        out = _inject_consistency_check_banner({}, sc)
        corrections = out["consistency_check_corrections"]
        assert len(corrections) == 2
        check_ids = {c["consistency_check_id"] for c in corrections}
        assert "CHECK-001" in check_ids
        assert "CHECK-002" in check_ids

    def test_empty_dimensions_no_key_injected(self):
        out = _inject_consistency_check_banner({}, {"dimensions": {}})
        assert "consistency_check_corrections" not in out

    def test_existing_brief_fields_preserved(self):
        brief = {"community_id": "ms-oxford-2803450", "composite_score": 6.28}
        sc = self._scorecard({
            "political_climate": {
                "score": 5.0, "weight": 0.10, "used_default": False,
                "consistency_check_triggered": True,
                "consistency_check_id": "CHECK-002",
                "consistency_check_reason": "Reason.",
            }
        })
        out = _inject_consistency_check_banner(brief, sc)
        assert out["community_id"] == "ms-oxford-2803450"
        assert out["composite_score"] == pytest.approx(6.28)
