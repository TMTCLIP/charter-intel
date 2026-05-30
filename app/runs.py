"""
app/runs.py — scan run history from app/runs/*.

Each run directory holds meta.json + status.json (written by runner.py). This
module reads them and returns a newest-first list for the History view. It also
refreshes the status of any run still marked "running" so the table is current.
"""

from __future__ import annotations

import json
from pathlib import Path

import config
import runner


def list_runs(refresh_running: bool = True) -> list[dict]:
    """Return all runs, newest first.

    Each item: run_id, target, flags, start_time, state, exit_code, command.
    """
    if not config.RUNS_DIR.exists():
        return []

    runs: list[dict] = []
    for run_dir in config.RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        meta = _read_json(run_dir / config.META_NAME)
        if not meta:
            continue

        status = _read_json(run_dir / config.STATUS_NAME) or {}
        if refresh_running and status.get("state") not in ("done", "failed"):
            status = runner.refresh_status(meta["run_id"])

        runs.append({
            "run_id": meta.get("run_id", run_dir.name),
            "target": meta.get("target", ""),
            "flags": meta.get("flags", {}),
            "command": meta.get("command", []),
            "start_time": meta.get("start_time", ""),
            "state": status.get("state", "unknown"),
            "exit_code": status.get("exit_code"),
        })

    runs.sort(key=lambda r: r.get("start_time", ""), reverse=True)
    return runs


def get_run(run_id: str) -> dict | None:
    """Return a single run's combined meta + (refreshed) status, or None."""
    run_dir = config.RUNS_DIR / run_id
    meta = _read_json(run_dir / config.META_NAME)
    if not meta:
        return None
    status = runner.refresh_status(run_id)
    return {**meta, "state": status.get("state"), "exit_code": status.get("exit_code")}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
