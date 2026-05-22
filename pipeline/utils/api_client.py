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

logger = logging.getLogger(__name__)


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

    Returns:
        APIResult. Check result.ok before using output.
    """
    client = anthropic.Anthropic()  # Reads ANTHROPIC_API_KEY from environment

    last_error = None
    delay = retry_delay_seconds

    for attempt in range(retry_attempts):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}]
            )

            text = response.content[0].text if response.content else ""
            tokens_in = response.usage.input_tokens
            tokens_out = response.usage.output_tokens

            logger.info(
                f"[{stage}] [{community_id}] {model} "
                f"in={tokens_in} out={tokens_out} total={tokens_in + tokens_out}"
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

        except anthropic.RateLimitError as e:
            last_error = e
            logger.warning(
                f"[{stage}] Rate limit on attempt {attempt + 1}/{retry_attempts}. "
                f"Waiting {delay}s..."
            )
            time.sleep(delay)
            delay *= 2

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
# JSON PARSING
# ─────────────────────────────────────────────

def _parse_json_response(text: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Parse JSON from model output. Handles markdown fencing and trailing artifacts.
    Returns (parsed_dict_or_None, error_message_or_None).
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
        return json.loads(cleaned), None
    except json.JSONDecodeError as e:
        # Attempt to extract JSON object if there's surrounding text
        try:
            start = cleaned.index("{")
            end = cleaned.rindex("}") + 1
            return json.loads(cleaned[start:end]), None
        except (ValueError, json.JSONDecodeError):
            position = f"line {e.lineno} col {e.colno} (char {e.pos})"
            preview = cleaned[:500]
            return None, (
                f"JSON parse failed at {position}: {e.msg}. "
                f"Cleaned preview: {preview!r}"
            )


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
