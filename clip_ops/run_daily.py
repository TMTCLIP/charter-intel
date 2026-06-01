"""
clip_ops/run_daily.py — daily orchestrator (idempotent).

Pipeline:
  1. aggregate canonical state          -> clip_ops/state.json
  2. rank backlog                        -> clip_ops/backlog_ranked.json
  3. render digest                       -> clip_ops/daily_digest.md
  4. send email (or log a skip/failure)

Re-running overwrites the three artifacts deterministically. The process
always exits 0 unless artifact writing itself fails; email problems never
fail the run.

Cron:
  0 9 * * * cd /path/to/charter-intel && python3 -m clip_ops.run_daily
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import List

from clip_ops import backlog as backlog_mod
from clip_ops import collectors, config, digest as digest_mod, send_email
from clip_ops import state as state_mod


def _log(msg: str) -> None:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with open(config.RUN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (stamp, msg))
    except Exception:
        pass


def _write_json_atomic(path: Path, obj: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)


def run() -> int:
    ref = config.today()
    _log("run_daily starting for %s" % ref.isoformat())

    # 1 — canonical state
    state = state_mod.build_state(ref)
    candidates = state.pop("_backlog_candidates", [])

    # Self-check: flag any missing canonical fields.
    for flag in state_mod.schema_field_flags(state):
        if flag not in state["missing_data_flags"]:
            state["missing_data_flags"].append(flag)

    _write_json_atomic(config.STATE_FILE, state)
    _log("wrote %s (%d issues, %d accomplishments)"
         % (config.STATE_FILE.name,
            len(state["open_issues"]), len(state["recent_accomplishments"])))

    # 2 — ranked backlog
    corpus = collectors.build_corpus()
    ranked = backlog_mod.rank_backlog(candidates, corpus, ref=ref)
    _write_json_atomic(config.BACKLOG_FILE, ranked)
    _log("wrote %s (%d items)" % (config.BACKLOG_FILE.name, len(ranked)))

    # 3 — digest
    digest_text = digest_mod.build_digest(state, ranked)
    _write_text_atomic(config.DIGEST_FILE, digest_text)
    _log("wrote %s" % config.DIGEST_FILE.name)

    # 4 — email (never fatal)
    subject = digest_mod.extract_subject(digest_text)
    result = send_email.send_email(subject, digest_text)
    _log("email result: %s" % json.dumps(result))

    _print_summary(state, ranked, result)
    _log("run_daily complete for %s" % ref.isoformat())
    return 0


def _print_summary(state: dict, ranked: List[dict], email_result: dict) -> None:
    h = state.get("system_health_summary", {})
    print("CLIP Ops — %s" % state.get("date"))
    print("  status:            %s" % h.get("status"))
    print("  completeness:      %s" % h.get("data_completeness_score"))
    print("  run_success_ratio: %s" % h.get("run_success_ratio"))
    print("  accomplishments:   %d" % len(state.get("recent_accomplishments", [])))
    print("  open_issues:       %d" % len(state.get("open_issues", [])))
    print("  backlog_ranked:    %d" % len(ranked))
    print("  risk_flags:        %d" % len(state.get("missing_data_flags", [])))
    print("  email:             %s (%s)"
          % (email_result.get("method"), email_result.get("reason")))
    print("  artifacts:         %s, %s, %s"
          % (config.STATE_FILE.name, config.BACKLOG_FILE.name, config.DIGEST_FILE.name))


def main() -> int:
    try:
        return run()
    except Exception as exc:
        # Last-resort guard: emit a minimal valid state so downstream tooling
        # and the next run never see a half-written artifact set.
        _log("run_daily FATAL: %r" % exc)
        fallback = {
            "date": config.today().isoformat(),
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "recent_accomplishments": [],
            "open_issues": [],
            "backlog_items": [],
            "system_health_summary": {"status": "NOT_INITIALIZED", "initialized": False},
            "missing_data_flags": ["system not fully initialized — run_daily error: %r" % exc],
            "last_run_summary": {"available": False, "detail": "run_daily errored"},
        }
        try:
            _write_json_atomic(config.STATE_FILE, fallback)
            _write_json_atomic(config.BACKLOG_FILE, [])
            _write_text_atomic(
                config.DIGEST_FILE,
                "Subject: %s %s — NOT_INITIALIZED\n\n"
                "# CLIP Daily System State — %s\n\n"
                "## System Health\nSystem not fully initialized; the ops run "
                "encountered an error. See run_daily.log.\n"
                % (config.EMAIL_SUBJECT_PREFIX, fallback["date"], fallback["date"]),
            )
        except Exception:
            pass
        print("run_daily: completed with fallback state (see run_daily.log)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
