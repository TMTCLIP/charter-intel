"""
pipeline/layer2/storage_interface.py

Backend-agnostic storage contract for Layer 2 soft signals.

`blend.py` and all future ingestion depend ONLY on this interface — never on
Notion (or Supabase) specifics. Swapping backends must not touch the blend math
or ingestion code. Both NotionSignalStore and SupabaseSignalStore implement
this ABC.

──────────────────────────────────────────────────────────────────────────────
Canonical dict shapes (field-name parity across every backend is mandatory —
the blend layer and the storage layer agree on exactly these names):

SIGNAL dict:
    signal_id        str            backend row id
    city             str            city slug (e.g. "nm-albuquerque")
    dimension        str            scoring_weights.yaml dimension key
    direction        str            "positive" | "negative" | "neutral"
    magnitude        int            1-3
    confidence       float          0.0-1.0
    evidence_text    str            verbatim, <= 300 chars
    source_id        str            FK → source row
    source_date      date           when the underlying evidence is dated
    ingested_at      datetime       when the signal was written
    status           str            "active" | "expired" | "superseded" | "rejected"
    confidence_tier  str            "auto" | "review" | "verified" | "rejected"
    operator_verified bool          human-confirmed → slower decay
    operator_note    str            free-text operator annotation
    superseded_by    Optional[str]  signal_id that replaced this one, else None
    source_metadata  dict           attendee context, email sender, participants…

SOURCE dict:
    source_id        str            backend row id
    source_type      str            "gmail" | "granola" | "manual"
    external_id      str            dedup key (unique per source_type)
    title            str
    source_date      date
    ingested_at      datetime
    raw_url          str
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import abc
from typing import List, Optional

# evidence_text is stored verbatim and capped; over-long text is rejected, not
# silently truncated, so an operator never reviews an altered quote.
MAX_EVIDENCE_TEXT_CHARS = 300


class SignalStore(abc.ABC):
    """Abstract soft-signal backend. Implementations map these dict shapes
    to/from their concrete store (Notion properties, Supabase columns, …)."""

    @abc.abstractmethod
    def write_signal(self, signal: dict) -> str:
        """Persist a signal dict; return its signal_id.

        Implementations MUST enforce ``len(evidence_text) <= 300`` by raising
        ValueError (truncation would alter a verbatim quote)."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_signals(
        self,
        city_slug: str,
        dimension: str,
        status: str = "active",
    ) -> List[dict]:
        """Return signal dicts for one (city, dimension), filtered by status."""
        raise NotImplementedError

    @abc.abstractmethod
    def update_signal(self, signal_id: str, updates: dict) -> None:
        """Patch the named fields on an existing signal."""
        raise NotImplementedError

    @abc.abstractmethod
    def expire_stale_signals(self, decay_floor: float = 0.1) -> int:
        """Expire active signals whose live decay has fallen below
        ``decay_floor`` (set status=expired); return the count expired."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_source_by_external_id(self, external_id: str) -> Optional[dict]:
        """Dedup primitive: return the source dict for ``external_id`` or None."""
        raise NotImplementedError

    @abc.abstractmethod
    def write_source(self, source: dict) -> str:
        """Persist a source dict; return its source_id."""
        raise NotImplementedError

    @staticmethod
    def _validate_evidence_text(signal: dict) -> None:
        """Shared guard: reject over-long verbatim evidence. Backends call this
        from write_signal so the 300-char rule is enforced identically."""
        text = signal.get("evidence_text") or ""
        if len(text) > MAX_EVIDENCE_TEXT_CHARS:
            raise ValueError(
                "evidence_text exceeds %d chars (got %d) — store verbatim, do not truncate"
                % (MAX_EVIDENCE_TEXT_CHARS, len(text))
            )
