"""
tests/unit/test_storage_interface.py
Pure-Python interface-compliance tests. No API calls, no network — the stub's
methods are never invoked; we only assert the contract is satisfied.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.layer2.storage_interface import SignalStore
from pipeline.layer2.supabase_client import SupabaseSignalStore

pytestmark = pytest.mark.unit


def test_abstract_base_cannot_be_instantiated():
    with pytest.raises(TypeError):
        SignalStore()


def test_supabase_stub_satisfies_interface():
    # Instantiable → it declares every abstract method. (Methods not called.)
    store = SupabaseSignalStore()
    assert isinstance(store, SignalStore)
    assert SupabaseSignalStore.__abstractmethods__ == frozenset()


def test_supabase_stub_declares_all_methods():
    required = {
        "write_signal",
        "get_signals",
        "update_signal",
        "expire_stale_signals",
        "get_source_by_external_id",
        "write_source",
    }
    for name in required:
        assert callable(getattr(SupabaseSignalStore, name))
