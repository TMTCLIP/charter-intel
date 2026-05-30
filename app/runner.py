"""
app/runner.py — launch a CLIP pipeline run as a detached subprocess.

Contract with the pipeline is CLI + filesystem only. We build the argv from
form inputs, launch `python main.py [flags]` detached with cwd=REPO_ROOT, pipe
stdout+stderr into the run's stdout.log, and return immediately. Status is
tracked via files (meta.json / status.json) so it survives Streamlit reruns.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import signal
import subprocess
import uuid
from pathlib import Path

import config


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _slugify_target(target: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (target or "all").strip().lower()).strip("-")
    return s or "all"


def build_command(form: dict) -> list[str]:
    """Build the python argv for main.py from submitted form inputs.

    `form` keys (all optional): target (str), state (str), depth (str),
    preset (str), mode (str), booleans matching config.BOOLEAN_FLAGS keys,
    and extra_args (str, appended verbatim).
    """
    argv: list[str] = [str(config.PYTHON_BIN), str(config.MAIN_PY)]

    target = (form.get("target") or "").strip()
    if target:
        argv.append(target)  # positional community (city name or community_id)

    state = (form.get("state") or "").strip()
    if state:
        argv += ["--state", state]

    depth = (form.get("depth") or "").strip()
    if depth:
        argv += ["--depth", depth]

    preset = (form.get("preset") or "").strip()
    if preset:
        argv += ["--preset", preset]

    mode = (form.get("mode") or "").strip()
    if mode:
        argv += ["--mode", mode]

    for key, flag in config.BOOLEAN_FLAGS.items():
        if form.get(key):
            argv.append(flag)

    extra = (form.get("extra_args") or "").strip()
    if extra:
        argv += shlex.split(extra)

    return argv


def launch_run(form: dict) -> str:
    """Launch a detached pipeline run. Returns the run_id immediately."""
    run_id = f"{_slugify_target(form.get('target', ''))}_{_dt.datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    argv = build_command(form)
    log_path = run_dir / config.STDOUT_LOG_NAME
    exit_code_path = run_dir / config.EXIT_CODE_NAME

    # Wrap in a shell purely so we can capture the pipeline's exit code for a
    # detached process we don't wait on. The pipeline itself is invoked exactly
    # as `argv`; only `echo $? > exit_code` is appended by us.
    wrapper = f"{shlex.join(argv)}\necho $? > {shlex.quote(str(exit_code_path))}\n"

    log_fh = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", wrapper],
            cwd=str(config.REPO_ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=config.subprocess_env(),
            start_new_session=True,  # detach: own process group, survives reruns
        )
    finally:
        log_fh.close()

    flags = {
        "target": form.get("target", ""),
        "state": form.get("state", ""),
        "depth": form.get("depth", ""),
        "preset": form.get("preset", ""),
        "mode": form.get("mode", ""),
        **{k: bool(form.get(k)) for k in config.BOOLEAN_FLAGS},
        "extra_args": form.get("extra_args", ""),
    }

    meta = {
        "run_id": run_id,
        "target": form.get("target", "") or "(all / unspecified)",
        "flags": flags,
        "command": argv,
        "start_time": _now_iso(),
        "pid": proc.pid,
    }
    _write_json(run_dir / config.META_NAME, meta)
    _write_json(run_dir / config.STATUS_NAME, {"state": "running", "exit_code": None})

    return run_id


def refresh_status(run_id: str) -> dict:
    """Re-check a run's process / exit code and update status.json. Returns it."""
    run_dir = config.RUNS_DIR / run_id
    status_path = run_dir / config.STATUS_NAME
    status = _read_json(status_path) or {"state": "running", "exit_code": None}

    # Terminal states are sticky.
    if status.get("state") in ("done", "failed"):
        return status

    exit_code_path = run_dir / config.EXIT_CODE_NAME
    if exit_code_path.exists():
        raw = exit_code_path.read_text().strip()
        try:
            code = int(raw)
        except ValueError:
            code = None
        status = {
            "state": "done" if code == 0 else "failed",
            "exit_code": code,
        }
        _write_json(status_path, status)
        return status

    # No exit-code file yet: is the process still alive?
    meta = _read_json(run_dir / config.META_NAME) or {}
    pid = meta.get("pid")
    if pid and _pid_alive(pid):
        status = {"state": "running", "exit_code": None}
    else:
        # Process gone but never wrote an exit code → crashed before the wrapper
        # could record it.
        status = {"state": "failed", "exit_code": None}
    _write_json(status_path, status)
    return status


def stop_run(run_id: str) -> bool:
    """Best-effort terminate a running run's process group. Returns True if signalled."""
    meta = _read_json(config.RUNS_DIR / run_id / config.META_NAME) or {}
    pid = meta.get("pid")
    if not pid or not _pid_alive(pid):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_log(run_id: str, max_bytes: int = 200_000) -> str:
    """Return the tail of a run's stdout.log (last max_bytes)."""
    log_path = config.RUNS_DIR / run_id / config.STDOUT_LOG_NAME
    if not log_path.exists():
        return ""
    size = log_path.stat().st_size
    with open(log_path, "r", errors="replace") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        return f.read()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
