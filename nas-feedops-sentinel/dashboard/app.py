"""
NAS FeedOps Sentinel -- the console's entry point: its navigation.

The sidebar lists the pages in the order the work runs, not the order of
their file names:

  Operate   Dashboard      is the picture trustworthy, and what needs someone?
            Review Queue   decide: approve, override, reject or keep monitoring
            Alert Log      the record: every alert the rules raised
  Observe   Live Airspace, Feed Health
  System    Ingest and Replay, Agent Evaluation

Each page is still its own script, and still makes the sign-in check itself
(lib/page_setup.bootstrap_page): nothing here is what keeps a page private.
A page's address is its entry in lib/theme.PAGES, the same map every
drill-down link is built from, so a link and the page it names cannot drift.

Run with:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DASHBOARD_DIR.parent))
sys.path.insert(0, str(DASHBOARD_DIR))

import streamlit as st

from lib import theme

# (sidebar section, key in theme.PAGES, script, sidebar label)
NAVIGATION = (
    ("Operate", "overview", "pages/0_Dashboard.py", "Dashboard"),
    ("Operate", "critic", "pages/3_AI_Critic_and_Review.py", "Review Queue"),
    ("Operate", "alerts", "pages/4_Alert_Log.py", "Alert Log"),
    ("Observe", "airspace", "pages/1_Live_Airspace.py", "Live Airspace"),
    ("Observe", "health", "pages/2_Feed_Health.py", "Feed Health"),
    ("System", "ingest", "pages/5_Ingest_and_Replay.py", "Ingest and Replay"),
    ("System", "evaluation", "pages/6_Agent_Evaluation.py", "Agent Evaluation"),
)


def build_navigation() -> dict[str, list]:
    sections: dict[str, list] = {}
    for section, key, script, label in NAVIGATION:
        address = theme.PAGES[key]
        page = (st.Page(script, title=label, default=True) if address == "/"
                else st.Page(script, title=label, url_path=address))
        sections.setdefault(section, []).append(page)
    return sections


st.navigation(build_navigation()).run()
