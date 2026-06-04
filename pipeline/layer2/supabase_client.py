"""
pipeline/layer2/supabase_client.py

Phase 1 stub. `SupabaseSignalStore` implements the SignalStore interface but
every method raises NotImplementedError. Its only job in Phase 1 is to prove the
storage layer is backend-agnostic: the codebase can swap stores later without
touching blend.py or (future) ingestion code.
"""
from __future__ import annotations

from typing import List, Optional

from pipeline.layer2.storage_interface import SignalStore

_MSG = "Phase 1 stub — Supabase backend not yet built"


class SupabaseSignalStore(SignalStore):
    """Interface-complete stub; no behavior until the Supabase backend is built."""

    def write_signal(self, signal: dict) -> str:
        raise NotImplementedError(_MSG)

    def get_signals(
        self,
        city_slug: str,
        dimension: str,
        status: str = "active",
    ) -> List[dict]:
        raise NotImplementedError(_MSG)

    def update_signal(self, signal_id: str, updates: dict) -> None:
        raise NotImplementedError(_MSG)

    def expire_stale_signals(self, decay_floor: float = 0.1) -> int:
        raise NotImplementedError(_MSG)

    def get_source_by_external_id(self, external_id: str) -> Optional[dict]:
        raise NotImplementedError(_MSG)

    def write_source(self, source: dict) -> str:
        raise NotImplementedError(_MSG)
