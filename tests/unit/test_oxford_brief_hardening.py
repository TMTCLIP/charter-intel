"""
tests/unit/test_oxford_brief_hardening.py

Regression tests for the Oxford MS brief hardening pass (eligibility-contradiction
+ presentation defects D1-D8). The load-bearing one is D1: the Local Authorizers
eligibility statement must agree with the statutory-barrier gate state that the
banner/snapshot consume — no separate, stale, contradicting string. Regression
target is the Ackerman-class systemic failure (gated-eligible markets rendered as
"not currently eligible").
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from jinja2 import Environment, FileSystemLoader
from pipeline.s6_synthesis import (
    _build_data_sources,
    _inject_market_entry_flags,
    _patch_authorizer_descriptions,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


_CONSENT_BARRIER = {
    "severity": "consent_required",
    "applies": True,
    "statute": "Miss. Code Ann. § 37-28-7",
    "banner_text": "Charter authorization in this district requires local school board approval ...",
}

_FALSE_STRINGS = ("designated geographic areas", "not currently eligible")


def _gated_eligible_ms_brief():
    """A minimal gated-eligible MS greenfield brief, mirroring Oxford's shape."""
    return {
        "community_name": "Testville", "state": "MS", "composite_score": 6.3,
        "confidence_overall": "LOW", "data_through": "2025-12-31",
        "data_coverage_tier": "reliable", "executive_snapshot": "x",
        "classification": "MODERATE OPPORTUNITY", "market_type": "greenfield",
        "verdict": "ELIGIBLE", "statutory_barrier": dict(_CONSENT_BARRIER),
        "recommendation_confidence_gate": {"gated": True, "reason": "Overall confidence is LOW."},
        "data_sources": [],
        "scorecard_summary": {
            "tier_display_label": "MODERATE OPPORTUNITY", "composite_score": 6.3,
            "top_drivers": [], "override_flags": [],
            "excluded_dimensions": ["charter_saturation", "competitive_opportunity"],
            "dimension_table": [
                {"dimension": "funding_environment", "display_name": "Funding Environment",
                 "score": 9.0, "weight": 0.153846, "confidence": "MODERATE",
                 "used_default": False, "driver": "d"},
                {"dimension": "charter_saturation", "display_name": "Charter Saturation",
                 "score": 5.0, "weight": 0.0625, "confidence": "LOW",
                 "used_default": True, "driver": "d"},
                {"dimension": "competitive_opportunity", "display_name": "Competitive Opportunity",
                 "score": 5.0, "weight": 0.125, "confidence": "LOW",
                 "used_default": True, "driver": "d"},
            ],
        },
        "recommendations": [
            {"action": "Conduct local board posture assessment.", "rationale": "r",
             "confidence": "HIGH", "supporting_fact_ids": []},
        ],
        "quick_reads": {"facilities": "a", "political": "b", "authorizer": "c"},
        "needs_verification": [], "sources": [], "top_charter_schools": [],
        "local_authorizers": [
            {"authorizer_name": "Mississippi Charter School Authorizer Board", "type": "SEA",
             "reputation_note": "Authorizes charter schools only in designated geographic areas; "
                                "Oxford is not currently eligible under Mississippi charter law.",
             "confidence": "HIGH"},
        ],
    }


def _render_greenfield(brief):
    env = Environment(loader=FileSystemLoader(os.path.join(_ROOT, "templates")), autoescape=True)
    return env.get_template("greenfield_brief.html.j2").render(
        brief=brief,
        debug={"run_id": "x", "timestamp": "t", "depth": "fast", "token_rows": [], "warn_lines": []},
        schools=[], pci_promoted=False,
    )


# ── D1 — Local Authorizers eligibility statement matches gate state ──────────────

class TestAuthorizerEligibilityMatchesGate:
    def test_consent_required_note_is_gated_not_barred(self):
        brief = _gated_eligible_ms_brief()
        out = _patch_authorizer_descriptions(brief, "MS")
        note = out["local_authorizers"][0]["reputation_note"]
        assert "process gate" in note and "not a prohibition" in note
        for bad in _FALSE_STRINGS:
            assert bad not in note

    def test_prohibition_note_says_prohibited(self):
        brief = _gated_eligible_ms_brief()
        brief["statutory_barrier"] = {"severity": "prohibition", "applies": True}
        out = _patch_authorizer_descriptions(brief, "MS")
        assert "prohibited" in out["local_authorizers"][0]["reputation_note"].lower()

    def test_non_ms_left_untouched(self):
        brief = _gated_eligible_ms_brief()
        original = brief["local_authorizers"][0]["reputation_note"]
        out = _patch_authorizer_descriptions(brief, "NM")
        assert out["local_authorizers"][0]["reputation_note"] == original

    def test_rendered_brief_has_no_false_eligibility_strings(self):
        """The Ackerman-class regression: a gated-eligible market must not render
        'not currently eligible' or 'designated geographic areas' anywhere."""
        brief = _patch_authorizer_descriptions(_gated_eligible_ms_brief(), "MS")
        html = _render_greenfield(brief)
        for bad in _FALSE_STRINGS:
            assert bad not in html


# ── D2 — recommendation labeling matches display ─────────────────────────────────

class TestRecommendationLabeling:
    def test_gated_brief_labels_recs_provisional_not_suppressed(self):
        html = _render_greenfield(_patch_authorizer_descriptions(_gated_eligible_ms_brief(), "MS"))
        assert "Recommendations suppressed" not in html
        assert "provisional" in html.lower()
        # recs are still displayed in full
        assert "Conduct local board posture assessment." in html


# ── D3 — proficiency vintage is a school-year span, not a bare calendar year ─────

class TestProficiencyVintageLabel:
    def test_ms_proficiency_vintage_is_school_year_span(self):
        bundle = {"has_proficiency_data": True,
                  "facts": [{"fact_key": "district_proficiency_ela_pct", "value_year": 2023}]}
        rows = _build_data_sources({}, bundle, "MS")
        prof = next(r for r in rows if "MSRC" in r["source"])
        assert prof["vintage"] == "2022-23"
        # status still computed from the end year — a precision fix, not a currency fix
        assert prof["status_icon"] == "⚠"


# ── D5 — demand-exclusion disclosure fires when demand dims are zero-weighted ────

class TestDemandExclusionDisclosure:
    def test_disclosure_present_when_both_demand_dims_excluded(self):
        html = _render_greenfield(_patch_authorizer_descriptions(_gated_eligible_ms_brief(), "MS"))
        assert "not charter demand" in html

    def test_disclosure_absent_when_demand_dims_scored(self):
        brief = _gated_eligible_ms_brief()
        brief["scorecard_summary"]["excluded_dimensions"] = []
        brief["scorecard_summary"]["dimension_table"] = [
            {"dimension": "funding_environment", "display_name": "Funding Environment",
             "score": 9.0, "weight": 0.15, "confidence": "MODERATE",
             "used_default": False, "driver": "d"},
        ]
        html = _render_greenfield(_patch_authorizer_descriptions(brief, "MS"))
        assert "not charter demand" not in html


# ── D7/D8 — market-entry flags + recommendations for consent-gated MS markets ────

class TestMarketEntryFlags:
    def test_consent_gated_ms_gets_flags_and_recs(self):
        out = _inject_market_entry_flags(_gated_eligible_ms_brief(), "MS")
        actions = " ".join(r["action"].lower() for r in out["recommendations"])
        assert "differentiation" in actions
        assert "amended" in actions
        claims = " ".join(i["claim"].lower() for i in out["needs_verification"])
        assert "rating cycle" in claims

    def test_idempotent(self):
        brief = _inject_market_entry_flags(_gated_eligible_ms_brief(), "MS")
        n_recs, n_nv = len(brief["recommendations"]), len(brief["needs_verification"])
        brief = _inject_market_entry_flags(brief, "MS")
        assert len(brief["recommendations"]) == n_recs
        assert len(brief["needs_verification"]) == n_nv

    def test_non_ms_is_noop(self):
        brief = _gated_eligible_ms_brief()
        n = len(brief["recommendations"])
        out = _inject_market_entry_flags(brief, "NM")
        assert len(out["recommendations"]) == n

    def test_no_barrier_is_noop(self):
        brief = _gated_eligible_ms_brief()
        brief["statutory_barrier"] = None
        n = len(brief["recommendations"])
        out = _inject_market_entry_flags(brief, "MS")
        assert len(out["recommendations"]) == n
