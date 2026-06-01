"""
clip_ops/digest.py — render the daily email digest (Markdown) from the
canonical state and the ranked backlog. Pure string synthesis; no narrative
expansion beyond the structured fields.
"""
from __future__ import annotations

from typing import Dict, List

from clip_ops import config


def build_subject(state: Dict[str, object]) -> str:
    health = state.get("system_health_summary", {}) or {}
    status = health.get("status", "UNKNOWN")
    n_issues = len(state.get("open_issues", []) or [])
    n_backlog = len(state.get("backlog_items", []) or [])
    return "%s %s — %s — %d open issue(s), %d backlog" % (
        config.EMAIL_SUBJECT_PREFIX, state.get("date", ""), status, n_issues, n_backlog
    )


def build_health_sentences(state: Dict[str, object]) -> List[str]:
    h = state.get("system_health_summary", {}) or {}
    sentences: List[str] = []
    sentences.append("Overall status is %s." % h.get("status", "UNKNOWN"))
    sentences.append(
        "Data completeness is %s with %d of %d communities producing output."
        % (h.get("data_completeness_score"),
           h.get("communities_with_output", 0),
           h.get("total_communities", 0))
    )
    rsr = h.get("run_success_ratio")
    if rsr is None:
        sentences.append("No pipeline run records were available to assess run success.")
    else:
        sentences.append(
            "Run success ratio is %s across %d recorded run(s)."
            % (rsr, h.get("total_runs", 0))
        )
    sentences.append(
        "%d token-usage log(s) and %d status document(s) were detected."
        % (h.get("token_log_count", 0), h.get("docs_present", 0))
    )
    flags = state.get("missing_data_flags", []) or []
    if flags:
        sentences.append("%d missing-data flag(s) require attention." % len(flags))
    else:
        sentences.append("No missing-data flags were raised.")
    return sentences[:config.HEALTH_MAX_SENTENCES]


def _bullets(items: List[str], limit: int, empty: str) -> str:
    if not items:
        return "- _%s_" % empty
    return "\n".join("- %s" % i for i in items[:limit])


def _numbered(items: List[str], limit: int, empty: str) -> str:
    if not items:
        return "_%s_" % empty
    return "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(items[:limit]))


def build_digest(state: Dict[str, object], ranked_backlog: List[dict]) -> str:
    """Return the full Markdown digest, beginning with a `Subject:` line."""
    subject = build_subject(state)
    health_sentences = build_health_sentences(state)

    accomplishments = state.get("recent_accomplishments", []) or []
    open_issues = state.get("open_issues", []) or []
    risk_flags = state.get("missing_data_flags", []) or []

    action_lines = [
        "[score %d | %s] %s" % (b["priority_score"], b["category"], b["title"])
        for b in ranked_backlog
    ]

    last_run = state.get("last_run_summary", {}) or {}
    if last_run.get("available"):
        last_run_line = ("%s → %s (exit %s) target=%s"
                         % (last_run.get("run_id"),
                            last_run.get("state"),
                            last_run.get("exit_code"),
                            last_run.get("target")))
    else:
        last_run_line = last_run.get("detail", "no run records")

    lines: List[str] = []
    lines.append("Subject: %s" % subject)
    lines.append("")
    lines.append("# CLIP Daily System State — %s" % state.get("date", ""))
    lines.append("_Generated %s · deterministic ops layer · no LLM_"
                 % state.get("generated_at", ""))
    lines.append("")
    lines.append("## System Health")
    lines.append(" ".join(health_sentences))
    lines.append("")
    lines.append("Last run: %s" % last_run_line)
    lines.append("")
    lines.append("## Completed Work")
    lines.append(_bullets(accomplishments, limit=10,
                          empty="No recent accomplishments detected"))
    lines.append("")
    lines.append("## Open Issues (top %d)" % config.TOP_ISSUES)
    lines.append(_numbered(open_issues, config.TOP_ISSUES,
                           empty="No open issues detected"))
    lines.append("")
    lines.append("## Ranked Next Actions (top %d)" % config.TOP_ACTIONS)
    lines.append(_numbered(action_lines, config.TOP_ACTIONS,
                           empty="No backlog items detected"))
    lines.append("")
    lines.append("## Risk Flags")
    lines.append(_bullets(risk_flags, limit=10, empty="No risk flags"))
    lines.append("")
    return "\n".join(lines)


def extract_subject(digest_md: str, default: str = "CLIP Daily System State") -> str:
    """Pull the `Subject:` line from a rendered digest."""
    for line in digest_md.splitlines():
        if line.lower().startswith("subject:"):
            return line.split(":", 1)[1].strip()
    return default
