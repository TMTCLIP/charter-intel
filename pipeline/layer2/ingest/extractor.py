"""
pipeline/layer2/ingest/extractor.py

SignalExtractor — calls claude-haiku-4-5 to pull structured soft signals from
free text (meeting notes, email threads) and writes them to NotionSignalStore.

Layer 2 signals are strictly independent of S1–S4 pipeline scores.  This file
must never import from any S-stage module or read scored output.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

import yaml

from pipeline.layer2.storage_interface import SignalStore

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

_INGEST_DIR = os.path.dirname(__file__)
_CONFIG_DIR = os.path.abspath(os.path.join(_INGEST_DIR, "..", "..", "..", "config"))

_SCORING_WEIGHTS_PATH = os.path.join(_CONFIG_DIR, "scoring_weights.yaml")
_STATES_PATH = os.path.join(_CONFIG_DIR, "states.yaml")
_FIELD_SIGNAL_WEIGHTS_PATH = os.path.join(_CONFIG_DIR, "field_signal_weights.yaml")

_MAX_SIGNALS_PER_DOC = 10

_SYSTEM_PROMPT = """You are a charter-school intelligence analyst. Given meeting notes or
an email thread, extract zero or more discrete soft signals about charter school market
conditions in specific cities.

Return ONLY a JSON array. No preamble, no markdown fences, no trailing text.

Each element in the array must have exactly these fields:
  city_slug        string   e.g. "nm-santa-fe"
  dimension        string   one of: academic_need, charter_saturation, population_trends,
                            political_climate, authorizer_friendliness,
                            facilities_feasibility, replication_feasibility,
                            funding_environment, competitive_opportunity,
                            operational_complexity
  signal_type      string   "qualitative" or "factual"
  direction        string   "positive", "negative", or "neutral"
  confidence       number   0.0 to 1.0
  summary          string   one declarative sentence, 120 chars max
  raw_quote        string|null  verbatim excerpt from the text if factual; null if qualitative

Rules:
- Only extract signals that are clearly supported by the text.
- city_slug must use the format: two-letter state code hyphen city name with hyphens (e.g. nm-albuquerque).
- If no signals can be extracted, return an empty array: []
- raw_quote must be copied verbatim from the source text, never paraphrased.
- confidence reflects how strongly the text supports the signal (1.0 = unambiguous).
"""


def _load_valid_dimensions(path: str = _SCORING_WEIGHTS_PATH) -> frozenset:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return frozenset((cfg.get("dimensions") or {}).keys())


def _load_valid_city_slugs(path: str = _STATES_PATH) -> frozenset:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    slugs: set[str] = set()
    for state_data in cfg.values():
        if not isinstance(state_data, dict):
            continue
        district_map = state_data.get("nm_district_map") or {}
        slugs.update(district_map.keys())
        # Also pick up community_ids declared in community_boundary_exceptions.
        for exc in state_data.get("community_boundary_exceptions") or []:
            if isinstance(exc, dict) and exc.get("community_id"):
                slugs.add(exc["community_id"])
    return frozenset(slugs)


def _load_auto_accept_confidence(path: str = _FIELD_SIGNAL_WEIGHTS_PATH) -> float:
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return float((cfg.get("defaults") or {}).get("auto_accept_confidence", 0.75))


class SignalExtractor:
    """Extracts soft signals from free text and writes them to a SignalStore.

    Parameters
    ----------
    store:
        A SignalStore implementation (typically NotionSignalStore).
    anthropic_client:
        Optional pre-built anthropic.Anthropic client. Created lazily from
        ANTHROPIC_API_KEY if not provided.
    """

    def __init__(
        self,
        store: SignalStore,
        anthropic_client=None,
        scoring_weights_path: str = _SCORING_WEIGHTS_PATH,
        states_path: str = _STATES_PATH,
        field_signal_weights_path: str = _FIELD_SIGNAL_WEIGHTS_PATH,
    ) -> None:
        self._store = store
        self._client = anthropic_client
        self._valid_dimensions = _load_valid_dimensions(scoring_weights_path)
        self._valid_city_slugs = _load_valid_city_slugs(states_path)
        self._auto_accept_threshold = _load_auto_accept_confidence(field_signal_weights_path)

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def extract(self, text: str, metadata: dict) -> list[dict]:
        """Extract signals from *text* and write each valid one to the store.

        Parameters
        ----------
        text:
            Raw document body (meeting note or email thread body).
        metadata:
            Must contain:
              source_type  str   "granola" or "gmail"
              city_slug    str   pre-matched city slug from the ingester
              source_date  date  when the document is dated
              source_id    str   Notion source row ID (from write_source)

        Returns
        -------
        list of signal dicts that were successfully written (excluding filtered/errored).
        """
        client = self._get_client()

        context_hint = (
            f"This document is associated with city: {metadata.get('city_slug', 'unknown')}.\n"
            f"Source type: {metadata.get('source_type', 'unknown')}.\n\n"
        )

        response = client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": context_hint + text}
            ],
        )

        # Cost checkpoint
        usage = response.usage
        print(
            f"[extractor] token usage — input: {usage.input_tokens}, "
            f"output: {usage.output_tokens}"
        )

        raw_text = response.content[0].text.strip() if response.content else ""

        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```[a-z]*\n?", "", raw_text)
            raw_text = re.sub(r"\n?```$", "", raw_text)
            raw_text = raw_text.strip()

        try:
            extracted = json.loads(raw_text)
            if not isinstance(extracted, list):
                raise ValueError("Expected a JSON array, got: %s" % type(extracted).__name__)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "[extractor] JSON parse failure (%s) — raw response: %r",
                exc,
                raw_text[:500],
            )
            return []

        # Cap at 10 signals
        if len(extracted) > _MAX_SIGNALS_PER_DOC:
            logger.warning(
                "[extractor] model returned %d signals; capping at %d",
                len(extracted),
                _MAX_SIGNALS_PER_DOC,
            )
            extracted = extracted[:_MAX_SIGNALS_PER_DOC]

        written: list[dict] = []
        for item in extracted:
            signal = self._validate_and_map(item, metadata)
            if signal is None:
                continue
            try:
                signal_id = self._store.write_signal(signal)
                signal["signal_id"] = signal_id
                written.append(signal)
            except Exception as exc:
                logger.error("[extractor] write_signal failed: %s — signal: %r", exc, signal)

        return written

    def _validate_and_map(self, item: Any, metadata: dict) -> Optional[dict]:
        """Validate extracted item and map it to the write_signal dict shape."""
        if not isinstance(item, dict):
            logger.warning("[extractor] skipping non-dict item: %r", item)
            return None

        city_slug = item.get("city_slug", "")
        dimension = item.get("dimension", "")

        if city_slug not in self._valid_city_slugs:
            logger.warning(
                "[extractor] unknown city_slug %r — skipping signal", city_slug
            )
            return None

        if dimension not in self._valid_dimensions:
            logger.warning(
                "[extractor] unknown dimension %r — skipping signal", dimension
            )
            return None

        direction = item.get("direction", "neutral")
        if direction not in ("positive", "negative", "neutral"):
            logger.warning(
                "[extractor] invalid direction %r — defaulting to neutral", direction
            )
            direction = "neutral"

        confidence_raw = item.get("confidence", 0.5)
        try:
            confidence = float(confidence_raw)
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.5

        raw_quote = item.get("raw_quote")
        summary = str(item.get("summary", ""))[:120]
        signal_type = item.get("signal_type", "qualitative")

        # Use verbatim quote when factual and within the 300-char limit;
        # otherwise fall back to summary so _validate_evidence_text never raises.
        if signal_type == "factual" and raw_quote and len(raw_quote) <= 300:
            evidence_text = raw_quote
        else:
            evidence_text = summary

        confidence_tier = (
            "auto" if confidence >= self._auto_accept_threshold else "review"
        )

        source_date = metadata.get("source_date")
        if isinstance(source_date, str):
            try:
                source_date = date.fromisoformat(source_date)
            except ValueError:
                source_date = None

        return {
            "city": city_slug,  # SignalStore uses key "city", value is city slug
            "dimension": dimension,
            "direction": direction,
            "magnitude": None,
            "confidence": confidence,
            "evidence_text": evidence_text,
            "source_id": metadata.get("source_id"),
            "source_date": source_date,
            "ingested_at": datetime.now(timezone.utc),
            "status": "active",
            "confidence_tier": confidence_tier,
            "operator_verified": False,
            "source_metadata": {
                "signal_type": signal_type,
                "source_type": metadata.get("source_type"),
                "city_slug": city_slug,
            },
        }
