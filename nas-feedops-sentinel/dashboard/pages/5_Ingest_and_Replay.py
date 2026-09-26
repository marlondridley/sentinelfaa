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
from src.async_bridge import run_async as run
from src.maintenance import purge_old_kit_files
from src.pipeline import run_pipeline
from src.replay import replay_from_directory
from src.sample_data import generate_sample_batch
from src.swim_file_ingest import read_ingest_log, scan_swim_folder

bootstrap_page(page_title="Ingest & Replay — PCT FeedOps Sentinel", page_icon="📥")

st.title("📥 Ingest & Replay")
st.caption(
    "Generate sample data, replay recorded SWIM files, scan the live "
    "swim_live/ folder, and review recent ingest activity."
)

# ── Controls ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙ Controls")

    gen_count = st.slider("Sample events to generate", min_value=10, max_value=500,
                          value=100, step=10)

    if st.button("Generate & Ingest Sample Data", use_container_width=True, type="primary"):
        # Through the one pipeline, so CRITICAL alerts from generated data are
        # staged for review exactly as live ones are. This used to validate,
        # save and alert inline and stop there -- no critic, no staging.
        with st.spinner("Generating …"):
            summary = run(run_pipeline(generate_sample_batch(gen_count), label="SAMPLE"))
        st.success(
            f"Ingested {summary['event_count']} events · {summary['alert_count']} alerts · "
            f"{summary['staged_count']} staged for review."
        )

    st.divider()

    st.subheader("Replay")
    replay_files = list(config.RAW_DIR.glob("*.json")) if config.RAW_DIR.exists() else []
    st.caption(f"{len(replay_files)} file(s) in data/raw/")
    if st.button("Run Replay from data/raw", use_container_width=True,
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
    if st.button("Clean Kit log now", use_container_width=True):
        removed, remaining = purge_old_kit_files(KIT_LOG_PATH, max_age_minutes=cleanup_minutes)
        st.success(f"Removed {removed} file(s). {remaining} remain in Kit log.")

    st.divider()
    # config.DATABASE_URL embeds the DB password in plain text, so it's never
    # shown in the UI -- this is host:port/db only, no credentials. (The
    # original single-file dashboard referenced a since-removed `config.DB_PATH`
    # here, a leftover from before this project's SQLite-to-Postgres migration;
    # it would raise AttributeError the moment this line rendered.)
    st.caption(f"DB: `{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{config.POSTGRES_DB or '—'}`")
    st.caption(f"Refreshed: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    if st.button("Refresh dashboard (auto-cleanup)", use_container_width=True, type="primary"):
        removed, remaining = purge_old_kit_files(KIT_LOG_PATH, max_age_minutes=cleanup_minutes)
        if removed > 0:
            st.toast(f"Cleaned {removed} Kit file(s) older than {cleanup_minutes} min")
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
        "No replay files found in data/raw/. "
        "Generate some first: `python -m src.main --mode sample`"
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

pending_files = list([p for p in swim_live_dir.iterdir() if p.is_file()]) if swim_live_dir.exists() else []
processed_files = list([p for p in processed_dir.iterdir() if p.is_file()]) if processed_dir.exists() else []
processed_files = [f for f in processed_files if f.name != "ingest_log.jsonl"]
failed_files = list([p for p in failed_dir.iterdir() if p.is_file()]) if failed_dir.exists() else []

sf1.metric("Pending Files", len(pending_files), help="Files waiting in swim_live/")
sf2.metric("Processed Files", len(processed_files), help="Successfully ingested")
sf3.metric("Failed Files", len(failed_files), help="Parse or normalisation errors")

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
    if st.button("Scan swim_live/ Now", use_container_width=True,
                 disabled=len(pending_files) == 0):
        with st.spinner("Scanning swim_live/ ..."):
            sw_events, sw_stats = scan_swim_folder(swim_live_dir)
            summary = run(run_pipeline(sw_events, label="SWIM_SCAN")) if sw_events else None
        st.success(
            f"Ingested {sw_stats.events_parsed} events from "
            f"{sw_stats.files_processed} file(s). "
            f"{sw_stats.files_failed} failed, "
            f"{sw_stats.files_withheld} withheld (blockAtFAA). "
            f"{summary['alert_count'] if summary else 0} alert(s), "
            f"{summary['staged_count'] if summary else 0} staged for review."
        )
        st.rerun()

with col_hint:
    if len(pending_files) == 0:
        st.info(
            "No files in `data/raw/swim_live/`. "
            "Drop .json / .xml / .txt STDDS messages here, or "
            "configure the FAA SCDS Jumpstart Kit to write to this folder."
        )

if ingest_log_entries:
    with st.expander("Ingest audit log (latest 50 entries)"):
        log_df = pd.DataFrame(ingest_log_entries)
        st.dataframe(log_df, hide_index=True, use_container_width=True)

st.divider()
st.caption(
    "NAS FeedOps Sentinel · FAA SWIM/STDDS PCT Automation Demonstration · "
    "All data is simulated unless the FAA SCDS Jumpstart Kit is connected."
)
theme.back_link()
