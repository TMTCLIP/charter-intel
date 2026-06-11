"""
app/config.py — central configuration for the CLIP Streamlit wrapper.

This module is the ONLY place where assumptions about the pipeline live.
The app never imports from pipeline/ or main.py; the CLI + filesystem are the
only contract. Everything below was verified by reading main.py and
pipeline/s7_render.py (see ASSUMPTIONS at the bottom for anything unresolved).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Repo root
# ─────────────────────────────────────────────────────────────────────────────
# Read from REPO_ROOT env var if set (so a deployment platform can point us at
# the checkout), otherwise fall back to the detected repo path: app/ lives at
# <repo>/app/, so the repo root is this file's grandparent.
_DETECTED_REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(_DETECTED_REPO_ROOT))).resolve()

# main.py entry point the app shells out to.
MAIN_PY = REPO_ROOT / "main.py"


# ─────────────────────────────────────────────────────────────────────────────
# Python interpreter
# ─────────────────────────────────────────────────────────────────────────────
# ASSUMPTION (unresolved — see notes): the repo has no venv, and the system
# python3 on this machine is 3.9.6, but the pipeline uses 3.10+ syntax and
# requirements.txt declares "Python 3.11+". We therefore default to the
# interpreter currently running this app (sys.executable, i.e. whatever runs
# Streamlit) and let it be overridden by CLIP_PYTHON. A deployment MUST point
# CLIP_PYTHON (or the Streamlit interpreter) at a 3.11+ environment that has the
# pipeline's requirements installed.
PYTHON_BIN = os.environ.get("CLIP_PYTHON", sys.executable)


# ─────────────────────────────────────────────────────────────────────────────
# Brief output convention (verified in pipeline/s7_render.py)
# ─────────────────────────────────────────────────────────────────────────────
# Per-community briefs:
#   outputs/by_community/{community_id}/{community_id}_{preset}_mode{mode}_{YYYY-MM-DD}.md
#   outputs/by_community/{community_id}/{community_id}_{preset}_mode{mode}.html
#   (+ a "..._print.html" print variant for some modes; no date in the html name)
# Statewide scan (mode 1):
#   outputs/by_state/{state}/{state}_scan_{YYYYMMDD}.html
OUTPUTS_DIR = REPO_ROOT / "outputs"
BY_COMMUNITY_DIR = OUTPUTS_DIR / "by_community"
BY_STATE_DIR = OUTPUTS_DIR / "by_state"

# {cid} = community_id, {preset} = preset value, {mode} = mode value.
MD_BRIEF_GLOB = "{cid}_{preset}_mode{mode}_*.md"        # newest-by-name wins
HTML_BRIEF_NAME = "{cid}_{preset}_mode{mode}.html"      # overwritten each run
HTML_PRINT_BRIEF_NAME = "{cid}_{preset}_mode{mode}_print.html"
SCAN_HTML_GLOB = "{state}_scan_*.html"                  # newest-by-name wins

# main.py logs the city→community_id resolution as:
#   "Resolved community: 'Santa Fe' → nm-santa-fe"
# briefs.py parses this from stdout.log to learn the canonical community_id
# without importing the pipeline's build_community_id().
RESOLVED_COMMUNITY_RE = r"Resolved community:.*?→\s*([a-z]{2}-[^\s]+)"


# ─────────────────────────────────────────────────────────────────────────────
# Stage markers (verified in main.py)
# ─────────────────────────────────────────────────────────────────────────────
# STAGE_ORDER in pipeline/__init__.py — 7 stages, fixed order.
STAGE_ORDER = [
    "s1_discovery",
    "s2_state_context",
    "s3_fact_extraction",
    "s4_verification",
    "s5_scoring",
    "s6_synthesis",
    "s7_render",
]
STAGE_COUNT = len(STAGE_ORDER)

# main.py emits (via logging, format "%(asctime)s [%(levelname)s] %(message)s"):
#   "[{community_id}] Running {stage_id}..."
#   "[{community_id}] {stage_id} — OK (...)"  / "— cache hit (...)"
#   "[{community_id}] {stage_id} FAILED: ..."
# We parse the "Running <stage>" marker as the per-stage progress signal.
STAGE_START_RE = r"Running\s+(s\d_[a-z_]+)\.\.\."
STAGE_DONE_RE = r"(s\d_[a-z_]+)\s+—\s+(OK|cache hit)"
PIPELINE_COMPLETE_MARKER = "PIPELINE COMPLETE"


# ─────────────────────────────────────────────────────────────────────────────
# Run storage (app-owned; never under the pipeline's outputs/)
# ─────────────────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
RUNS_DIR = APP_DIR / "runs"
STDOUT_LOG_NAME = "stdout.log"
META_NAME = "meta.json"
STATUS_NAME = "status.json"
EXIT_CODE_NAME = "exit_code"  # written by the launch wrapper, see runner.py


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting (abuse guard for the Streamlit app — see app/rate_limit.py)
# ─────────────────────────────────────────────────────────────────────────────
RATE_LIMIT_SESSION_MAX = 3          # max scan jobs per session
RATE_LIMIT_DAILY_MAX = 20           # max scan jobs per day
RATE_LIMIT_DAILY_COST_CAP = 10.00   # max estimated API spend per day ($)
RATE_LIMIT_WINDOW_HOURS = 24        # rolling window in hours
RATE_LIMIT_STATEWIDE_WEIGHT = 3     # --all scans count as this many jobs
RATE_LIMIT_FREE_FLAGS = ["dry_run", "mock"]  # these flags = exempt
# Conservative cost estimate per job-weight unit. This is an abuse guard, NOT a
# billing system — intentionally over-estimates (real maturity_adjusted standard
# single-city ≈ $0.11–0.16; we charge $0.30 per weight unit).
RATE_LIMIT_COST_PER_WEIGHT = 0.30


# ─────────────────────────────────────────────────────────────────────────────
# CLI flags exposed by the New Scan form (real flags, verified in main.py)
# ─────────────────────────────────────────────────────────────────────────────
PRESET_CHOICES = ["growth", "replication", "turnaround", "maturity_adjusted"]
PRESET_DEFAULT = "growth"

DEPTH_CHOICES = ["fast", "standard", "deep"]
DEPTH_DEFAULT = "standard"

# --mode accepts ints 1|2|3 plus the string sub-modes "zip"/"zip_v2".
MODE_CHOICES = ["1", "2", "3", "zip", "zip_v2"]
MODE_DEFAULT = "2"

STATE_DEFAULT = "NM"

# Boolean (store_true) flags the form exposes as toggles. Each maps a UI label
# to the literal CLI flag. (--record/--force-record/--interactive are NOT
# surfaced as toggles but can still be passed via the free-text extra-args box.)
BOOLEAN_FLAGS = {
    "all": "--all",
    "dry_run": "--dry-run",
    "mock": "--mock",
    "batch": "--batch",
    "no_cache": "--no-cache",
    "force_refresh": "--force-refresh",
    "regen_data": "--regen-data",
}


# ─────────────────────────────────────────────────────────────────────────────
# Secrets / password gate
# ─────────────────────────────────────────────────────────────────────────────
# All secrets come from the process environment. The pipeline's .env (loaded by
# main.py via python-dotenv with override=True) is just ONE way to populate that
# environment; we never require .env to exist and never read or write it.
PASSWORD_ENV = "CLIP_PASSWORD"

# Set CLIP_VIEWER_ONLY=1 on deployments where the pipeline cannot run
# (e.g. Streamlit Community Cloud, which lacks data/raw/ CSVs and pipeline deps).
# When true, the New Scan tab shows an info message instead of the scan form.
VIEWER_ONLY: bool = os.environ.get("CLIP_VIEWER_ONLY", "").strip() == "1"


def get_password() -> str | None:
    """Return the configured gate password, or None if unset."""
    return os.environ.get(PASSWORD_ENV)


def subprocess_env() -> dict[str, str]:
    """Environment passed to the pipeline subprocess.

    A copy of the app's process environment so a deployment platform's injected
    secrets (ANTHROPIC_API_KEY, CENSUS_API_KEY, ...) flow through. main.py will
    additionally load the repo's .env with override=True if it exists.
    """
    return dict(os.environ)


# ─────────────────────────────────────────────────────────────────────────────
# ASSUMPTIONS (flag for human confirmation)
# ─────────────────────────────────────────────────────────────────────────────
# 1. PYTHON_BIN — UNRESOLVED. No venv in the repo; system python3 is 3.9.6 but
#    the pipeline needs 3.11+. Defaulted to sys.executable, overridable via
#    CLIP_PYTHON. Confirm the deployment interpreter has pipeline deps installed.
# 2. The .html brief name carries NO date and is overwritten each run, while the
#    .md name carries a YYYY-MM-DD date — so multiple .md files accumulate and we
#    pick the newest by filename. Confirmed in s7_render.py.
# 3. Stage progress is parsed from the "Running <stage>..." log line. If the
#    pipeline's logging format changes, the Live Run view falls back to a raw
#    log tail automatically (no markers matched).
ASSUMPTIONS = [
    ("PYTHON_BIN", str(PYTHON_BIN),
     "No venv found; system python3 is 3.9.6 but pipeline needs 3.11+. "
     "Override with CLIP_PYTHON to a 3.11+ env that has pipeline deps."),
    ("REPO_ROOT", str(REPO_ROOT),
     "Detected from app/ location; override with REPO_ROOT env var."),
    ("HTML_BRIEF_NAME", HTML_BRIEF_NAME,
     "HTML brief has no date and is overwritten each run (verified)."),
    ("STAGE_START_RE", STAGE_START_RE,
     "Stage progress parsed from 'Running <stage>...' log lines."),
]
