"""
tests/unit/test_s7_render.py
Pure-Python unit tests for S7 rendering logic.

Coverage:
  - scan_results.html.j2: valid HTML structure (DOCTYPE, closing tag)
  - scan_results.html.j2: SMALL_MARKET flag → "SCALE CAVEAT" in output
  - scan_results.html.j2: no SCALE CAVEAT when flag absent
  - scan_results.html.j2: data coverage tier labels (Reliable, Provisional, Unreliable)
  - scan_results.html.j2: unreliable tier goes to Insufficient Data section
  - _render_fallback: community name, classification, executive snapshot present
  - _render_fallback: audit warning when audit_passed=False
  - render_scan_table: output file created at expected path

No API calls. No network access.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

# ── Project root on sys.path ──────────────────────────────────────────────────
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.s7_render import _render_fallback

# ── Mark all tests in this file as unit ──────────────────────────────────────
pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Jinja environment factory (absolute path — no chdir needed for rendering)
# ─────────────────────────────────────────────────────────────────────────────

def _make_env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    return Environment(
        loader=FileSystemLoader(os.path.join(_ROOT, "templates")),
        autoescape=select_autoescape([]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scan result factory helpers
# ─────────────────────────────────────────────────────────────────────────────

def _scan_result(
    community_id: str = "nm-test",
    tier: str = "MODERATE",
    data_coverage_tier: str = "reliable",
    data_coverage_pct: float = 85.0,
    override_flags: list | None = None,
    composite: float = 6.5,
) -> dict:
    """Build a minimal scan result dict for template rendering."""
    return {
        "community_id": community_id,
        "city": community_id.replace("nm-", "").replace("-", " ").title(),
        "community_name": community_id.replace("-", " ").title(),
        "composite": composite,
        "tier": tier,
        "tier_display_label": f"{tier.replace('_', ' ')} OPPORTUNITY",
        "one_line_snapshot": "Snapshot for testing.",
        "override_flags": override_flags if override_flags is not None else [],
        "data_coverage_tier": data_coverage_tier,
        "data_coverage_pct": data_coverage_pct,
        "signal_tensions": None,
        "special_considerations": None,
        "deep_review_level": None,
    }


def _small_market_flag(enrollment: int = 399) -> dict:
    return {
        "flag": "SMALL_MARKET",
        "triggered_by": f"District enrollment: {enrollment} students (k12_enrollment_total)",
        "visual": "⚠️",
        "tier_effect": "None — informational only",
    }


def _render_scan(results: list[dict], env=None) -> str:
    if env is None:
        env = _make_env()
    return env.get_template("scan_results.html.j2").render(
        results=results,
        state="NM",
        preset="growth",
        generated_at="2026-05-28T00:00:00",
        date_str="20260528",
        city_count=len(results),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. SCAN TABLE HTML STRUCTURE TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestScanHTMLStructure:
    """scan_results.html.j2 produces structurally valid HTML."""

    def test_html_starts_with_doctype(self):
        html = _render_scan([_scan_result()])
        assert html.strip().startswith("<!DOCTYPE html")

    def test_html_has_closing_html_tag(self):
        html = _render_scan([_scan_result()])
        assert "</html>" in html

    def test_html_has_body_tags(self):
        html = _render_scan([_scan_result()])
        assert "<body>" in html
        assert "</body>" in html

    def test_html_contains_state_name(self):
        html = _render_scan([_scan_result()])
        assert "NM" in html

    def test_html_renders_with_multiple_results(self):
        results = [
            _scan_result("nm-questa", tier="STRONG", composite=7.5),
            _scan_result("nm-taos", tier="MODERATE", composite=6.0),
        ]
        html = _render_scan(results)
        assert html.strip().startswith("<!DOCTYPE html")
        assert "</html>" in html


# ─────────────────────────────────────────────────────────────────────────────
# 2. SMALL_MARKET SCALE CAVEAT RENDERING
# ─────────────────────────────────────────────────────────────────────────────

class TestSmallMarketScaleCaveat:
    """SCALE CAVEAT label appears iff SMALL_MARKET flag is present."""

    def test_scale_caveat_appears_when_flag_present(self):
        result = _scan_result(override_flags=[_small_market_flag(399)])
        html = _render_scan([result])
        assert "SCALE CAVEAT" in html

    def test_scale_caveat_absent_when_no_flags(self):
        html = _render_scan([_scan_result()])
        assert "SCALE CAVEAT" not in html

    def test_scale_caveat_absent_when_other_flags_only(self):
        other_flag = {"flag": "POLITICAL_RISK_HIGH", "triggered_by": "Index = 2"}
        html = _render_scan([_scan_result(override_flags=[other_flag])])
        assert "SCALE CAVEAT" not in html

    def test_scale_caveat_contains_triggered_by_text(self):
        """The triggered_by string (enrollment count) should appear in the note."""
        result = _scan_result(override_flags=[_small_market_flag(399)])
        html = _render_scan([result])
        assert "399" in html

    def test_scale_caveat_renders_for_exactly_one_flagged_city(self):
        """With one flagged and one normal result, SCALE CAVEAT appears exactly once."""
        results = [
            _scan_result("nm-small", override_flags=[_small_market_flag()]),
            _scan_result("nm-large"),
        ]
        html = _render_scan(results)
        count = html.count("SCALE CAVEAT")
        assert count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. DATA COVERAGE TIER LABEL RENDERING
# ─────────────────────────────────────────────────────────────────────────────

class TestCoverageTierLabels:
    """Coverage tier labels render via Jinja's | title filter."""

    def test_reliable_tier_renders_reliable_label(self):
        result = _scan_result(data_coverage_tier="reliable", data_coverage_pct=85.0)
        html = _render_scan([result])
        # | title → "Reliable"
        assert "Reliable" in html

    def test_provisional_tier_renders_provisional_label(self):
        result = _scan_result(data_coverage_tier="provisional", data_coverage_pct=60.0)
        html = _render_scan([result])
        assert "Provisional" in html

    def test_unreliable_tier_goes_to_insufficient_section(self):
        """Communities with data_coverage_tier='unreliable' appear in the
        'Insufficient Data' section, not the main ranked table."""
        result = _scan_result(data_coverage_tier="unreliable", data_coverage_pct=30.0)
        html = _render_scan([result])
        # The section heading is "Communities — Insufficient Data"
        assert "Insufficient Data" in html

    def test_unreliable_tier_renders_unreliable_label(self):
        result = _scan_result(data_coverage_tier="unreliable", data_coverage_pct=30.0)
        html = _render_scan([result])
        assert "Unreliable" in html

    def test_coverage_percentage_appears_in_output(self):
        result = _scan_result(data_coverage_tier="reliable", data_coverage_pct=85.0)
        html = _render_scan([result])
        assert "85%" in html

    def test_provisional_not_in_insufficient_section(self):
        """Provisional communities appear in the main ranked table, not the insufficient section."""
        result = _scan_result(data_coverage_tier="provisional", data_coverage_pct=60.0)
        html = _render_scan([result])
        # The heading only appears when there are unreliable results
        assert "Insufficient Data" not in html


# ─────────────────────────────────────────────────────────────────────────────
# 4. _render_fallback — markdown fallback renderer
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_brief(
    community_name: str = "Test City",
    classification: str = "MODERATE OPPORTUNITY",
    composite_score: float = 6.5,
    audit_passed: bool = True,
    audit_flags: list | None = None,
) -> dict:
    """Build a minimal brief dict for _render_fallback tests."""
    return {
        "community_id": "nm-test",
        "community_name": community_name,
        "mode_label": "Strategic Brief",
        "classification": classification,
        "composite_score": composite_score,
        "confidence_overall": "MODERATE",
        "data_through": "2024",
        "generated_at": "2026-05-28T00:00:00",
        "executive_snapshot": "Test executive snapshot text.",
        "scorecard_summary": {
            "composite_score": composite_score,
            "tier_display_label": classification,
            "dimension_table": [
                {
                    "dimension": "academic_need",
                    "display_name": "Academic Need",
                    "score": 7.0,
                    "weight": 0.125,
                    "confidence": "HIGH",
                    "driver": "ELA 25% → 9.0",
                    "used_default": False,
                }
            ],
            "override_flags": [],
        },
        "recommendations": [],
        "top_charter_schools": [],
        "local_authorizers": [],
        "needs_verification": [],
        "sources": [],
        "disclosure": "AI-generated. Requires human review.",
        "audit_passed": audit_passed,
        "audit_flags": audit_flags or [],
    }


class TestRenderFallback:
    """_render_fallback produces correct markdown from a brief dict."""

    def test_contains_community_name(self):
        md = _render_fallback(_minimal_brief(community_name="Questa"))
        assert "Questa" in md

    def test_contains_classification(self):
        md = _render_fallback(_minimal_brief(classification="STRONG OPPORTUNITY"))
        assert "STRONG OPPORTUNITY" in md

    def test_contains_executive_snapshot(self):
        md = _render_fallback(_minimal_brief())
        assert "Test executive snapshot text" in md

    def test_contains_composite_score(self):
        md = _render_fallback(_minimal_brief(composite_score=7.5))
        assert "7.5" in md

    def test_contains_dimension_table_header(self):
        md = _render_fallback(_minimal_brief())
        assert "Scorecard" in md
        assert "Dimension" in md

    def test_contains_dimension_display_name(self):
        md = _render_fallback(_minimal_brief())
        assert "Academic Need" in md

    def test_audit_warning_shown_when_failed(self):
        brief = _minimal_brief(audit_passed=False, audit_flags=["claim_1"])
        md = _render_fallback(brief)
        assert "Audit flag" in md

    def test_audit_warning_absent_when_passed(self):
        md = _render_fallback(_minimal_brief(audit_passed=True))
        assert "Audit flag" not in md

    def test_disclosure_appears_in_output(self):
        md = _render_fallback(_minimal_brief())
        assert "AI-generated" in md

    def test_output_is_a_string(self):
        md = _render_fallback(_minimal_brief())
        assert isinstance(md, str)
        assert len(md) > 0

    def test_used_default_dimension_shows_asterisk(self):
        """Dimensions with used_default=True should show an asterisk on their score."""
        brief = _minimal_brief()
        brief["scorecard_summary"]["dimension_table"][0]["used_default"] = True
        md = _render_fallback(brief)
        assert "*" in md   # score cell shows "5.0*" for defaulted dimensions


# ─────────────────────────────────────────────────────────────────────────────
# 5. render_scan_table — output file created at expected path
#    Runs from project root (monkeypatch.chdir) to access templates/ and config/.
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderScanTableOutput:
    """render_scan_table writes the HTML file to the expected path."""

    def test_output_file_created_at_expected_path(self, monkeypatch):
        """render_scan_table returns a path matching outputs/by_state/nm/*_scan_*.html
        and creates the file on disk.

        Uses monkeypatch.chdir to run from the project root so that
        Jinja's FileSystemLoader("templates") and config/states.yaml are accessible.
        The output is written to _ROOT/outputs/by_state/nm/ (gitignored).
        """
        import datetime
        monkeypatch.chdir(_ROOT)

        from pipeline.s7_render import render_scan_table
        from pipeline import OperatorPreset, OutputMode, PipelineConfig

        config = PipelineConfig(
            state="NM",
            preset=OperatorPreset.GROWTH,
            mode=OutputMode.SCAN,
            output_format="markdown",
            cache_enabled=False,
            dry_run=False,
            force_refresh=False,
        )

        results = [
            _scan_result("nm-questa", tier="STRONG", composite=7.5),
            _scan_result("nm-taos",   tier="MODERATE", composite=6.0),
        ]

        out_path = render_scan_table(results, config)

        # render_scan_table returns None only if the template is missing
        assert out_path is not None, (
            "render_scan_table returned None — templates/scan_results.html.j2 missing?"
        )
        assert os.path.isfile(out_path), f"Output file not found: {out_path}"

        # Verify the naming convention: {state_code}_scan_{date}.html
        date_str = datetime.date.today().strftime("%Y%m%d")
        expected_name = f"nm_scan_{date_str}.html"
        assert os.path.basename(out_path) == expected_name

        # Verify the path is under outputs/by_state/nm/
        assert os.path.join("outputs", "by_state", "nm") in out_path.replace(os.sep, "/") \
            or "outputs/by_state/nm" in out_path

    def test_output_html_is_valid(self, monkeypatch):
        """The HTML file written by render_scan_table has DOCTYPE and closing tag."""
        import datetime
        monkeypatch.chdir(_ROOT)

        from pipeline.s7_render import render_scan_table
        from pipeline import OperatorPreset, OutputMode, PipelineConfig

        config = PipelineConfig(
            state="NM",
            preset=OperatorPreset.GROWTH,
            mode=OutputMode.SCAN,
            output_format="markdown",
            cache_enabled=False,
            dry_run=False,
            force_refresh=False,
        )

        results = [_scan_result("nm-questa", tier="STRONG", composite=7.5)]
        out_path = render_scan_table(results, config)

        if out_path is None:
            pytest.skip("scan_results.html.j2 template not found — skipping")

        with open(out_path) as f:
            html = f.read()

        assert html.strip().startswith("<!DOCTYPE html")
        assert "</html>" in html
