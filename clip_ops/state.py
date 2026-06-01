"""
clip_ops/state.py — assemble the canonical system state (state.json) and the
deterministic system-health summary from collected, read-only signals.

Degrades gracefully: with no outputs and no runs the state is still valid and
carries a "system not fully initialized" flag.
"""
from __future__ import annotations

import datetime
from typing import Dict, List, Optional

from clip_ops import collectors, config


def _dedup(seq: List[str]) -> List[str]:
    """Order-preserving dedup on normalized text."""
    seen: set = set()
    out: List[str] = []
    for item in seq:
        key = " ".join(item.split()).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def build_accomplishments(docs: Dict[str, dict], commits: List[dict]) -> List[str]:
    items: List[str] = []
    for c in commits:
        items.append("git %s: %s" % (c["date"], c["subject"]))
    for doc in docs.values():
        items.extend(doc.get("completed", []))
    return _dedup(items)


def build_open_issues(docs: Dict[str, dict]) -> List[str]:
    items: List[str] = []
    for doc in docs.values():
        items.extend(doc.get("issues", []))
    return _dedup(items)


def build_backlog_candidates(
    docs: Dict[str, dict],
    open_issues: List[str],
    notes: List[str],
) -> List[Dict[str, Optional[str]]]:
    """
    Collect backlog candidates with the source-file date attached so the
    ranker can compute recency. Issues are actionable, so they seed backlog too.
    """
    candidates: List[Dict[str, Optional[str]]] = []

    for doc in docs.values():
        src_date = doc.get("mtime")
        for title in doc.get("issues", []):
            candidates.append({"title": title, "source_date": src_date})
        for title in doc.get("next_steps", []):
            candidates.append({"title": title, "source_date": src_date})
        for title in doc.get("open_checklist", []):
            candidates.append({"title": title, "source_date": src_date})

    notes_date = collectors.mtime_date(config.NOTES_FILE)
    notes_date_s = notes_date.isoformat() if notes_date else None
    for title in notes:
        candidates.append({"title": title, "source_date": notes_date_s})

    # open_issues already folded in via docs, but include any extras explicitly.
    existing = {" ".join(c["title"].split()).lower() for c in candidates}
    for title in open_issues:
        if " ".join(title.split()).lower() not in existing:
            candidates.append({"title": title, "source_date": None})

    return candidates


def build_last_run_summary(runs: List[dict]) -> Dict[str, object]:
    if not runs:
        return {"available": False, "detail": "no run records found in app/runs"}
    last = runs[-1]
    return {
        "available": True,
        "run_id": last["run_id"],
        "target": last["target"],
        "state": last["state"],
        "exit_code": last["exit_code"],
        "success": last["success"],
        "start_time": last["start_time"],
    }


def build_health_summary(
    runs: List[dict],
    communities: List[dict],
    token_logs: List[dict],
    docs: Dict[str, dict],
) -> Dict[str, object]:
    """
    Deterministic health metrics:
      - data_completeness_score: mean of present-signal sub-metrics (0..1)
      - run_success_ratio: successful runs / total runs (None if no runs)
      - counts and a coarse status label
    """
    total_comm = len(communities)
    comm_with_output = sum(1 for c in communities if c["artifact_count"] > 0)
    comm_ratio = (comm_with_output / total_comm) if total_comm else 0.0

    docs_present = len(docs)
    docs_ratio = docs_present / len(config.DOC_FILES) if config.DOC_FILES else 0.0

    has_runs = 1.0 if runs else 0.0
    has_logs = 1.0 if token_logs else 0.0

    sub_metrics = [comm_ratio, docs_ratio, has_runs, has_logs]
    completeness = round(sum(sub_metrics) / len(sub_metrics), 3)

    total_runs = len(runs)
    successful = sum(1 for r in runs if r["success"])
    run_success_ratio = round(successful / total_runs, 3) if total_runs else None

    initialized = bool(communities or runs)
    if not initialized:
        status = "NOT_INITIALIZED"
    elif (run_success_ratio is not None and run_success_ratio < 1.0) or completeness < 0.75:
        status = "DEGRADED"
    else:
        status = "OK"

    return {
        "status": status,
        "initialized": initialized,
        "data_completeness_score": completeness,
        "run_success_ratio": run_success_ratio,
        "total_runs": total_runs,
        "successful_runs": successful,
        "total_communities": total_comm,
        "communities_with_output": comm_with_output,
        "token_log_count": len(token_logs),
        "docs_present": docs_present,
    }


def build_missing_data_flags(
    runs: List[dict],
    communities: List[dict],
    token_logs: List[dict],
    docs: Dict[str, dict],
) -> List[str]:
    flags: List[str] = []
    if not config.OUTPUTS_DIR.is_dir():
        flags.append("outputs/ directory is absent")
    if not communities:
        flags.append("no community outputs under outputs/by_community")
    else:
        empty = [c["community"] for c in communities if c["artifact_count"] == 0]
        if empty:
            flags.append("%d community dir(s) have no artifacts: %s"
                         % (len(empty), ", ".join(sorted(empty)[:8])
                            + ("…" if len(empty) > 8 else "")))
    if not runs:
        flags.append("no run records in app/runs")
    else:
        failed = [r["run_id"] for r in runs if not r["success"]]
        if failed:
            flags.append("%d run(s) did not succeed: %s"
                         % (len(failed), ", ".join(failed)))
    if not token_logs:
        flags.append("no token-usage logs in data/logs")
    if not docs:
        flags.append("no status documents (SESSION_STATE/HANDOFF/README) found")
    if not config.NOTES_FILE.is_file():
        flags.append("no manual notes file (clip_ops/notes.md) — optional")
    return flags


def build_state(ref: Optional[datetime.date] = None) -> Dict[str, object]:
    """Assemble the full canonical state dict (does not write to disk)."""
    ref = ref or config.today()

    docs = collectors.collect_docs()
    commits = collectors.collect_git_commits()
    runs = collectors.collect_runs()
    communities = collectors.collect_community_outputs()
    token_logs = collectors.collect_token_logs()
    notes = collectors.collect_notes()

    accomplishments = build_accomplishments(docs, commits)
    open_issues = build_open_issues(docs)
    candidates = build_backlog_candidates(docs, open_issues, notes)
    backlog_items = _dedup([c["title"] for c in candidates])
    health = build_health_summary(runs, communities, token_logs, docs)
    missing = build_missing_data_flags(runs, communities, token_logs, docs)
    last_run = build_last_run_summary(runs)

    if not health["initialized"]:
        missing.insert(0, "system not fully initialized — CLIP has produced no outputs or runs")

    return {
        "date": ref.isoformat(),
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "recent_accomplishments": accomplishments,
        "open_issues": open_issues,
        "backlog_items": backlog_items,
        "system_health_summary": health,
        "missing_data_flags": missing,
        "last_run_summary": last_run,
        # retained internally so run_daily can rank without recollecting:
        "_backlog_candidates": candidates,
    }


def schema_field_flags(state: Dict[str, object]) -> List[str]:
    """Return flags for any expected canonical fields missing from state."""
    return ["state.json missing field: %s" % f
            for f in config.EXPECTED_STATE_FIELDS if f not in state]
