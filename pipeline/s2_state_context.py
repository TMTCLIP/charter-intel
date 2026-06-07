"""
pipeline/s2_state_context.py
Stage 2: State Context Pack Generation

Calls Claude Opus once to generate a structured state-level reference pack.
Output is cached and reused across all community analyses.

Cache is event-driven: regeneration happens only when
  (a) the cache file is missing,
  (b) --force is passed, or
  (c) the Notion Signal table has an uncleared s2_cache_bust signal for the state.
The 90-day TTL timer has been removed.

INPUT:  config/states.yaml (source URLs for this state)
OUTPUT: data/cache/state/{state}/s2_state_context.json
"""
from __future__ import annotations
import json
import logging
import os
import time
from typing import Optional, Tuple

import yaml

from pipeline import PipelineConfig, StageResult, StageStatus, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema

log = logging.getLogger(__name__)

STAGE_ID = "s2_state_context"


def _should_regenerate(
    state: str,
    cache_path: str,
    force: bool,
) -> Tuple[bool, Optional[str]]:
    """
    Returns (should_regenerate, signal_id_to_clear).

    Priority order:
      1. Cache file does not exist → (True, None)
      2. --force flag passed → (True, None)
      3. Notion Signal table has an uncleared s2_cache_bust signal for this
         state → (True, signal_id)
      4. All other cases → (False, None)

    If the Notion query fails for any reason: log warning, return (False, None).
    Never raises.
    """
    if not os.path.exists(cache_path):
        log.info("S2 [%s]: cache file absent — will regenerate", state)
        return True, None

    if force:
        log.info("S2 [%s]: --force — will regenerate", state)
        return True, None

    # Check Notion Signal table for uncleared s2_cache_bust signals.
    # Lazy import so S2 stays importable even when notion-client is not installed.
    try:
        from pipeline.layer2.notion_client import NotionSignalStore
        store = NotionSignalStore()
        signals = store.get_s2_cache_bust_signals(state)
        if signals:
            sig_id = signals[0].get("signal_id")
            log.info(
                "S2 [%s]: cache bust signal found (id=%s) — will regenerate",
                state, sig_id,
            )
            return True, sig_id
        log.info("S2 [%s]: no cache bust signals — using cache", state)
    except Exception as exc:
        log.warning(
            "S2 [%s]: Notion signal check failed (%s) — using cache as-is",
            state, exc,
        )

    return False, None


def run(
    community_id: str,  # unused — this stage is state-level
    state: str,
    config: PipelineConfig,
    **kwargs
) -> StageResult:
    start = time.time()

    cache = CacheManager(config)
    cache_key = f"state/{state.lower()}/s2_state_context.json"
    cache_path = os.path.join("data/cache", cache_key)

    signal_id_to_clear: Optional[str] = None

    if config.cache_enabled:
        should_regen, signal_id_to_clear = _should_regenerate(
            state, cache_path, config.force_refresh
        )
        if not should_regen:
            try:
                with open(cache_path) as f:
                    cached = json.load(f)
                return StageResult(
                    stage_id=STAGE_ID, community_id="ALL", state=state,
                    status=StageStatus.SUCCESS, output_data=cached,
                    cache_hit=True, duration_seconds=round(time.time() - start, 2)
                )
            except Exception as exc:
                log.warning(
                    "S2 [%s]: cache read failed (%s) — regenerating", state, exc
                )

    if config.dry_run:
        return StageResult(
            stage_id=STAGE_ID, community_id="ALL", state=state,
            status=StageStatus.SKIPPED,
            warnings=["Dry run — skipping Opus API call for S2"]
        )

    # Load state sources from config
    with open("config/states.yaml") as f:
        states_cfg = yaml.safe_load(f)
    state_cfg = states_cfg.get(state, {})
    sources = state_cfg.get("data_sources", {})

    prompt_text = load_prompt("prompts/extract/s2_state_context.md", {
        "STATE_NAME": state_cfg.get("name", state),
        "STATE_CODE": state,
        "SOURCES_JSON": json.dumps(sources, indent=2),
        "TODAY_DATE": today_str(),
    })

    result = call_claude(
        model=config.model_opus,
        system="You are an education policy analyst. Respond ONLY with valid JSON.",
        user=prompt_text,
        max_tokens=4000,
        temperature=0.1,
        expect_json=True,
        stage=STAGE_ID,
        community_id="ALL"
    )

    if result.parse_error:
        return StageResult(
            stage_id=STAGE_ID, community_id="ALL", state=state,
            status=StageStatus.ERROR,
            errors=[f"S2 JSON parse failed: {result.parse_error}"]
        )

    out_path = f"data/cache/state/{state.lower()}/s2_state_context.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result.parsed_json, f, indent=2)

    cache.set(cache_key, result.parsed_json)

    # Mark the Notion signal as cleared now that regeneration succeeded.
    if signal_id_to_clear:
        try:
            from pipeline.layer2.notion_client import NotionSignalStore
            NotionSignalStore().update_signal(signal_id_to_clear, {"status": "expired"})
            log.info(
                "S2 [%s]: cleared cache bust signal %s", state, signal_id_to_clear
            )
        except Exception as exc:
            log.warning(
                "S2 [%s]: could not clear signal %s — %s",
                state, signal_id_to_clear, exc,
            )

    return StageResult(
        stage_id=STAGE_ID, community_id="ALL", state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        output_data=result.parsed_json, tokens_used=result.total_tokens,
        duration_seconds=round(time.time() - start, 2)
    )
