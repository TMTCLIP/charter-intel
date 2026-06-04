"""
tests/fixtures/synthetic_signals.py

Shared builders for synthetic Layer 2 signal dicts. Used by the pure decay /
blend unit tests so cases read clearly and stay deterministic. No I/O, no
network — these are plain dicts only.
"""
from __future__ import annotations

import itertools
from datetime import date
from typing import Optional

# Fixed reference "now" so every decay-driven test is deterministic.
AS_OF = date(2026, 6, 4)

_ID_COUNTER = itertools.count(1)


def make_signal(
    dimension: str,
    direction: str,
    magnitude: int,
    confidence: float,
    source_date: date,
    operator_verified: bool = False,
    status: str = "active",
    signal_id: Optional[str] = None,
    city: str = "nm-albuquerque",
    **extra,
) -> dict:
    """
    Build a synthetic signal dict with the blend-relevant fields populated.

    `signal_id` auto-increments when not supplied so each signal is uniquely
    addressable (the expire helper keys on it). Extra storage-layer fields can
    be passed via **extra.
    """
    sig = {
        "signal_id": signal_id or "sig-%d" % next(_ID_COUNTER),
        "city": city,
        "dimension": dimension,
        "direction": direction,
        "magnitude": magnitude,
        "confidence": confidence,
        "source_date": source_date,
        "operator_verified": operator_verified,
        "status": status,
    }
    sig.update(extra)
    return sig
