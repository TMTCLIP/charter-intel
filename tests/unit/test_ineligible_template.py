"""
tests/unit/test_ineligible_template.py

Tests for templates/ineligible_brief.html.j2 rendering.
Verifies structural completeness (section IDs, verdict, suppressed recs, score bars).
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.unit


def _make_env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    return Environment(
        loader=FileSystemLoader(os.path.join(_ROOT, "templates")),
        autoescape=select_autoescape([]),
    )


def _minimal_ineligible_brief(
    composite_score: float = 2.8,
    ineligibility_reason: str = "scored below minimum threshold",
) -> dict:
    """Minimal brief dict for ineligible template rendering."""
    return {
        "community_id": "ms-oxford",
        "community_name": "Oxford",
        "state": "MS",
        "preset": "growth",
        "mode": 2,
        "classification": "AVOID ENTRY",
        "composite_score": composite_score,
        "confidence_overall": "LOW",
        "data_through": "2024-12-31",
        "generated_at": "2026-06-07T00:00:00",
        "verdict": "INELIGIBLE",
        "market_type": "established",
        "executive_snapshot": "Oxford is a small college town in Mississippi with limited charter landscape.",
        "scorecard_summary": {
            "composite_score": composite_score,
            "tier_display_label": "AVOID ENTRY",
            "override_flags": [],
            "top_drivers": [
                {"dimension": "political_climate", "score": 2.1, "driver": "Hostile legislative environment."},
                {"dimension": "market_size", "score": 2.5, "driver": "Small enrollment base."},
            ],
            "dimension_table": [
                {"dimension": "political_climate", "display_name": "Political Climate",
                 "score": 2.1, "weight": 0.15, "confidence": "MODERATE",
                 "driver": "Hostile legislative environment.", "used_default": False},
                {"dimension": "market_size", "display_name": "Market Size",
                 "score": 2.5, "weight": 0.10, "confidence": "HIGH",
                 "driver": "Small enrollment base.", "used_default": False},
                {"dimension": "academic_need", "display_name": "Academic Need",
                 "score": 5.0, "weight": 0.0, "confidence": "LOW",
                 "driver": "", "used_default": True},
            ],
        },
        "recommendation_confidence_gate": {
            "gated": True,
            "data_coverage_tier": "reliable",
            "confidence_overall": "LOW",
            "reason": "Ineligible market — recommendations suppressed.",
        },
        "recommendations": [],
        "top_charter_schools": [],
        "local_authorizers": [],
        "needs_verification": [
            {
                "claim": "Political climate index",
                "reason": "Limited data sources for MS political climate.",
                "source_class_attempted": "WEB_SEARCH",
                "resolution_path": "Cross-check with MS PEC records.",
                "impact_if_wrong": "HIGH",
            }
        ],
        "sources": [
            {"ref_num": 1, "source_class": "FEDERAL_DATA", "title": "NCES CCD",
             "url": None, "date": "2024-01-01", "confidence": "HIGH"},
        ],
        "audit_passed": True,
        "audit_flags": [],
        "disclosure": "AI-generated. Requires human review.",
        "data_coverage_tier": "reliable",
    }


@pytest.fixture
def env():
    return _make_env()


@pytest.fixture
def html(env):
    brief = _minimal_ineligible_brief()
    return env.get_template("ineligible_brief.html.j2").render(brief=brief)


# ── Structure ──────────────────────────────────────────────────────────────────

class TestIneligibleTemplateStructure:
    def test_doctype_present(self, html):
        assert html.strip().startswith("<!DOCTYPE html")

    def test_closing_html_tag(self, html):
        assert "</html>" in html

    def test_has_body_tags(self, html):
        assert "<body>" in html
        assert "</body>" in html

    def test_all_section_ids_present(self, html):
        for section_id in ["snapshot", "scorecard", "drivers", "flags",
                           "reevaluation", "landscape", "authorizers",
                           "recommendations", "verification", "sources"]:
            assert f'id="{section_id}"' in html, (
                f"Missing section id='{section_id}' in ineligible template"
            )

    def test_sidebar_nav_present(self, html):
        assert 'class="sb-nav"' in html

    def test_disclosure_footer_present(self, html):
        assert "disclosure-footer" in html


# ── Ineligibility verdict ──────────────────────────────────────────────────────

class TestIneligibleVerdict:
    def test_verdict_ineligible_shown(self, html):
        assert "VERDICT: INELIGIBLE" in html

    def test_ineligible_badge_shown(self, html):
        assert "ineligible-badge" in html

    def test_ineligibility_reason_shown(self, html):
        """The composite score must appear in the verdict banner."""
        assert "2.8" in html

    def test_low_dimension_names_in_banner(self, html):
        """Dimensions scoring below 4.0 must be named in the verdict banner."""
        assert "Political Climate" in html or "political_climate" in html

    def test_avoid_entry_tier_shown(self, html):
        assert "AVOID ENTRY" in html


# ── Score bars ─────────────────────────────────────────────────────────────────

class TestIneligibleScoreBars:
    def test_scorecard_section_present(self, html):
        assert 'id="scorecard"' in html

    def test_bar_row_class_present(self, html):
        assert 'class="bar-row"' in html

    def test_fill_red_class_for_low_scorers(self, html):
        """Low-scoring dimensions must use fill-red bar class."""
        assert "fill-red" in html

    def test_awaiting_data_for_academic_need(self, html):
        """academic_need with used_default=True must render Awaiting Data state."""
        assert "Awaiting Data" in html


# ── Charter landscape — ineligible notice ─────────────────────────────────────

class TestIneligibleCharterLandscape:
    def test_ineligible_notice_shown(self, html):
        assert "ineligible-notice" in html

    def test_no_school_cards(self, html):
        """School expansion opportunity cards must not appear for ineligible markets."""
        assert "school-card" not in html
        assert "replication-candidate" not in html.lower()

    def test_ineligible_market_language(self, html):
        assert "ineligible for charter development" in html.lower() or \
               "ineligible" in html.lower()


# ── Recommendations suppressed ────────────────────────────────────────────────

class TestIneligibleRecommendationsSuppressed:
    def test_recommendations_section_present(self, html):
        assert 'id="recommendations"' in html

    def test_no_recommendations_language(self, html):
        assert "No recommendations generated" in html

    def test_rec_suppressed_class_present(self, html):
        assert "rec-suppressed" in html


# ── Re-evaluation conditions ───────────────────────────────────────────────────

class TestReEvaluationConditions:
    def test_reevaluation_section_present(self, html):
        """Low-scoring dimensions must generate a Re-evaluation section."""
        assert 'id="reevaluation"' in html

    def test_condition_items_present(self, html):
        assert "condition-item" in html

    def test_low_dim_names_in_conditions(self, html):
        assert "Political Climate" in html
        assert "Market Size" in html


# ── Needs verification ─────────────────────────────────────────────────────────

class TestIneligibleNeedsVerification:
    def test_nv_section_present(self, html):
        assert 'id="verification"' in html

    def test_nv_claim_shown(self, html):
        assert "Political climate index" in html


# ── Sources ────────────────────────────────────────────────────────────────────

class TestIneligibleSources:
    def test_sources_section_present(self, html):
        assert 'id="sources"' in html

    def test_nces_ccd_source_shown(self, html):
        assert "NCES CCD" in html


# ── Community name and state ───────────────────────────────────────────────────

class TestIneligibleCommunityIdentity:
    def test_community_name_in_output(self, html):
        assert "Oxford" in html

    def test_state_in_output(self, html):
        assert "MS" in html
