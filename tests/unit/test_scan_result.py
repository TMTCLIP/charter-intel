"""
tests/unit/test_scan_result.py
GET /api/scan/result/<job_id> — complete, incomplete, and missing-file cases.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app" / "ui"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))

import server as srv


@pytest.fixture(autouse=True)
def clear_jobs():
    with srv._jobs_lock:
        srv._jobs.clear()
    yield
    with srv._jobs_lock:
        srv._jobs.clear()


@pytest.fixture()
def client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c


def _seed(job_id: str, status: str, brief_path: str | None = None) -> None:
    with srv._jobs_lock:
        srv._jobs[job_id] = {
            "job_id": job_id,
            "community_id": "nm-questa",
            "depth": "standard",
            "preset": "growth",
            "mode": "2",
            "status": status,
            "started_at": "2026-01-01T00:00:00",
            "finished_at": "2026-01-01T00:05:00" if status in ("complete", "failed") else None,
            "stage_log": ["line 1", "line 2"],
            "error": "oops" if status == "failed" else None,
            "brief_path": brief_path,
        }


# ── Status endpoint ─────────────────────────────────────────────────────────

def test_status_unknown_job_404(client):
    resp = client.get("/api/scan/status/nope")
    assert resp.status_code == 404


def test_status_returns_full_record(client):
    _seed("s1", "running")
    resp = client.get("/api/scan/status/s1")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "running"
    assert data["community_id"] == "nm-questa"
    assert data["stage_log"] == ["line 1", "line 2"]


def test_status_complete(client):
    _seed("s2", "complete", brief_path="/tmp/brief.html")
    data = client.get("/api/scan/status/s2").get_json()
    assert data["status"] == "complete"
    assert data["brief_path"] == "/tmp/brief.html"


def test_status_failed(client):
    _seed("s3", "failed")
    data = client.get("/api/scan/status/s3").get_json()
    assert data["status"] == "failed"
    assert data["error"] == "oops"


# ── Result endpoint ─────────────────────────────────────────────────────────

def test_result_unknown_job_404(client):
    resp = client.get("/api/scan/result/nope")
    assert resp.status_code == 404


def test_result_incomplete_job_409(client):
    _seed("r1", "running")
    resp = client.get("/api/scan/result/r1")
    assert resp.status_code == 409
    assert "not complete" in resp.get_json()["error"]


def test_result_failed_job_409(client):
    _seed("r2", "failed")
    resp = client.get("/api/scan/result/r2")
    assert resp.status_code == 409


def test_result_complete_with_existing_brief(client, tmp_path):
    brief = tmp_path / "brief.html"
    brief.write_text("<html>hello</html>")
    _seed("r3", "complete", brief_path=str(brief))
    resp = client.get("/api/scan/result/r3")
    assert resp.status_code == 200
    assert b"hello" in resp.data
    assert "text/html" in resp.content_type


def test_result_complete_brief_path_missing_from_disk(client, tmp_path):
    _seed("r4", "complete", brief_path=str(tmp_path / "ghost.html"))
    resp = client.get("/api/scan/result/r4")
    assert resp.status_code == 404


def test_result_complete_no_brief_path_find_fallback(client, tmp_path):
    """brief_path=None on job; _find_brief_path should be called as fallback."""
    _seed("r5", "complete", brief_path=None)
    fake_path = tmp_path / "brief.html"
    fake_path.write_text("<html>fallback</html>")

    with patch.object(srv, "_find_brief_path", return_value=fake_path):
        resp = client.get("/api/scan/result/r5")

    assert resp.status_code == 200
    assert b"fallback" in resp.data


def test_result_complete_no_brief_path_find_returns_none(client):
    """brief_path=None on job and _find_brief_path returns None → 404."""
    _seed("r6", "complete", brief_path=None)

    with patch.object(srv, "_find_brief_path", return_value=None):
        resp = client.get("/api/scan/result/r6")

    assert resp.status_code == 404
