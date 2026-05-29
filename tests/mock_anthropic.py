"""
tests/mock_anthropic.py
Mock + recording infrastructure for CLIP pipeline testing.

Three public objects:
  MockCallClaude       — callable replacing pipeline.utils.api_client.call_claude
                         Replays recorded fixtures; raises FileNotFoundError loudly
                         if the fixture is missing (never silently succeeds).
  RecordingCallClaude  — wraps the real call_claude to intercept and persist
                         request + response pairs to fixture files.
  MockAnthropicClient  — drop-in for anthropic.Anthropic (spec-compliance object;
                         implements client.messages.create interface).

Injection pattern (main.py):
    import pipeline.utils.api_client as _api_module
    from tests.mock_anthropic import MockCallClaude
    _mock = MockCallClaude()
    _api_module.call_claude = _mock          # replaces for duration of run

The mock never imports from pipeline internals at module load time — all imports
are lazy (inside __call__) to prevent circular-import issues.

Fixture file layout:
    tests/fixtures/{community_id}/{stage}/call_{n}.json

    {stage} is the exact string passed as stage= to call_claude.
    {n} increments per (community_id, stage) pair across the run.

    Example paths for a standard-depth Questa run:
        nm-questa/s3_fact_extraction/call_0.json
        nm-questa/s3_fact_extraction_political_climate/call_0.json
        nm-questa/s3_fact_extraction_charter_intel/call_0.json
        nm-questa/s4_verification/call_0.json
        nm-questa/s6_synthesis/call_0.json
        nm-questa/s6_synthesis_audit/call_0.json

Fixture JSON format (matches spec):
    {
      "request": {
        "model": "claude-sonnet-4-5",
        "max_tokens": 8192,
        "temperature": 0.0,
        "system": "...",
        "messages": [{"role": "user", "content": "..."}],
        "tools": null | [...]
      },
      "response": {
        "content": [{"type": "text", "text": "..."}],
        "usage": {"input_tokens": N, "output_tokens": N},
        "stop_reason": "end_turn"
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Canonical fixtures root — used by all three classes as default.
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC PATCH HELPER
# ─────────────────────────────────────────────────────────────────────────────

def patch_call_claude(fn: Any) -> None:
    """Patch call_claude everywhere it lives in the pipeline process.

    Stages bind call_claude at import time via:
        from pipeline.utils.api_client import call_claude
    This creates a local reference in each stage module that is completely
    independent of pipeline.utils.api_client.call_claude.  Patching only
    the source module has no effect on those copies.

    This helper patches both the source module AND every stage module that
    holds a direct binding.  It must be called AFTER all stage modules have
    been imported (which is always true inside main() or test setup, since
    the imports are at the top of main.py and are triggered by the first
    `from pipeline import ...` in any test).

    All imports are lazy (inside the function body) to satisfy the rule that
    mock_anthropic.py never imports pipeline internals at module load time.
    """
    import pipeline.utils.api_client as _api_client
    import pipeline.s2_state_context as _s2
    import pipeline.s3_fact_extraction as _s3
    import pipeline.s4_verification as _s4
    import pipeline.s6_synthesis as _s6

    _api_client.call_claude = fn
    _s2.call_claude = fn
    _s3.call_claude = fn
    _s4.call_claude = fn
    _s6.call_claude = fn


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fixture_path(fixtures_dir: str, community_id: str, stage: str, n: int) -> str:
    """Return the canonical path for fixture call n of (community_id, stage)."""
    return os.path.join(fixtures_dir, community_id, stage, f"call_{n}.json")


def _write_fixture(path: str, data: dict, force: bool) -> bool:
    """Write fixture to disk. Returns True if written, False if skipped."""
    if os.path.exists(path) and not force:
        logger.info(
            "[RECORD] %s already exists — skipping (use --force-record to overwrite)", path
        )
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return True


def _lazy_api_result():
    """Lazy import of APIResult to avoid circular imports at module load."""
    from pipeline.utils.api_client import APIResult
    return APIResult


def _lazy_parse_json():
    """Lazy import of JSON parser from api_client."""
    from pipeline.utils.api_client import _parse_json_response
    return _parse_json_response


def _extract_text_from_content(content: list[dict], use_web_search: bool) -> str:
    """Reconstruct the text string from fixture content blocks.

    Mirrors the exact logic in call_claude so replay produces the same text
    the real pipeline would have consumed.
    """
    if use_web_search:
        return "\n".join(
            b["text"] for b in content if b.get("type") == "text"
        ).strip()
    text_blocks = [b for b in content if b.get("type") == "text"]
    return text_blocks[0]["text"] if text_blocks else ""


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY 1 — MOCK CALL CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

class MockCallClaude:
    """Drop-in replacement for pipeline.utils.api_client.call_claude.

    On each call looks up the fixture by (community_id, stage, call_index),
    reconstructs an APIResult identical in shape to a real call, and logs
    [MOCK] to stderr.  Raises FileNotFoundError immediately if no fixture
    exists — never silently returns a wrong or empty result.

    Call MockCallClaude.reset() between communities to clear per-stage counters.
    """

    def __init__(self, fixtures_dir: str = FIXTURES_DIR) -> None:
        self.fixtures_dir = fixtures_dir
        # (community_id, stage) → next call index
        self._call_counts: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        """Reset per-stage call counters. Call between community runs."""
        self._call_counts = {}

    def __call__(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        expect_json: bool = False,
        stage: str = "",
        community_id: str = "",
        retry_attempts: int = 3,
        retry_delay_seconds: float = 5.0,
        use_web_search: bool = False,
        web_search_max_uses: int = 5,
    ):
        APIResult = _lazy_api_result()

        key = (community_id, stage)
        n = self._call_counts.get(key, 0)
        path = _fixture_path(self.fixtures_dir, community_id, stage, n)

        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No fixture found for {community_id}/{stage}/call_{n}.json "
                f"— run with --record first\n"
                f"  Expected path: {path}"
            )

        with open(path) as f:
            fixture = json.load(f)

        self._call_counts[key] = n + 1

        # Announce to stderr so integration tests can assert on [MOCK] lines
        print(f"[MOCK] {stage} call {n} — serving fixture", file=sys.stderr)
        logger.info("[MOCK] %s call %d — serving fixture: %s", stage, n, path)

        resp = fixture.get("response", {})
        content = resp.get("content", [])
        text = _extract_text_from_content(content, use_web_search)

        tokens_in = resp.get("usage", {}).get("input_tokens", 0)
        tokens_out = resp.get("usage", {}).get("output_tokens", 0)

        # Re-parse JSON from the stored text so validation logic in stages runs
        # identically to a real call.
        parsed: Optional[dict] = None
        parse_error: Optional[str] = None
        if expect_json and text:
            parsed, parse_error = _lazy_parse_json()(text)

        return APIResult(
            text=text,
            parsed_json=parsed,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            model=model,
            stage=stage,
            community_id=community_id,
            parse_error=parse_error,
        )


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITY 2 — RECORDING CALL CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

class RecordingCallClaude:
    """Transparent wrapper around the real call_claude for --record mode.

    Passes every call through to the real implementation, then persists the
    request + response pair to a fixture file.  The pipeline receives the
    unmodified real APIResult — recording is completely transparent.

    Fixtures are skipped (not overwritten) if they already exist unless
    force=True (--force-record flag).

    Call reset() between communities; call summary(community_id) to get a
    formatted log message of how many fixtures were written.
    """

    def __init__(
        self,
        original_fn: Any,
        fixtures_dir: str = FIXTURES_DIR,
        force: bool = False,
    ) -> None:
        self._fn = original_fn
        self.fixtures_dir = fixtures_dir
        self.force = force
        self._call_counts: dict[tuple[str, str], int] = {}
        self._written: int = 0

    def reset(self) -> None:
        """Reset counters. Call between community runs."""
        self._call_counts = {}
        self._written = 0

    def __call__(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        expect_json: bool = False,
        stage: str = "",
        community_id: str = "",
        retry_attempts: int = 3,
        retry_delay_seconds: float = 5.0,
        use_web_search: bool = False,
        web_search_max_uses: int = 5,
    ):
        # Run the real call_claude — any exception propagates naturally
        result = self._fn(
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            expect_json=expect_json,
            stage=stage,
            community_id=community_id,
            retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds,
            use_web_search=use_web_search,
            web_search_max_uses=web_search_max_uses,
        )

        key = (community_id, stage)
        n = self._call_counts.get(key, 0)
        self._call_counts[key] = n + 1

        path = _fixture_path(self.fixtures_dir, community_id, stage, n)

        # Build fixture in the spec format: request (API-level shape) + response
        tools_field = (
            [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_search_max_uses}]
            if use_web_search
            else None
        )
        fixture = {
            "request": {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "tools": tools_field,
            },
            "response": {
                "content": [{"type": "text", "text": result.text}],
                "usage": {
                    "input_tokens": result.tokens_input,
                    "output_tokens": result.tokens_output,
                },
                "stop_reason": "end_turn",
            },
        }

        written = _write_fixture(path, fixture, self.force)
        if written:
            self._written += 1
            logger.info("[RECORD] Wrote %s", path)

        return result

    def summary(self, community_id: str) -> str:
        return f"[RECORD] Wrote {self._written} fixtures for {community_id}"


# ─────────────────────────────────────────────────────────────────────────────
# MockAnthropicClient — spec-compliance, client.messages.create interface
# ─────────────────────────────────────────────────────────────────────────────

class _MockUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _MockContentBlock:
    def __init__(self, block_type: str, text: str = "") -> None:
        self.type = block_type
        self.text = text


class _MockResponse:
    """Mimics the anthropic.types.Message object returned by client.messages.create."""

    def __init__(
        self,
        content: list[_MockContentBlock],
        usage: _MockUsage,
        stop_reason: str,
        model: str,
    ) -> None:
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason
        self.model = model


class _MockMessages:
    """Mimics the client.messages namespace."""

    def __init__(
        self,
        fixtures_dir: str,
        community_id: str,
        stage: str,
        call_counts: dict,
    ) -> None:
        self._fixtures_dir = fixtures_dir
        self._community_id = community_id
        self._stage = stage
        self._call_counts = call_counts

    def create(self, **kwargs) -> _MockResponse:
        """Match anthropic.Anthropic().messages.create(**kwargs) interface."""
        use_web_search = bool(kwargs.get("tools"))
        key = (self._community_id, self._stage)
        n = self._call_counts.get(key, 0)
        path = _fixture_path(self._fixtures_dir, self._community_id, self._stage, n)

        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No fixture found for {self._community_id}/{self._stage}/call_{n}.json "
                f"— run with --record first\n  Expected: {path}"
            )

        with open(path) as f:
            fixture = json.load(f)

        self._call_counts[key] = n + 1

        resp = fixture.get("response", {})
        content_raw = resp.get("content", [])
        usage = _MockUsage(
            input_tokens=resp.get("usage", {}).get("input_tokens", 0),
            output_tokens=resp.get("usage", {}).get("output_tokens", 0),
        )
        content_blocks = [
            _MockContentBlock(b.get("type", "text"), b.get("text", ""))
            for b in content_raw
        ]
        return _MockResponse(
            content=content_blocks,
            usage=usage,
            stop_reason=resp.get("stop_reason", "end_turn"),
            model=kwargs.get("model", ""),
        )


class MockAnthropicClient:
    """Drop-in for anthropic.Anthropic.  Implements client.messages.create.

    Usage:
        client = MockAnthropicClient(
            community_id="nm-questa",
            stage="s3_fact_extraction",
        )
        response = client.messages.create(model=..., messages=..., ...)

    Because call_claude creates a fresh anthropic.Anthropic() on every call,
    you need to set community_id and stage before each use, OR inject via
    MockCallClaude (the primary mechanism used by main.py).
    """

    def __init__(
        self,
        community_id: str = "",
        stage: str = "",
        fixtures_dir: str = FIXTURES_DIR,
    ) -> None:
        self.community_id = community_id
        self.stage = stage
        self.fixtures_dir = fixtures_dir
        self._call_counts: dict[tuple[str, str], int] = {}
        self.messages = _MockMessages(
            fixtures_dir=self.fixtures_dir,
            community_id=self.community_id,
            stage=self.stage,
            call_counts=self._call_counts,
        )

    def set_context(self, community_id: str, stage: str) -> None:
        """Update the community/stage context and rebind self.messages."""
        self.community_id = community_id
        self.stage = stage
        self.messages = _MockMessages(
            fixtures_dir=self.fixtures_dir,
            community_id=community_id,
            stage=stage,
            call_counts=self._call_counts,
        )
