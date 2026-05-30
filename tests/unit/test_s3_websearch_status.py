"""
tests/unit/test_s3_websearch_status.py

Unit tests for _resolve_websearch_status (Session 18 Area A): the honesty
resolver that distinguishes "we found evidence of a middling market" from
"we found nothing" for the 5 supplemental web-search dimensions.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.s3_fact_extraction import _resolve_websearch_status

pytestmark = pytest.mark.unit


def test_explicit_not_found_is_low():
    conf, vs, rationale = _resolve_websearch_status(
        {"confidence": "LOW", "verification_status": "NOT_FOUND", "sources": []}
    )
    assert (conf, vs) == ("LOW", "NOT_FOUND")
    assert rationale and "NOT_FOUND" in rationale


def test_rural_thin_market_is_moderate_provisional():
    conf, vs, rationale = _resolve_websearch_status(
        {"confidence": "LOW", "market_thin": True, "sources": []}
    )
    assert (conf, vs) == ("MODERATE", "PROVISIONAL")
    assert "availability unknown" in rationale.lower()


def test_no_sources_infers_not_found():
    conf, vs, rationale = _resolve_websearch_status(
        {"confidence": "MODERATE", "sources": []}
    )
    assert (conf, vs) == ("LOW", "NOT_FOUND")
    assert rationale is not None


def test_high_confidence_with_sources_is_verified():
    conf, vs, rationale = _resolve_websearch_status(
        {"confidence": "HIGH", "sources": [{"url": "https://example.gov/x"}]}
    )
    assert (conf, vs) == ("HIGH", "VERIFIED")
    assert rationale is None


def test_moderate_confidence_with_sources_is_provisional():
    conf, vs, rationale = _resolve_websearch_status(
        {"confidence": "MODERATE", "sources": [{"url": "https://example.gov/x"}]}
    )
    assert (conf, vs) == ("MODERATE", "PROVISIONAL")
    assert rationale is None


def test_thin_market_with_sources_prefers_evidence_path():
    """market_thin only triggers the rural branch when there are no sources."""
    conf, vs, _ = _resolve_websearch_status(
        {"confidence": "HIGH", "market_thin": True, "sources": [{"url": "https://x.gov"}]}
    )
    assert (conf, vs) == ("HIGH", "VERIFIED")
