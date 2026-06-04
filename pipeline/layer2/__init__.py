"""CLIP Layer 2 — soft signal intelligence (storage, decay, blending).

Public surface: the storage interface + both backends, the blend entry point,
and the decay helpers. Future ingestion / S5 wiring imports from here.
"""
from __future__ import annotations

from pipeline.layer2.blend import (
    DimensionConfig,
    blend_dimension,
    expire_stale_signals_in_memory,
    resolve_dimension_config,
)
from pipeline.layer2.decay import compute_decay, effective_half_life, is_stale
from pipeline.layer2.notion_client import NotionSignalStore
from pipeline.layer2.storage_interface import SignalStore
from pipeline.layer2.supabase_client import SupabaseSignalStore

__all__ = [
    "SignalStore",
    "NotionSignalStore",
    "SupabaseSignalStore",
    "blend_dimension",
    "resolve_dimension_config",
    "expire_stale_signals_in_memory",
    "DimensionConfig",
    "compute_decay",
    "effective_half_life",
    "is_stale",
]
