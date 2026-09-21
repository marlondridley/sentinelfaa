"""
Feed Health page: airport/service status, RVR, SMES volume, latency, and
schema validation posture.

Answers mission questions 1, 5, and 6 from the Overview page's mission
panel: is the feed alive, are tracks stale/missing/abnormal, and is a
problem isolated to one airport or sector-wide. Split out of the original
single-file dashboard's "Airport & Service Status", "Latest RVR Table",
"SMES Counts & Latency Metrics", and "Validation Summary" sections.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import pandas as pd
import streamlit as st

from lib.data import fetch_live_events, status_label
from lib.page_setup import bootstrap_page
from src import config, storage
from src.async_bridge import run_async as run
from src.feed_health import compute_feed_health

bootstrap_page(page_title="Feed Health — PCT FeedOps Sentinel", page_icon="🏢")

st.title("🏢 Feed & Service Health")
st.caption(
    "**Answers:** *Is the PCT TAIS feed alive, are tracks stale/missing/abnormal, "
    "and is a problem isolated to one facility or sector-wide?*"
)

live_events = fetch_live_events(limit=1000)
health = compute_feed_health(live_events[:500]) if live_events else {
    "airport_status": {}, "service_status": {}, "latency_stats": {}, "smes_counts": {}
}
events_raw = run(storage.get_recent_events(limit=1000))
events_df = pd.DataFrame(events_raw) if events_raw else pd.DataFrame()
rvr_rows = run(storage.get_latest_rvr())
val_summary = run(storage.get_validation_summary())
total = val_summary.get("total", 0)
passed = val_summary.get("passed", 0) or 0
failed = val_summary.get("failed", 0) or 0

# ── Airport & Service Status ──────────────────────────────────────────────────
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("🏢 Airport Feed Health")
    st.caption(
        "**Answers:** *Which airports have a SWIM feed I can trust right now?* "
        "🟢 OK · 🟡 STALE · 🟠 DEGRADED · 🔴 UNAVAILABLE/DISCONNECTED."
    )
    if health["airport_status"]:
        airport_rows = [
            {
                "Airport": ap,
                "Status": status_label(health["airport_status"].get(ap, "NO DATA")),
            }
            for ap in sorted(config.SUPPORTED_AIRPORTS)
        ]
        st.dataframe(pd.DataFrame(airport_rows), hide_index=True, use_container_width=True)
    else:
        st.info("No data yet. Generate sample events from the Ingest & Replay page.")

with col_right:
    st.subheader("📡 Service Health by Airport")
    st.caption(
        "**Answers:** *Which specific SWIM service at which specific airport is "
        "the problem?*"
    )
    svc_rows = []
    for service, airport_map in health.get("service_status", {}).items():
        for airport, status in sorted(airport_map.items()):
            svc_rows.append({
                "Service": service,
                "Airport": airport,
                "Status": status_label(status),
            })
    if svc_rows:
        st.dataframe(pd.DataFrame(svc_rows), hide_index=True, use_container_width=True)
    else:
        st.info("No service health data yet.")

st.divider()

# ── Latest RVR Table ──────────────────────────────────────────────────────────
st.subheader("👁 Runway Visual Range (RVR)")
st.caption(
    "**Answers:** *Is the visibility on each runway safe for normal instrument "
    "approach procedures?* RVR below **1,200 ft** triggers low-visibility ops procedures."
)
if rvr_rows:
    rvr_df = pd.DataFrame(rvr_rows)[[
        "airport_code", "runway", "rvr_touchdown",
        "rvr_midpoint", "rvr_rollout", "event_time_utc",
    ]].rename(columns={
        "airport_code": "Airport",
        "runway": "Runway",
        "rvr_touchdown": "Touchdown (ft)",
        "rvr_midpoint": "Midpoint (ft)",
        "rvr_rollout": "Rollout (ft)",
        "event_time_utc": "Event Time (UTC)",
    })

    def _highlight_rvr(val):
        try:
            if val is not None and float(val) < config.RVR_LOW_THRESHOLD_FT:
                return "background-color: #FF4B4B; color: white;"
        except (TypeError, ValueError):
            pass
        return ""

    st.dataframe(
        rvr_df.style.applymap(
            _highlight_rvr,
            subset=["Touchdown (ft)", "Midpoint (ft)", "Rollout (ft)"],
        ),
        hide_index=True,
        use_container_width=True,
    )
    st.caption(f"Red cells indicate RVR < {config.RVR_LOW_THRESHOLD_FT} ft.")
else:
    st.info("No RVR data yet.")

st.divider()

# ── SMES Counts & Latency Metrics ─────────────────────────────────────────────
col_smes, col_lat = st.columns(2)

with col_smes:
    st.subheader("🛬 Surface Movement Volume")
    st.caption(
        "**Answers:** *How busy is the airport surface at each facility?*"
    )
    smes = health.get("smes_counts", {})
    if smes:
        smes_df = pd.DataFrame(
            [{"Airport": k, "SMES Events": v} for k, v in sorted(smes.items())]
        )
        st.dataframe(smes_df, hide_index=True, use_container_width=True)
        st.bar_chart(smes_df.set_index("Airport"))
    else:
        st.info("No SMES events recorded yet.")

with col_lat:
    st.subheader("⏱ Delivery Latency by Service")
    st.caption(
        "**Answers:** *Is each SWIM service delivering messages fast enough to support real-time decisions?*"
    )
    lat_rows = []
    for svc, stats in sorted(health.get("latency_stats", {}).items()):
        lat_rows.append({
            "Service": svc,
            "Avg (ms)": f"{stats['avg_ms']:,.0f}",
            "Min (ms)": f"{stats['min_ms']:,.0f}",
            "Max (ms)": f"{stats['max_ms']:,.0f}",
            "Count": stats["count"],
        })
    if lat_rows:
        st.dataframe(pd.DataFrame(lat_rows), hide_index=True, use_container_width=True)
        st.caption(f"High-latency alert threshold: {config.LATENCY_ALERT_MS:,} ms")
    else:
        st.info("No latency data yet.")

st.divider()

# ── Validation Summary ────────────────────────────────────────────────────────
st.subheader("✅ Data Quality Posture (Schema Validation)")
st.caption(
    "**Answers:** *Are upstream FAA systems sending us well-formed data?*"
)
v1, v2, v3 = st.columns(3)
v1.metric("Total Validated", f"{total:,}")
v2.metric("Passed ✅", f"{passed:,}")
v3.metric("Failed ❌", f"{failed:,}")

if not events_df.empty and "validation_status" in events_df.columns:
    val_by_service = (
        events_df.groupby(["service_name", "validation_status"])
        .size()
        .reset_index(name="count")
        .pivot(index="service_name", columns="validation_status", values="count")
        .fillna(0)
        .astype(int)
        .reset_index()
    )
    val_by_service.columns.name = None
    st.dataframe(val_by_service, hide_index=True, use_container_width=True)

st.divider()
st.page_link("app.py", label="◀ Back to Overview", icon="✈")
