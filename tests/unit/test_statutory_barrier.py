"""
tests/unit/test_statutory_barrier.py

Tests for the Session 8 statutory barrier layer:
  - config/charter_law/{state}.yaml loading
  - S5 statutory_barrier_check evaluation (consent_required + prohibition)
  - graceful degradation when the district rating is absent (no fabrication)
  - the prohibition firewall (composite=0) and S6 verdict=PROHIBITED routing
  - statutory_barrier present in both schemas

Crucial domain fact (verified against primary text, Sessions 7 & 8): MS
§ 37-28-7 is a LOCAL-BOARD-CONSENT requirement for A/B/C districts, NOT a
prohibition. These tests assert MS is modeled as consent_required and never
zeroes the score.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.s5_scoring as s5
from pipeline.utils.charter_law import load_charter_law_barriers
from pipeline.s6_synthesis import _inject_market_routing

pytestmark = pytest.mark.unit


def _bundle(rating=None):
    facts = []
    if rating is not None:
        facts.append({
            "fact_key": "district_accountability_rating", "value": rating,
            "datapoint_id": "dp_rating", "dimension": "academic_need",
            "in_main_analysis": True, "confidence": "HIGH",
        })
    return {"facts": facts, "state": "MS", "community_id": "ms-oxford"}


# ── config loading ─────────────────────────────────────────────────────────────

class TestConfigLoad:
    def test_ms_loads_one_consent_barrier(self):
        barriers = load_charter_law_barriers("ms")
        assert len(barriers) == 1
        b = barriers[0]
        assert b["severity"] == "consent_required"   # NOT prohibition
        assert b["statute"] == "Miss. Code Ann. § 37-28-7"
        assert b["type"] == "district_rating_consent_gate"

    def test_nm_loads_empty(self):
        assert load_charter_law_barriers("nm") == []

    def test_missing_state_degrades_to_empty(self):
        assert load_charter_law_barriers("zz") == []

    def test_case_insensitive(self):
        assert len(load_charter_law_barriers("MS")) == 1


# ── consent_required gate (MS § 37-28-7) ────────────────────────────────────────

class TestConsentGate:
    @pytest.mark.parametrize("rating", ["A", "B", "C", "a", "b", "c"])
    def test_abc_requires_consent(self, rating):
        out = s5.statutory_barrier_check("MS", "ms-oxford", _bundle(rating))
        assert out is not None
        assert out["severity"] == "consent_required"
        assert out["applies"] is True
        assert out["evaluable"] is True

    @pytest.mark.parametrize("rating", ["D", "F", "d", "f"])
    def test_df_no_barrier(self, rating):
        # D/F districts need no local-board consent → no barrier surfaced.
        assert s5.statutory_barrier_check("MS", "ms-oxford", _bundle(rating)) is None

    def test_missing_rating_degrades_to_advisory(self):
        out = s5.statutory_barrier_check("MS", "ms-oxford", _bundle(None))
        assert out is not None
        assert out["severity"] == "consent_required"
        assert out["evaluable"] is False
        assert out["applies"] == "unknown"          # not a false positive
        assert out["observed_rating"] is None

    def test_unrecognized_rating_value_is_advisory(self):
        out = s5.statutory_barrier_check("MS", "ms-oxford", _bundle("Z"))
        assert out is not None
        assert out["evaluable"] is False
        assert out["applies"] == "unknown"

    def test_nm_no_barrier(self):
        assert s5.statutory_barrier_check("NM", "nm-questa", _bundle("A")) is None


# ── graceful handling of unrecognized barrier types ─────────────────────────────

class TestUnknownBarrierType:
    def test_unknown_type_is_skipped_not_crashed(self, monkeypatch):
        monkeypatch.setattr(s5, "load_charter_law_barriers", lambda state: [
            {"id": "weird", "severity": "prohibition", "type": "does_not_exist"},
        ])
        # Should log a warning and return None (no evaluator), never raise.
        assert s5.statutory_barrier_check("XX", "xx-1", _bundle("A")) is None


# ── prohibition firewall ─────────────────────────────────────────────────────────

class TestProhibitionFirewall:
    _PROHIB = [{
        "id": "synthetic_prohibition",
        "label": "Synthetic prohibition",
        "statute": "Synthetic § 1-2-3",
        "severity": "prohibition",
        "type": "district_rating_gate",
        "banner_text": "Charter schools cannot be authorized here.",
        "rule": {"ratings_required": ["C", "D", "F"]},  # A/B not allowed → prohibited
    }]

    def test_prohibition_applies_for_disallowed_rating(self, monkeypatch):
        monkeypatch.setattr(s5, "load_charter_law_barriers", lambda state: self._PROHIB)
        out = s5.statutory_barrier_check("XX", "xx-1", _bundle("A"))
        assert out["severity"] == "prohibition"
        assert out["applies"] is True

    def test_prohibition_does_not_apply_for_allowed_rating(self, monkeypatch):
        monkeypatch.setattr(s5, "load_charter_law_barriers", lambda state: self._PROHIB)
        out = s5.statutory_barrier_check("XX", "xx-1", _bundle("D"))
        assert out["applies"] is False

    def test_prohibition_firewalls_composite_to_zero_and_schema_valid(self, monkeypatch, tmp_path):
        """Through s5.run: a prohibition hard-floors composite to 0, pins tier AVOID,
        keeps the scorecard schema-valid, and attaches the barrier."""
        import types
        from pipeline import StageStatus, OperatorPreset, OutputMode, PipelineConfig

        monkeypatch.setattr(s5, "load_charter_law_barriers", lambda state: self._PROHIB)
        monkeypatch.chdir(tmp_path)
        for sub in ("config", "schemas"):
            (tmp_path / sub).symlink_to(os.path.join(_ROOT, sub))

        prev = types.SimpleNamespace(output_data=_bundle("A"))  # rating A → prohibited
        cfg = PipelineConfig(
            state="MS", preset=OperatorPreset.MATURITY_ADJUSTED,
            mode=OutputMode.STRATEGIC_BRIEF, output_format="markdown",
            cache_enabled=False, dry_run=False, force_refresh=False,
            communities=["ms-oxford"], depth="standard",
        )
        result = s5.run(community_id="ms-oxford", state="MS", config=cfg, previous_result=prev)
        assert result.status == StageStatus.SUCCESS, result.errors
        sc = result.output_data
        assert sc["composite_score"] == 0
        assert sc["tier"] == "AVOID"            # schema has no PROHIBITED tier
        assert sc["statutory_barrier"]["severity"] == "prohibition"
        assert len(sc["dimensions"]) == 10      # dimensions kept for schema validity


# ── S6 market routing ────────────────────────────────────────────────────────────

class TestMarketRouting:
    def test_prohibition_sets_verdict_prohibited(self):
        scorecard = {"tier": "AVOID", "statutory_barrier": {
            "id": "x", "severity": "prohibition", "applies": True, "banner_text": "no",
        }}
        out = _inject_market_routing({}, scorecard, "XX", "xx-1")
        assert out["verdict"] == "PROHIBITED"
        assert out["market_type"] == "statutory_ineligible"
        assert out["statutory_barrier"]["severity"] == "prohibition"

    def test_consent_required_keeps_normal_verdict(self):
        # consent_required is advisory: verdict/market_type follow normal routing,
        # but the barrier is still carried onto the brief for the banner.
        scorecard = {"tier": "MODERATE", "statutory_barrier": {
            "id": "ms_district_rating_consent", "severity": "consent_required",
            "applies": "unknown", "banner_text": "consent",
        }}
        out = _inject_market_routing({}, scorecard, "MS", "ms-oxford")
        assert out["verdict"] == "ELIGIBLE"        # not PROHIBITED
        assert out["statutory_barrier"]["severity"] == "consent_required"

    def test_no_barrier_is_none_on_brief(self):
        out = _inject_market_routing({}, {"tier": "STRONG", "statutory_barrier": None}, "NM", "nm-questa")
        assert out["statutory_barrier"] is None
        assert out["verdict"] == "ELIGIBLE"


# ── schema presence ──────────────────────────────────────────────────────────────

class TestSchema:
    def test_statutory_barrier_in_both_schemas(self):
        for name in ("scorecard", "brief"):
            with open(os.path.join(_ROOT, "schemas", f"{name}.schema.json")) as f:
                schema = json.load(f)
            assert "statutory_barrier" in schema["properties"], f"{name} schema missing statutory_barrier"

    def test_brief_verdict_enum_includes_prohibited(self):
        with open(os.path.join(_ROOT, "schemas", "brief.schema.json")) as f:
            schema = json.load(f)
        assert "PROHIBITED" in schema["properties"]["verdict"]["enum"]
        assert "statutory_ineligible" in schema["properties"]["market_type"]["enum"]
