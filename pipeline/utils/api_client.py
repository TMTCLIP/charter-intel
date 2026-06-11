"""
pipeline/utils/api_client.py
Anthropic API wrapper for the charter intelligence pipeline.

PURPOSE:
  Centralize all Claude API calls. Enforce:
  - JSON-only output for extraction/audit stages
  - Token logging for cost tracking
  - Retry with backoff for rate limits
  - Model routing from pipeline.yaml

USAGE:
  from pipeline.utils.api_client import call_claude

  result = call_claude(
      model="claude-sonnet-4-5",
      system=system_prompt,
      user=user_prompt,
      expect_json=True,
      max_tokens=2000,
      stage="s3_fact_extraction",
      community_id="nm-albuquerque"
  )
  data = result.parsed_json  # None if expect_json=False or parse failed
  text = result.text

DO NOT add logic here that belongs in a stage.
This module only handles transport, logging, and retry.
"""

from __future__ import annotations
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic
from pipeline.utils.token_logger import token_logger

logger = logging.getLogger(__name__)


# Module-level dry-run backstop. main.py sets this from config.dry_run at
# startup so call_claude refuses a billable call even if a stage forgets its own
# `if config.dry_run` guard. The per-stage guards remain the primary mechanism;
# this is redundant safety, not a replacement for them.
_DRY_RUN = False


def set_dry_run(enabled: bool) -> None:
    """Enable/disable the global dry-run backstop for call_claude."""
    global _DRY_RUN
    _DRY_RUN = bool(enabled)


# ─────────────────────────────────────────────
# PER-RUN SPEND CEILING (C-1)
# ─────────────────────────────────────────────
# A hard, configurable budget for one pipeline run. Enforced inside call_claude
# against the cumulative totals already tracked by token_logger, so it adds no
# new bookkeeping. Both limits default to None (unlimited) — runs that don't set
# a ceiling behave exactly as before. main.py wires these from env vars
# (CLIP_MAX_RUN_COST_USD / CLIP_MAX_RUN_TOKENS) at startup.

_MAX_RUN_COST_USD: Optional[float] = None
_MAX_RUN_TOKENS: Optional[int] = None
_BUDGET_WARN_FRACTION = 0.8   # warn once when cumulative spend crosses 80%
_budget_warned = False


class BudgetExceededError(RuntimeError):
    """Raised when a run exceeds its configured token/$ ceiling.

    Carries a human-readable message; main.py catches it to print a clean
    abort line (no stack trace) and exit non-zero.
    """


def set_run_budget(
    max_cost_usd: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> None:
    """Set the per-run spend ceiling. None disables that limit (the default)."""
    global _MAX_RUN_COST_USD, _MAX_RUN_TOKENS, _budget_warned
    _MAX_RUN_COST_USD = max_cost_usd
    _MAX_RUN_TOKENS = max_tokens
    _budget_warned = False


def _enforce_budget(stage: str) -> None:
    """Abort the run if cumulative spend has passed the configured ceiling.

    Checked at the start of every call_claude, so once a prior call pushes the
    run over budget the next call is refused before spending more. Warns once at
    80% of either limit. No-op when no ceiling is configured.
    """
    if _MAX_RUN_COST_USD is None and _MAX_RUN_TOKENS is None:
        return

    cost = token_logger.total_cost_usd
    tokens = token_logger.total_tokens

    global _budget_warned
    if not _budget_warned and (
        (_MAX_RUN_COST_USD is not None and cost >= _BUDGET_WARN_FRACTION * _MAX_RUN_COST_USD)
        or (_MAX_RUN_TOKENS is not None and tokens >= _BUDGET_WARN_FRACTION * _MAX_RUN_TOKENS)
    ):
        logger.warning(
            "[%s] approaching run budget: $%.4f"
            "%s spent, %s%s tokens used.",
            stage, cost,
            f" / ${_MAX_RUN_COST_USD:.2f}" if _MAX_RUN_COST_USD is not None else "",
            f"{tokens:,}",
            f" / {_MAX_RUN_TOKENS:,}" if _MAX_RUN_TOKENS is not None else "",
        )
        _budget_warned = True

    if _MAX_RUN_COST_USD is not None and cost > _MAX_RUN_COST_USD:
        raise BudgetExceededError(
            f"Run budget exceeded at stage '{stage}': estimated ${cost:.4f} spent "
            f"> ceiling ${_MAX_RUN_COST_USD:.2f} (CLIP_MAX_RUN_COST_USD). "
            f"Aborting before further API calls."
        )
    if _MAX_RUN_TOKENS is not None and tokens > _MAX_RUN_TOKENS:
        raise BudgetExceededError(
            f"Run budget exceeded at stage '{stage}': {tokens:,} tokens used "
            f"> ceiling {_MAX_RUN_TOKENS:,} (CLIP_MAX_RUN_TOKENS). "
            f"Aborting before further API calls."
        )


# ─────────────────────────────────────────────
# RESULT OBJECT
# ─────────────────────────────────────────────

@dataclass
class APIResult:
    text: str                        # Raw model output text
    parsed_json: Optional[dict]      # Parsed JSON if expect_json=True and parse succeeded
    tokens_input: int = 0
    tokens_output: int = 0
    model: str = ""
    stage: str = ""
    community_id: str = ""
    parse_error: Optional[str] = None  # Set if expect_json=True but parse failed
    raw_response: Any = None

    @property
    def total_tokens(self) -> int:
        return self.tokens_input + self.tokens_output

    @property
    def ok(self) -> bool:
        """True if we got text back. Does not imply valid JSON."""
        return bool(self.text)


# ─────────────────────────────────────────────
# MAIN API CALL FUNCTION
# ─────────────────────────────────────────────

# C-1: call_claude enforces a per-run spend ceiling (see set_run_budget /
# _enforce_budget above). For response-level caching, callers may use the
# opt-in call_claude_cached() wrapper below; existing per-stage caches are
# unaffected.

def call_claude(
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
    timeout_seconds: float = 300.0,
) -> APIResult:
    """
    Call Claude and return an APIResult.

    Args:
        model:           Model string from pipeline.yaml
        system:          System prompt
        user:            User turn content
        max_tokens:      Max output tokens
        temperature:     Sampling temperature (0.0 for extraction, >0 for synthesis)
        expect_json:     If True, attempt to parse response as JSON
        stage:           Pipeline stage name (for logging)
        community_id:    Community being processed (for logging)
        retry_attempts:  Number of retries on rate limit or server error
        retry_delay_seconds: Initial backoff; doubled on each retry
        use_web_search:  If True, attach the Anthropic web search tool. Response
                         content will be a mixed list of text/tool_use/tool_result
                         blocks; all text blocks are joined as the final output.
                         Existing callers with use_web_search=False are unaffected.
        web_search_max_uses: Maximum searches the model may issue per call.
                         Only applies when use_web_search=True.
                         Depth routing: standard=4/6, deep=8/10 (per-call caps).
        timeout_seconds: Hard wall-clock timeout per API call (default 300s / 5 min).
                         The SDK default is 600s; without this a stuck web-search
                         call hangs the entire pipeline for 10 minutes.

    Returns:
        APIResult. Check result.ok before using output.
    """
    if _DRY_RUN:
        logger.warning(
            "[%s] call_claude reached in dry-run mode — returning empty result, "
            "no API call made (a stage guard was expected to prevent this).",
            stage,
        )
        return APIResult(
            text="",
            parsed_json=None,
            model=model,
            stage=stage,
            community_id=community_id,
            parse_error="dry-run: no API call made" if expect_json else None,
        )

    # Refuse to spend more once the run is already over its configured ceiling.
    _enforce_budget(stage)

    client = anthropic.Anthropic(timeout=timeout_seconds)  # hard per-call ceiling

    last_error = None
    delay = retry_delay_seconds          # 5xx backoff: 5s → 10s → 20s
    rate_limit_delay = 30.0              # 429 backoff: 30s → 60s → 120s

    for attempt in range(retry_attempts):
        try:
            call_kwargs: dict = dict(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            if use_web_search:
                call_kwargs["tools"] = [
                    {"type": "web_search_20250305", "name": "web_search",
                     "max_uses": web_search_max_uses}
                ]

            response = client.messages.create(**call_kwargs)

            # With web search the response contains mixed block types
            # (text, tool_use, tool_result). Extract and join all text blocks.
            if use_web_search:
                text = "\n".join(
                    block.text
                    for block in response.content
                    if block.type == "text"
                ).strip()
            else:
                # Don't assume the first block is text — filter by type so a
                # leading non-text block can't raise AttributeError.
                text = "\n".join(
                    block.text
                    for block in (response.content or [])
                    if getattr(block, "type", None) == "text"
                ).strip()

            tokens_in = response.usage.input_tokens
            tokens_out = response.usage.output_tokens

            logger.info(
                f"[{stage}] [{community_id}] {model} "
                f"in={tokens_in} out={tokens_out} total={tokens_in + tokens_out}"
            )
            token_logger.log_call(
                stage=stage,
                model=model,
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                community_id=community_id,
            )

            # JSON parsing
            parsed = None
            parse_error = None
            if expect_json:
                parsed, parse_error = _parse_json_response(text)

            return APIResult(
                text=text,
                parsed_json=parsed,
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                model=model,
                stage=stage,
                community_id=community_id,
                parse_error=parse_error,
                raw_response=response
            )

        except anthropic.APITimeoutError as e:
            last_error = e
            logger.warning(
                f"[{stage}] Timeout after {timeout_seconds}s on attempt "
                f"{attempt + 1}/{retry_attempts}. Retrying..."
            )
            time.sleep(delay)
            delay *= 2

        except anthropic.RateLimitError as e:
            last_error = e
            logger.warning(
                f"[{stage}] Rate limit on attempt {attempt + 1}/{retry_attempts}. "
                f"Waiting {rate_limit_delay}s..."
            )
            time.sleep(rate_limit_delay)
            rate_limit_delay *= 2

        except anthropic.APIStatusError as e:
            last_error = e
            if e.status_code >= 500:
                logger.warning(
                    f"[{stage}] Server error {e.status_code} on attempt {attempt + 1}. "
                    f"Waiting {delay}s..."
                )
                time.sleep(delay)
                delay *= 2
            else:
                # 4xx errors are not retried
                logger.error(f"[{stage}] API error {e.status_code}: {e.message}")
                break

    # All retries exhausted
    raise RuntimeError(
        f"[{stage}] Claude API call failed after {retry_attempts} attempts. "
        f"Last error: {last_error}"
    )


# ─────────────────────────────────────────────
# CACHE-AWARE WRAPPER (C-1)
# ─────────────────────────────────────────────

# Fields persisted on a cache hit. raw_response (the live SDK object) is
# intentionally omitted — it is not JSON-serialisable and not needed downstream.
_CACHED_RESULT_FIELDS = (
    "text", "parsed_json", "tokens_input", "tokens_output",
    "model", "stage", "community_id", "parse_error",
)


def call_claude_cached(
    cache: Any,
    cache_key: str,
    *,
    ttl_days: Optional[int] = None,
    **call_kwargs: Any,
) -> APIResult:
    """Cache-aware wrapper around call_claude.

    On a cache hit, returns a reconstructed APIResult and makes NO API call (so
    it never counts against the spend ceiling). On a miss, calls call_claude and
    stores the serialisable result under cache_key.

    This is opt-in: it uses the existing CacheManager.get/set unchanged and does
    not alter any stage's own cache checks. Pass cache=None to bypass caching
    entirely (equivalent to calling call_claude directly).

    Args:
        cache:        a CacheManager (or None to skip caching).
        cache_key:    relative cache key, e.g. "llm/nm/<community>/<stage>.json".
        ttl_days:     optional freshness window passed through to CacheManager.get.
        **call_kwargs: forwarded verbatim to call_claude (model, system, user, ...).
    """
    if cache is not None:
        cached = cache.get(cache_key, ttl_days=ttl_days)
        if cached is not None:
            logger.debug(
                "[%s] LLM cache hit (%s) — no API call",
                call_kwargs.get("stage", ""), cache_key,
            )
            return APIResult(**{f: cached.get(f) for f in _CACHED_RESULT_FIELDS})

    result = call_claude(**call_kwargs)

    # Only persist real, successful responses. Dry-run returns empty text (ok is
    # False), so this naturally skips caching empty backstop results.
    if cache is not None and result.ok:
        cache.set(cache_key, {f: getattr(result, f) for f in _CACHED_RESULT_FIELDS})

    return result


# ─────────────────────────────────────────────
# JSON PARSING
# ─────────────────────────────────────────────

def _parse_json_response(
    text: str, schema: Optional[dict] = None
) -> tuple[Optional[dict], Optional[str]]:
    """
    Parse JSON from model output. Handles markdown fencing and trailing artifacts.
    Returns (parsed_dict_or_None, error_message_or_None).

    If `schema` is provided, the parsed dict is validated against it. Validation
    failures are logged as warnings and do NOT block (parsed dict is still
    returned) — this is infrastructure for future enforcement.
    """
    cleaned = text.strip()

    # Strip leading ```json / ``` fence by skipping the opening fence line entirely,
    # regardless of language tag or whether the closing fence is present (truncated responses).
    if cleaned.startswith("```"):
        first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
        cleaned = cleaned[first_newline:].strip()
        # Strip trailing ``` fence if the response was not truncated
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

    try:
        return _validate_parsed(json.loads(cleaned), schema)
    except json.JSONDecodeError as e:
        # Attempt to extract JSON object if there's surrounding text
        try:
            start = cleaned.index("{")
            end = cleaned.rindex("}") + 1
            return _validate_parsed(json.loads(cleaned[start:end]), schema)
        except (ValueError, json.JSONDecodeError):
            position = f"line {e.lineno} col {e.colno} (char {e.pos})"
            preview = cleaned[:500]
            return None, (
                f"JSON parse failed at {position}: {e.msg}. "
                f"Cleaned preview: {preview!r}"
            )


def _validate_parsed(
    parsed: dict, schema: Optional[dict]
) -> tuple[Optional[dict], Optional[str]]:
    """Validate `parsed` against `schema` if given. Returns (parsed, error).

    On schema validation failure the failure is surfaced as a structured error
    string (and parsed is returned as None) so the caller sees it via
    APIResult.parse_error and can skip that item gracefully. We never raise —
    a malformed response must not kill the whole run.
    """
    if schema is not None:
        import jsonschema
        try:
            jsonschema.validate(parsed, schema)
        except jsonschema.ValidationError as e:
            msg = f"LLM response failed schema validation: {e.message}"
            logger.warning(msg)
            return None, msg
    return parsed, None


# ─────────────────────────────────────────────
# PROMPT LOADER
# ─────────────────────────────────────────────

def load_prompt(prompt_path: str, substitutions: Optional[dict] = None) -> str:
    """
    Load a prompt from the prompts/ directory and apply substitutions.
    
    Substitutions replace {{KEY}} placeholders with values.
    Example: load_prompt("prompts/extract/s3_community_facts.md",
                         {"community_name": "Albuquerque", "state": "NM"})
    """
    with open(prompt_path) as f:
        text = f.read()

    if substitutions:
        for key, value in substitutions.items():
            placeholder = "{{" + key + "}}"
            text = text.replace(placeholder, str(value))

    return text
