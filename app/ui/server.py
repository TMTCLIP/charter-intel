"""
CLIP UI — Flask backend.
Serves the single-page frontend and API routes for state/city/run data.

Run from project root:
    cd ~/Downloads/charter-intel
    python3 app/ui/server.py

Port: 5001 (avoids macOS AirPlay conflict on 5000).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).parent.parent.parent
RUNS_DIR = BASE_DIR / "app" / "runs"
OUTPUTS_DIR = BASE_DIR / "outputs"
BY_COMMUNITY_DIR = OUTPUTS_DIR / "by_community"
CONFIG_FILE = BASE_DIR / "config" / "states.yaml"

app = Flask(__name__, template_folder="templates", static_folder="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_states() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def _is_junk_slug(target: str) -> bool:
    """True if target is a numeric/address-fragment slug (mirrors app/app.py)."""
    try:
        parts = target.strip().lower().split("-")
        if not (len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha()):
            return False
        city_parts = parts[1:]
        if not city_parts:
            return False
        first = city_parts[0]
        second = city_parts[1] if len(city_parts) > 1 else ""
        if re.match(r"^\d+$", first):
            return True
        if re.match(r"^bldg?\.?$", first) or first == "ste":
            return True
        if first in ("us", "nm") and re.match(r"^\d+$", second):
            return True
    except Exception:
        pass
    return False


def _parse_slug(run_id: str) -> dict:
    """Extract state_code and city_display from a community slug or run_id."""
    try:
        parts = run_id.strip().lower().split("-")
        if len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha():
            return {
                "state_code": parts[0].upper(),
                "city_display": " ".join(parts[1:]).title(),
            }
        if len(parts) >= 3:
            inner = parts[1:]
            if inner and inner[-1].isdigit():
                inner = inner[:-1]
            if inner and len(inner[0]) == 2 and inner[0].isalpha():
                return {
                    "state_code": inner[0].upper(),
                    "city_display": " ".join(inner[1:]).title() or run_id,
                }
    except Exception:
        pass
    return {"state_code": "", "city_display": run_id}


def _find_brief_path(target: str, preset: str, mode: str) -> Path | None:
    """Locate the best HTML brief file for a community+preset+mode."""
    comm_dir = BY_COMMUNITY_DIR / target
    if not comm_dir.exists():
        return None
    canonical = comm_dir / f"{target}_{preset}_mode{mode}.html"
    if canonical.exists():
        return canonical
    # Fall back to any non-print HTML
    htmls = sorted(comm_dir.glob("*.html"))
    non_print = [h for h in htmls if "_print" not in h.name]
    candidates = non_print or htmls
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/states")
def states():
    """List states present in states.yaml with basic metadata."""
    data = _load_states()
    result = []
    for code, info in data.items():
        if code.startswith("_") or code == "version":
            continue
        if not isinstance(info, dict):
            continue
        result.append({
            "code": code,
            "name": info.get("name", code),
            "active": info.get("status") == "ACTIVE",
        })
    result.sort(key=lambda s: s["name"])
    return jsonify(result)


@app.route("/api/cities")
def cities():
    """Return community list for a state, excluding junk/excluded slugs.

    Query param: state=NM
    Response: [{"slug": "nm-santa-fe", "name": "Santa Fe"}, ...]
    """
    state_code = request.args.get("state", "").upper()
    data = _load_states()
    state_data = data.get(state_code)
    if not state_data or not isinstance(state_data, dict):
        return jsonify([])

    state_lower = state_code.lower()
    # Prefer state-specific district map, fall back to NCES map
    district_map: dict = (
        state_data.get(f"{state_lower}_district_map")
        or state_data.get("nces_district_map")
        or {}
    )
    excluded = set(state_data.get("excluded_communities", []))

    result = []
    for slug, district_name in district_map.items():
        if slug in excluded:
            continue
        if _is_junk_slug(slug):
            continue
        city_name = (
            str(district_name).title()
            if district_name
            else " ".join(slug.split("-")[1:]).title()
        )
        result.append({"slug": slug, "name": city_name})

    result.sort(key=lambda c: c["name"])
    return jsonify(result)


@app.route("/api/runs")
def runs():
    """Return scan run history, optionally filtered by state.

    Query param: state=NM  (optional)
    Response: [{run_id, target, city, state, state_code, date, preset, mode, has_brief}, ...]
    """
    state_filter = request.args.get("state", "").upper()

    if not RUNS_DIR.exists():
        return jsonify([])

    result = []
    for run_dir in RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        meta_file = run_dir / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            continue

        target = meta.get("target", "")
        if _is_junk_slug(target):
            continue

        parsed = _parse_slug(target)
        state_code = parsed["state_code"]
        city_display = parsed["city_display"]

        if state_filter and state_code != state_filter:
            continue

        start_time = meta.get("start_time", "")
        date_str = start_time[:10] if start_time else ""

        flags = meta.get("flags", {})
        preset = (flags.get("preset") or "growth").strip()
        mode = (flags.get("mode") or "2").strip()
        run_id = meta.get("run_id", run_dir.name)

        has_brief = _find_brief_path(target, preset, mode) is not None

        status_file = run_dir / "status.json"
        try:
            status_data = json.loads(status_file.read_text()) if status_file.exists() else {}
        except Exception:
            status_data = {}
        run_state = status_data.get("state", "unknown")

        result.append({
            "run_id": run_id,
            "target": target,
            "city": city_display,
            "state": state_code,
            "state_code": state_code,
            "date": date_str,
            "preset": preset,
            "mode": mode,
            "has_brief": has_brief,
            "status": run_state,
        })

    result.sort(key=lambda r: r["date"], reverse=True)
    return jsonify(result)


@app.route("/api/brief")
def brief():
    """Return the HTML content of a community brief.

    Query param: run_id=<run_id>
    Response: text/html
    """
    run_id = request.args.get("run_id", "").strip()
    if not run_id:
        return "Missing run_id", 400

    run_dir = RUNS_DIR / run_id
    meta_file = run_dir / "meta.json"
    if not meta_file.exists():
        return "Run not found", 404

    try:
        meta = json.loads(meta_file.read_text())
    except Exception:
        return "Invalid run metadata", 500

    target = meta.get("target", "")
    flags = meta.get("flags", {})
    preset = (flags.get("preset") or "growth").strip()
    mode = (flags.get("mode") or "2").strip()

    brief_path = _find_brief_path(target, preset, mode)
    if not brief_path:
        return "Brief not found", 404

    return brief_path.read_text(errors="replace"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/scan", methods=["POST"])
def scan():
    # TODO: implement real scan dispatch (wire to runner.py / main.py)
    return jsonify({"status": "queued", "run_id": "stub-001"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
