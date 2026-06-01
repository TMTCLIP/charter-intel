"""
clip_ops/config.py — central, static configuration.

All paths are resolved relative to this file so the module is reproducible
regardless of the current working directory. No I/O happens at import time
except reading environment variables for email defaults.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

# ── Locations ──────────────────────────────────────────────────────────────
OPS_DIR = Path(__file__).resolve().parent          # .../charter-intel/clip_ops
REPO_ROOT = OPS_DIR.parent                          # .../charter-intel

# ── Input sources (READ-ONLY) ───────────────────────────────────────────────
OUTPUTS_DIR = REPO_ROOT / "outputs"
BY_COMMUNITY_DIR = OUTPUTS_DIR / "by_community"
BY_STATE_DIR = OUTPUTS_DIR / "by_state"
ZIP_DIR = OUTPUTS_DIR / "zip"
RUNS_DIR = REPO_ROOT / "app" / "runs"
DATA_LOGS_DIR = REPO_ROOT / "data" / "logs"
PIPELINE_LOGS_DIR = REPO_ROOT / "logs"
NOTES_FILE = OPS_DIR / "notes.md"

# Documents mined for accomplishments / issues / next-steps.
DOC_FILES = [
    REPO_ROOT / "SESSION_STATE.md",
    REPO_ROOT / "HANDOFF.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "DEPLOY.md",
]

# ── Output artifacts ─────────────────────────────────────────────────────────
STATE_FILE = OPS_DIR / "state.json"
BACKLOG_FILE = OPS_DIR / "backlog_ranked.json"
DIGEST_FILE = OPS_DIR / "daily_digest.md"
EMAIL_LOG_FILE = OPS_DIR / "email.log"
RUN_LOG_FILE = OPS_DIR / "run_daily.log"

# ── Ranking weights (priority_score = Σ weight*component) ────────────────────
WEIGHT_BLOCKING = 3
WEIGHT_FREQUENCY = 2
WEIGHT_RECENCY = 1
FREQUENCY_CAP = 3

# ── Severity keyword map ─────────────────────────────────────────────────────
# Order matters: HIGH checked before MED. Matching is substring, lowercased.
BLOCKING_KEYWORDS_HIGH = ["broken", "fail", "error", "missing", "cannot", "invalid"]
BLOCKING_KEYWORDS_MED = ["issue", "bug", "wrong"]

# ── Category keyword map (first match wins; default = "feature") ─────────────
CATEGORY_KEYWORDS = {
    "bug": ["broken", "fail", "error", "crash", "wrong", "invalid", "regression", "bug"],
    "data": ["fixture", "census", "acs", "nces", "ccd", "saipe", "ipeds", "cache",
             "parquet", "csv", "token", "dataset", "data"],
    "infra": ["infra", "ci ", "deploy", "docker", "cron", "pipeline", "schema",
              "batch", "cost", "render", "railway"],
}

# ── Recency thresholds (whole days vs. "today") ──────────────────────────────
RECENCY_TODAY_SCORE = 2
RECENCY_RECENT_SCORE = 1
RECENCY_RECENT_MAX_DAYS = 3      # 1..3 days → RECENCY_RECENT_SCORE; older → 0

# ── Corpus reading limits ────────────────────────────────────────────────────
MAX_FILE_READ_BYTES = 200_000    # per-file cap when building the keyword corpus

# ── Digest limits ────────────────────────────────────────────────────────────
TOP_ISSUES = 5
TOP_ACTIONS = 5
HEALTH_MAX_SENTENCES = 5
RECENT_DAYS = 7                  # window for "recent" accomplishments / commits

# ── Canonical state schema (used for missing-field self-check) ───────────────
EXPECTED_STATE_FIELDS = [
    "date",
    "recent_accomplishments",
    "open_issues",
    "backlog_items",
    "system_health_summary",
    "missing_data_flags",
    "last_run_summary",
]

# ── Email config defaults (env-driven; never required at import) ─────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
EMAIL_USER = os.environ.get("EMAIL_USER", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "") or EMAIL_USER
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_ENDPOINT = "https://api.sendgrid.com/v3/mail/send"
EMAIL_SUBJECT_PREFIX = os.environ.get("EMAIL_SUBJECT_PREFIX", "[CLIP Ops]")


def today() -> datetime.date:
    """
    Reference date for the run. Honors CLIP_OPS_DATE=YYYY-MM-DD for
    reproducible/testable runs; otherwise the system date.
    """
    override = os.environ.get("CLIP_OPS_DATE", "").strip()
    if override:
        try:
            return datetime.date.fromisoformat(override)
        except ValueError:
            pass
    return datetime.date.today()
