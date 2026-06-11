"""
tests/unit/test_budget_and_cache.py

Unit tests for C-1:
  - per-run spend ceiling enforced by api_client._enforce_budget
  - cache-aware wrapper api_client.call_claude_cached
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline.utils.api_client as api_client
from pipeline.utils.api_client import (
    APIResult,
    BudgetExceededError,
    call_claude_cached,
    set_run_budget,
    _enforce_budget,
)
from pipeline.utils.token_logger import token_logger

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset run-level module state so tests don't leak into each other."""
    token_logger.set_run_id("test-budget")   # clears accumulated records
    set_run_budget(None, None)
    yield
    set_run_budget(None, None)
    token_logger.set_run_id("unset")


# ── spend ceiling ─────────────────────────────────────────────────────────────

class TestRunBudget:
    def test_no_ceiling_never_raises(self):
        # Even with real spend logged, an unset budget is a no-op.
        token_logger.log_call("s3", "claude-sonnet-4-5", 1_000_000, 0)
        _enforce_budget("s3")   # should not raise

    def test_cost_ceiling_raises_when_exceeded(self):
        set_run_budget(max_cost_usd=0.01)
        # 10k Sonnet input tokens ≈ $0.03 > $0.01 ceiling
        token_logger.log_call("s3", "claude-sonnet-4-5", 10_000, 0)
        with pytest.raises(BudgetExceededError):
            _enforce_budget("s3")

    def test_cost_ceiling_ok_when_under(self):
        set_run_budget(max_cost_usd=10.0)
        token_logger.log_call("s3", "claude-sonnet-4-5", 10_000, 0)
        _enforce_budget("s3")   # well under $10 — should not raise

    def test_token_ceiling_raises_when_exceeded(self):
        set_run_budget(max_tokens=100)
        token_logger.log_call("s3", "claude-sonnet-4-5", 80, 80)  # 160 > 100
        with pytest.raises(BudgetExceededError):
            _enforce_budget("s3")


# ── cache-aware wrapper ─────────────────────────────────────────────────────────

class _DictCache:
    """Minimal CacheManager stand-in (get/set on a dict)."""
    def __init__(self):
        self.store = {}
        self.set_calls = 0

    def get(self, key, ttl_days=None):
        return self.store.get(key)

    def set(self, key, value):
        self.set_calls += 1
        self.store[key] = value


def _fake_result(text="hello", parsed=None):
    return APIResult(
        text=text, parsed_json=parsed, tokens_input=5, tokens_output=7,
        model="claude-sonnet-4-5", stage="s3", community_id="nm-x",
    )


class TestCallClaudeCached:
    def test_miss_calls_through_and_stores(self, monkeypatch):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            return _fake_result(parsed={"k": "v"})

        monkeypatch.setattr(api_client, "call_claude", fake_call)
        cache = _DictCache()

        result = call_claude_cached(
            cache, "llm/nm/nm-x/s3.json",
            model="m", system="s", user="u", stage="s3",
        )

        assert calls["n"] == 1
        assert result.parsed_json == {"k": "v"}
        assert cache.set_calls == 1
        assert "llm/nm/nm-x/s3.json" in cache.store

    def test_hit_does_not_call_through(self, monkeypatch):
        def boom(**kwargs):
            raise AssertionError("call_claude must not run on a cache hit")

        monkeypatch.setattr(api_client, "call_claude", boom)
        cache = _DictCache()
        cache.store["llm/nm/nm-x/s3.json"] = {
            "text": "cached", "parsed_json": {"a": 1}, "tokens_input": 5,
            "tokens_output": 7, "model": "claude-sonnet-4-5",
            "stage": "s3", "community_id": "nm-x", "parse_error": None,
        }

        result = call_claude_cached(
            cache, "llm/nm/nm-x/s3.json",
            model="m", system="s", user="u", stage="s3",
        )

        assert result.text == "cached"
        assert result.parsed_json == {"a": 1}

    def test_none_cache_bypasses(self, monkeypatch):
        calls = {"n": 0}

        def fake_call(**kwargs):
            calls["n"] += 1
            return _fake_result()

        monkeypatch.setattr(api_client, "call_claude", fake_call)
        result = call_claude_cached(
            None, "ignored-key",
            model="m", system="s", user="u", stage="s3",
        )
        assert calls["n"] == 1
        assert result.text == "hello"

    def test_empty_result_not_cached(self, monkeypatch):
        # A dry-run / empty result (ok is False) must not be stored.
        def empty_call(**kwargs):
            return APIResult(text="", parsed_json=None, stage="s3")

        monkeypatch.setattr(api_client, "call_claude", empty_call)
        cache = _DictCache()
        call_claude_cached(
            cache, "llm/nm/nm-x/s3.json",
            model="m", system="s", user="u", stage="s3",
        )
        assert cache.set_calls == 0
        assert cache.store == {}
