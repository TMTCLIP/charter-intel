"""
app/app.py — Streamlit wrapper around the CLIP pipeline.

Thin UI: shells out to main.py via runner.py and reads the filesystem. It never
imports pipeline/ or main.py. Three views behind a password gate: New Scan,
Live Run, History + Brief viewer.

Run locally:   streamlit run app/app.py
Deployed:      honors PORT and binds 0.0.0.0 (see app/run.sh / .streamlit).
"""

from __future__ import annotations

import hmac
import os
import re
import sys
import time
import uuid
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
import rate_limit


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

    attempts = st.session_state.get("login_attempts", 0)
    if attempts >= 10:
        st.error("Too many failed attempts. Close and reopen the app.")
        st.stop()

    st.title("CLIP")
    st.caption("Charter Intel Platform")
    pw = st.text_input("Password", type="password")
    if st.button("Enter"):
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["login_attempts"] = 0
            st.session_state["authed"] = True
            st.rerun()
        else:
            attempts += 1
            st.session_state["login_attempts"] = attempts
            time.sleep(min(attempts, 5))
            st.error("Incorrect password.")
    st.stop()


_password_gate()


# ── Per-session rate-limit token (resets on refresh/new tab) + startup cleanup ─
if "session_token" not in st.session_state:
    st.session_state["session_token"] = uuid.uuid4().hex
    rate_limit.cleanup_sessions(config.RUNS_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────
# Plain-English strategy labels mapped to the real --preset flag values.
_PRESET_LABELS = {
    "Growth": "growth",
    "Replication": "replication",
    "Turnaround": "turnaround",
    "Maturity Adjusted": "maturity_adjusted",
}


def view_new_scan() -> None:
    st.header("New Scan")

    if config.VIEWER_ONLY:
        st.info(
            "**Scan not available on this deployment.**\n\n"
            "This instance is configured as a brief viewer only. "
            "Running a new scan requires the full pipeline environment "
            "(Python 3.11+, pipeline dependencies, and NM PED data files), "
            "which are not available here.\n\n"
            "To run scans, deploy the app on Railway with a full Docker image, "
            "or run locally with `streamlit run app/app.py` from the repo root.",
            icon="ℹ️",
        )
        return

    # NOTE: deliberately NOT an st.form. Rate limiting needs the dry_run/mock
    # toggle state reactively (so an exempt scan is never blocked and the Run
    # Scan button enables the moment Dry run is switched on). st.form batches
    # widget state until submit, which would deadlock a rate-limited user who
    # wants a free dry-run. The submitted data passed to runner is identical.

    # ── Simple view (always visible) ────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        target = st.text_input("City", placeholder="Santa Fe")
    with col2:
        state = st.text_input("State", value=config.STATE_DEFAULT)

    preset_labels = list(_PRESET_LABELS.keys())
    preset_values = list(_PRESET_LABELS.values())
    default_preset_idx = (
        preset_values.index(config.PRESET_DEFAULT)
        if config.PRESET_DEFAULT in preset_values else 0
    )
    strategy_label = st.selectbox("Strategy", preset_labels, index=default_preset_idx)
    preset = _PRESET_LABELS[strategy_label]

    # ── Advanced settings (collapsed by default) ────────────────────────────
    with st.expander("Advanced settings", expanded=False):
        depth = st.selectbox(
            "Scan depth", config.DEPTH_CHOICES,
            index=config.DEPTH_CHOICES.index(config.DEPTH_DEFAULT),
        )
        mode = st.selectbox(
            "Output mode", config.MODE_CHOICES,
            index=config.MODE_CHOICES.index(config.MODE_DEFAULT),
        )

        run_all = st.toggle("All communities (--all)")
        mock = st.toggle("Use mock fixtures (--mock)")
        batch = st.toggle("Batch mode (--batch)")
        no_cache = st.toggle("Skip cache (--no-cache)")
        force_refresh = st.toggle("Force refresh (--force-refresh)")

    # ── Dry run (prominent, directly above the Run Scan button) ─────────────
    st.markdown(
        """
        <div style="border:2px solid #d93025; border-radius:8px;
                    padding:10px 14px; margin:4px 0 2px 0;
                    background-color:rgba(217,48,37,0.06);">
          <span style="color:#d93025; font-weight:700;">
            ⚠ Dry run (no API calls)
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    dry_run = st.toggle("Dry run (no API calls)", value=False)
    st.caption("Turn on to estimate cost without running a real scan")

    # ── Assemble flags + rate-limit gate ────────────────────────────────────
    form = {
        "target": target, "state": state, "depth": depth,
        "preset": preset, "mode": mode,
        "all": run_all, "dry_run": dry_run, "mock": mock, "batch": batch,
        "no_cache": no_cache, "force_refresh": force_refresh,
    }
    exempt = rate_limit.is_exempt(form)
    token = st.session_state["session_token"]
    session_count = rate_limit.get_session_count(config.RUNS_DIR, token)
    status = rate_limit.get_rate_limit_status(config.RUNS_DIR, session_count)

    st.caption(
        f"This session: {status['session_used']}/{status['session_max']} jobs · "
        f"Today: {status['daily_used']}/{status['daily_max']} jobs · "
        f"Est. spend today: ${status['estimated_spend']:.2f} / ${status['cost_cap']:.2f}"
    )
    if exempt:
        st.caption("✓ Dry run / mock — does not count toward limits")
    elif status["is_blocked"]:
        st.warning(status["block_reason"])

    # Exempt scans are never blocked (principle: zero API cost).
    disabled = status["is_blocked"] and not exempt
    submitted = st.button("Run Scan", type="primary", disabled=disabled)

    if submitted:
        if not target and not run_all:
            st.error("Provide a target community or enable --all.")
            return
        # Defense in depth: enforce the limit at launch even if the button
        # state lagged (only non-exempt scans are gated).
        if status["is_blocked"] and not exempt:
            st.warning(status["block_reason"])
            return
        preview = runner.build_command(form)
        run_id = runner.launch_run(form)
        if not exempt:
            rate_limit.record_job(config.RUNS_DIR, token, rate_limit.get_job_weight(form))
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


def parse_run_slug(run_id: str) -> dict:
    """Extract state_code and city_display from a run_id or community slug.

    Accepts both formats:
      "nm-rio-rancho"           → {state_code: "NM", city_display: "Rio Rancho"}
      "scan-nm-rio-rancho-001"  → same

    Falls back gracefully: state_code="" and city_display=run_id on parse failure.
    """
    try:
        parts = run_id.strip().lower().split("-")
        # Community slug format: first segment is a 2-letter state code.
        # e.g. "nm-rio-rancho" from the run's `target` field.
        if len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha():
            return {
                "state_code": parts[0].upper(),
                "city_display": " ".join(parts[1:]).title(),
                "run_id": run_id,
            }
        # Run ID format: {prefix}-{state}-{city...}-{digits}
        # e.g. "scan-nm-rio-rancho-001" → drop prefix + trailing digit block.
        if len(parts) >= 3:
            inner = parts[1:]                       # drop prefix ("scan", "demo", …)
            if inner and inner[-1].isdigit():
                inner = inner[:-1]                  # drop numeric suffix ("001")
            if inner and len(inner[0]) == 2 and inner[0].isalpha():
                return {
                    "state_code": inner[0].upper(),
                    "city_display": " ".join(inner[1:]).title() or run_id,
                    "run_id": run_id,
                }
    except Exception:
        pass
    return {"state_code": "", "city_display": run_id, "run_id": run_id}


def _is_junk_slug(target: str) -> bool:
    """Return True if the target community slug is a junk/address-fragment entry.

    Excluded after stripping the 2-letter state prefix when the first city
    segment is:
      a) purely numeric  (e.g. nm-66-edgewood, nm-4386-shiprock)
      b) a known address-fragment abbreviation  (bld, bldg, ste)
      c) a highway-style prefix  (us-NNN, nm-NNN)
    """
    try:
        parts = target.strip().lower().split("-")
        if not (len(parts) >= 2 and len(parts[0]) == 2 and parts[0].isalpha()):
            return False
        city_parts = parts[1:]
        if not city_parts:
            return False
        first  = city_parts[0]
        second = city_parts[1] if len(city_parts) > 1 else ""
        if re.match(r"^\d+$", first):                             # (a) numeric
            return True
        if re.match(r"^bldg?\.?$", first) or first == "ste":     # (b) address abbrev
            return True
        if first in ("us", "nm") and re.match(r"^\d+$", second): # (c) highway prefix
            return True
    except Exception:
        pass
    return False


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

    # Build display label for each run — prefer the `target` community slug for
    # human-readable parsing; fall back to run_id if target is absent.
    # Junk slugs (numeric-prefixed / address-fragment targets) are skipped.
    run_meta: list[dict] = []
    for r in all_runs:
        target = r.get("target") or ""
        if _is_junk_slug(target):
            continue
        p = parse_run_slug(target or r["run_id"])
        date_str = (r.get("start_time") or "")[:10]
        label = f"{p['city_display']}  ({date_str})" if date_str else p["city_display"]
        run_meta.append({
            "run_id": r["run_id"],
            "state_code": p["state_code"],
            "city_display": p["city_display"],
            "label": label,
        })
    run_meta.sort(key=lambda m: m["city_display"].casefold())

    # State filter — skip entirely when only one state is represented.
    unique_states = sorted({m["state_code"] for m in run_meta if m["state_code"]})
    if len(unique_states) > 1:
        chosen_state = st.selectbox("State", ["All States"] + unique_states)
        visible = [m for m in run_meta
                   if chosen_state == "All States" or m["state_code"] == chosen_state]
    else:
        visible = run_meta

    if not visible:
        st.info("No runs for the selected state.")
        return

    visible_ids = [m["run_id"] for m in visible]
    label_map = {m["run_id"]: m["label"] for m in visible}
    active_id = st.session_state.get("active_run_id")
    default_idx = visible_ids.index(active_id) if active_id in visible_ids else 0
    selected = st.selectbox(
        "Select a run to view its briefs",
        visible_ids,
        format_func=lambda rid: label_map.get(rid, rid),
        index=default_idx,
    )

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
                st.download_button(
                    label="🖨 Download & Print",
                    data=html,
                    file_name=f"{entry['community_id']}_brief.html",
                    mime="text/html",
                    key=f"dl_{entry['community_id']}",
                )
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

_ALLIGATOR = r"""
      ___
  .-'   '-.
 /  0   0  \
|    ___    |
 \  '---'  /
  '-._____.-'
   CLIP 🐊
"""

with st.sidebar:
    st.markdown(
        "<div style='font-size:2.2rem; font-weight:800; color:#1B2A47; "
        "line-height:1;'>CLIP</div>",
        unsafe_allow_html=True,
    )
    st.caption("Charter Intel Platform — pipeline wrapper")
    nav = st.radio("View", list(VIEWS.keys()),
                   index=list(VIEWS.keys()).index(st.session_state.get("nav", "New Scan")))
    st.session_state["nav"] = nav
    st.divider()
    st.caption(f"Interpreter: `{config.PYTHON_BIN}`")
    st.caption(f"Repo: `{config.REPO_ROOT}`")

    # Hidden mascot — curious users find it, everyone else ignores it.
    with st.expander("·", expanded=False):
        st.code(_ALLIGATOR, language=None)
        st.caption("CLIP watches the market so you don't have to.")

# ── Brand header bar (top of main content area) ─────────────────────────────
st.markdown(
    "<div style='border-left:6px solid #82c341; padding:4px 0 4px 14px; "
    "margin-bottom:18px;'>"
    "<div style='font-size:1.6rem; font-weight:800; color:#1B2A47; "
    "line-height:1.15;'>CLIP</div>"
    "<div style='font-size:0.95rem; color:#1B2A47;'>"
    "Charter Community Landscape Intelligence Platform</div>"
    "</div>",
    unsafe_allow_html=True,
)

VIEWS[nav]()
