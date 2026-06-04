"""
tests/unit/test_blend.py
Pure-Python unit tests for pipeline/layer2/blend.py. No API calls, no network.

Every test uses a fixed AS_OF so decay-driven weights are deterministic.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.layer2.blend import (
    DimensionConfig,
    blend_dimension,
    expire_stale_signals_in_memory,
    resolve_dimension_config,
)
from tests.fixtures.synthetic_signals import AS_OF, make_signal

pytestmark = pytest.mark.unit


# ── config helpers ────────────────────────────────────────────────────────────

def cfg(dimension: str) -> DimensionConfig:
    return resolve_dimension_config(dimension)


def custom_cfg(**overrides) -> DimensionConfig:
    base = dict(
        dimension="custom",
        wf_base=0.6,
        saturation_k=2.0,
        half_life_days=180.0,
        anchor_slope=13.33,
        anchor_midpoint=50.0,
        field_score_floor=0.25,
        decay_floor=0.1,
        verified_half_life_multiplier=1.5,
        auto_accept_confidence=0.75,
    )
    base.update(overrides)
    return DimensionConfig(**base)


def blend(P, signals, dimension="charter_saturation", config=None):
    config = config or cfg(dimension)
    return blend_dimension(P, signals, dimension, config, as_of=AS_OF)


# ── 1 ───────────────────────────────────────────────────────────────────────
def test_no_signals_falls_back_to_public():
    r = blend(62, [])
    assert r["field"] is None
    assert r["blended"] == 62.0
    assert r["field_informed"] is False
    assert r["n_active"] == 0


# ── 2 ───────────────────────────────────────────────────────────────────────
def test_sum_weight_below_floor_falls_back():
    # confidence 0.2 fresh → Σw = 0.2 < field_score_floor (0.25)
    s = make_signal("charter_saturation", "positive", 3, 0.2, AS_OF)
    r = blend(62, [s])
    assert r["field"] is None
    assert r["blended"] == 62.0
    assert r["field_informed"] is False


# ── 3 ───────────────────────────────────────────────────────────────────────
def test_single_fresh_strong_positive_raises_blended():
    s = make_signal("charter_saturation", "positive", 3, 0.9, AS_OF)
    r = blend(50, [s])
    assert r["field"] is not None
    assert r["field"] > 80           # anchor = 50 + 13.33*3 ≈ 90
    assert r["blended"] > 50
    assert r["field_informed"] is True


# ── 4 ───────────────────────────────────────────────────────────────────────
def test_single_fresh_strong_negative_lowers_blended():
    s = make_signal("charter_saturation", "negative", 3, 0.9, AS_OF)
    r = blend(50, [s])
    assert r["field"] < 50           # anchor ≈ 10
    assert r["blended"] < 50


# ── 5 ───────────────────────────────────────────────────────────────────────
def test_neutral_signal_anchors_at_midpoint():
    s = make_signal("charter_saturation", "neutral", 2, 0.9, AS_OF)
    r = blend(80, [s])
    assert r["field"] == pytest.approx(50.0)   # sign 0 → anchor == midpoint
    assert r["blended"] < 80                    # pulled toward 50


# ── 6 ───────────────────────────────────────────────────────────────────────
def test_two_corroborating_positives_average():
    s1 = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    s2 = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    r = blend(60, [s1, s2])
    expected_anchor = 50 + 13.33 * 2           # both identical → F == anchor
    assert r["field"] == pytest.approx(expected_anchor, abs=1e-6)
    assert r["n_active"] == 2


# ── 7 ───────────────────────────────────────────────────────────────────────
def test_older_signal_contributes_less_weight():
    fresh = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    old = make_signal(
        "charter_saturation", "positive", 2, 0.9, AS_OF - timedelta(days=180)
    )
    r_fresh = blend(60, [fresh])
    r_old = blend(60, [old])
    assert r_old["sum_weight"] < r_fresh["sum_weight"]
    assert r_old["reliability"] < r_fresh["reliability"]


# ── 8 ───────────────────────────────────────────────────────────────────────
def test_lower_confidence_contributes_less_weight():
    hi = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    lo = make_signal("charter_saturation", "positive", 2, 0.4, AS_OF)
    assert blend(60, [lo])["sum_weight"] < blend(60, [hi])["sum_weight"]


# ── 9 ───────────────────────────────────────────────────────────────────────
def test_thin_evidence_keeps_blended_near_public():
    # one weak-but-above-floor signal → low reliability → blended ≈ P
    s = make_signal("charter_saturation", "positive", 3, 0.3, AS_OF)
    r = blend(60, [s])
    assert r["reliability"] < 0.2
    assert abs(r["blended"] - 60) < 3


# ── 10 ──────────────────────────────────────────────────────────────────────
def test_reliability_caps_at_one():
    signals = [
        make_signal("charter_saturation", "positive", 2, 0.9, AS_OF) for _ in range(10)
    ]
    r = blend(60, signals)
    assert r["reliability"] == 1.0      # min(1.0, 9.0/2.0) clamps


# ── 11 ──────────────────────────────────────────────────────────────────────
def test_anchor_clamps_at_100():
    c = custom_cfg(anchor_slope=100.0)
    s = make_signal("custom", "positive", 1, 0.9, AS_OF)   # 50 + 100 = 150 → 100
    r = blend(50, [s], config=c)
    assert r["field"] == pytest.approx(100.0)


# ── 12 ──────────────────────────────────────────────────────────────────────
def test_anchor_clamps_at_0():
    c = custom_cfg(anchor_slope=100.0)
    s = make_signal("custom", "negative", 1, 0.9, AS_OF)   # 50 - 100 = -50 → 0
    r = blend(50, [s], config=c)
    assert r["field"] == pytest.approx(0.0)


# ── 13 ──────────────────────────────────────────────────────────────────────
def test_expired_signals_excluded():
    active = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    expired = make_signal(
        "charter_saturation", "negative", 3, 0.9, AS_OF, status="expired"
    )
    r = blend(60, [active, expired])
    assert r["n_active"] == 1
    assert r["field"] == pytest.approx(50 + 13.33 * 2, abs=1e-6)


# ── 14 ──────────────────────────────────────────────────────────────────────
def test_superseded_signals_excluded():
    active = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    superseded = make_signal(
        "charter_saturation", "negative", 3, 0.9, AS_OF, status="superseded"
    )
    r = blend(60, [active, superseded])
    assert r["n_active"] == 1


# ── 15 ──────────────────────────────────────────────────────────────────────
def test_rejected_signals_excluded():
    active = make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)
    rejected = make_signal(
        "charter_saturation", "negative", 3, 0.9, AS_OF, status="rejected"
    )
    r = blend(60, [active, rejected])
    assert r["n_active"] == 1


# ── 16 ──────────────────────────────────────────────────────────────────────
def test_mixed_positive_negative_net_out():
    pos = make_signal("charter_saturation", "positive", 3, 0.9, AS_OF)  # anchor ≈ 90
    neg = make_signal("charter_saturation", "negative", 3, 0.9, AS_OF)  # anchor ≈ 10
    r = blend(60, [pos, neg])
    assert r["field"] == pytest.approx(50.0, abs=1e-6)   # symmetric → midpoint


# ── 17 ──────────────────────────────────────────────────────────────────────
def test_magnitude_scaling_changes_field():
    weak = blend(50, [make_signal("charter_saturation", "positive", 1, 0.9, AS_OF)])
    strong = blend(50, [make_signal("charter_saturation", "positive", 3, 0.9, AS_OF)])
    assert weak["field"] < strong["field"]
    assert weak["field"] == pytest.approx(50 + 13.33 * 1, abs=1e-6)
    assert strong["field"] == pytest.approx(50 + 13.33 * 3, abs=1e-6)


# ── 18 ──────────────────────────────────────────────────────────────────────
def test_per_dimension_wf_base_override_applied():
    # facilities_feasibility wf_base=0.7 vs charter_saturation 0.6; both HL=*fresh*.
    sig_fac = [make_signal("facilities_feasibility", "positive", 3, 0.9, AS_OF)]
    sig_sat = [make_signal("charter_saturation", "positive", 3, 0.9, AS_OF)]
    fac = blend_dimension(50, sig_fac, "facilities_feasibility",
                          cfg("facilities_feasibility"), as_of=AS_OF)
    sat = blend_dimension(50, sig_sat, "charter_saturation",
                          cfg("charter_saturation"), as_of=AS_OF)
    # higher wf_base pulls blended further toward F (F > P here)
    assert fac["blended"] > sat["blended"]


# ── 19 ──────────────────────────────────────────────────────────────────────
def test_per_dimension_half_life_override_changes_weight():
    # 120-day-old signal: political_climate HL=120 decays more than replication HL=365
    src = AS_OF - timedelta(days=120)
    pol = blend_dimension(
        50, [make_signal("political_climate", "positive", 2, 0.9, src)],
        "political_climate", cfg("political_climate"), as_of=AS_OF,
    )
    rep = blend_dimension(
        50, [make_signal("replication_feasibility", "positive", 2, 0.9, src)],
        "replication_feasibility", cfg("replication_feasibility"), as_of=AS_OF,
    )
    assert pol["sum_weight"] < rep["sum_weight"]


# ── 20 ──────────────────────────────────────────────────────────────────────
def test_worked_example_p62_f78_r08_blended67():
    # Custom config: slope 14 → mag-2 positive anchor = 50 + 28 = 78 exactly.
    # Two fresh confidence-0.8 signals → Σw = 1.6 → R = 1.6/2.0 = 0.8.
    # wf_eff = 0.6 * 0.8 = 0.48; blended = (62 + 0.48*78)/1.48 ≈ 67.19.
    c = custom_cfg(anchor_slope=14.0, wf_base=0.6, saturation_k=2.0)
    signals = [
        make_signal("custom", "positive", 2, 0.8, AS_OF),
        make_signal("custom", "positive", 2, 0.8, AS_OF),
    ]
    r = blend_dimension(62, signals, "custom", c, as_of=AS_OF)
    assert r["field"] == pytest.approx(78.0, abs=0.5)
    assert r["reliability"] == pytest.approx(0.8, abs=0.02)
    assert r["blended"] == pytest.approx(67.0, abs=0.5)


# ── 21 ──────────────────────────────────────────────────────────────────────
def test_blended_always_within_bounds():
    src_old = AS_OF - timedelta(days=90)
    cases = [
        (0, [make_signal("charter_saturation", "negative", 3, 0.9, AS_OF)]),
        (100, [make_signal("charter_saturation", "positive", 3, 0.9, AS_OF)]),
        (50, [make_signal("charter_saturation", "positive", 2, 0.5, src_old),
              make_signal("charter_saturation", "negative", 1, 0.7, AS_OF)]),
        (62, []),
        (30, [make_signal("charter_saturation", "neutral", 2, 0.9, AS_OF)]),
    ]
    for P, signals in cases:
        b = blend(P, signals)["blended"]
        assert 0.0 <= b <= 100.0


# ── 22 ──────────────────────────────────────────────────────────────────────
def test_field_informed_matches_field_presence():
    none_case = blend(60, [])
    assert none_case["field_informed"] == (none_case["field"] is not None)

    below_floor = blend(60, [make_signal("charter_saturation", "positive", 2, 0.2, AS_OF)])
    assert below_floor["field_informed"] == (below_floor["field"] is not None)

    informed = blend(60, [make_signal("charter_saturation", "positive", 2, 0.9, AS_OF)])
    assert informed["field_informed"] is True
    assert informed["field_informed"] == (informed["field"] is not None)


# ── 23 ──────────────────────────────────────────────────────────────────────
def test_config_falls_back_to_defaults():
    # competitive_opportunity has no per_dimension entry → inherits defaults
    c = cfg("competitive_opportunity")
    assert c.wf_base == 0.6
    assert c.half_life_days == 180
    assert c.anchor_slope == pytest.approx(13.33)


# ── extra: expire helper (pure) ───────────────────────────────────────────────
def test_expire_stale_signals_in_memory():
    c = cfg("political_climate")   # HL 120, decay_floor 0.1
    fresh = make_signal("political_climate", "positive", 2, 0.9, AS_OF, signal_id="keep")
    # ~575 days old at HL=120 → 0.5**(575/120) ≈ 0.036 < 0.1 → stale
    stale = make_signal(
        "political_climate", "positive", 2, 0.9,
        AS_OF - timedelta(days=575), signal_id="drop",
    )
    expired_status = make_signal(
        "political_climate", "positive", 2, 0.9,
        AS_OF - timedelta(days=575), signal_id="already", status="expired",
    )
    out = expire_stale_signals_in_memory(
        [fresh, stale, expired_status], "political_climate", c, as_of=AS_OF
    )
    assert out == ["drop"]   # only active + below floor
