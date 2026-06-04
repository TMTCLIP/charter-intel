"""
tests/unit/test_s6_state_name.py

Tests that _load_state_config returns the full state name from states.yaml,
which is used to expand STATE_NAME in the synthesis prompt (S35/task3).
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.s6_synthesis import _load_state_config

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _chdir_root(monkeypatch):
    monkeypatch.chdir(_ROOT)


def test_nm_state_name_is_new_mexico():
    cfg = _load_state_config("NM")
    assert cfg.get("name") == "New Mexico"


def test_state_name_case_insensitive_lookup():
    # lowercase "nm" must resolve the same block as "NM"
    cfg = _load_state_config("nm")
    assert cfg.get("name") == "New Mexico"


def test_unknown_state_returns_empty_dict():
    cfg = _load_state_config("ZZ")
    assert cfg == {}


def test_state_name_fallback_to_code():
    """When states.yaml has no entry for a code, callers fall back to the raw code."""
    cfg = _load_state_config("ZZ")
    state_name = cfg.get("name") or "ZZ"
    assert state_name == "ZZ"
