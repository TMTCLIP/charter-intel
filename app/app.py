"""
app/app.py — Streamlit wrapper around the CLIP pipeline.

Thin UI: shells out to main.py via runner.py and reads the filesystem. It never
imports pipeline/ or main.py. Three views behind a password gate: New Scan,
Live Run, History + Brief viewer.

Run locally:   streamlit run app/app.py
Deployed:      honors PORT and binds 0.0.0.0 (see app/run.sh / .streamlit).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Streamlit executes this file as a top-level script; ensure sibling modules
# (config, runner, runs, briefs) are importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st
import streamlit.components.v1 as components

import config
import runner
import runs as runs_mod
import briefs as briefs_mod


st.set_page_config(page_title="CLIP — Charter Intel Platform", layout="wide")


# ─────────────────────────────────────────────────────────────────────────────
# Password gate (rendered before any other UI)
# ─────────────────────────────────────────────────────────────────────────────
def _get_password() -> str | None:
    """Resolve the gate password: st.secrets (Streamlit Community Cloud) first,
    then the process environment (config.get_password reads os.environ)."""
    try:
        if config.PASSWORD_ENV in st.secrets:
            return st.secrets[config.PASSWORD_ENV]
    except Exception:
        # No secrets.toml present (e.g. local dev) — fall through to env.
        pass
    return config.get_password()


def _password_gate() -> bool:
    expected = _get_password()
    if not expected:
        st.error(
            f"**{config.PASSWORD_ENV} is not set.** The app refuses to start "
            f"without a password. Set the `{config.PASSWORD_ENV}` environment "
            f"variable (the repo's .env can populate it) and reload."
        )
        st.stop()

    if st.session_state.get("authed"):
        return True

    st.title("CLIP")
    st.caption("Charter Intel Platform")
    pw = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pw == expected:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()


_password_gate()


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────
def view_new_scan() -> None:
    st.header("New Scan")
    st.caption("Launches `main.py` as a detached subprocess. The CLI + filesystem "
               "are the only contract — no pipeline code is imported.")

    with st.form("new_scan"):
        col1, col2 = st.columns(2)
        with col1:
            target = st.text_input(
                "Target (city name or community_id)",
                placeholder="Santa Fe   |   nm-santa-fe",
                help="Leave blank only if using --all.",
            )
            state = st.text_input("State", value=config.STATE_DEFAULT)
            depth = st.selectbox("Depth (--depth)", config.DEPTH_CHOICES,
                                 index=config.DEPTH_CHOICES.index(config.DEPTH_DEFAULT))
        with col2:
            preset = st.selectbox("Preset (--preset)", config.PRESET_CHOICES,
                                  index=config.PRESET_CHOICES.index(config.PRESET_DEFAULT))
            mode = st.selectbox("Mode (--mode)", config.MODE_CHOICES,
                                index=config.MODE_CHOICES.index(config.MODE_DEFAULT))

        st.markdown("**Toggles**")
        t1, t2, t3 = st.columns(3)
        with t1:
            run_all = st.toggle("All communities (--all)")
            dry_run = st.toggle("Dry run (--dry-run)", value=True)
        with t2:
            mock = st.toggle("Mock fixtures (--mock)")
            batch = st.toggle("Batch API (--batch)")
        with t3:
            no_cache = st.toggle("No cache (--no-cache)")
            force_refresh = st.toggle("Force refresh (--force-refresh)")

        extra_args = st.text_input(
            "Extra args (appended verbatim)",
            placeholder="--stages s5,s6,s7",
            help="Anything not covered above, e.g. --stages, --record, --interactive.",
        )

        submitted = st.form_submit_button("Launch scan", type="primary")

    if submitted:
        if not target and not run_all:
            st.error("Provide a target community or enable --all.")
            return
        form = {
            "target": target, "state": state, "depth": depth,
            "preset": preset, "mode": mode,
            "all": run_all, "dry_run": dry_run, "mock": mock, "batch": batch,
            "no_cache": no_cache, "force_refresh": force_refresh,
            "extra_args": extra_args,
        }
        preview = runner.build_command(form)
        run_id = runner.launch_run(form)
        st.session_state["active_run_id"] = run_id
        st.session_state["nav"] = "Live Run"
        st.success(f"Launched run `{run_id}`")
        st.code(" ".join(preview), language="bash")
        st.rerun()


def _parse_stage_progress(log: str) -> list[str]:
    """Stages observed as started, in order, de-duped."""
    started = re.findall(config.STAGE_START_RE, log)
    seen, out = set(), []
    for s in started:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def view_live_run() -> None:
    st.header("Live Run")
    run_id = st.session_state.get("active_run_id")
    if not run_id:
        st.info("No active run. Start one from **New Scan** or pick one in **History**.")
        return

    status = runner.refresh_status(run_id)
    state = status.get("state", "unknown")
    log = runner.read_log(run_id)

    top = st.columns([3, 1, 1])
    with top[0]:
        st.markdown(f"**Run:** `{run_id}`")
    with top[1]:
        badge = {"running": "🟡 running", "done": "🟢 done", "failed": "🔴 failed"}.get(state, state)
        st.markdown(f"**Status:** {badge}")
    with top[2]:
        if state == "running" and st.button("Stop run"):
            runner.stop_run(run_id)
            st.rerun()

    # Stage progress (if markers present) else raw tail only.
    observed = _parse_stage_progress(log)
    if observed:
        done_count = len(observed)
        if state == "done":
            done_count = config.STAGE_COUNT
        st.progress(min(done_count / config.STAGE_COUNT, 1.0),
                    text=f"{done_count}/{config.STAGE_COUNT} stages")
        cols = st.columns(config.STAGE_COUNT)
        for i, stage in enumerate(config.STAGE_ORDER):
            mark = "✅" if stage in observed else ("⏳" if state == "running" else "⬜")
            cols[i].markdown(f"{mark}<br><small>{stage.replace('_', ' ')}</small>",
                             unsafe_allow_html=True)
    else:
        st.caption("No stage markers detected yet — showing raw log tail.")

    st.subheader("Log")
    st.code(log or "(waiting for output...)", language="text")

    if state == "running":
        # Short poll: rerun after a brief pause so the log/status refresh.
        import time
        time.sleep(2)
        st.rerun()
    elif state == "done":
        st.success("Run complete. Open it in **History** to view briefs.")
    else:
        st.error(f"Run failed (exit code {status.get('exit_code')}). Check the log above.")


def view_history() -> None:
    st.header("History + Briefs")
    all_runs = runs_mod.list_runs()
    if not all_runs:
        st.info("No runs yet.")
        return

    rows = [{
        "run_id": r["run_id"],
        "target": r["target"],
        "state": r["state"],
        "exit_code": r["exit_code"],
        "start_time": r["start_time"],
        "preset": r["flags"].get("preset", ""),
        "depth": r["flags"].get("depth", ""),
        "mode": r["flags"].get("mode", ""),
    } for r in all_runs]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    run_ids = [r["run_id"] for r in all_runs]
    default_idx = run_ids.index(st.session_state["active_run_id"]) \
        if st.session_state.get("active_run_id") in run_ids else 0
    selected = st.selectbox("Select a run to view its briefs", run_ids, index=default_idx)

    run = runs_mod.get_run(selected)
    if not run:
        return

    with st.expander("Run details"):
        st.json({"command": run.get("command"), "flags": run.get("flags"),
                 "state": run.get("state"), "exit_code": run.get("exit_code")})

    brief_entries = briefs_mod.find_briefs(run)
    if not brief_entries:
        st.warning("No briefs located for this run yet.")
        return

    for entry in brief_entries:
        st.subheader(entry["community_id"])
        html = briefs_mod.load_text(entry.get("html_path"))
        md = briefs_mod.load_text(entry.get("md_path"))

        if not html and not md:
            st.caption("No brief files found on disk for this community.")
            continue

        tabs = st.tabs(["HTML brief", "Markdown brief"])
        with tabs[0]:
            if html:
                st.caption(str(entry["html_path"]))
                components.html(html, height=900, scrolling=True)
            else:
                st.caption("No .html brief found.")
        with tabs[1]:
            if md:
                st.caption(str(entry["md_path"]))
                st.markdown(md)
            else:
                st.caption("No .md brief found.")


# ─────────────────────────────────────────────────────────────────────────────
# Navigation
# ─────────────────────────────────────────────────────────────────────────────
VIEWS = {
    "New Scan": view_new_scan,
    "Live Run": view_live_run,
    "History + Briefs": view_history,
}

with st.sidebar:
    st.title("CLIP")
    st.caption("Charter Intel Platform — pipeline wrapper")
    nav = st.radio("View", list(VIEWS.keys()),
                   index=list(VIEWS.keys()).index(st.session_state.get("nav", "New Scan")))
    st.session_state["nav"] = nav
    st.divider()
    st.caption(f"Interpreter: `{config.PYTHON_BIN}`")
    st.caption(f"Repo: `{config.REPO_ROOT}`")

VIEWS[nav]()
