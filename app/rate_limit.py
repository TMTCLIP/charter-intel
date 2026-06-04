"""
app/rate_limit.py — filesystem-only abuse guard for the Streamlit app.

Protects API credits without blocking legitimate statewide scans. Three layers:
  1. Daily estimated cost cap (primary — directly protects spend)
  2. Daily weighted job limit
  3. Per-session job limit
Dry-run / mock scans are exempt (zero API cost). Statewide (--all) scans weigh
the number of communities they cover (derived from states.yaml).

No pipeline imports, no external deps. Reads only meta.json files written by
runner.py. Malformed / missing meta.json is skipped silently (graceful).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
from pathlib import Path

import yaml

import config

logger = logging.getLogger(__name__)

# Conservative weight used when the statewide community count cannot be
# determined from states.yaml (state missing, file unreadable, empty list).
_STATEWIDE_FALLBACK_WEIGHT = 20


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────
def is_exempt(flags: dict) -> bool:
    """True if dry_run or mock is set — exempt from all limits (zero API cost)."""
    flags = flags or {}
    return any(bool(flags.get(f)) for f in config.RATE_LIMIT_FREE_FLAGS)


def get_job_weight(flags: dict) -> int:
    """Weight of a job: 1 for a single city, the statewide community count for --all.

    A statewide (--all) scan runs every community in the state, so its cost must
    reflect that count rather than a flat constant (otherwise the daily cost cap
    is undercounted). The count is derived from states.yaml; if it cannot be
    determined, fall back to a conservative weight.
    """
    flags = flags or {}
    if not flags.get("all"):
        return 1
    return _statewide_weight(flags.get("state", ""))


def _statewide_weight(state: str) -> int:
    """Number of communities a --all scan covers for `state`, per states.yaml.

    states.yaml has no explicit community list; the authoritative per-community
    mapping is `nces_district_map`. We count its entries minus any in
    `excluded_communities`. Returns _STATEWIDE_FALLBACK_WEIGHT (with a warning)
    if the state is missing or the file is unreadable.
    """
    try:
        states_path = config.REPO_ROOT / "config" / "states.yaml"
        cfg = yaml.safe_load(states_path.read_text())
        state_cfg = (cfg or {}).get((state or "").upper())
        if not isinstance(state_cfg, dict):
            logger.warning(
                "states.yaml has no entry for state %r; using fallback weight %d",
                state, _STATEWIDE_FALLBACK_WEIGHT,
            )
            return _STATEWIDE_FALLBACK_WEIGHT
        communities = set((state_cfg.get("nces_district_map") or {}).keys())
        excluded = set(state_cfg.get("excluded_communities") or [])
        count = len(communities - excluded)
        return max(1, count) if count else _STATEWIDE_FALLBACK_WEIGHT
    except (OSError, yaml.YAMLError) as e:
        logger.warning(
            "Could not read states.yaml (%s); using fallback weight %d",
            e, _STATEWIDE_FALLBACK_WEIGHT,
        )
        return _STATEWIDE_FALLBACK_WEIGHT


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem-backed per-session counter
#
# Each browser session gets a uuid token (held in st.session_state) and a
# {token}.json file under runs/.rate_limit/. A refresh/new tab mints a new token
# → a fresh per-session counter (preserves prior UX), but the global daily cap
# below sums every session file from today, so refresh cannot bypass the day cap.
# ─────────────────────────────────────────────────────────────────────────────
_RATE_LIMIT_SUBDIR = ".rate_limit"
_SESSION_RETENTION_HOURS = 48


def _rate_limit_dir(runs_dir: Path) -> Path:
    return Path(runs_dir) / _RATE_LIMIT_SUBDIR


def _session_file(runs_dir: Path, token: str) -> Path:
    return _rate_limit_dir(runs_dir) / f"{token}.json"


def cleanup_sessions(runs_dir: Path) -> None:
    """Delete session counter files older than 48h. Best-effort, never raises."""
    d = _rate_limit_dir(runs_dir)
    if not d.exists():
        return
    cutoff = _dt.datetime.now().astimezone() - _dt.timedelta(hours=_SESSION_RETENTION_HOURS)
    for f in d.glob("*.json"):
        started = None
        try:
            started = _parse_dt(json.loads(f.read_text()).get("started"))
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            pass
        if started is None:
            try:
                started = _dt.datetime.fromtimestamp(f.stat().st_mtime).astimezone()
            except OSError:
                continue
        if started < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


def get_session_count(runs_dir: Path, token: str) -> int:
    """Jobs recorded for this session token (0 if missing / unreadable)."""
    try:
        data = json.loads(_session_file(runs_dir, token).read_text())
        return int(data.get("jobs", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return 0


def record_job(runs_dir: Path, token: str, weight: int) -> None:
    """Increment this session's job + cost_weight counters on disk."""
    path = _session_file(runs_dir, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError, ValueError):
        data = {}
    data["jobs"] = int(data.get("jobs", 0)) + 1
    data["cost_weight"] = int(data.get("cost_weight", 0)) + int(weight)
    data.setdefault("started", _dt.datetime.now().astimezone().isoformat())
    path.write_text(json.dumps(data))


def check_global_daily_cap(runs_dir: Path) -> tuple[bool, str]:
    """Block if total jobs across all session files started today (UTC) exceed the cap.

    Uses RATE_LIMIT_DAILY_MAX as the threshold.  All date comparisons are done
    in UTC so the result is identical regardless of the server's local timezone.
    """
    d = _rate_limit_dir(runs_dir)
    if not d.exists():
        return True, ""
    today_utc = _utc_today_date()
    total = 0
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            started = _parse_dt(data.get("started"))
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            continue
        if started is not None and started.astimezone(_dt.timezone.utc).date() == today_utc:
            total += int(data.get("jobs", 0))
    if total > config.RATE_LIMIT_DAILY_MAX:
        return False, "Daily scan limit reached across all sessions."
    return True, ""


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
    """Summary for display. Block priority: cost cap > global daily > daily jobs > session."""
    s_ok, s_msg = check_session_limit(session_count)
    d_ok, d_msg, daily_used = check_daily_limit(runs_dir)
    c_ok, c_msg, spend = check_cost_cap(runs_dir)
    g_ok, g_msg = check_global_daily_cap(runs_dir)

    block_reason = None
    if not c_ok:
        block_reason = c_msg
    elif not g_ok:
        block_reason = g_msg
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
        "is_blocked": not (s_ok and d_ok and c_ok and g_ok),
        "block_reason": block_reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────
def _utc_today_start() -> _dt.datetime:
    """Return midnight UTC of the current calendar day (timezone-aware)."""
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    return now_utc.replace(hour=0, minute=0, second=0, microsecond=0)


def _utc_today_date() -> _dt.date:
    """Return today's date in UTC — isolated for testability."""
    return _dt.datetime.now(_dt.timezone.utc).date()


def _window_entries(runs_dir: Path) -> list[tuple[_dt.datetime, int]]:
    """(started_dt, weight) for non-exempt runs started since UTC midnight today.

    Malformed / missing meta.json and unparseable timestamps are skipped silently.
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []

    cutoff = _utc_today_start()

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
        dt = dt.replace(tzinfo=_dt.timezone.utc)  # treat naive timestamps as UTC
    return dt


def _utc_now() -> _dt.datetime:
    """Return current UTC datetime — isolated for testability."""
    return _dt.datetime.now(_dt.timezone.utc)


def _hours_until_reset(_entries=None) -> int:
    """Hours until the next UTC midnight (when the daily window resets)."""
    now_utc = _utc_now()
    midnight_utc = (now_utc + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    remaining = (midnight_utc - now_utc).total_seconds() / 3600.0
    return max(1, math.ceil(remaining))
