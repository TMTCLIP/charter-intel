"""
Tests for app/rate_limit.py — midnight reset and UTC consistency.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make app/ importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "app"))

import rate_limit


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
UTC = _dt.timezone.utc


def _utc(year, month, day, hour=0, minute=0, second=0):
    return _dt.datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _make_meta(runs_dir: Path, run_id: str, start_time: _dt.datetime, weight: int = 1):
    """Write a fake meta.json into runs_dir/run_id/."""
    run_path = runs_dir / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    meta = {
        "start_time": start_time.isoformat(),
        "flags": {},
        "weight": weight,
    }
    (run_path / "meta.json").write_text(json.dumps(meta))


def _make_session_file(runs_dir: Path, token: str, jobs: int, started: _dt.datetime):
    d = runs_dir / ".rate_limit"
    d.mkdir(parents=True, exist_ok=True)
    data = {"jobs": jobs, "started": started.isoformat()}
    (d / f"{token}.json").write_text(json.dumps(data))


# ─────────────────────────────────────────────────────────────────────────────
# _parse_dt
# ─────────────────────────────────────────────────────────────────────────────
def test_parse_dt_aware_preserved():
    iso = "2026-06-03T10:00:00+05:30"
    dt = rate_limit._parse_dt(iso)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == _dt.timedelta(hours=5, minutes=30)


def test_parse_dt_naive_treated_as_utc():
    """Naive timestamps must be interpreted as UTC, not server local time."""
    iso = "2026-06-03T10:00:00"
    dt = rate_limit._parse_dt(iso)
    assert dt is not None
    assert dt.tzinfo == UTC
    assert dt == _utc(2026, 6, 3, 10, 0, 0)


def test_parse_dt_invalid_returns_none():
    assert rate_limit._parse_dt(None) is None
    assert rate_limit._parse_dt("not-a-date") is None
    assert rate_limit._parse_dt(12345) is None


# ─────────────────────────────────────────────────────────────────────────────
# _window_entries — midnight reset
# ─────────────────────────────────────────────────────────────────────────────
def test_window_entries_resets_at_utc_midnight(tmp_path):
    """Jobs from before UTC midnight must not count; jobs after must count."""
    # Fix "now" to 2026-06-03 01:00:00 UTC so UTC midnight was at 00:00
    now_utc = _utc(2026, 6, 3, 1, 0, 0)

    yesterday_job = _utc(2026, 6, 2, 23, 59, 0)  # before UTC midnight → excluded
    today_job = _utc(2026, 6, 3, 0, 30, 0)       # after UTC midnight  → included

    _make_meta(tmp_path, "run-yesterday", yesterday_job)
    _make_meta(tmp_path, "run-today", today_job)

    with patch("rate_limit._utc_today_start", return_value=_utc(2026, 6, 3, 0, 0, 0)):
        entries = rate_limit._window_entries(tmp_path)

    started_times = [s for s, _w in entries]
    assert today_job in started_times
    assert yesterday_job not in started_times


def test_window_entries_empty_when_no_runs(tmp_path):
    entries = rate_limit._window_entries(tmp_path)
    assert entries == []


def test_window_entries_excludes_exempt_runs(tmp_path):
    """dry_run / mock runs must not be counted even if they fall within today."""
    run_path = tmp_path / "dry-run-001"
    run_path.mkdir()
    meta = {
        "start_time": _utc(2026, 6, 3, 0, 30, 0).isoformat(),
        "flags": {"dry_run": True},
    }
    (run_path / "meta.json").write_text(json.dumps(meta))

    with patch("rate_limit._utc_today_start", return_value=_utc(2026, 6, 3, 0, 0, 0)):
        entries = rate_limit._window_entries(tmp_path)

    assert entries == []


# ─────────────────────────────────────────────────────────────────────────────
# check_global_daily_cap — UTC consistency
# ─────────────────────────────────────────────────────────────────────────────
def test_global_daily_cap_uses_utc_not_server_local(tmp_path):
    """A session written with a non-UTC offset should be compared against UTC date.

    Timestamp 2026-06-02T23:30:00-02:00 == 2026-06-03T01:30:00Z → UTC June 3.
    The global cap must treat this as a today job on June 3 UTC.
    """
    ts_with_offset = "2026-06-02T23:30:00-02:00"
    _make_session_file(tmp_path, "sess-1", jobs=5,
                       started=_dt.datetime.fromisoformat(ts_with_offset))

    # Tell rate_limit "today UTC" is June 3 → the session counts as today
    with patch("rate_limit._utc_today_date", return_value=_dt.date(2026, 6, 3)):
        ok, _ = rate_limit.check_global_daily_cap(tmp_path)
    # 5 jobs < RATE_LIMIT_DAILY_MAX (20) → allowed
    assert ok


def test_global_daily_cap_yesterday_session_not_counted(tmp_path):
    """Session files from UTC yesterday must not count toward today's cap."""
    yesterday_utc = _utc(2026, 6, 2, 12, 0, 0)
    _make_session_file(tmp_path, "old-sess", jobs=25, started=yesterday_utc)

    # Today is June 3 UTC; the session was June 2 UTC → should not count
    with patch("rate_limit._utc_today_date", return_value=_dt.date(2026, 6, 3)):
        ok, msg = rate_limit.check_global_daily_cap(tmp_path)
    assert ok, f"Expected allowed but got: {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# _hours_until_reset
# ─────────────────────────────────────────────────────────────────────────────
def test_hours_until_reset_near_midnight():
    """At 23:30 UTC, reset should be ~1 hour away (rounds up to 1)."""
    with patch("rate_limit._utc_now", return_value=_utc(2026, 6, 3, 23, 30, 0)):
        h = rate_limit._hours_until_reset()
    assert h == 1


def test_hours_until_reset_early_morning():
    """At 01:00 UTC, reset should be ~23 hours away."""
    with patch("rate_limit._utc_now", return_value=_utc(2026, 6, 3, 1, 0, 0)):
        h = rate_limit._hours_until_reset()
    assert h == 23


# ─────────────────────────────────────────────────────────────────────────────
# Midnight rollover integration
# ─────────────────────────────────────────────────────────────────────────────
def test_midnight_rollover_clears_daily_count(tmp_path):
    """After UTC midnight, yesterday's jobs must not count against today's limit."""
    # Write 15 jobs from yesterday
    for i in range(15):
        _make_meta(tmp_path, f"run-yesterday-{i}", _utc(2026, 6, 2, 22, 0, 0))

    # Now it's just after UTC midnight → cutoff is 2026-06-03 00:00 UTC
    with patch("rate_limit._utc_today_start", return_value=_utc(2026, 6, 3, 0, 0, 0)):
        ok, _msg, jobs_used = rate_limit.check_daily_limit(tmp_path)

    assert jobs_used == 0, f"Expected 0 jobs after midnight rollover, got {jobs_used}"
    assert ok


def test_within_day_jobs_accumulate(tmp_path):
    """Jobs after UTC midnight must be counted toward today's limit."""
    for i in range(5):
        _make_meta(tmp_path, f"run-today-{i}", _utc(2026, 6, 3, 0, 30, 0))

    with patch("rate_limit._utc_today_start", return_value=_utc(2026, 6, 3, 0, 0, 0)):
        ok, _msg, jobs_used = rate_limit.check_daily_limit(tmp_path)

    assert jobs_used == 5
    assert ok
