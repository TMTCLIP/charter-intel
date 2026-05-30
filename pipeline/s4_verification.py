"""
pipeline/s4_verification.py
Stage 4: Verification and Confidence Tagging

Two-pass verification:
  Pass A — Deterministic: URL pattern matching against sources.yaml
  Pass B — LLM (Haiku): Classify remaining facts and set in_main_analysis

INPUT:  data/cache/community/{state}/{community_id}/s3_facts_raw.json
OUTPUT: data/cache/community/{state}/{community_id}/s4_verified.json
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

from pipeline import PipelineConfig, StageResult, StageStatus, today_str
from pipeline.utils.api_client import call_claude, load_prompt
from pipeline.utils.cache import CacheManager

STAGE_ID = "s4_verification"

# Source classes that block in_main_analysis regardless of confidence
BLOCKED_SOURCE_CLASSES = {"ADVOCACY", "SELF_REPORTED", "UNVERIFIED"}


def run(
    community_id: str,
    state: str,
    config: PipelineConfig,
    previous_result: Optional[StageResult] = None,
    **kwargs
) -> StageResult:
    start = time.time()

    cache = CacheManager(config)
    cache_key = f"community/{state.lower()}/{community_id}/s4_verified.json"

    if config.cache_enabled and not config.force_refresh:
        cached = cache.get(cache_key)
        if cached:
            s3_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
            s4_path = f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"
            if (
                os.path.exists(s3_path)
                and os.path.exists(s4_path)
                and os.path.getmtime(s3_path) > os.path.getmtime(s4_path)
            ):
                logger.info(
                    "[%s] S4 cache predates S3 output — invalidating to propagate fresh facts",
                    community_id,
                )
                # Fall through to full re-run
            else:
                return StageResult(
                    stage_id=STAGE_ID, community_id=community_id, state=state,
                    status=StageStatus.SUCCESS, output_data=cached,
                    cache_hit=True, duration_seconds=round(time.time() - start, 2)
                )

    # Load S3 output
    if previous_result and previous_result.output_data:
        raw_bundle = previous_result.output_data
    else:
        raw_path = f"data/cache/community/{state.lower()}/{community_id}/s3_facts_raw.json"
        if not os.path.exists(raw_path):
            return StageResult(
                stage_id=STAGE_ID, community_id=community_id, state=state,
                status=StageStatus.ERROR,
                errors=[f"S3 output not found at {raw_path}. Run S3 first."]
            )
        with open(raw_path) as f:
            raw_bundle = json.load(f)

    # Load source patterns from config
    with open("config/sources.yaml") as f:
        sources_cfg = yaml.safe_load(f)
    url_patterns = sources_cfg.get("url_patterns", [])
    source_classes = sources_cfg.get("source_classes", {})

    # Pass A: Deterministic URL classification
    facts = raw_bundle.get("facts", [])
    facts = [_classify_by_url(f, url_patterns) for f in facts]

    # Pass B: LLM confidence tagging (Haiku)
    if not config.dry_run:
        sources_summary = _build_sources_summary(source_classes)
        prompt_text = load_prompt("prompts/audit/s4_verification_tags.md", {
            "FACTS_JSON": json.dumps(facts, indent=2),
            "COMMUNITY_ID": community_id,
            "TODAY_DATE": today_str(),
            "SOURCES_YAML_SUMMARY": sources_summary,
        })
        result = call_claude(
            model=config.model_haiku,
            system="You are a verification classifier. Respond ONLY with valid JSON.",
            user=prompt_text,
            max_tokens=1000,
            temperature=0.0,
            expect_json=True,
            stage=STAGE_ID,
            community_id=community_id
        )
        if not result.parse_error and result.parsed_json:
            facts = result.parsed_json.get("facts", facts)
        tokens_used = result.total_tokens if not config.dry_run else 0
    else:
        tokens_used = 0

    # Enforce in_main_analysis rules deterministically (non-negotiable)
    facts = [_enforce_main_analysis_rules(f) for f in facts]

    verified_bundle = {
        "community_id": community_id,
        "state": state,
        "verified_at": today_str(),
        "facts": facts,
        "summary": _build_summary(facts)
    }

    out_path = f"data/cache/community/{state.lower()}/{community_id}/s4_verified.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(verified_bundle, f, indent=2)

    cache.set(cache_key, verified_bundle)

    return StageResult(
        stage_id=STAGE_ID, community_id=community_id, state=state,
        status=StageStatus.SUCCESS, output_path=out_path,
        output_data=verified_bundle, tokens_used=tokens_used,
        duration_seconds=round(time.time() - start, 2)
    )


def _classify_by_url(fact: dict, url_patterns: list[dict]) -> dict:
    """Infer source_class from URL before LLM pass."""
    url = fact.get("source_url") or ""
    for pattern in url_patterns:
        if pattern["pattern"] in url:
            fact = dict(fact)
            fact["source_class"] = pattern["source_class"]
            if "bias_direction" in pattern:
                fact["bias_flag"] = pattern["bias_direction"]
            return fact
    return fact


def _enforce_main_analysis_rules(fact: dict) -> dict:
    """Hard rule: certain source classes or low confidence always blocks in_main_analysis."""
    fact = dict(fact)
    # Use None as default (not "UNVERIFIED" / "NONE") so that facts whose
    # source_class or confidence were never set by Pass A or Pass B are not
    # silently blocked. The original defaults caused false positives: a fact
    # whose URL didn't match any pattern AND whose LLM classification was
    # absent would default to UNVERIFIED/NONE and be incorrectly excluded.
    source_class = fact.get("source_class")
    confidence = fact.get("confidence")

    if source_class in BLOCKED_SOURCE_CLASSES or confidence in ("LOW", "NONE"):
        fact["in_main_analysis"] = False
        if not fact.get("needs_verification_reason"):
            fact["needs_verification_reason"] = (
                f"Blocked: source_class={source_class}, confidence={confidence}"
            )
    return fact


def _build_summary(facts: list[dict]) -> dict:
    return {
        "total_facts": len(facts),
        "in_main_analysis": sum(1 for f in facts if f.get("in_main_analysis")),
        "routed_to_verification": sum(1 for f in facts if not f.get("in_main_analysis")),
        "high_confidence": sum(1 for f in facts if f.get("confidence") == "HIGH"),
        "moderate_confidence": sum(1 for f in facts if f.get("confidence") == "MODERATE"),
        "low_confidence": sum(1 for f in facts if f.get("confidence") in ("LOW", "NONE")),
    }


def _build_sources_summary(source_classes: dict) -> str:
    lines = []
    for name, cfg in source_classes.items():
        lines.append(
            f"{name}: default_confidence={cfg['confidence_default']}, "
            f"allows_in_main={cfg['allows_in_main']}, "
            f"requires_url={cfg['requires_url']}"
        )
    return "\n".join(lines)
