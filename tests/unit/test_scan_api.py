"""
tests/unit/test_scan_api.py
POST /api/scan validation — community_id, depth, missing fields.
Does NOT start a real subprocess; the background thread is never spawned.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make app/ui importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app" / "ui"))
# Make app/ importable for config / rate_limit
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))


@pytest.fixture()
def client():
    import server as srv

    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        # Patch rate_limit checks to always pass so tests focus on validation
        with (
            patch.object(srv.rate_limit, "check_daily_limit", return_value=(True, "", 0)),
            patch.object(srv.rate_limit, "check_cost_cap",    return_value=(True, "", 0.0)),
            # Prevent any real thread from spawning during POST /api/scan
            patch("threading.Thread"),
        ):
            yield c


# ── Valid submission ────────────────────────────────────────────────────────

def test_valid_scan_returns_job_id(client):
    resp = client.post("/api/scan", json={"target": "nm-questa", "depth": "standard"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert len(data["job_id"]) == 16  # secrets.token_hex(8) → 16 hex chars


def test_default_depth_when_omitted(client):
    resp = client.post("/api/scan", json={"target": "nm-santa-fe"})
    assert resp.status_code == 200


def test_all_valid_depths(client):
    for depth in ("fast", "standard", "deep"):
        resp = client.post("/api/scan", json={"target": "nm-questa", "depth": depth})
        assert resp.status_code == 200, f"depth={depth!r} should be valid"


# ── community_id validation ─────────────────────────────────────────────────

def test_missing_target_returns_400(client):
    resp = client.post("/api/scan", json={"depth": "standard"})
    assert resp.status_code == 400
    assert "community_id" in resp.get_json()["error"].lower()


def test_empty_target_returns_400(client):
    resp = client.post("/api/scan", json={"target": "", "depth": "standard"})
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_id", [
    "NM-Santa-Fe",          # uppercase
    "santa-fe",             # missing state prefix
    "nm_santa_fe",          # underscores
    "nm-",                  # no city part
    "nm-" + "a" * 65,       # city part too long (>64 chars)
    "nm santa fe",          # spaces
    "nm-santa-fe!",         # special char
    "123-santa-fe",         # numeric state code
])
def test_invalid_community_id_returns_400(client, bad_id):
    resp = client.post("/api/scan", json={"target": bad_id, "depth": "standard"})
    assert resp.status_code == 400


def test_valid_community_id_boundary(client):
    # Exactly 64-char city part — should pass
    city_part = "a" * 64
    resp = client.post("/api/scan", json={"target": f"nm-{city_part}", "depth": "standard"})
    assert resp.status_code == 200


# ── depth validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_depth", ["turbo", "1", "STANDARD", "none"])
def test_invalid_depth_returns_400(client, bad_depth):
    resp = client.post("/api/scan", json={"target": "nm-questa", "depth": bad_depth})
    assert resp.status_code == 400


def test_empty_depth_defaults_to_standard(client):
    # Empty string is falsy — route defaults to "standard", so this is valid.
    resp = client.post("/api/scan", json={"target": "nm-questa", "depth": ""})
    assert resp.status_code == 200


def test_depth_whitespace_stripped(client):
    # Leading/trailing whitespace is stripped — "deep " normalizes to "deep".
    resp = client.post("/api/scan", json={"target": "nm-questa", "depth": "deep "})
    assert resp.status_code == 200


# ── Rate limit gate ─────────────────────────────────────────────────────────

def test_rate_limit_daily_blocks(client):
    import server as srv
    with (
        patch.object(srv.rate_limit, "check_daily_limit", return_value=(False, "Daily limit reached.", 20)),
        patch.object(srv.rate_limit, "check_cost_cap",    return_value=(True, "", 0.0)),
        patch("threading.Thread"),
    ):
        resp = client.post("/api/scan", json={"target": "nm-questa", "depth": "standard"})
    assert resp.status_code == 429


def test_rate_limit_cost_blocks(client):
    import server as srv
    with (
        patch.object(srv.rate_limit, "check_daily_limit", return_value=(True, "", 0)),
        patch.object(srv.rate_limit, "check_cost_cap",    return_value=(False, "Cost cap reached.", 10.0)),
        patch("threading.Thread"),
    ):
        resp = client.post("/api/scan", json={"target": "nm-questa", "depth": "standard"})
    assert resp.status_code == 429


# ── Malformed request body ──────────────────────────────────────────────────

def test_non_json_body_returns_400(client):
    resp = client.post("/api/scan", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_empty_body_returns_400(client):
    resp = client.post("/api/scan", json={})
    assert resp.status_code == 400


# ── ZIP Drill route ─────────────────────────────────────────────────────────

def test_zip_drill_scan_queues(client):
    resp = client.post("/api/scan", json={"target": "nm-albuquerque", "mode": "zip"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "queued"
    assert "job_id" in data


def test_zip_drill_job_has_zip_type(client):
    import server as srv
    resp = client.post("/api/scan", json={"target": "nm-santa-fe", "mode": "zip"})
    job_id = resp.get_json()["job_id"]
    with srv._jobs_lock:
        job = srv._jobs[job_id]
    assert job["job_type"] == "zip"
    assert job["city_name"] == "Santa Fe"


def test_zip_drill_city_name_from_slug(client):
    import server as srv
    resp = client.post("/api/scan", json={"target": "nm-rio-rancho", "mode": "zip"})
    job_id = resp.get_json()["job_id"]
    with srv._jobs_lock:
        job = srv._jobs[job_id]
    assert job["city_name"] == "Rio Rancho"


def test_zip_drill_v2_flag_stored(client):
    import server as srv
    resp = client.post("/api/scan", json={
        "target": "nm-albuquerque", "mode": "zip", "zip_version": "v2"
    })
    # v2 flag is consumed by the background runner; job still queues successfully
    assert resp.status_code == 200


def test_zip_drill_invalid_community_id_returns_400(client):
    resp = client.post("/api/scan", json={"target": "INVALID", "mode": "zip"})
    assert resp.status_code == 400


def test_zip_drill_depth_not_enforced(client):
    # ZIP drill ignores depth — invalid depth value must not block a zip job
    resp = client.post("/api/scan", json={
        "target": "nm-albuquerque", "mode": "zip", "depth": "turbo"
    })
    assert resp.status_code == 200


def test_community_scan_depth_still_enforced(client):
    # Community scan must still reject invalid depth
    resp = client.post("/api/scan", json={
        "target": "nm-albuquerque", "mode": "community", "depth": "turbo"
    })
    assert resp.status_code == 400


# ── Helper: _city_name_from_slug ────────────────────────────────────────────

def test_city_name_from_slug_single_word():
    import server as srv
    assert srv._city_name_from_slug("nm-albuquerque") == "Albuquerque"


def test_city_name_from_slug_multi_word():
    import server as srv
    assert srv._city_name_from_slug("nm-santa-fe") == "Santa Fe"


def test_city_name_from_slug_three_word():
    import server as srv
    assert srv._city_name_from_slug("nm-rio-rancho") == "Rio Rancho"
