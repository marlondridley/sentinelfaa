"""
Ingest & Replay page: sample-data generation, replay-from-directory,
Kit-log maintenance, replay test results, and the live SWIM folder status
panel.

This is the operational/admin tooling that used to live in the single-file
dashboard's global sidebar plus its "Replay Test Results Panel" and "Live
SWIM Folder Status Panel" sections. It's grouped onto one page rather than
duplicated across every page's sidebar, since these are one-off
maintenance actions rather than something an operator needs available no
matter which page they're viewing.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import pandas as pd
import streamlit as st

from lib import theme
from lib.data import KIT_LOG_PATH
from lib.page_setup import bootstrap_page
from src import config
from src.auth import Decision, can_decide
from src.config import settings
from src.async_bridge import run_async as run
from src.maintenance import purge_old_kit_files
from src.pipeline import run_pipeline
from src.replay import replay_from_directory
from src.swim_file_ingest import (
    commit_scan,
    message_files,
    read_failure_reasons,
    read_ingest_log,
    scan_swim_folder,
)

_principal = bootstrap_page(page_title="Ingest & Replay — PCT FeedOps Sentinel", page_icon="📥")

# Replay and Scan stage reviews and Clean deletes Kit messages, so they are for the
# people who operate the console: a signed-in watch_officer or supervisor. An auditor
# reads, and a visitor with no verified identity does not change anything here.
_MAY_OPERATE = can_decide(_principal, Decision.MONITOR, allow_unverified=settings.ALLOW_UNVERIFIED_DECISIONS)
_OPERATORS_ONLY = "Requires the watch_officer or supervisor role and a verified sign-in."


def _operate(label: str, **kwargs) -> bool:
    """
    A button that only an operator's click counts for. Anyone else sees it
    disabled with the reason, and a click that reaches the server anyway does nothing.
    """
    kwargs["disabled"] = bool(kwargs.get("disabled")) or not _MAY_OPERATE
    if not _MAY_OPERATE:
        kwargs["help"] = _OPERATORS_ONLY
    return st.button(label, **kwargs) and _MAY_OPERATE


st.title("📥 Ingest & Replay")
st.caption(
    "Replay recorded SWIM files, scan the live swim_live/ folder, and review "
    "recent ingest activity. Real FAA data only."
)

# ── Controls ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙ Controls")

    st.subheader("Replay")
    replay_files = list(config.RAW_DIR.glob("*.json")) if config.RAW_DIR.exists() else []
    st.caption(f"{len(replay_files)} file(s) in data/raw/")
    if _operate("Run Replay from data/raw", use_container_width=True,
                disabled=len(replay_files) == 0):
        with st.spinner("Replaying …"):
            r_events, r_errors = replay_from_directory()
            summary = run(run_pipeline(r_events, label="REPLAY")) if r_events else None
        if r_errors:
            st.warning(f"{len(r_errors)} parse error(s). Check console.")
        if summary:
            st.success(
                f"Replay complete · {summary['event_count']} events · "
                f"{summary['alert_count']} alerts · {summary['staged_count']} staged for review."
            )
        else:
            st.info("Replay found no events.")

    st.divider()
    st.subheader("Kit Log Maintenance")
    cleanup_minutes = st.number_input(
        "Purge files older than (minutes)",
        min_value=1, max_value=60, value=5, step=1,
    )
    st.caption("Deletes Kit messages older than this that the watcher has not ingested. Operators only.")
    if _operate("Clean Kit log now", use_container_width=True):
        removed, remaining = purge_old_kit_files(KIT_LOG_PATH, max_age_minutes=cleanup_minutes)
        st.success(f"Removed {removed} file(s). {remaining} remain in Kit log.")

    st.divider()
    # The database address is not shown: any signed-in user reads this page, and a host and port are a map
    # for someone who is not meant to have one. Operators have the compose file.
    st.caption(f"Refreshed: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    # A refresh only refreshes: deleting Kit files is the explicit button above.
    if st.button("Refresh dashboard", use_container_width=True, type="primary"):
        st.rerun()

# ── Replay Test Results Panel ─────────────────────────────────────────────────
st.subheader("Replay Test Results")
replay_files_list = sorted(config.RAW_DIR.glob("*.json")) if config.RAW_DIR.exists() else []
if replay_files_list:
    file_data = [
        {"File": f.name, "Size": f"{f.stat().st_size:,} bytes"}
        for f in replay_files_list
    ]
    st.dataframe(pd.DataFrame(file_data), hide_index=True, use_container_width=True)
    st.caption("Click 'Run Replay from data/raw' in the sidebar to process these files.")
else:
    st.info(
        "No replay files found in data/raw/. Copy captured SWIM files there "
        "to replay them; the live feed lands in data/raw/swim_live/ on its own."
    )

st.divider()

# ── Live SWIM Folder Status Panel ─────────────────────────────────────────────
st.subheader("Live SWIM Folder Status")
st.caption(
    "This panel reflects the output folder written by the FAA SCDS Jumpstart JMS consumer."
)

swim_live_dir = config.SWIM_RAW_FOLDER
failed_dir = config.DATA_DIR / "failed"
processed_dir = config.PROCESSED_DIR

sf1, sf2, sf3, sf4 = st.columns(4)

pending_files = message_files(swim_live_dir) if swim_live_dir.exists() else []
processed_files = list([p for p in processed_dir.iterdir() if p.is_file()]) if processed_dir.exists() else []
processed_files = [f for f in processed_files if f.name != "ingest_log.jsonl"]
failed_files = list([p for p in failed_dir.iterdir() if p.is_file()]) if failed_dir.exists() else []

sf1.metric("Pending Files", len(pending_files), help="Kit messages waiting; the live watcher takes them within seconds")
sf2.metric("Processed Files", len(processed_files), help="Successfully ingested")
sf3.metric("Failed Files", len(failed_files), help="Kept in data/failed; the reasons are below")

ingest_log_entries = read_ingest_log(limit=50)
processed_entries = [e for e in ingest_log_entries if e.get("status") == "processed"]
if processed_entries:
    latest_ts = processed_entries[0].get("timestamp", "")
    latest_svc = processed_entries[0].get("services", [])
    sf4.metric("Latest Ingest", latest_ts[11:19] + " UTC" if latest_ts else "--")
    st.caption(f"Latest service(s) detected: {', '.join(latest_svc) if latest_svc else '--'}")
else:
    sf4.metric("Latest Ingest", "--")

col_ingest, col_hint = st.columns([1, 2])
with col_ingest:
    if _operate("Scan swim_live/ Now", use_container_width=True,
                disabled=len(pending_files) == 0):
        with st.spinner("Scanning swim_live/ ..."):
            sw_events, sw_stats = scan_swim_folder(swim_live_dir, defer_archive=True)
            summary = run(run_pipeline(sw_events, label="SWIM_SCAN")) if sw_events else None
            commit_scan(sw_stats)   # archived and logged only now that the events are stored
        st.session_state["swim_scan_result"] = (
            f"Ingested {sw_stats.events_parsed} events from "
            f"{sw_stats.files_processed} file(s). "
            f"{sw_stats.files_failed} failed, "
            f"{sw_stats.files_withheld} withheld (blockAtFAA). "
            f"{summary['alert_count'] if summary else 0} alert(s), "
            f"{summary['staged_count'] if summary else 0} staged for review."
        )
        st.rerun()
    # Shown after the rerun that refreshes the counts above, which used to erase it.
    if scan_result := st.session_state.pop("swim_scan_result", None):
        st.success(scan_result)

with col_hint:
    if len(pending_files) == 0:
        st.info(
            "No Kit messages are waiting in the live SWIM folder: the live watcher ingests "
            "each one within seconds. If this stays empty, the feed is not running; "
            "start it with `jumpstart\run-live.ps1` (or the launcher's `-WithLiveFeed`)."
        )

failure_reasons = read_failure_reasons(limit=10)
if failure_reasons:
    with st.expander("Why files failed, by recorded reason", expanded=True):
        st.dataframe(pd.DataFrame([
            {"Reason": r["reason"], "Attempts": r["files"], "Last seen (UTC)": f"{r['last_seen']:%Y-%m-%d %H:%M}"}
            for r in failure_reasons
        ]), hide_index=True, use_container_width=True)

if ingest_log_entries:
    with st.expander("Ingest audit log (latest 50 entries)"):
        log_df = pd.DataFrame(ingest_log_entries)
        st.dataframe(log_df, hide_index=True, use_container_width=True)

st.divider()
st.caption(
    "NAS FeedOps Sentinel · FAA SWIM/STDDS PCT · "
    "Real FAA data: live from the SCDS Jumpstart Kit, or replayed captures."
)
theme.back_link()
