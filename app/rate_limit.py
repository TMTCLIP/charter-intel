"""
app/rate_limit.py — filesystem-only abuse guard for the Streamlit app.

Protects API credits without blocking legitimate statewide scans. Three layers:
  1. Daily estimated cost cap (primary — directly protects spend)
  2. Daily weighted job limit
  3. Per-session job limit
Dry-run / mock scans are exempt (zero API cost). Statewide (--all) scans weigh 3.

No pipeline imports, no external deps. Reads only meta.json files written by
runner.py. Malformed / missing meta.json is skipped silently (graceful).
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from pathlib import Path

import config


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────
def is_exempt(flags: dict) -> bool:
    """True if dry_run or mock is set — exempt from all limits (zero API cost)."""
    flags = flags or {}
    return any(bool(flags.get(f)) for f in config.RATE_LIMIT_FREE_FLAGS)


def get_job_weight(flags: dict) -> int:
    """3 for a statewide (--all) scan, 1 for a single city."""
    flags = flags or {}
    return config.RATE_LIMIT_STATEWIDE_WEIGHT if flags.get("all") else 1


# ─────────────────────────────────────────────────────────────────────────────
# Limit checks
# ─────────────────────────────────────────────────────────────────────────────
def check_session_limit(session_count: int) -> tuple[bool, str]:
    """(is_allowed, message). Blocks once this session's jobs hit the max."""
    if session_count >= config.RATE_LIMIT_SESSION_MAX:
        return False, (
            f"Session job limit reached ({session_count}/{config.RATE_LIMIT_SESSION_MAX}). "
            "Refresh to start a new session."
        )
    return True, ""


def check_daily_limit(runs_dir: Path) -> tuple[bool, str, int]:
    """(is_allowed, message, jobs_used_today). Weighted, exempt runs skipped."""
    entries = _window_entries(runs_dir)
    jobs_used = sum(weight for _started, weight in entries)
    if jobs_used >= config.RATE_LIMIT_DAILY_MAX:
        reset_h = _hours_until_reset(entries)
        return False, (
            f"Daily job limit reached ({jobs_used}/{config.RATE_LIMIT_DAILY_MAX}). "
            f"Resets in {reset_h} hours."
        ), jobs_used
    return True, "", jobs_used


def check_cost_cap(runs_dir: Path) -> tuple[bool, str, float]:
    """(is_allowed, message, estimated_spend_today). Exempt runs skipped."""
    entries = _window_entries(runs_dir)
    weight_total = sum(weight for _started, weight in entries)
    spend = round(weight_total * config.RATE_LIMIT_COST_PER_WEIGHT, 2)
    if spend >= config.RATE_LIMIT_DAILY_COST_CAP:
        return False, (
            f"Daily cost cap reached (est. ${spend:.2f} / "
            f"${config.RATE_LIMIT_DAILY_COST_CAP:.2f}). Resets tomorrow."
        ), spend
    return True, "", spend


def get_rate_limit_status(runs_dir: Path, session_count: int) -> dict:
    """Summary for display. Block reason priority: cost cap > daily jobs > session."""
    s_ok, s_msg = check_session_limit(session_count)
    d_ok, d_msg, daily_used = check_daily_limit(runs_dir)
    c_ok, c_msg, spend = check_cost_cap(runs_dir)

    block_reason = None
    if not c_ok:
        block_reason = c_msg
    elif not d_ok:
        block_reason = d_msg
    elif not s_ok:
        block_reason = s_msg

    return {
        "session_used": session_count,
        "session_max": config.RATE_LIMIT_SESSION_MAX,
        "daily_used": daily_used,
        "daily_max": config.RATE_LIMIT_DAILY_MAX,
        "estimated_spend": spend,
        "cost_cap": config.RATE_LIMIT_DAILY_COST_CAP,
        "is_blocked": not (s_ok and d_ok and c_ok),
        "block_reason": block_reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────
def _window_entries(runs_dir: Path) -> list[tuple[_dt.datetime, int]]:
    """(started_dt, weight) for non-exempt runs started within the rolling window.

    Malformed / missing meta.json and unparseable timestamps are skipped silently.
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []

    now = _dt.datetime.now().astimezone()
    cutoff = now - _dt.timedelta(hours=config.RATE_LIMIT_WINDOW_HOURS)

    entries: list[tuple[_dt.datetime, int]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            meta = json.loads((run_dir / config.META_NAME).read_text())
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        flags = meta.get("flags") or {}
        if is_exempt(flags):
            continue
        started = _parse_dt(meta.get("start_time"))
        if started is None or started < cutoff:
            continue
        entries.append((started, get_job_weight(flags)))
    return entries


def _parse_dt(value) -> _dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = _dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # assume local tz for naive timestamps
    return dt


def _hours_until_reset(entries: list[tuple[_dt.datetime, int]]) -> int:
    """Hours until the oldest in-window run ages out of the rolling window."""
    if not entries:
        return config.RATE_LIMIT_WINDOW_HOURS
    oldest = min(started for started, _weight in entries)
    now = _dt.datetime.now().astimezone()
    reset_at = oldest + _dt.timedelta(hours=config.RATE_LIMIT_WINDOW_HOURS)
    remaining = (reset_at - now).total_seconds() / 3600.0
    return max(1, math.ceil(remaining))
