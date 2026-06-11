"""
app/cache_utils.py — non-S2 cache bust for a community.

S2 reads static YAML from config/charter_law/ and is NEVER touched.
All deletions are scoped to data/cache/ for the specified community.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parent
_CACHE_ROOT = _REPO_ROOT / "data" / "cache"
_CONFIG_CHARTER_LAW = _REPO_ROOT / "config" / "charter_law"


def bust_community_cache(community_id: str, state: str, leaid: str) -> dict:
    """
    Delete all non-S2 cache files for a community.

    Returns:
        {
            "deleted":   [relative paths deleted],
            "skipped":   [paths that didn't exist],
            "protected": [paths explicitly not touched],
            "error":     None or error string
        }
    """
    deleted:   list[str] = []
    skipped:   list[str] = []
    protected: list[str] = []
    error:     str | None = None

    try:
        state_lower = state.lower().strip()
        leaid_str   = str(leaid).strip() if leaid else ""

        # Permanent guard — always noted in every call
        protected.append(str(_CONFIG_CHARTER_LAW.relative_to(_REPO_ROOT)))

        # 1. Community cache dir — all JSON files
        _delete_dir_json(
            _CACHE_ROOT / "community" / state_lower / community_id,
            deleted, skipped,
        )

        # 2. Synthesis cache dir — all JSON files
        _delete_dir_json(
            _CACHE_ROOT / "synthesis" / state_lower / community_id,
            deleted, skipped,
        )

        # 3. Fetcher cache — files whose path contains community_id OR leaid
        fetcher_dir = _CACHE_ROOT / "fetcher"
        if fetcher_dir.exists():
            for path in fetcher_dir.rglob("*"):
                if not path.is_file():
                    continue
                path_str = str(path)
                if community_id in path_str or (leaid_str and leaid_str in path_str):
                    _safe_delete(path, deleted, skipped)

    except Exception as exc:
        logger.error("[CACHE-BUST] unexpected error for %s: %s", community_id, exc, exc_info=True)
        error = str(exc)

    return {"deleted": deleted, "skipped": skipped, "protected": protected, "error": error}


# ── helpers ────────────────────────────────────────────────────────────────────

def _delete_dir_json(dir_path: Path, deleted: list[str], skipped: list[str]) -> None:
    if not dir_path.exists():
        try:
            rel = str(dir_path.relative_to(_REPO_ROOT))
        except ValueError:
            rel = str(dir_path)
        skipped.append(rel)
        return
    for path in sorted(dir_path.glob("*.json")):
        if path.is_file():
            _safe_delete(path, deleted, skipped)


def _safe_delete(path: Path, deleted: list[str], skipped: list[str]) -> None:
    # Absolute guard: never touch config/charter_law/
    if str(_CONFIG_CHARTER_LAW) in str(path):
        logger.warning("[CACHE-BUST] BLOCKED attempt to touch charter_law: %s", path)
        return
    try:
        rel = str(path.relative_to(_REPO_ROOT))
    except ValueError:
        rel = str(path)
    try:
        path.unlink()
        logger.info("[CACHE-BUST] deleted: %s", rel)
        deleted.append(rel)
    except FileNotFoundError:
        skipped.append(rel)
    except Exception as exc:
        logger.error("[CACHE-BUST] failed to delete %s: %s", rel, exc)
        skipped.append(rel)
