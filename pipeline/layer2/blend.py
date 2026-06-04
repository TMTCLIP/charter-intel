"""
pipeline/layer2/blend.py

The s5_blend math: fuse a public score P with soft field signals into a
blended score. Pure functions over signal dicts + a resolved per-dimension
config. No storage, no network — the storage layer drives I/O and feeds these
functions plain dicts.

NOT wired into the S5 pipeline in Phase 1; this module stands alone.

A signal dict has at least:
    dimension, direction (positive|negative|neutral), magnitude (1-3),
    confidence (0-1), source_date, operator_verified, status.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import yaml

from pipeline.layer2 import decay as decay_mod

_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "field_signal_weights.yaml"
)

_SIGN = {"positive": 1, "negative": -1, "neutral": 0}


@dataclass(frozen=True)
class DimensionConfig:
    """Resolved blending config for a single dimension (defaults + override)."""

    dimension: str
    wf_base: float
    saturation_k: float
    half_life_days: float
    anchor_slope: float
    anchor_midpoint: float
    field_score_floor: float
    decay_floor: float
    verified_half_life_multiplier: float
    auto_accept_confidence: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def load_field_signal_weights(path: Optional[str] = None) -> dict:
    """Load the raw field_signal_weights.yaml document."""
    with open(path or _CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_dimension_config(
    dimension: str,
    raw_cfg: Optional[dict] = None,
) -> DimensionConfig:
    """
    Build a DimensionConfig by overlaying per_dimension[dimension] (if any) on
    top of `defaults`. Dimensions absent from per_dimension inherit defaults.
    """
    if raw_cfg is None:
        raw_cfg = load_field_signal_weights()
    merged = dict(raw_cfg.get("defaults", {}))
    merged.update(raw_cfg.get("per_dimension", {}).get(dimension, {}))
    return DimensionConfig(
        dimension=dimension,
        wf_base=merged["wf_base"],
        saturation_k=merged["saturation_k"],
        half_life_days=merged["half_life_days"],
        anchor_slope=merged["anchor_slope"],
        anchor_midpoint=merged["anchor_midpoint"],
        field_score_floor=merged["field_score_floor"],
        decay_floor=merged["decay_floor"],
        verified_half_life_multiplier=merged["verified_half_life_multiplier"],
        auto_accept_confidence=merged["auto_accept_confidence"],
    )


def _signal_decay(signal: dict, config: DimensionConfig, *, as_of) -> float:
    return decay_mod.compute_decay(
        signal["source_date"],
        config.half_life_days,
        as_of=as_of,
        operator_verified=bool(signal.get("operator_verified", False)),
        verified_multiplier=config.verified_half_life_multiplier,
    )


def blend_dimension(
    public_score: float,
    signals: List[dict],
    dimension: str,
    config: DimensionConfig,
    *,
    as_of,
) -> dict:
    """
    Blend the public score P with active field signals for one (city, dimension).

    Returns everything downstream display / S5 wiring needs without recompute:
        {public, field, blended, field_informed, reliability, sum_weight, n_active}
    """
    P = float(public_score)
    active = [s for s in signals if s.get("status") == "active"]

    sum_aw = 0.0
    sum_w = 0.0
    for sig in active:
        sign = _SIGN.get(sig.get("direction", "neutral"), 0)
        s = sig["magnitude"] * sign
        anchor = _clamp(config.anchor_midpoint + config.anchor_slope * s, 0.0, 100.0)
        w = sig["confidence"] * _signal_decay(sig, config, as_of=as_of)
        sum_aw += anchor * w
        sum_w += w

    if not active or sum_w < config.field_score_floor:
        field: Optional[float] = None
    else:
        field = sum_aw / sum_w

    reliability = min(1.0, sum_w / config.saturation_k)

    if field is None:
        blended = P
    else:
        wf_eff = config.wf_base * reliability
        blended = _clamp((P + wf_eff * field) / (1.0 + wf_eff), 0.0, 100.0)

    return {
        "public": P,
        "field": field,
        "blended": blended,
        "field_informed": field is not None,
        "reliability": reliability,
        "sum_weight": sum_w,
        "n_active": len(active),
    }


def expire_stale_signals_in_memory(
    signals: List[dict],
    dimension: str,
    config: DimensionConfig,
    *,
    as_of,
) -> List[str]:
    """
    Pure helper: return the signal_ids of active signals whose decay has fallen
    below `decay_floor`. The storage layer uses this to drive expiry I/O — the
    math stays here, the writes stay in the client.
    """
    stale: List[str] = []
    for sig in signals:
        if sig.get("status") != "active":
            continue
        d = _signal_decay(sig, config, as_of=as_of)
        if decay_mod.is_stale(d, config.decay_floor):
            stale.append(sig["signal_id"])
    return stale
