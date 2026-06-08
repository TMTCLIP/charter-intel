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
    _inject_market_routing,
    _patch_authorizer_descriptions,
    _patch_statutory_narrative,
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


# ─────────────────────────────────────────────────────────────────────────────
# Fresh-run hardening (this session): RC3 LLM-narrative guard + lock-in tests
# ─────────────────────────────────────────────────────────────────────────────

from pipeline.fetchers.mde_ratings_fetcher import get_mde_district_rating, _LOCAL_XLSX, _CCD_PARQUET
from pipeline.utils.nces_fetcher import get_district_data

_OXFORD = "ms-oxford-2803450"
_OXFORD_LEAID = "2803450"


# ── RC3 — _patch_statutory_narrative: targeted find-and-correct, not a rewriter ──

def _consent_brief_with_false_framing():
    return {
        "statutory_barrier": dict(_CONSENT_BARRIER),
        "executive_snapshot": (
            "Oxford holds a B rating, making it ineligible for charter authorization "
            "under Miss. Code Ann. § 37-28-5 unless the district rating changes to D/F."
        ),
        "recommendations": [
            {"action": "Engage board.",
             "rationale": "The district is not currently eligible under § 37-28-5; consent needed.",
             "evidence_summary": "A-rated.", "primary_risk": "Board opposition.",
             "confidence": "HIGH", "supporting_fact_ids": []},
        ],
        "quick_reads": {"authorizer": "District is ineligible until the district rating declines.",
                        "political": "x", "facilities": "y"},
    }


class TestStatutoryNarrativePatch:
    def test_corrects_three_false_patterns_and_logs(self):
        out = _patch_statutory_narrative(_consent_brief_with_false_framing(), "MS")
        blob = " ".join([
            out["executive_snapshot"],
            out["recommendations"][0]["rationale"],
            out["quick_reads"]["authorizer"],
        ])
        # (1) wrong statute section gone, correct section present
        assert "37-28-5" not in blob and "37-28-7" in blob
        # (2) ineligible verdict replaced
        assert "ineligible" not in blob.lower()
        assert "eligible for charter authorization with local board endorsement" in blob
        # (3) D/F escape clause removed
        assert "D/F" not in blob and "rating declines" not in blob
        # audit trail logged with all three pattern types
        patterns = {c["pattern_matched"] for c in out["narrative_corrections"]}
        assert patterns == {"wrong_statute_section", "ineligible_verdict", "df_escape_clause"}
        assert all({"field", "original_phrase", "corrected_phrase"} <= set(c) for c in out["narrative_corrections"])

    def test_does_not_truncate_sentence_mid_citation(self):
        out = _patch_statutory_narrative(_consent_brief_with_false_framing(), "MS")
        # the old fragment bug left an orphan "(§ 37-28-7). Code Ann. …" after the
        # verdict substitution swallowed "under Miss". Guard that exact signature.
        assert "(§ 37-28-7). Code" not in out["executive_snapshot"]
        assert "  " not in out["executive_snapshot"]
        assert out["executive_snapshot"].endswith(".")

    def test_idempotent(self):
        out = _patch_statutory_narrative(_consent_brief_with_false_framing(), "MS")
        out2 = _patch_statutory_narrative(out, "MS")
        assert out2["narrative_corrections"] == []

    def test_clean_consent_brief_is_unchanged(self):
        brief = {
            "statutory_barrier": dict(_CONSENT_BARRIER),
            "executive_snapshot": "Key risk: local board consent required under Miss. Code Ann. § 37-28-7.",
            "recommendations": [], "quick_reads": {},
        }
        snap = brief["executive_snapshot"]
        out = _patch_statutory_narrative(brief, "MS")
        assert out["executive_snapshot"] == snap
        assert out["narrative_corrections"] == []

    def test_does_not_fire_in_non_consent_context(self):
        # A prohibition (not a consent gate) must not be touched by this guard.
        brief = {"statutory_barrier": {"severity": "prohibition", "applies": True},
                 "executive_snapshot": "ineligible under § 37-28-5"}
        out = _patch_statutory_narrative(brief, "MS")
        assert out["executive_snapshot"] == "ineligible under § 37-28-5"
        assert "narrative_corrections" not in out

    def test_no_barrier_is_noop(self):
        brief = {"statutory_barrier": None, "executive_snapshot": "ineligible under § 37-28-5"}
        out = _patch_statutory_narrative(brief, "MS")
        assert "narrative_corrections" not in out


# ── Lock-in tests — pin the verified-correct deterministic behavior ──────────────
# These guard against a real regression of the (currently correct) fresh-run data
# paths the red-team report claimed were broken. They are not behavior changes.

class TestDeterministicLockIn:
    def test_oxford_market_type_is_greenfield(self):
        out = _inject_market_routing(
            {}, {"tier": "MODERATE", "statutory_barrier": dict(_CONSENT_BARRIER)}, "MS", _OXFORD,
        )
        assert out["market_type"] == "greenfield"
        assert out["verdict"] == "ELIGIBLE"

    @pytest.mark.skipif(not (os.path.exists(_LOCAL_XLSX) and os.path.exists(_CCD_PARQUET)),
                        reason="MDE xlsx / CCD parquet not present")
    def test_oxford_mde_rating_is_A(self):
        r = get_mde_district_rating(_OXFORD_LEAID)
        assert r is not None
        assert r["district_accountability_rating"] == "A"

    @pytest.mark.skipif(not os.path.exists("data/raw/national/nces_lea_finance_2024.csv")
                        and not os.path.exists("data/raw/national/nces_lea_finance.parquet"),
                        reason="NCES finance data not present")
    def test_oxford_per_pupil_present(self):
        d = get_district_data(_OXFORD, "MS")
        assert d is not None
        assert (d.get("per_pupil_expenditure") or 0) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Session 11 — widened RC3: prohibition-class framing + driver scope
# ─────────────────────────────────────────────────────────────────────────────

def _consent_brief_with_prohibition_framing():
    """Mirrors the stale ms-oxford/growth artifact's false prohibition framing —
    the phrasing class the original RC3 patterns did NOT catch."""
    return {
        "statutory_barrier": dict(_CONSENT_BARRIER),
        "executive_snapshot": (
            "Oxford is WATCHLIST-tier due to statutory prohibition on charter entry. "
            "Mississippi law restricts charters to C, D, F districts only; Oxford's B "
            "rating makes it ineligible. No charter operator can legally enter under current law."
        ),
        "recommendations": [
            {"action": "Do not pursue charter entry in Oxford under current Mississippi law.",
             "rationale": "Mississippi law restricts charters to C, D, F districts only.",
             "evidence_summary": "MCSAB cannot authorize in B-rated districts; automatic rejection "
                                 "regardless of application quality or local board support.",
             "primary_risk": "x", "confidence": "HIGH", "supporting_fact_ids": []},
        ],
        "quick_reads": {"authorizer": "charter authorization is statutorily prohibited for B-rated districts.",
                        "political": "y", "facilities": "z"},
        "scorecard_summary": {
            "dimension_table": [
                {"dimension": "political_climate",
                 "driver": "Statutory restriction on charter eligibility creates hostile environment."},
                {"dimension": "authorizer_friendliness",
                 "driver": "district ineligibility under state law eliminates authorization pathway entirely."},
            ],
            "top_drivers": [
                {"dimension": "competitive_opportunity",
                 "driver": "Entire K-12 spectrum is closed to charter entry by law."},
            ],
        },
    }


_PROHIBITION_FALSE_CLAIMS = [
    "restricts charter", "c, d, f districts", "statutory prohibition", "cannot authorize",
    "automatic rejection", "legally enter", "statutorily prohibited",
    "closed to charter entry by law", "restriction on charter eligibility",
    "eliminates", "regardless of",
]


def _prose_fields(brief):
    """All LLM-narrative prose fields the patch touches — NOT the audit log."""
    out = [brief.get("executive_snapshot", "")]
    for r in brief.get("recommendations", []):
        out += [r.get(k, "") for k in ("action", "rationale", "evidence_summary", "primary_risk")]
    out += list((brief.get("quick_reads") or {}).values())
    sc = brief.get("scorecard_summary") or {}
    out += [d.get("driver", "") for d in sc.get("dimension_table", [])]
    out += [t.get("driver", "") for t in sc.get("top_drivers", [])]
    return " ".join(out).lower()


class TestStatutoryNarrativeProhibition:
    def test_all_prohibition_claims_removed_from_prose(self):
        out = _patch_statutory_narrative(_consent_brief_with_prohibition_framing(), "MS")
        blob = _prose_fields(out)
        residual = [c for c in _PROHIBITION_FALSE_CLAIMS if c in blob]
        assert residual == [], f"residual false claims: {residual}"

    def test_prohibition_framing_logged(self):
        out = _patch_statutory_narrative(_consent_brief_with_prohibition_framing(), "MS")
        patterns = {c["pattern_matched"] for c in out["narrative_corrections"]}
        assert "prohibition_framing" in patterns
        assert all({"field", "original_phrase", "corrected_phrase"} <= set(c)
                   for c in out["narrative_corrections"])

    def test_driver_narratives_are_in_scope(self):
        out = _patch_statutory_narrative(_consent_brief_with_prohibition_framing(), "MS")
        # the political_climate / authorizer drivers must no longer assert prohibition
        drivers = " ".join(d["driver"] for d in out["scorecard_summary"]["dimension_table"]).lower()
        assert "restriction on charter eligibility" not in drivers
        assert "eliminates authorization pathway" not in drivers
        assert "local school board endorsement" in drivers

    def test_idempotent(self):
        out = _patch_statutory_narrative(_consent_brief_with_prohibition_framing(), "MS")
        out2 = _patch_statutory_narrative(out, "MS")
        assert out2["narrative_corrections"] == []

    def test_no_false_positive_on_clean_consent_prose(self):
        clean = {
            "statutory_barrier": dict(_CONSENT_BARRIER),
            "executive_snapshot": "Key risk: local board consent required under Miss. Code Ann. § 37-28-7.",
            "recommendations": [], "quick_reads": {},
            "scorecard_summary": {"dimension_table": [
                {"dimension": "funding_environment", "driver": "Strong local revenue base."}], "top_drivers": []},
        }
        out = _patch_statutory_narrative(clean, "MS")
        assert out["narrative_corrections"] == []

    def test_prohibition_strings_untouched_in_non_consent_market(self):
        brief = _consent_brief_with_prohibition_framing()
        brief["statutory_barrier"] = {"severity": "prohibition", "applies": True}
        snap = brief["executive_snapshot"]
        out = _patch_statutory_narrative(brief, "MS")
        assert out["executive_snapshot"] == snap
        assert "narrative_corrections" not in out
