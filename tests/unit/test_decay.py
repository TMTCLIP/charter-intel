"""
tests/unit/test_decay.py
Pure-Python unit tests for pipeline/layer2/decay.py. No API calls, no network.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.layer2.decay import compute_decay, effective_half_life, is_stale

pytestmark = pytest.mark.unit

AS_OF = date(2026, 6, 4)
HL = 180.0


def test_decay_at_age_zero_is_one():
    assert compute_decay(AS_OF, HL, as_of=AS_OF) == 1.0


def test_decay_at_one_half_life_is_half():
    src = AS_OF - timedelta(days=int(HL))
    assert compute_decay(src, HL, as_of=AS_OF) == pytest.approx(0.5, abs=1e-9)


def test_decay_at_two_half_lives_is_quarter():
    src = AS_OF - timedelta(days=int(HL * 2))
    assert compute_decay(src, HL, as_of=AS_OF) == pytest.approx(0.25, abs=1e-9)


def test_operator_verified_extends_half_life():
    # At the *original* half-life age, a verified signal has not yet halved.
    src = AS_OF - timedelta(days=int(HL))
    plain = compute_decay(src, HL, as_of=AS_OF, operator_verified=False)
    verified = compute_decay(src, HL, as_of=AS_OF, operator_verified=True)
    assert plain == pytest.approx(0.5, abs=1e-9)
    assert verified > 0.5


def test_effective_half_life_multiplier():
    assert effective_half_life(180, False) == 180
    assert effective_half_life(180, True) == pytest.approx(270.0)
    assert effective_half_life(180, True, multiplier=2.0) == pytest.approx(360.0)


def test_is_stale_boundary():
    assert is_stale(0.09, floor=0.1) is True
    assert is_stale(0.1, floor=0.1) is False   # strictly below
    assert is_stale(0.11, floor=0.1) is False


def test_future_source_date_clamps_to_one():
    src = AS_OF + timedelta(days=30)
    assert compute_decay(src, HL, as_of=AS_OF) == 1.0


def test_non_positive_half_life_raises():
    with pytest.raises(ValueError):
        compute_decay(AS_OF, 0, as_of=AS_OF)
    with pytest.raises(ValueError):
        compute_decay(AS_OF, -5, as_of=AS_OF)


def test_accepts_datetime_and_date():
    src_dt = datetime(2026, 6, 4, 13, 30)
    # time-of-day is dropped; same-day source still decays to 1.0
    assert compute_decay(src_dt, HL, as_of=AS_OF) == 1.0
    assert compute_decay(AS_OF, HL, as_of=datetime(2026, 6, 4, 9, 0)) == 1.0
