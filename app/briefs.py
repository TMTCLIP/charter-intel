"""
app/briefs.py — locate and load the .md / .html briefs a run produced.

Uses only the filesystem convention captured in config.py (verified against
pipeline/s7_render.py). The canonical community_id is learned by parsing the
"Resolved community: ... → nm-x" line from the run's stdout.log, so we never
import the pipeline's build_community_id().
"""

from __future__ import annotations

import re
from pathlib import Path

import config
import runner


def _resolved_community_ids(run_id: str) -> list[str]:
    """community_ids the pipeline resolved during this run, parsed from the log."""
    log = runner.read_log(run_id, max_bytes=1_000_000)
    ids = re.findall(config.RESOLVED_COMMUNITY_RE, log)
    # de-dupe, preserve order
    seen, out = set(), []
    for cid in ids:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def find_briefs(run: dict) -> list[dict]:
    """Locate brief files for a run.

    `run` is a meta dict (from runs.get_run / runs.list_runs). Returns a list of
    {community_id, md_path, html_path} dicts (one per community). For scan
    mode (mode 1) returns a single statewide-scan entry instead.
    """
    flags = run.get("flags", {})
    preset = (flags.get("preset") or config.PRESET_DEFAULT).strip()
    mode = (flags.get("mode") or config.MODE_DEFAULT).strip()
    state = (flags.get("state") or config.STATE_DEFAULT).strip()

    # Scan mode (mode 1) renders a statewide table under by_state, not per-city.
    if mode == "1":
        return _find_scan(state)

    results: list[dict] = []
    cids = _resolved_community_ids(run["run_id"])

    # Fallback: a community_id may have been passed directly as the target.
    target = (flags.get("target") or "").strip()
    if not cids and re.fullmatch(r"[a-z]{2}-[^\s]+", target):
        cids = [target]

    for cid in cids:
        comm_dir = config.BY_COMMUNITY_DIR / cid
        md_path = _newest(comm_dir, config.MD_BRIEF_GLOB.format(cid=cid, preset=preset, mode=mode))
        html_path = comm_dir / config.HTML_BRIEF_NAME.format(cid=cid, preset=preset, mode=mode)
        results.append({
            "community_id": cid,
            "md_path": md_path if md_path and md_path.exists() else None,
            "html_path": html_path if html_path.exists() else None,
        })
    return results


def _find_scan(state: str) -> list[dict]:
    state_dir = config.BY_STATE_DIR / state.lower()
    html_path = _newest(state_dir, config.SCAN_HTML_GLOB.format(state=state.lower()))
    return [{
        "community_id": f"{state.upper()} statewide scan",
        "md_path": None,
        "html_path": html_path if html_path else None,
    }]


def load_text(path: Path | None) -> str | None:
    """Read a brief file's contents, or None if missing."""
    if path and Path(path).exists():
        return Path(path).read_text(errors="replace")
    return None


def _newest(directory: Path, pattern: str) -> Path | None:
    """Newest file in `directory` matching glob `pattern` (by filename sort)."""
    if not directory.exists():
        return None
    matches = sorted(directory.glob(pattern))
    return matches[-1] if matches else None
