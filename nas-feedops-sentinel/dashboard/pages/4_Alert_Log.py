"""
Operational Alert Log page.

Answers "what does the duty manager need to act on?" Split out of the
original single-file dashboard's "Alert Log" section.
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

from lib.data import SEVERITY_EMOJI
from lib.page_setup import bootstrap_page
from src import storage
from src.async_bridge import run_async as run

bootstrap_page(page_title="Alert Log — PCT FeedOps Sentinel", page_icon="🚨")

st.title("🚨 Operational Alert Log")
st.caption(
    "**Answers:** *What does the duty manager need to act on?*"
)

alerts_raw = run(storage.get_recent_alerts(limit=500))
alerts_df = pd.DataFrame(alerts_raw) if alerts_raw else pd.DataFrame()

if not alerts_df.empty:
    display_cols = [c for c in
                    ["triggered_at", "severity", "alert_type",
                     "airport_code", "service_name", "description"]
                    if c in alerts_df.columns]
    disp = alerts_df[display_cols].copy()
    disp["severity"] = disp["severity"].apply(
        lambda s: f"{SEVERITY_EMOJI.get(s, '')} {s}"
    )
    st.dataframe(
        disp.rename(columns={
            "triggered_at": "Triggered (UTC)",
            "severity": "Severity",
            "alert_type": "Alert Type",
            "airport_code": "Airport",
            "service_name": "Service",
            "description": "Description",
        }),
        hide_index=True,
        use_container_width=True,
    )

    with st.expander("Alert type breakdown"):
        type_counts = alerts_df["alert_type"].value_counts().reset_index()
        type_counts.columns = ["Alert Type", "Count"]
        st.dataframe(type_counts, hide_index=True, use_container_width=True)
        st.bar_chart(type_counts.set_index("Alert Type"))
else:
    st.success("No alerts in the log. System appears healthy.")

st.divider()
st.page_link("app.py", label="◀ Back to Overview", icon="✈")
