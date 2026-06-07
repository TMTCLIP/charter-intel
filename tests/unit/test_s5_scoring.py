"""
tests/unit/test_s5_scoring.py
Pure-Python unit tests for S5 scoring logic.

Coverage:
  - Every operator preset's dimension weights sum to 1.0
  - maturity_adjusted specific weights (charter_saturation, replication_feasibility)
  - SMALL_MARKET flag: fires below 1500, does not fire at or above
  - SMALL_MARKET: enrollment_by_year fallback when k12_enrollment_total absent
  - SMALL_MARKET: integer and string year keys both resolve (KeyError bug fix)
  - Score output [0.0, 10.0] for all dimensions with no data
  - academic_need inverse scoring
  - _score_population_trends threshold table
  - compute_tier: raw tiers and override flag caps
  - nm-cleveland in excluded_communities (states.yaml)
  - Confidence.minimum() enum helper

No API calls. No network access.
"""
from __future__ import annotations

import os
import sys

import pytest
import yaml

# ── Project root on sys.path ──────────────────────────────────────────────────
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.s5_scoring import (
    _build_fact_index,
    _score_population_trends,
    check_override_flags,
    compute_tier,
    run as s5_run,
    score_dimension,
)
from pipeline import Confidence, StageStatus

# ── Mark all tests in this file as unit ──────────────────────────────────────
pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# YAML config helpers — absolute paths; no chdir required
# ─────────────────────────────────────────────────────────────────────────────

def _load_presets() -> dict:
    with open(os.path.join(_ROOT, "config", "scoring_presets.yaml")) as f:
        return yaml.safe_load(f)


def _load_weights() -> dict:
    with open(os.path.join(_ROOT, "config", "scoring_weights.yaml")) as f:
        return yaml.safe_load(f)


def _load_states() -> dict:
    with open(os.path.join(_ROOT, "config", "states.yaml")) as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Bundle factory helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bundle(fact_pairs: dict) -> dict:
    """Build a minimal verified bundle from {fact_key: value} pairs.

    Facts are tagged dimension='test' so they won't be picked up by
    extract_facts_for_dimension() (which filters by dimension name).
    Use this for override-flag tests where dimension filtering is irrelevant.
    """
    return {
        "facts": [
            {
                "fact_key": k,
                "value": v,
                "datapoint_id": f"dp_{i}",
                "dimension": "test",
                "in_main_analysis": True,
                "confidence": "HIGH",
            }
            for i, (k, v) in enumerate(fact_pairs.items())
        ]
    }


def _dim_bundle(dim_name: str, fact_pairs: dict) -> dict:
    """Build a verified bundle scoped to a specific dimension.

    Facts are tagged dimension=dim_name so extract_facts_for_dimension()
    finds them correctly inside score_dimension().
    """
    return {
        "facts": [
            {
                "fact_key": k,
                "value": v,
                "datapoint_id": f"dp_{i}",
                "dimension": dim_name,
                "in_main_analysis": True,
                "confidence": "HIGH",
            }
            for i, (k, v) in enumerate(fact_pairs.items())
        ]
    }


def _pop_fact(pct_change: float) -> list[dict]:
    """Build a single pct_change_total fact list for _score_population_trends."""
    return [
        {
            "fact_key": "pct_change_total",
            "value": pct_change,
            "datapoint_id": "dp_0",
            "dimension": "population_trends",
            "in_main_analysis": True,
            "confidence": "HIGH",
        }
    ]


def _flag_names(bundle: dict, dimensions_out: dict | None = None) -> list[str]:
    return [f["flag"] for f in check_override_flags(bundle, dimensions_out or {})]


# ─────────────────────────────────────────────────────────────────────────────
# 1. PRESET WEIGHT SUM TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestPresetWeightSum:
    """Every operator preset must have dimension weights summing to exactly 1.0."""

    def test_all_operator_presets_sum_to_one(self):
        presets = _load_presets()
        operator_presets = {
            name: p for name, p in presets.items()
            if isinstance(p, dict) and "dimensions" in p
        }
        assert operator_presets, "No operator presets found in scoring_presets.yaml"
        for preset_name, preset_def in operator_presets.items():
            total = sum(preset_def["dimensions"].values())
            assert total == pytest.approx(1.0, abs=1e-9), (
                f"Preset '{preset_name}' weights sum to {total:.10f}, expected 1.0"
            )

    def test_maturity_adjusted_charter_saturation_weight(self):
        dims = _load_presets()["maturity_adjusted"]["dimensions"]
        assert dims["charter_saturation"] == pytest.approx(0.0625)

    def test_maturity_adjusted_replication_feasibility_weight(self):
        dims = _load_presets()["maturity_adjusted"]["dimensions"]
        assert dims["replication_feasibility"] == pytest.approx(0.0625)

    def test_maturity_adjusted_facilities_excluded(self):
        """facilities_feasibility is intentionally zeroed in maturity_adjusted."""
        dims = _load_presets()["maturity_adjusted"]["dimensions"]
        assert dims["facilities_feasibility"] == pytest.approx(0.0)

    def test_all_ten_dimensions_present_in_each_preset(self):
        """Every operator preset must define weights for all 10 scoring dimensions."""
        weights_cfg = _load_weights()
        canonical_dims = set(weights_cfg["dimensions"].keys())
        presets = _load_presets()
        operator_presets = {
            name: p for name, p in presets.items()
            if isinstance(p, dict) and "dimensions" in p
        }
        for preset_name, preset_def in operator_presets.items():
            defined_dims = set(preset_def["dimensions"].keys())
            assert defined_dims == canonical_dims, (
                f"Preset '{preset_name}' missing dimensions: "
                f"{canonical_dims - defined_dims}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. SMALL_MARKET OVERRIDE FLAG TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestSmallMarketFlag:
    """SMALL_MARKET fires when district enrollment < 1500; not at or above."""

    # ── k12_enrollment_total ─────────────────────────────────────────────────

    def test_fires_well_below_threshold(self):
        assert "SMALL_MARKET" in _flag_names(_bundle({"k12_enrollment_total": 1000}))

    def test_fires_at_1499(self):
        assert "SMALL_MARKET" in _flag_names(_bundle({"k12_enrollment_total": 1499}))

    def test_fires_at_zero(self):
        assert "SMALL_MARKET" in _flag_names(_bundle({"k12_enrollment_total": 0}))

    def test_does_not_fire_at_1500(self):
        """1500 is the threshold; the flag fires for < 1500, not ≤ 1500."""
        assert "SMALL_MARKET" not in _flag_names(_bundle({"k12_enrollment_total": 1500}))

    def test_does_not_fire_above_threshold(self):
        assert "SMALL_MARKET" not in _flag_names(_bundle({"k12_enrollment_total": 5000}))

    # ── enrollment_by_year fallback ──────────────────────────────────────────

    def test_fallback_fires_when_k12_absent_and_small(self):
        """When k12_enrollment_total is absent, check enrollment_by_year."""
        bundle = _bundle({"enrollment_by_year": {"2023": 400}})
        assert "SMALL_MARKET" in _flag_names(bundle)

    def test_fallback_no_flag_when_by_year_large(self):
        bundle = _bundle({"enrollment_by_year": {"2023": 2000}})
        assert "SMALL_MARKET" not in _flag_names(bundle)

    # ── Year-key normalisation (the KeyError bug fix at commit b7372af) ───────

    def test_int_year_keys_resolve_without_error(self):
        """enrollment_by_year with integer keys must not raise KeyError."""
        bundle = _bundle({"enrollment_by_year": {2023: 400}})
        # Should not raise; should also trigger SMALL_MARKET
        assert "SMALL_MARKET" in _flag_names(bundle)

    def test_str_year_keys_resolve_without_error(self):
        """enrollment_by_year with string keys works correctly."""
        bundle = _bundle({"enrollment_by_year": {"2023": 400}})
        assert "SMALL_MARKET" in _flag_names(bundle)

    def test_latest_year_is_selected_from_mixed_keys(self):
        """With mixed int/str keys the latest year's value is used.

        2022 → 400 students (would flag), "2023" → 2000 students (no flag).
        Latest year = 2023; expected outcome: no flag.
        """
        bundle = _bundle({"enrollment_by_year": {2022: 400, "2023": 2000}})
        assert "SMALL_MARKET" not in _flag_names(bundle)

    def test_k12_enrollment_total_takes_priority_over_by_year(self):
        """k12_enrollment_total wins when present; enrollment_by_year is skipped."""
        # k12=2000 (above threshold — no flag) even though by_year shows 400 (would flag)
        bundle = _bundle({
            "k12_enrollment_total": 2000,
            "enrollment_by_year": {"2023": 400},
        })
        assert "SMALL_MARKET" not in _flag_names(bundle)

    def test_triggered_by_mentions_enrollment_count(self):
        """The SMALL_MARKET flag's triggered_by field mentions the enrollment count."""
        bundle = _bundle({"k12_enrollment_total": 399})
        flags = check_override_flags(bundle, {})
        sm_flags = [f for f in flags if f["flag"] == "SMALL_MARKET"]
        assert len(sm_flags) == 1
        assert "399" in sm_flags[0]["triggered_by"]

    def test_flag_tier_effect_is_informational(self):
        """SMALL_MARKET is informational only — should not cap the tier."""
        bundle = _bundle({"k12_enrollment_total": 399})
        flags = check_override_flags(bundle, {})
        sm_flags = [f for f in flags if f["flag"] == "SMALL_MARKET"]
        assert len(sm_flags) == 1
        tier_effect = sm_flags[0].get("tier_effect", "")
        assert "None" in tier_effect or "informational" in tier_effect.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 3. SCORE RANGE TESTS — every dimension stays in [0.0, 10.0]
# ─────────────────────────────────────────────────────────────────────────────

class TestScoreRange:
    """Dimension scores must lie in [0.0, 10.0] regardless of input data."""

    def test_all_dimensions_default_within_range(self):
        """When no facts exist, every dimension must default within [0, 10]."""
        weights_cfg = _load_weights()
        preset_weights = _load_presets()["maturity_adjusted"]["dimensions"]
        empty_bundle = {"facts": []}
        for dim_name, dim_def in weights_cfg["dimensions"].items():
            weight = preset_weights.get(dim_name, 0.10)
            result = score_dimension(dim_name, dim_def, empty_bundle, weight)
            score = result["score"]
            assert 0.0 <= score <= 10.0, (
                f"Dimension '{dim_name}' default score {score} outside [0, 10]"
            )

    def test_academic_need_high_proficiency_scores_2(self):
        """ELA proficiency 90% → inverse direction → lowest urgency → score 2."""
        weights_cfg = _load_weights()
        bundle = _dim_bundle("academic_need", {"district_proficiency_ela_pct": 90})
        result = score_dimension(
            "academic_need",
            weights_cfg["dimensions"]["academic_need"],
            bundle,
            0.10,
        )
        # Primary score: 90 > 85 → fallback threshold {score: 2} → 2.0
        # Secondary facts absent; apply_secondary_adjustments returns 2.0 unchanged
        assert result["score"] == pytest.approx(2.0)
        assert result["used_default"] is False
        assert 0.0 <= result["score"] <= 10.0

    def test_academic_need_low_proficiency_scores_high(self):
        """ELA proficiency 10% → highest urgency → primary score 10."""
        weights_cfg = _load_weights()
        bundle = _dim_bundle("academic_need", {"district_proficiency_ela_pct": 10})
        result = score_dimension(
            "academic_need",
            weights_cfg["dimensions"]["academic_need"],
            bundle,
            0.10,
        )
        assert result["score"] >= 9.0
        assert 0.0 <= result["score"] <= 10.0
        assert result["used_default"] is False

    def test_charter_saturation_no_data_is_defaulted(self):
        """charter_saturation missing_data_behavior=degrade_confidence → used_default=True."""
        weights_cfg = _load_weights()
        result = score_dimension(
            "charter_saturation",
            weights_cfg["dimensions"]["charter_saturation"],
            {"facts": []},
            0.14,
        )
        assert result["used_default"] is True
        assert 0.0 <= result["score"] <= 10.0

    def test_weighted_contribution_equals_score_times_weight(self):
        """weighted_contribution = round(score * weight, 4) for every dimension."""
        weights_cfg = _load_weights()
        preset_weights = _load_presets()["maturity_adjusted"]["dimensions"]
        empty_bundle = {"facts": []}
        for dim_name, dim_def in weights_cfg["dimensions"].items():
            weight = preset_weights.get(dim_name, 0.10)
            result = score_dimension(dim_name, dim_def, empty_bundle, weight)
            expected_contribution = round(result["score"] * weight, 4)
            assert result["weighted_contribution"] == pytest.approx(
                expected_contribution, abs=1e-6
            ), f"Dimension '{dim_name}' weighted_contribution mismatch"


# ─────────────────────────────────────────────────────────────────────────────
# 3b. CHARTER SATURATION RELIABILITY GATE (charter-adjacent schools)
# ─────────────────────────────────────────────────────────────────────────────

class TestCharterSaturationAdjacentGate:
    """The Jackson, MS case: formal roster empty → optimistic 'wide-open' score,
    but charter-adjacent schools present → the reading is unverified, so it must
    be clamped to neutral and excluded from the composite. Genuinely-empty markets
    (no charter-adjacent schools) are unaffected. No weight changes anywhere.
    """

    def _dim_def(self):
        return _load_weights()["dimensions"]["charter_saturation"]

    def _bundle(self, formal_count, adjacent_count=None):
        facts = [{
            "fact_key": "num_charter_schools", "value": formal_count,
            "datapoint_id": "dp_formal", "dimension": "charter_saturation",
            "in_main_analysis": True, "confidence": "HIGH", "source_class": "PED_DATA",
        }]
        if adjacent_count is not None:
            facts.append({
                "fact_key": "num_charter_adjacent_schools", "value": adjacent_count,
                "datapoint_id": "dp_adj", "dimension": "charter_saturation",
                "in_main_analysis": False,  # non-scoring by design
                "confidence": "LOW", "source_class": "WEB_SEARCH",
            })
        return {"facts": facts, "state": "MS", "community_id": "ms-jackson"}

    def test_clamps_when_empty_roster_but_adjacent_present(self):
        r = score_dimension("charter_saturation", self._dim_def(), self._bundle(0, 4), 0.0625)
        assert r["used_default"] is True
        assert r["score"] == pytest.approx(5.0)
        assert "unverified" in r["primary_driver"].lower()

    def test_no_clamp_when_no_adjacent_fact(self):
        """Genuinely empty market (no charter-adjacent fact) keeps its high score."""
        r = score_dimension("charter_saturation", self._dim_def(), self._bundle(0, None), 0.0625)
        assert r["used_default"] is False
        assert r["score"] > 5.0

    def test_no_clamp_when_adjacent_count_zero(self):
        r = score_dimension("charter_saturation", self._dim_def(), self._bundle(0, 0), 0.0625)
        assert r["used_default"] is False
        assert r["score"] > 5.0

    def test_no_clamp_when_already_saturated(self):
        """High formal count → low (saturated) score → not optimistic → no clamp,
        even with adjacent schools present."""
        r = score_dimension("charter_saturation", self._dim_def(), self._bundle(40, 4), 0.0625)
        assert r["used_default"] is False
        assert r["score"] <= 5.0

    def test_adjacent_count_invisible_to_main_analysis_filter(self):
        """The adjacent fact is non-scoring (in_main_analysis=False) but the gate
        still reads it via the raw-bundle helper."""
        from pipeline.s5_scoring import _get_charter_adjacent_count, extract_facts_for_dimension
        bundle = self._bundle(0, 3)
        assert _get_charter_adjacent_count(bundle) == 3
        # not surfaced through the scoring filter
        scored = extract_facts_for_dimension("charter_saturation", bundle)
        assert all(f["fact_key"] != "num_charter_adjacent_schools" for f in scored)


# ─────────────────────────────────────────────────────────────────────────────
# 4. POPULATION TRENDS — _score_population_trends threshold table
# ─────────────────────────────────────────────────────────────────────────────

class TestPopulationTrends:
    """_score_population_trends maps pct_change_total to the correct score tier."""

    @pytest.mark.parametrize("pct_change,expected_score", [
        (15.0,   9.0),   # >= +10% — strong growth
        (7.0,    7.5),   # +5% to +10%
        (3.0,    6.5),   # +1% to +5%
        (0.0,    5.5),   # -1% to +1% — stable
        (-3.0,   4.5),   # -5% to -1%
        (-8.0,   3.0),   # -10% to -5%
        (-15.0,  1.5),   # < -10% — sharp decline
    ])
    def test_pct_change_thresholds(self, pct_change, expected_score):
        facts = _pop_fact(pct_change)
        result = _score_population_trends(facts, 0.125)
        assert result is not None
        assert result["score"] == pytest.approx(expected_score)
        assert result["used_default"] is False

    def test_returns_none_when_fact_key_wrong(self):
        """Falls through to caller when pct_change_total is not in the facts."""
        facts = [
            {
                "fact_key": "k12_population_trend_5yr_pct",  # different key
                "value": 3.0,
                "datapoint_id": "dp_0",
                "dimension": "population_trends",
                "in_main_analysis": True,
                "confidence": "HIGH",
            }
        ]
        assert _score_population_trends(facts, 0.125) is None

    def test_returns_none_when_facts_empty(self):
        assert _score_population_trends([], 0.125) is None

    def test_returns_none_when_value_is_string(self):
        """Non-numeric pct_change_total → fall through."""
        facts = _pop_fact.__wrapped__ if hasattr(_pop_fact, "__wrapped__") else None
        fact_list = [
            {
                "fact_key": "pct_change_total",
                "value": "INCREASING",   # qualitative, not numeric
                "datapoint_id": "dp_0",
                "dimension": "population_trends",
                "in_main_analysis": True,
                "confidence": "HIGH",
            }
        ]
        assert _score_population_trends(fact_list, 0.125) is None

    def test_result_has_correct_weight(self):
        result = _score_population_trends(_pop_fact(5.0), 0.125)
        assert result is not None
        assert result["weight"] == pytest.approx(0.125)

    def test_result_confidence_is_high(self):
        """pct_change_total from NCES → HIGH confidence."""
        result = _score_population_trends(_pop_fact(3.0), 0.125)
        assert result is not None
        assert result["confidence"] == "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# 5. TIER COMPUTATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeTier:
    """compute_tier maps composite scores and applies override flag caps correctly."""

    @pytest.fixture(scope="class")
    def wcfg(self):
        return _load_weights()

    @pytest.mark.parametrize("composite,expected_tier", [
        (9.0,  "HIGH_PRIORITY"),
        (8.5,  "HIGH_PRIORITY"),   # exact min boundary
        (8.49, "STRONG"),
        (7.0,  "STRONG"),          # exact min boundary
        (6.99, "MODERATE"),
        (5.5,  "MODERATE"),        # exact min boundary
        (5.49, "WATCHLIST"),
        (4.0,  "WATCHLIST"),       # exact min boundary
        (3.99, "AVOID"),
        (2.0,  "AVOID"),
        (0.0,  "AVOID"),
    ])
    def test_tier_from_composite(self, composite, expected_tier, wcfg):
        tier, _ = compute_tier(composite, [], wcfg)
        assert tier == expected_tier

    def test_oversaturated_caps_high_priority_to_watchlist(self, wcfg):
        flag = {"flag": "OVERSATURATED"}
        tier, _ = compute_tier(9.0, [flag], wcfg)
        assert tier == "WATCHLIST"

    def test_hostile_authorizer_caps_strong_to_watchlist(self, wcfg):
        flag = {"flag": "HOSTILE_AUTHORIZER"}
        tier, _ = compute_tier(7.5, [flag], wcfg)
        assert tier == "WATCHLIST"

    def test_political_risk_caps_high_priority_to_watchlist(self, wcfg):
        flag = {"flag": "POLITICAL_RISK_HIGH"}
        tier, _ = compute_tier(9.0, [flag], wcfg)
        assert tier == "WATCHLIST"

    def test_facilities_bottleneck_caps_high_priority_to_moderate(self, wcfg):
        flag = {"flag": "FACILITIES_BOTTLENECK"}
        tier, _ = compute_tier(9.0, [flag], wcfg)
        assert tier == "MODERATE"

    def test_facilities_bottleneck_does_not_cap_below_moderate(self, wcfg):
        """FACILITIES_BOTTLENECK caps at MODERATE; WATCHLIST stays WATCHLIST."""
        flag = {"flag": "FACILITIES_BOTTLENECK"}
        tier, _ = compute_tier(4.5, [flag], wcfg)
        assert tier == "WATCHLIST"

    def test_replication_friendly_does_not_cap_tier(self, wcfg):
        """REPLICATION_FRIENDLY is a positive flag — never downgrades tier."""
        flag = {"flag": "REPLICATION_FRIENDLY"}
        tier, _ = compute_tier(9.0, [flag], wcfg)
        assert tier == "HIGH_PRIORITY"

    def test_flag_does_not_upgrade_tier(self, wcfg):
        """No flag should ever upgrade a tier above what the composite earns."""
        flag = {"flag": "OVERSATURATED"}
        tier, _ = compute_tier(2.0, [flag], wcfg)
        assert tier == "AVOID"   # already below WATCHLIST; cap cannot upgrade

    def test_tier_label_is_non_empty(self, wcfg):
        _, label = compute_tier(7.0, [], wcfg)
        assert label and isinstance(label, str)

    def test_small_market_flag_does_not_cap_tier(self, wcfg):
        """SMALL_MARKET is informational only — no tier cap."""
        flag = {"flag": "SMALL_MARKET"}
        tier, _ = compute_tier(9.0, [flag], wcfg)
        assert tier == "HIGH_PRIORITY"


# ─────────────────────────────────────────────────────────────────────────────
# 6. STATES.YAML — structural checks
# ─────────────────────────────────────────────────────────────────────────────

class TestStatesConfig:
    """Structural checks on config/states.yaml."""

    def test_nm_cleveland_in_excluded_communities(self):
        """nm-cleveland is excluded because district enrollment ~399 is below viable scale."""
        states = _load_states()
        excluded = states["NM"]["excluded_communities"]
        assert "nm-cleveland" in excluded

    def test_nm_is_marked_pilot(self):
        states = _load_states()
        assert states["NM"]["pilot"] is True

    def test_nm_status_is_active(self):
        states = _load_states()
        assert states["NM"]["status"] == "ACTIVE"

    def test_excluded_communities_is_a_list(self):
        states = _load_states()
        assert isinstance(states["NM"]["excluded_communities"], list)


# ─────────────────────────────────────────────────────────────────────────────
# 7. CONFIDENCE ENUM
# ─────────────────────────────────────────────────────────────────────────────

class TestConfidenceEnum:
    """Confidence.minimum() returns the lowest level in the list."""

    def test_all_high_returns_high(self):
        result = Confidence.minimum([Confidence.HIGH, Confidence.HIGH])
        assert result == Confidence.HIGH

    def test_mixed_high_moderate_returns_moderate(self):
        result = Confidence.minimum([Confidence.HIGH, Confidence.MODERATE])
        assert result == Confidence.MODERATE

    def test_mixed_returns_lowest(self):
        result = Confidence.minimum([Confidence.HIGH, Confidence.MODERATE, Confidence.LOW])
        assert result == Confidence.LOW

    def test_none_beats_low(self):
        result = Confidence.minimum([Confidence.LOW, Confidence.NONE])
        assert result == Confidence.NONE

    def test_none_beats_high(self):
        result = Confidence.minimum([Confidence.HIGH, Confidence.NONE])
        assert result == Confidence.NONE

    def test_empty_list_returns_none(self):
        assert Confidence.minimum([]) == Confidence.NONE

    def test_single_element_returns_itself(self):
        assert Confidence.minimum([Confidence.MODERATE]) == Confidence.MODERATE


# ─────────────────────────────────────────────────────────────────────────────
# 8. COMPETITIVE OPPORTUNITY DEMAND-EVIDENCE GATE (Session 18 Area D)
# ─────────────────────────────────────────────────────────────────────────────

class TestCompetitiveOpportunityGate:
    """competitive_opportunity may not score above midpoint without a demand signal."""

    def _dim_def(self):
        return _load_weights()["dimensions"]["competitive_opportunity"]

    def test_high_gap_no_demand_signal_is_clamped(self):
        """High demand_supply_gap_index but no waitlist/grade-band signal → clamp to 5, excluded."""
        bundle = _dim_bundle("competitive_opportunity", {"demand_supply_gap_index": 9})
        result = score_dimension("competitive_opportunity", self._dim_def(), bundle, 0.10)
        assert result["score"] == pytest.approx(5.0)
        assert result["used_default"] is True
        assert result["confidence"] == Confidence.LOW.value
        assert "demand evidence" in result["primary_driver"].lower()

    def test_geographic_whitespace_alone_does_not_satisfy_gate(self):
        """geographic_whitespace is itself web-search-derived and was removed from
        the qualifying signal set (Review Fix 1). It must NOT lift the gate."""
        bundle = _dim_bundle(
            "competitive_opportunity",
            {"demand_supply_gap_index": 9, "geographic_whitespace": 8},
        )
        result = score_dimension("competitive_opportunity", self._dim_def(), bundle, 0.10)
        assert result["score"] == pytest.approx(5.0)
        assert result["used_default"] is True

    def test_high_gap_with_demand_signal_is_not_clamped(self):
        """A real demand signal (grade_band_gaps) present → high score allowed."""
        bundle = _dim_bundle(
            "competitive_opportunity",
            {"demand_supply_gap_index": 9, "grade_band_gaps": 8},
        )
        result = score_dimension("competitive_opportunity", self._dim_def(), bundle, 0.10)
        assert result["score"] > 5.0
        assert result["used_default"] is False

    def test_low_gap_no_demand_signal_is_not_clamped(self):
        """Low (≤5) opportunity scores are real findings — the gate only clamps highs."""
        bundle = _dim_bundle("competitive_opportunity", {"demand_supply_gap_index": 2})
        result = score_dimension("competitive_opportunity", self._dim_def(), bundle, 0.10)
        assert result["score"] <= 5.0
        assert result["used_default"] is False

    def test_other_dimension_high_score_is_not_gated(self):
        """The demand-evidence gate is specific to competitive_opportunity."""
        bundle = _dim_bundle("academic_need", {"district_proficiency_ela_pct": 10})
        result = score_dimension(
            "academic_need", _load_weights()["dimensions"]["academic_need"], bundle, 0.10
        )
        assert result["score"] >= 9.0
        assert result["used_default"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 9. DATA COVERAGE — INSUFFICIENT OVERRIDE (Session 18 Area C)
# ─────────────────────────────────────────────────────────────────────────────

class TestDataCoverageInsufficient:
    """When 4+ dimensions default, data_coverage_tier is forced to INSUFFICIENT."""

    def _config(self):
        from pipeline import OperatorPreset, OutputMode, PipelineConfig
        return PipelineConfig(
            state="NM",
            preset=OperatorPreset.MATURITY_ADJUSTED,
            mode=OutputMode.STRATEGIC_BRIEF,
            output_format="markdown",
            cache_enabled=False,
            dry_run=False,
            force_refresh=False,
            communities=["nm-test-insufficient"],
            depth="standard",
        )

    def test_empty_bundle_yields_insufficient_tier(self, monkeypatch, tmp_path):
        """An empty fact bundle defaults all dimensions → INSUFFICIENT, not 'unreliable'."""
        import types
        monkeypatch.chdir(tmp_path)  # isolate the scorecard write to a temp dir
        prev = types.SimpleNamespace(output_data={"facts": []})
        # load_scoring_config + schema validation read from the project tree, so
        # point those reads back at the real config/schemas via absolute symlink-free copy.
        for sub in ("config", "schemas"):
            (tmp_path / sub).symlink_to(os.path.join(_ROOT, sub))

        result = s5_run(
            community_id="nm-test-insufficient",
            state="NM",
            config=self._config(),
            previous_result=prev,
        )
        assert result.status == StageStatus.SUCCESS, result.errors
        assert result.output_data["data_coverage_tier"] == "INSUFFICIENT"
        assert len(result.output_data["excluded_dimensions"]) >= 4

    def test_schema_accepts_insufficient_tier(self):
        """The scorecard schema enum must include INSUFFICIENT."""
        import json
        with open(os.path.join(_ROOT, "schemas", "scorecard.schema.json")) as f:
            schema = json.load(f)
        enum = schema["properties"]["data_coverage_tier"]["enum"]
        assert "INSUFFICIENT" in enum
