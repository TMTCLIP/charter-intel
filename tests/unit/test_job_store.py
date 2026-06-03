"""
tests/unit/test_job_store.py
Concurrent job creation, status transitions, lock safety.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app" / "ui"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))

import server as srv


@pytest.fixture(autouse=True)
def clear_jobs():
    """Wipe the in-memory store before and after each test."""
    with srv._jobs_lock:
        srv._jobs.clear()
    yield
    with srv._jobs_lock:
        srv._jobs.clear()


def _make_job(job_id: str, community_id: str = "nm-questa") -> dict:
    return {
        "job_id": job_id,
        "community_id": community_id,
        "depth": "standard",
        "preset": "growth",
        "mode": "2",
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "stage_log": [],
        "error": None,
        "brief_path": None,
    }


# ── Basic store operations ──────────────────────────────────────────────────

def test_get_unknown_job_returns_none():
    assert srv._get_job("nope") is None


def test_get_job_returns_copy():
    with srv._jobs_lock:
        srv._jobs["abc"] = _make_job("abc")
    job = srv._get_job("abc")
    assert job is not None
    # Mutating the returned copy must not affect the store
    job["status"] = "mutated"
    assert srv._jobs["abc"]["status"] == "queued"


def test_update_job_single_field():
    with srv._jobs_lock:
        srv._jobs["j1"] = _make_job("j1")
    srv._update_job("j1", status="running")
    assert srv._jobs["j1"]["status"] == "running"


def test_update_job_multiple_fields():
    with srv._jobs_lock:
        srv._jobs["j2"] = _make_job("j2")
    srv._update_job("j2", status="complete", brief_path="/data/x.html")
    assert srv._jobs["j2"]["status"] == "complete"
    assert srv._jobs["j2"]["brief_path"] == "/data/x.html"


def test_update_nonexistent_job_is_noop():
    srv._update_job("ghost", status="running")  # must not raise


def test_append_stage_log():
    with srv._jobs_lock:
        srv._jobs["j3"] = _make_job("j3")
    srv._append_stage_log("j3", "line one")
    srv._append_stage_log("j3", "line two")
    assert srv._jobs["j3"]["stage_log"] == ["line one", "line two"]


def test_append_stage_log_nonexistent_is_noop():
    srv._append_stage_log("ghost", "ignored")  # must not raise


# ── Status transitions ──────────────────────────────────────────────────────

def test_queued_to_running_to_complete():
    with srv._jobs_lock:
        srv._jobs["flow"] = _make_job("flow")
    assert srv._jobs["flow"]["status"] == "queued"

    srv._update_job("flow", status="running", started_at="2026-01-01T00:00:00")
    assert srv._jobs["flow"]["status"] == "running"

    srv._update_job("flow", status="complete", finished_at="2026-01-01T00:05:00", brief_path="/p.html")
    assert srv._jobs["flow"]["status"] == "complete"
    assert srv._jobs["flow"]["brief_path"] == "/p.html"


def test_queued_to_failed():
    with srv._jobs_lock:
        srv._jobs["fail"] = _make_job("fail")
    srv._update_job("fail", status="failed", error="subprocess crashed")
    assert srv._jobs["fail"]["status"] == "failed"
    assert srv._jobs["fail"]["error"] == "subprocess crashed"


# ── Concurrent access ───────────────────────────────────────────────────────

def test_concurrent_job_creation():
    N = 50
    ids: list[str] = []
    errors: list[Exception] = []

    def _create(i):
        import secrets
        job_id = secrets.token_hex(8)
        with srv._jobs_lock:
            srv._jobs[job_id] = _make_job(job_id, community_id=f"nm-city-{i}")
        ids.append(job_id)

    threads = [threading.Thread(target=_create, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(ids) == N
    with srv._jobs_lock:
        assert len(srv._jobs) == N


def test_concurrent_log_appends():
    with srv._jobs_lock:
        srv._jobs["log_stress"] = _make_job("log_stress")

    N = 100

    def _append(i):
        srv._append_stage_log("log_stress", f"line {i}")

    threads = [threading.Thread(target=_append, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(srv._jobs["log_stress"]["stage_log"]) == N


def test_concurrent_updates_no_data_race():
    with srv._jobs_lock:
        srv._jobs["race"] = _make_job("race")

    def _update_status(status):
        for _ in range(20):
            srv._update_job("race", status=status)

    threads = [
        threading.Thread(target=_update_status, args=("running",)),
        threading.Thread(target=_update_status, args=("complete",)),
        threading.Thread(target=_update_status, args=("failed",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should be one of the three valid statuses — no corruption
    assert srv._jobs["race"]["status"] in ("running", "complete", "failed")
