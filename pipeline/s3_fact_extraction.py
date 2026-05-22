"""
pipeline/s3_fact_extraction.py
Stage 3: Community Fact Extraction

Calls Claude Sonnet (temperature=0) to extract structured facts for all
10 scoring dimensions. Every fact requires a source URL or explicit NOT_FOUND.

INPUT:
  - data/cache/state/{state}/s2_state_context.json (state reference)
  - data/processed/{state}/community_list.json (known schools from S1)
OUTPUT:
  - data/cache/community/{state}/{community_id}/s3_facts_raw.json
"""
from __future__ import annotations
import json
import os
import time
from typing import Optional

from pipeline import PipelineConfig, StageResult, StageStatus, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager
from pipeline.utils.schema_validator import validate_against_schema

STAGE_ID = "s3_fact_extraction"


def run(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    **kwargs
) -> StageResult:
    start = time.time()

    cache = CacheManager(config)
    cache_key = f"community/{state.lower()}/{community_id}/s3_facts_raw.json"

    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.SUCCESS, output_data=cached,
                cache_hit=True, duration_seconds=round(time.time() - start, 2)
            )

    if config.dry_run:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.SKIPPED,
            warnings=["Dry run — skipping Sonnet API call for S3"]
        )

    # Load state context
    state_context_path = f"data/cache/state/{state.lower()}/s2_state_context.json"
    if not os.path.exists(state_context_path):
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"State context not found at {state_context_path}. Run S2 first."]
        )
    with open(state_context_path) as f:
        state_context = json.load(f)

    # Get known schools for this community from S1 output
    community_list_path = f"data/processed/{state.lower()}/community_list.json"
    known_schools = []
    if os.path.exists(community_list_path):
        with open(community_list_path) as f:
            cl = json.load(f)
        for c in cl.get("communities", []):
            if c["community_id"] == community_id:
                known_schools = c.get("school_names", [])
                break

    community_name = community_id.split("-", 1)[1].replace("-", " ").title()

    # Condense state context to reduce tokens (only governance + law + ecosystem)
    state_context_condensed = {
        "charter_law": state_context.get("charter_law", {}),
        "governance": state_context.get("governance", {}),
        "ecosystem": {
            "major_advocacy_orgs": state_context.get("ecosystem", {}).get("major_advocacy_orgs", [])[:3],
            "notable_operators": state_context.get("ecosystem", {}).get("notable_operators", [])[:5],
        }
    }

    prompt_text = load_prompt("prompts/extract/s3_community_facts.md", {
        "COMMUNITY_NAME": community_name,
        "STATE_NAME": state_context.get("state_name", state),
        "STATE_CODE": state,
        "COMMUNITY_ID": community_id,
        "TODAY_DATE": today_str(),
        "STATE_CONTEXT_JSON": json.dumps(state_context_condensed, indent=2),
        "KNOWN_SCHOOLS": json.dumps(known_schools, indent=2),
    })

    result = call_claude(
        model=config.model_sonnet,
        system="You are a structured data extraction agent. Respond ONLY with valid JSON.",
        user=prompt_text,
        max_tokens=8192,
        temperature=0.0,
        expect_json=True,
        stage=STAGE_ID,
        community_id=community_id
    )

    if result.parse_error:
        return StageResult(
            stage_id=STAGE_ID, community_id=community_id, state=state,
            status=StageStatus.ERROR,
            errors=[f"S3 JSON parse failed: {result.parse_error}"]
        )

    out_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result.parsed_json, f, indent=2)

    cache.set(cache_key, result.parsed_json)

    return StageResult(
        stage_id=STAGE_ID, community_id=community_id, state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        output_data=result.parsed_json, tokens_used=result.total_tokens,
        duration_seconds=round(time.time() - start, 2)
    )
