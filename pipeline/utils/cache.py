"""
pipeline/utils/cache.py
Cache manager for the charter intelligence pipeline.

Implements a three-tier cache:
  - state/    — quarterly refresh
  - community/ — quarterly refresh
  - synthesis/ — per-run, invalidated on community cache update

Keys are file paths under data/cache/{tier}/{state}/{community_id}/{stage}_{date}.json
Cache reads return the most recent matching file; writes create new dated files.

TTL POLICY (per-source, in days):
  Pass the appropriate constant to CacheManager.get(key, ttl_days=N) to enforce
  TTL. Expired reads are treated as cache misses; stale files are overwritten on
  fresh fetch (not deleted).
"""
from __future__ import annotations
import json
import os
import glob
import time
from typing import Optional

from pipeline import PipelineConfig

# ── TTL constants (days) ──────────────────────────────────────────────────────
# Define all per-source TTLs in one place.  Pass the appropriate constant when
# calling CacheManager.get() to enforce freshness.

CACHE_TTL_DAYS = {
    # Federal government data — annual release cadence
    "saipe":                 365,   # Census SAIPE school district poverty
    "nces_finance":          365,   # NCES CCD LEA finance (FY2023 CSV)
    "nces_lunch":            365,   # NCES CCD LEA lunch / FRL (FY2023 CSV)
    "nces_membership":       365,   # NCES CCD LEA membership / enrollment
    # Census ACS — 5-year rolling estimates, new vintage released each Dec
    "acs_district":          180,   # ACS B16004 ELL data
    "acs_population":        180,   # ACS B01003 total population
    # State context — quarterly refresh
    "s2_state_context":       90,   # S2 Claude state context pack
    "s1_ped_roster":          90,   # NM PED charter roster (S1 output)
    # Web search results — 30 days (politically volatile)
    "s3_political_climate":   30,   # political_climate Sonnet web search
    # TODO: "edfacts": 365,         # EDFacts IDEA child count (not yet integrated)
}


class CacheManager:
    def __init__(self, config: PipelineConfig):
        self.enabled = config.cache_enabled
        self.force_refresh = config.force_refresh
        self.base = "data/cache"

    def get(self, key: str, ttl_days: Optional[int] = None) -> Optional[dict]:
        """Return cached value for key, or None if not found / disabled / expired.

        Parameters
        ----------
        key      : path relative to data/cache/
        ttl_days : if set, treat file as expired when older than this many days.
                   On expiry, return None — the caller is responsible for
                   fetching fresh data and calling set() to overwrite.
        """
        if not self.enabled or self.force_refresh:
            return None
        path = os.path.join(self.base, key)
        if not os.path.exists(path):
            return None
        if ttl_days is not None:
            age_days = (time.time() - os.path.getmtime(path)) / 86400
            if age_days > ttl_days:
                return None   # expired — treat as miss; caller overwrites on fresh fetch
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def set(self, key: str, value: dict) -> None:
        """Write value to cache at key."""
        if not self.enabled:
            return
        path = os.path.join(self.base, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(value, f, indent=2)

    def invalidate(self, prefix: str) -> int:
        """Delete all cache files matching prefix. Returns count deleted."""
        pattern = os.path.join(self.base, prefix, "**", "*.json")
        files = glob.glob(pattern, recursive=True)
        for f in files:
            os.remove(f)
        return len(files)
