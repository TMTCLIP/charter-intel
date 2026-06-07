"""
tests/unit/test_notion_city_relation.py
Regression tests for the city=None null-relation bug (Layer 2 Phase 2).

When a signal has no matched city, it must be written with a genuinely empty
Notion relation — not coerced to "" and not linked to the blank-slug placeholder
city row. These tests bypass NotionSignalStore.__init__ (which needs a live
notion-client + API key) and exercise the pure relation-resolution logic.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.layer2.notion_client import NotionSignalStore, _p_relation

pytestmark = pytest.mark.unit


def _bare_store():
    """A NotionSignalStore with no live client — only relation-cache state set."""
    store = NotionSignalStore.__new__(NotionSignalStore)
    store._city_id_by_slug = {}

    def _must_not_call(*_a, **_k):  # pragma: no cover - asserted not reached
        raise AssertionError("_call must not be invoked for an unknown city")

    store._call = _must_not_call
    return store


@pytest.mark.parametrize("city", [None, ""])
def test_unknown_city_resolves_to_none_without_query(city):
    # A falsy city must short-circuit before any Notion lookup, so it can never
    # match the blank-slug placeholder row via {"equals": ""}.
    store = _bare_store()
    assert store._city_page_id(city) is None


@pytest.mark.parametrize("page_ids", [None, "", []])
def test_p_relation_empty_for_falsy(page_ids):
    assert _p_relation(page_ids) == {"relation": []}


def test_p_relation_wraps_single_id():
    assert _p_relation("abc123") == {"relation": [{"id": "abc123"}]}


def test_cached_city_still_resolves():
    store = _bare_store()
    store._city_id_by_slug["nm-questa"] = "page-questa"
    assert store._city_page_id("nm-questa") == "page-questa"
