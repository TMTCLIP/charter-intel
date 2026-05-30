"""
pipeline/s2_state_context.py
Stage 2: State Context Pack Generation

Calls Claude Opus once (per quarter) to generate a structured state-level
reference pack. Output is cached and reused across all community analyses.

INPUT:  config/states.yaml (source URLs for this state)
OUTPUT: data/cache/state/{state}/s2_state_context.json
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

import yaml

from pipeline import PipelineConfig, StageResult, StageStatus, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager, CACHE_TTL_DAYS
from pipeline.utils.schema_validator import validate_against_schema

STAGE_ID = "s2_state_context"


def run(
    community_id: str,  # unused — this stage is state-level
    state: str,
    config: PipelineConfig,
    **kwargs
) -> StageResult:
    start = time.time()

    cache = CacheManager(config)
    cache_key = f"state/{state.lower()}/s2_state_context.json"

    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key, ttl_days=CACHE_TTL_DAYS["s2_state_context"])
        if cached:
            return StageResult(
                stage_id=STAGE_ID, community_id="ALL", state=state,
                status=StageStatus.SUCCESS, output_data=cached,
                cache_hit=True, duration_seconds=round(time.time() - start, 2)
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

    return StageResult(
        stage_id=STAGE_ID, community_id="ALL", state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        output_data=result.parsed_json, tokens_used=result.total_tokens,
        duration_seconds=round(time.time() - start, 2)
    )
