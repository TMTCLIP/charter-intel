"""
clip_ops/collectors.py — read-only ingestion of CLIP outputs, logs, run
records, docs, git history, and manual notes.

Every collector is defensive: a missing path, corrupt JSON, or unavailable
git binary yields an empty/neutral result, never an exception. Nothing here
writes to disk or calls the network.
"""
from __future__ import annotations

import csv
import datetime
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from clip_ops import config

# ── Low-level safe readers ───────────────────────────────────────────────────


def read_text(path: Path, max_bytes: int = config.MAX_FILE_READ_BYTES) -> str:
    """Read up to max_bytes of text. Returns '' on any failure."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def load_json(path: Path) -> Optional[dict]:
    """Load JSON. Returns None on missing file or corrupt content."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"_value": data}
    except Exception:
        return None


def mtime_date(path: Path) -> Optional[datetime.date]:
    """File modification date (local). None if unavailable."""
    try:
        ts = path.stat().st_mtime
        return datetime.datetime.fromtimestamp(ts).date()
    except Exception:
        return None


# ── Markdown section / bullet extraction ─────────────────────────────────────

_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*\S)\s*$")
_CHECK_OPEN_RE = re.compile(r"^\s*[-*+]\s+\[\s\]\s+(.*\S)\s*$")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _clean_line(text: str) -> str:
    """Strip markdown emphasis/backticks/checkboxes and collapse whitespace."""
    text = re.sub(r"^\[[ xX]\]\s*", "", text)          # leftover checkbox
    text = text.replace("`", "")
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)      # bold
    text = re.sub(r"\*([^*]+)\*", r"\1", text)          # italic
    text = re.sub(r"\s+", " ", text).strip()
    return text


def section_bullets(text: str, header_keyword: str) -> List[str]:
    """
    Return cleaned bullet/numbered lines that appear under any header whose
    title contains header_keyword (case-insensitive), until the next header.
    """
    out: List[str] = []
    lines = text.splitlines()
    capturing = False
    for line in lines:
        hm = _HEADER_RE.match(line)
        if hm:
            capturing = header_keyword.lower() in hm.group(2).lower()
            continue
        if not capturing:
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            cleaned = _clean_line(bm.group(1))
            if cleaned:
                out.append(cleaned)
    return out


def open_checklist_items(text: str) -> List[str]:
    """Return cleaned text of unchecked '- [ ]' checklist items."""
    out: List[str] = []
    for line in text.splitlines():
        cm = _CHECK_OPEN_RE.match(line)
        if cm:
            cleaned = _clean_line(cm.group(1))
            if cleaned:
                out.append(cleaned)
    return out


# ── Collectors ───────────────────────────────────────────────────────────────


def collect_runs() -> List[dict]:
    """
    One record per app/runs/<run_id>/ directory, sorted by start_time
    (then run_id) ascending. Combines status.json + meta.json defensively.
    """
    runs: List[dict] = []
    if not config.RUNS_DIR.is_dir():
        return runs
    for run_dir in sorted(p for p in config.RUNS_DIR.iterdir() if p.is_dir()):
        status = load_json(run_dir / "status.json") or {}
        meta = load_json(run_dir / "meta.json") or {}
        exit_code = status.get("exit_code")
        state = str(status.get("state", "")).lower()
        success = (exit_code == 0) and (state in ("", "done", "success", "completed"))
        run_dt = mtime_date(run_dir)
        runs.append({
            "run_id": meta.get("run_id", run_dir.name),
            "target": meta.get("target", ""),
            "state": state or "unknown",
            "exit_code": exit_code,
            "success": bool(success and exit_code is not None),
            "start_time": meta.get("start_time", ""),
            "command": meta.get("command", []),
            "mtime": run_dt.isoformat() if run_dt else None,
        })
    runs.sort(key=lambda r: (str(r.get("start_time") or ""), r["run_id"]))
    return runs


def collect_community_outputs() -> List[dict]:
    """
    One record per outputs/by_community/<slug>/ directory: artifact counts,
    most-recent artifact, and its modification date.
    """
    items: List[dict] = []
    if not config.BY_COMMUNITY_DIR.is_dir():
        return items
    for cdir in sorted(p for p in config.BY_COMMUNITY_DIR.iterdir() if p.is_dir()):
        files = [f for f in cdir.iterdir() if f.is_file()]
        artifacts = [f for f in files if f.suffix.lower() in (".md", ".html", ".json")]
        latest = None
        latest_dt: Optional[datetime.date] = None
        for f in artifacts:
            d = mtime_date(f)
            if d is not None and (latest_dt is None or d > latest_dt):
                latest_dt = d
                latest = f.name
        items.append({
            "community": cdir.name,
            "artifact_count": len(artifacts),
            "latest_artifact": latest,
            "latest_date": latest_dt.isoformat() if latest_dt else None,
        })
    items.sort(key=lambda c: c["community"])
    return items


def collect_token_logs() -> List[dict]:
    """token_usage_*.csv records from data/logs/, sorted by name."""
    logs: List[dict] = []
    if not config.DATA_LOGS_DIR.is_dir():
        return logs
    for f in sorted(config.DATA_LOGS_DIR.glob("token_usage_*.csv")):
        d = mtime_date(f)
        logs.append({
            "name": f.name,
            "date": d.isoformat() if d else None,
        })
    return logs


def collect_git_commits(days: int = config.RECENT_DAYS) -> List[dict]:
    """
    Recent commit subjects from the CLIP repo via a local `git log` call.
    Returns [] if git is unavailable or the directory is not a repo.
    """
    since = (config.today() - datetime.timedelta(days=days)).isoformat()
    try:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", "--date=short",
             "--pretty=format:%ad\t%s"],
            cwd=str(config.REPO_ROOT),
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    commits: List[dict] = []
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        date_str, subject = line.split("\t", 1)
        subject = _clean_line(subject)
        if subject:
            commits.append({"date": date_str.strip(), "subject": subject})
    return commits


def collect_notes() -> List[str]:
    """
    Cleaned bullet lines from the optional clip_ops/notes.md. Only bullet
    lines are ingested; prose and headings are ignored so the file can carry
    instructions without polluting the backlog.
    """
    if not config.NOTES_FILE.is_file():
        return []
    out: List[str] = []
    for line in read_text(config.NOTES_FILE).splitlines():
        bm = _BULLET_RE.match(line)
        if bm:
            cleaned = _clean_line(bm.group(1))
            if cleaned:
                out.append(cleaned)
    return out


def collect_docs() -> Dict[str, dict]:
    """
    Mine each present doc file for accomplishment / issue / next-step bullets
    and record the file's modification date (used for recency scoring).
    """
    result: Dict[str, dict] = {}
    for path in config.DOC_FILES:
        if not path.is_file():
            continue
        text = read_text(path)
        result[path.name] = {
            "mtime": (mtime_date(path).isoformat() if mtime_date(path) else None),
            "completed": (section_bullets(text, "completed")
                          + section_bullets(text, "what was built")),
            "issues": (section_bullets(text, "known issue")
                       + section_bullets(text, "open issue")),
            "next_steps": (section_bullets(text, "next step")
                           + section_bullets(text, "not yet implemented")
                           + section_bullets(text, "remaining")
                           + section_bullets(text, "roadmap")),
            "open_checklist": open_checklist_items(text),
        }
    return result


def build_corpus() -> str:
    """
    Concatenated, lowercased text drawn from docs, run metadata, log file
    names, and output filenames. Used only for deterministic frequency
    counting in the backlog ranker.
    """
    parts: List[str] = []
    for path in config.DOC_FILES:
        if path.is_file():
            parts.append(read_text(path).lower())
    if config.NOTES_FILE.is_file():
        parts.append(read_text(config.NOTES_FILE).lower())
    for run in collect_runs():
        parts.append(json.dumps(run).lower())
    for c in collect_community_outputs():
        parts.append((c["community"] + " " + (c["latest_artifact"] or "")).lower())
    for lg in collect_token_logs():
        parts.append(lg["name"].lower())
    # First data row of each token CSV adds a little signal without bloating.
    if config.DATA_LOGS_DIR.is_dir():
        for f in sorted(config.DATA_LOGS_DIR.glob("token_usage_*.csv")):
            try:
                with open(f, newline="", encoding="utf-8", errors="replace") as fh:
                    reader = csv.reader(fh)
                    for i, row in enumerate(reader):
                        parts.append(" ".join(row).lower())
                        if i >= 1:
                            break
            except Exception:
                continue
    return "\n".join(parts)
