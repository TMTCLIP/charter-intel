"""
CLIP UI — Flask backend.
Serves the single-page frontend and API routes for state/city/run data.

Run from project root:
    cd ~/Downloads/charter-intel
    python3 app/ui/server.py

Port: 5001 (avoids macOS AirPlay conflict on 5000).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template, request
from jinja2 import Environment, FileSystemLoader, select_autoescape

# Make app/ importable so we can reuse config, rate_limit without duplicating.
_APP_DIR = Path(__file__).resolve().parent.parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import config as app_config  # noqa: E402 — must follow sys.path insert
import rate_limit             # noqa: E402
from auth import auth_bp, require_login  # noqa: E402

BASE_DIR = Path(__file__).parent.parent.parent
RUNS_DIR = BASE_DIR / "app" / "runs"
OUTPUTS_DIR = BASE_DIR / "outputs"
BY_COMMUNITY_DIR = OUTPUTS_DIR / "by_community"
ZIP_OUTPUTS_DIR = OUTPUTS_DIR / "zip"
CONFIG_FILE = BASE_DIR / "config" / "states.yaml"
COMMUNITY_REGISTRY_DIR = BASE_DIR / "config" / "community_registry"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY")
app.register_blueprint(auth_bp)

# ── Validation constants ────────────────────────────────────────────────────
_COMMUNITY_ID_RE = re.compile(r'^[a-z]{2}-[a-z0-9-]{1,64}$')
_DEPTH_WHITELIST = frozenset({"fast", "standard", "deep"})


# ── In-memory job store ─────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _iso_now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None


def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _append_stage_log(job_id: str, line: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["stage_log"].append(line)


# ── Background scan runner ──────────────────────────────────────────────────

def _build_scan_cmd(community_id: str, state: str, depth: str, preset: str, mode: str, flags: dict) -> list[str]:
    argv = [str(app_config.PYTHON_BIN), str(app_config.MAIN_PY), community_id]
    if state:
        argv += ["--state", state]
    if depth:
        argv += ["--depth", depth]
    if preset:
        argv += ["--preset", preset]
    if mode:
        argv += ["--mode", mode]
    for key, cli_flag in app_config.BOOLEAN_FLAGS.items():
        if flags.get(key):
            argv.append(cli_flag)
    return argv


def run_scan_background(
    job_id: str,
    community_id: str,
    depth: str,
    state: str,
    preset: str,
    mode: str,
    extra_flags: dict,
) -> None:
    _update_job(job_id, status="running", started_at=_iso_now())
    cmd = _build_scan_cmd(community_id, state, depth, preset, mode, extra_flags)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(app_config.REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=app_config.subprocess_env(),
            text=True,
            bufsize=1,
        )

        tail: list[str] = []
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.rstrip()
            if not line:
                continue
            _append_stage_log(job_id, line)
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)

        proc.wait()

        if proc.returncode == 0:
            brief = _find_brief_path(community_id, preset or "growth", mode or "2")
            _update_job(
                job_id,
                status="complete",
                finished_at=_iso_now(),
                brief_path=str(brief) if brief else None,
            )
        else:
            _update_job(
                job_id,
                status="failed",
                finished_at=_iso_now(),
                error="\n".join(tail[-10:]),
            )

    except Exception as exc:
        _update_job(job_id, status="failed", finished_at=_iso_now(), error=str(exc))


# ── Background zip drill runner ─────────────────────────────────────────────

def run_zip_drill_background(job_id: str, city_name: str, state: str, use_v2: bool) -> None:
    _update_job(job_id, status="running", started_at=_iso_now())
    mode_flag = "zip_v2" if use_v2 else "zip"
    cmd = [str(app_config.PYTHON_BIN), str(app_config.MAIN_PY),
           city_name, "--mode", mode_flag, "--state", state]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(app_config.REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=app_config.subprocess_env(),
            text=True,
            bufsize=1,
        )

        tail: list[str] = []
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.rstrip()
            if not line:
                continue
            _append_stage_log(job_id, line)
            tail.append(line)
            if len(tail) > 30:
                tail.pop(0)

        proc.wait()

        if proc.returncode == 0:
            out = _find_zip_drill_path(city_name)
            _update_job(job_id, status="complete", finished_at=_iso_now(),
                        brief_path=str(out) if out else None)
            job_snapshot = _get_job(job_id)
            _write_zip_run(
                community_id=job_snapshot["community_id"],
                city_name=city_name,
                state=state,
                use_v2=use_v2,
            )
        else:
            _update_job(job_id, status="failed", finished_at=_iso_now(),
                        error="\n".join(tail[-10:]))

    except Exception as exc:
        _update_job(job_id, status="failed", finished_at=_iso_now(), error=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_states() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def _communities_from_registry(state_code: str) -> list[dict] | None:
    """Read config/community_registry/{state_lower}.yaml and return [{slug, name}] or None if absent."""
    registry_path = COMMUNITY_REGISTRY_DIR / f"{state_code.lower()}.yaml"
    if not registry_path.exists():
        return None
    try:
        with open(registry_path) as f:
            registry = yaml.safe_load(f) or {}
    except Exception:
        return None

    states_data = _load_states()
    state_info = states_data.get(state_code.upper(), {})
    excluded = set(state_info.get("excluded_communities", []))

    result = []
    for slug, entry in registry.items():
        if slug in excluded or _is_junk_slug(slug):
            continue
        display = entry.get("display_name") or " ".join(slug.split("-")[1:]).title()
        result.append({"slug": slug, "name": f"{display}, {state_code.upper()}"})

    result.sort(key=lambda c: c["name"])
    # Deduplicate by display name: when multiple registry entries share the same
    # city name within a state, expose only the first — the pipeline's slug
    # resolver picks the correct LEAID at scan time regardless.
    seen: set[str] = set()
    deduped: list[dict] = []
    for entry in result:
        if entry["name"] not in seen:
            seen.add(entry["name"])
            deduped.append(entry)
    return deduped


def _communities_from_district_map(state_code: str, state_data: dict) -> list[dict]:
    """Fall back to nces_district_map in states.yaml for states without a registry YAML."""
    state_lower = state_code.lower()
    district_map: dict = (
        state_data.get(f"{state_lower}_district_map")
        or state_data.get("nces_district_map")
        or {}
    )
    excluded = set(state_data.get("excluded_communities", []))

    result = []
    for slug, district_name in district_map.items():
        if slug in excluded or _is_junk_slug(slug):
            continue
        city_name = (
            str(district_name).title()
            if district_name
            else " ".join(slug.split("-")[1:]).title()
        )
        result.append({"slug": slug, "name": f"{city_name}, {state_code.upper()}"})

    result.sort(key=lambda c: c["name"])
    seen: set[str] = set()
    deduped: list[dict] = []
    for entry in result:
        if entry["name"] not in seen:
            seen.add(entry["name"])
            deduped.append(entry)
    return deduped


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


def _city_name_from_slug(community_id: str) -> str:
    """'nm-santa-fe' → 'Santa Fe'. Strips the two-letter state prefix."""
    parts = community_id.split("-")
    return " ".join(parts[1:]).title() if len(parts) >= 2 else community_id.title()


def _write_zip_run(community_id: str, city_name: str, state: str, use_v2: bool) -> None:
    """Persist a minimal run record so the brief sidebar can load zip drill results."""
    run_id = f"zip-{community_id}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "target": community_id,
        "flags": {
            "mode": "zip",
            "city_name": city_name,
            "state": state,
            "zip_version": "v2" if use_v2 else "v1",
        },
        "start_time": _iso_now(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    (run_dir / "status.json").write_text(json.dumps({"state": "done", "exit_code": 0}))


def _find_zip_drill_path(city_name: str) -> Path | None:
    """Locate the zip drill HTML output for a city. Prefers v2 over v1."""
    city_slug = re.sub(r"[^a-z0-9]+", "-", city_name.lower()).strip("-")
    for fname in (f"{city_slug}_zip_drill_v2.html", f"{city_slug}_zip_drill.html"):
        p = ZIP_OUTPUTS_DIR / city_slug / fname
        if p.exists():
            return p
    return None


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


def _pdf_patch_excluded_weights(brief: dict, state: str, community_id: str) -> None:
    """Mirror of s7_render._patch_excluded_weights for the PDF route.

    The LLM sometimes zeroes weights for excluded dimensions. Restore the original
    design weight from s5_scorecard.json so the PDF table is accurate.
    """
    dim_table = (brief.get("scorecard_summary") or {}).get("dimension_table")
    if not dim_table:
        return
    sc_path = BASE_DIR / "data" / "cache" / "community" / state / community_id / "s5_scorecard.json"
    if not sc_path.exists():
        return
    try:
        sc_dims = json.loads(sc_path.read_text()).get("dimensions", {})
    except Exception:
        return
    for row in dim_table:
        if row.get("used_default") and row.get("weight") == 0:
            original = sc_dims.get(row.get("dimension"), {}).get("weight")
            if original is not None:
                row["weight"] = original


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
@require_login
def index():
    return render_template("index.html")


@app.route("/api/health")
@require_login
def health():
    return jsonify({"status": "ok"})


@app.route("/api/states")
@require_login
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
@require_login
def cities():
    """Return community list for a state (or all active states), excluding junk/excluded slugs.

    Query param: state=NM  (omit or pass empty for all active states)
    Response: [{"slug": "ms-oxford", "name": "Oxford, MS"}, ...]

    Reads from config/community_registry/{state_lower}.yaml when present;
    falls back to nces_district_map in states.yaml for states without a registry file.
    """
    state_code = request.args.get("state", "").upper()
    states_data = _load_states()

    if state_code:
        # Single-state request
        state_data = states_data.get(state_code)
        if not state_data or not isinstance(state_data, dict):
            return jsonify([])
        result = _communities_from_registry(state_code)
        if result is None:
            result = _communities_from_district_map(state_code, state_data)
        return jsonify(result)

    # No state filter: return communities for all active states combined
    combined: list[dict] = []
    for code, info in states_data.items():
        if code.startswith("_") or code == "version":
            continue
        if not isinstance(info, dict) or info.get("status") != "ACTIVE":
            continue
        entries = _communities_from_registry(code)
        if entries is None:
            entries = _communities_from_district_map(code, info)
        combined.extend(entries)

    combined.sort(key=lambda c: c["name"])
    return jsonify(combined)


@app.route("/api/runs")
@require_login
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

        if mode == "zip":
            city_name = flags.get("city_name") or _city_name_from_slug(target)
            has_brief = _find_zip_drill_path(city_name) is not None
        else:
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
@require_login
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

    if mode == "zip":
        city_name = flags.get("city_name") or _city_name_from_slug(target)
        brief_path = _find_zip_drill_path(city_name)
    else:
        brief_path = _find_brief_path(target, preset, mode)

    if not brief_path:
        return "Brief not found", 404

    return brief_path.read_text(errors="replace"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/brief/pdf")
# @require_login  # disabled for local test only — restore before push
def brief_pdf():
    """Render a community brief as a PDF and return it as a download.

    Query param: run_id=<run_id>
    Response: application/pdf
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

    if mode == "zip":
        return "PDF export is not available for ZIP drill briefs", 400

    state = (flags.get("state") or target.split("-")[0]).lower().strip()

    brief_json_path = (
        BASE_DIR / "data" / "cache" / "synthesis" / state / target
        / f"s6_brief_{preset}_mode{mode}.json"
    )
    if not brief_json_path.exists():
        return "Brief data not found — run S6 first or use the HTML brief instead", 404

    try:
        brief = json.loads(brief_json_path.read_text())
    except Exception:
        return "Failed to load brief data", 500

    _pdf_patch_excluded_weights(brief, state, target)

    pci_promoted = False
    s4_path = BASE_DIR / "data" / "cache" / "community" / state / target / "s4_verified.json"
    if s4_path.exists():
        try:
            pci_promoted = json.loads(s4_path.read_text()).get("pci_promoted_from_cache", False)
        except Exception:
            pass

    pdf_env = Environment(
        loader=FileSystemLoader(str(BASE_DIR / "templates")),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
    html_string = pdf_env.get_template("pdf_brief.html.j2").render(
        brief=brief,
        schools=brief.get("top_charter_schools", []),
        pci_promoted=pci_promoted,
    )

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html_string, wait_until="networkidle")
            pdf_bytes = page.pdf(format="Letter", print_background=True)
            browser.close()
    except Exception as exc:
        app.logger.error("Playwright PDF generation failed for %s: %s", run_id, exc)
        return f"PDF generation failed: {exc}", 500

    filename = f"{target}_{preset}_brief.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/scan", methods=["POST"])
@require_login
def scan():
    body = request.get_json(silent=True) or {}

    community_id = (body.get("target") or "").strip()
    scan_type    = (body.get("mode")  or "community").strip()  # "community" | "zip"
    depth        = (body.get("depth") or "standard").strip()

    if not community_id:
        return jsonify({"error": "community_id is required"}), 400
    if not _COMMUNITY_ID_RE.match(community_id):
        return jsonify({"error": f"Invalid community_id: {community_id!r}"}), 400

    # ── ZIP Drill branch — no Claude API calls so no cost-cap check ──────────
    if scan_type == "zip":
        state     = (body.get("state") or "NM").strip().upper()
        use_v2    = body.get("zip_version") == "v2"
        city_name = _city_name_from_slug(community_id)
        job_id    = secrets.token_hex(8)
        with _jobs_lock:
            _jobs[job_id] = {
                "job_id": job_id,
                "community_id": community_id,
                "city_name": city_name,
                "job_type": "zip",
                "depth": "n/a",
                "preset": "",
                "mode": "",
                "status": "queued",
                "started_at": None,
                "finished_at": None,
                "stage_log": [],
                "error": None,
                "brief_path": None,
            }
        threading.Thread(
            target=run_zip_drill_background,
            args=(job_id, city_name, state, use_v2),
            daemon=True,
        ).start()
        return jsonify({"job_id": job_id, "status": "queued"})

    # ── Community scan branch ────────────────────────────────────────────────
    if depth not in _DEPTH_WHITELIST:
        return jsonify({"error": f"Invalid depth: {depth!r}. Must be fast, standard, or deep"}), 400

    # Rate limit gate (read-only check; in-memory MVP jobs don't write to disk)
    if not os.environ.get("CLIP_DEV_MODE"):
        ok, msg, _ = rate_limit.check_daily_limit(app_config.RUNS_DIR)
        if not ok:
            return jsonify({"error": msg}), 429
        ok, msg, _ = rate_limit.check_cost_cap(app_config.RUNS_DIR)
        if not ok:
            return jsonify({"error": msg}), 429

    state  = (body.get("state") or "NM").strip().upper()
    preset = (body.get("preset") or "growth").strip()
    mode   = (body.get("mode_num") or "2").strip()
    extra_flags = {k: bool(body.get(k)) for k in app_config.BOOLEAN_FLAGS}

    job_id = secrets.token_hex(8)
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "community_id": community_id,
            "job_type": "community",
            "depth": depth,
            "preset": preset,
            "mode": mode,
            "status": "queued",
            "started_at": None,
            "finished_at": None,
            "stage_log": [],
            "error": None,
            "brief_path": None,
        }

    threading.Thread(
        target=run_scan_background,
        args=(job_id, community_id, depth, state, preset, mode, extra_flags),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/scan/status/<job_id>")
@require_login
def scan_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(dict(job))


@app.route("/api/scan/result/<job_id>")
@require_login
def scan_result(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        status       = job["status"]
        brief_path   = job["brief_path"]
        community_id = job["community_id"]
        preset       = job["preset"]
        mode         = job["mode"]
        job_type     = job.get("job_type", "community")
        city_name    = job.get("city_name", "")

    if status != "complete":
        return jsonify({"error": f"Scan not complete (status: {status})"}), 409

    # brief_path may be None if the file wasn't written by the time the process
    # exited — try resolving it now before giving up.
    if not brief_path:
        if job_type == "zip":
            p = _find_zip_drill_path(city_name)
        else:
            p = _find_brief_path(community_id, preset or "growth", mode or "2")
        if p is None:
            return jsonify({"error": "Brief not yet available"}), 404
        brief_path = str(p)
        _update_job(job_id, brief_path=brief_path)

    try:
        content = Path(brief_path).read_text(errors="replace")
    except FileNotFoundError:
        return jsonify({"error": "Brief file missing from disk"}), 404

    return content, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
