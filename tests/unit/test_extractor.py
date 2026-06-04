"""
tests/unit/test_extractor.py

Unit tests for SignalExtractor. All Anthropic API calls and
NotionSignalStore.write_signal are mocked — no network traffic.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch, call

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.layer2.ingest.extractor import SignalExtractor

pytestmark = pytest.mark.unit

# ── Fixtures ───────────────────────────────────────────────────────────────────

_VALID_CITY = "nm-albuquerque"
_VALID_DIM = "political_climate"

_BASE_METADATA = {
    "source_type": "granola",
    "city_slug": _VALID_CITY,
    "source_date": date(2026, 6, 1),
    "source_id": "source-abc-123",
}

_GOOD_SIGNAL = {
    "city_slug": _VALID_CITY,
    "dimension": _VALID_DIM,
    "signal_type": "qualitative",
    "direction": "positive",
    "confidence": 0.80,
    "summary": "School board expressed strong support for new charters.",
    "raw_quote": None,
}


def _make_extractor(store=None, mock_response_text=None):
    """Return a SignalExtractor wired to a mock Anthropic client and store."""
    store = store or MagicMock()

    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=mock_response_text or "[]")]
    mock_message.usage.input_tokens = 100
    mock_message.usage.output_tokens = 50

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    extractor = SignalExtractor(store=store, anthropic_client=mock_client)
    return extractor, store, mock_client


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestValidJsonExtraction:
    def test_single_valid_signal_is_written(self):
        payload = json.dumps([_GOOD_SIGNAL])
        extractor, store, _ = _make_extractor(mock_response_text=payload)
        store.write_signal.return_value = "sig-001"

        result = extractor.extract("Meeting notes text", _BASE_METADATA)

        assert len(result) == 1
        store.write_signal.assert_called_once()
        written_dict = store.write_signal.call_args[0][0]
        assert written_dict["city"] == _VALID_CITY
        assert written_dict["dimension"] == _VALID_DIM
        assert written_dict["direction"] == "positive"
        assert written_dict["confidence"] == 0.80
        assert written_dict["status"] == "active"
        assert written_dict["operator_verified"] is False

    def test_confidence_above_threshold_gets_auto_tier(self):
        sig = {**_GOOD_SIGNAL, "confidence": 0.90}
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))
        store.write_signal.return_value = "sig-001"

        extractor.extract("text", _BASE_METADATA)

        written = store.write_signal.call_args[0][0]
        assert written["confidence_tier"] == "auto"

    def test_confidence_below_threshold_gets_review_tier(self):
        sig = {**_GOOD_SIGNAL, "confidence": 0.60}
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))
        store.write_signal.return_value = "sig-001"

        extractor.extract("text", _BASE_METADATA)

        written = store.write_signal.call_args[0][0]
        assert written["confidence_tier"] == "review"

    def test_factual_signal_uses_raw_quote_as_evidence(self):
        sig = {
            **_GOOD_SIGNAL,
            "signal_type": "factual",
            "raw_quote": "Mayor said 'we will approve two charters this year'.",
        }
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))
        store.write_signal.return_value = "sig-001"

        extractor.extract("text", _BASE_METADATA)

        written = store.write_signal.call_args[0][0]
        assert written["evidence_text"] == sig["raw_quote"]

    def test_factual_signal_with_long_quote_falls_back_to_summary(self):
        long_quote = "x" * 301  # exceeds 300-char limit
        sig = {
            **_GOOD_SIGNAL,
            "signal_type": "factual",
            "raw_quote": long_quote,
            "summary": "Short summary within 120 chars.",
        }
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))
        store.write_signal.return_value = "sig-001"

        extractor.extract("text", _BASE_METADATA)

        written = store.write_signal.call_args[0][0]
        assert written["evidence_text"] == "Short summary within 120 chars."

    def test_empty_array_results_in_no_writes(self):
        extractor, store, _ = _make_extractor(mock_response_text="[]")

        result = extractor.extract("text", _BASE_METADATA)

        assert result == []
        store.write_signal.assert_not_called()

    def test_multiple_valid_signals_all_written(self):
        signals = [
            {**_GOOD_SIGNAL, "dimension": "academic_need"},
            {**_GOOD_SIGNAL, "dimension": "funding_environment"},
        ]
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps(signals))
        store.write_signal.side_effect = ["sig-001", "sig-002"]

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 2
        assert store.write_signal.call_count == 2


class TestJsonParseFailure:
    def test_invalid_json_returns_empty_list(self):
        extractor, store, _ = _make_extractor(mock_response_text="not valid json {{")

        result = extractor.extract("text", _BASE_METADATA)

        assert result == []
        store.write_signal.assert_not_called()

    def test_non_array_json_returns_empty_list(self):
        extractor, store, _ = _make_extractor(
            mock_response_text=json.dumps({"signals": []})
        )

        result = extractor.extract("text", _BASE_METADATA)

        assert result == []
        store.write_signal.assert_not_called()

    def test_markdown_fenced_json_is_parsed(self):
        payload = "```json\n" + json.dumps([_GOOD_SIGNAL]) + "\n```"
        extractor, store, _ = _make_extractor(mock_response_text=payload)
        store.write_signal.return_value = "sig-001"

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 1
        store.write_signal.assert_called_once()


class TestSignalCap:
    def test_cap_at_10_signals(self):
        signals = [
            {**_GOOD_SIGNAL, "dimension": "academic_need"}
        ] * 15
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps(signals))
        store.write_signal.side_effect = [f"sig-{i}" for i in range(15)]

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 10
        assert store.write_signal.call_count == 10

    def test_exactly_10_signals_all_written(self):
        signals = [
            {**_GOOD_SIGNAL, "dimension": "academic_need"}
        ] * 10
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps(signals))
        store.write_signal.side_effect = [f"sig-{i}" for i in range(10)]

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 10

    def test_cap_takes_first_10_not_last_10(self):
        signals = [
            {**_GOOD_SIGNAL, "dimension": "academic_need", "confidence": float(i) / 20}
            for i in range(12)
        ]
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps(signals))
        store.write_signal.side_effect = [f"sig-{i}" for i in range(12)]

        extractor.extract("text", _BASE_METADATA)

        calls = store.write_signal.call_args_list
        # The 10 written signals should be the first 10 (indices 0-9)
        written_confidences = [c[0][0]["confidence"] for c in calls]
        expected = [float(i) / 20 for i in range(10)]
        assert written_confidences == expected


class TestUnknownCitySlugFiltered:
    def test_unknown_city_slug_not_written(self):
        sig = {**_GOOD_SIGNAL, "city_slug": "xx-nonexistent-city"}
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))

        result = extractor.extract("text", _BASE_METADATA)

        assert result == []
        store.write_signal.assert_not_called()

    def test_known_city_passes_filter(self):
        sig = {**_GOOD_SIGNAL, "city_slug": "nm-santa-fe"}
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))
        store.write_signal.return_value = "sig-001"

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 1

    def test_mixed_batch_only_valid_cities_written(self):
        signals = [
            {**_GOOD_SIGNAL, "city_slug": _VALID_CITY},
            {**_GOOD_SIGNAL, "city_slug": "zz-fake-city", "dimension": "academic_need"},
        ]
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps(signals))
        store.write_signal.return_value = "sig-001"

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 1
        assert store.write_signal.call_count == 1


class TestUnknownDimensionFiltered:
    def test_unknown_dimension_not_written(self):
        sig = {**_GOOD_SIGNAL, "dimension": "made_up_dimension_xyz"}
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))

        result = extractor.extract("text", _BASE_METADATA)

        assert result == []
        store.write_signal.assert_not_called()

    def test_known_dimension_passes_filter(self):
        for dim in (
            "academic_need", "charter_saturation", "population_trends",
            "political_climate", "authorizer_friendliness", "facilities_feasibility",
            "replication_feasibility", "funding_environment",
            "competitive_opportunity", "operational_complexity",
        ):
            sig = {**_GOOD_SIGNAL, "dimension": dim}
            extractor, store, _ = _make_extractor(mock_response_text=json.dumps([sig]))
            store.write_signal.return_value = f"sig-{dim}"

            result = extractor.extract("text", _BASE_METADATA)
            assert len(result) == 1, f"Dimension {dim!r} should pass filter"

    def test_mixed_batch_only_valid_dimensions_written(self):
        signals = [
            {**_GOOD_SIGNAL, "dimension": _VALID_DIM},
            {**_GOOD_SIGNAL, "dimension": "s4_verification_score"},  # S-stage key: forbidden
        ]
        extractor, store, _ = _make_extractor(mock_response_text=json.dumps(signals))
        store.write_signal.return_value = "sig-001"

        result = extractor.extract("text", _BASE_METADATA)

        assert len(result) == 1
        assert store.write_signal.call_count == 1
