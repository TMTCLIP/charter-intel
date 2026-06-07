"""
pipeline/utils/charter_law.py
Shared loader for statutory charter-law barriers (config-read only).

Barriers are human-verified statutory conditions encoded in
config/charter_law/{state}.yaml. This module is the single source of truth used
by S2 (to carry barriers into the state-context bundle) and S5 (to evaluate
them). It never calls an LLM or web search — it only reads YAML.

Each barrier dict carries a `severity`:
  - "prohibition"      → a charter cannot be authorized at all (hard override).
  - "consent_required" → authorization is permitted but requires an extra
                         approval step (e.g. local school-board endorsement).
                         Advisory only — never zeroes the score.
"""
from __future__ import annotations

import logging
import os
from typing import List

import yaml

log = logging.getLogger(__name__)

_CONFIG_DIR = "config/charter_law"


def load_charter_law_barriers(state: str, config_dir: str = _CONFIG_DIR) -> List[dict]:
    """Return the list of statutory barriers for *state*.

    Reads config/charter_law/{state}.yaml. Returns [] (graceful degradation) when
    the file is missing, unreadable, or has no barriers. Never raises.
    """
    path = os.path.join(config_dir, f"{state.lower()}.yaml")
    if not os.path.exists(path):
        log.info("charter_law: no config for state %s (%s) — no barriers", state, path)
        return []
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never crash scoring
        log.warning("charter_law: failed to read %s (%s) — treating as no barriers", path, exc)
        return []

    barriers = data.get("barriers")
    if not isinstance(barriers, list):
        return []
    # Keep only well-formed barrier dicts that declare an id and a severity.
    return [b for b in barriers if isinstance(b, dict) and b.get("id") and b.get("severity")]
