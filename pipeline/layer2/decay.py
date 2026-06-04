"""
pipeline/layer2/decay.py

Pure, isolated decay math for Layer 2 soft signals. No I/O, no network.

Decay is computed live at blend time from a signal's `source_date`. A signal's
weight in the blend is scaled by its decay factor, which falls from 1.0 (fresh)
toward 0.0 (stale) on an exponential half-life curve. Operator-verified signals
decay more slowly (their half-life is multiplied).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Union

DateLike = Union[date, datetime]


def _to_date(value: DateLike) -> date:
    """Normalize a date or datetime to a plain date (time-of-day ignored)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("expected datetime.date or datetime.datetime, got %r" % type(value))


def effective_half_life(
    half_life_days: float,
    operator_verified: bool,
    multiplier: float = 1.5,
) -> float:
    """Half-life in days, extended by `multiplier` when operator-verified."""
    return half_life_days * multiplier if operator_verified else half_life_days


def compute_decay(
    source_date: DateLike,
    half_life_days: float,
    *,
    as_of: DateLike,
    operator_verified: bool = False,
    verified_multiplier: float = 1.5,
) -> float:
    """
    Exponential half-life decay of a signal as of `as_of`.

        age_days = (as_of - source_date).days
        decay    = 0.5 ** (age_days / effective_half_life)

    The result is clamped to <= 1.0 so a future-dated source cannot amplify a
    signal beyond its fresh weight. `as_of` is keyword-only and required so
    callers (and tests) are deterministic.

    Raises ValueError if `half_life_days <= 0`.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be > 0, got %r" % (half_life_days,))

    hl = effective_half_life(half_life_days, operator_verified, verified_multiplier)
    age_days = (_to_date(as_of) - _to_date(source_date)).days
    decay = 0.5 ** (age_days / hl)
    return decay if decay <= 1.0 else 1.0


def is_stale(decay: float, floor: float = 0.1) -> bool:
    """True when decay has fallen strictly below the staleness floor."""
    return decay < floor
