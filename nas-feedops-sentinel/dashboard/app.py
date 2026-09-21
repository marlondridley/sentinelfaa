"""
NAS FeedOps Sentinel — Streamlit Dashboard (Overview)

This is the landing page of a multipage Streamlit app. It answers the
top-level question -- "does PCT TRACON have a trustworthy aircraft
movement picture right now, and what should the duty manager do about
it?" -- via the live status ticker, the AI Planner, the Watch Officer
brief, and the operational readiness snapshot. Everything else (the live
airspace map, feed/service health, the AI Critic and human review queue,
the alert log, and ingest/replay tooling) lives on its own page under
dashboard/pages/ -- see the docstring in dashboard/lib/__init__.py for why
this was split out of what used to be a single ~1500-line script.

Run with:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `from src import ...` and `from lib import ...` regardless of which
# page/script Streamlit actually executes -- every page under
# dashboard/pages/ repeats this same boilerplate for the same reason: each
# page is its own independently-run script, not a submodule of app.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import streamlit as st

from lib.data import format_ts, readiness_label
from lib.page_setup import bootstrap_page
from lib.ticker import render_ticker
from src import storage
from src.async_bridge import run_async as run
from src.planner_agent import PlannerAgent
from src.storage import save_planner_result
from src.watch_officer_agent import build_watch_officer_context, generate_watch_officer_brief

bootstrap_page(page_title="PCT TAIS FeedOps Sentinel", page_icon="✈")

render_ticker()


def _render_mission_panel() -> None:
    """7-row decision support table that doubles as a nav index across pages."""
    st.markdown(
        """
        <style>
        .mission-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14.5px;
            margin: 6px 0 12px 0;
        }
        .mission-table th {
            background: #1f2937;
            color: #f9fafb;
            text-align: left;
            padding: 10px 12px;
            font-weight: 700;
            border-bottom: 2px solid #374151;
        }
        .mission-table td {
            padding: 9px 12px;
            border-bottom: 1px solid #2a3340;
            vertical-align: top;
            color: #e5e7eb;
        }
        .mission-table tr:nth-child(even) td { background: #161b22; }
        .mission-table tr:nth-child(odd) td  { background: #0f1419; }
        .mission-table a {
            color: #60a5fa;
            text-decoration: none;
            font-weight: 600;
        }
        .mission-table a:hover { text-decoration: underline; }
        .mission-q { color: #fbbf24; font-weight: 700; }
        .mission-decision { color: #4ade80; font-style: italic; }
        .mission-num { color: #9ca3af; font-weight: 700; margin-right: 4px; }
        </style>
        <table class="mission-table">
          <thead>
            <tr>
              <th style="width: 30%;">Business / Mission Question</th>
              <th style="width: 35%;">Dashboard Evidence</th>
              <th style="width: 35%;">Decision Supported</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><span class="mission-num">1.</span><span class="mission-q">Is the PCT TAIS feed alive right now?</span></td>
              <td><a href="Feed_Health" target="_self">Message rate, last message time, files processed</a></td>
              <td><span class="mission-decision">Confirms the SWIM feed is connected and usable</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">2.</span><span class="mission-q">Can we trust the current aircraft picture?</span></td>
              <td><a href="Live_Airspace" target="_self">Live positions, stale status, no-data status</a></td>
              <td><span class="mission-decision">Determines whether the airspace view is operationally reliable</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">3.</span><span class="mission-q">Which airport is driving the traffic load?</span></td>
              <td><a href="Live_Airspace" target="_self">KDCA, KIAD, KBWI, KADW departure / arrival / en route counts</a></td>
              <td><span class="mission-decision">Helps prioritize airport-level monitoring</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">4.</span><span class="mission-q">What phase of flight is most active?</span></td>
              <td><a href="Live_Airspace" target="_self">Departures, arrivals, en route, ground counts</a></td>
              <td><span class="mission-decision">Shows demand by operational phase</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">5.</span><span class="mission-q">Are tracks stale, missing, or abnormal?</span></td>
              <td><a href="Feed_Health" target="_self">Stale flags, altitude distribution, speed distribution</a></td>
              <td><span class="mission-decision">Detects data-quality or movement anomalies</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">6.</span><span class="mission-q">Is this isolated or sector-wide?</span></td>
              <td><a href="Feed_Health" target="_self">Per-airport comparison across all PCT airports</a></td>
              <td><span class="mission-decision">Supports escalation and root-cause triage</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">7.</span><span class="mission-q">What should the operator do next?</span></td>
              <td><a href="AI_Critic_and_Review" target="_self">AI Planner + AI Critic recommendation</a></td>
              <td><span class="mission-decision">Turns monitoring into action</span></td>
            </tr>
          </tbody>
        </table>
        """,
        unsafe_allow_html=True,
    )


# ── Header ────────────────────────────────────────────────────────────────────
st.title("PCT TAIS FeedOps Sentinel")
st.markdown(
    "**Operational decision supported:** *determine whether PCT TRACON has a "
    "current, trustworthy aircraft movement picture across **KDCA · KIAD · "
    "KBWI · KADW**.*"
)
st.caption(
    "This dashboard answers one operational question: **can PCT TRACON trust "
    "the current aircraft movement picture?** It ingests live FAA SWIM **TAIS** "
    "flight-plan and track messages, converts them into airport-level activity, "
    "checks freshness and phase-of-flight distribution, and uses an AI planner "
    "to help decide whether the issue is normal traffic, stale data, missing "
    "data, or something that needs escalation."
)

st.markdown("##### Mission Questions Answered")
st.caption(
    "Click any row's evidence to jump to the dashboard page that answers it. "
    "These seven questions are the contract between the dashboard and the "
    "operational decisions it supports."
)
_render_mission_panel()

st.divider()

# ── Operational Planner query ─────────────────────────────────────────────────
with st.expander("🤖 AI Planner — *What should the operator investigate or escalate next?*", expanded=False):
    st.caption(
        "**Answers:** *Plain-English operational questions over live SWIM data, "
        "answered in the voice of a senior PCT TRACON duty manager.* "
        "Examples: \"What's happening at KIAD right now?\" · \"Is the IAD arrival flow normal?\" · "
        "\"What should I do about the ISMC degradation?\" · \"Are any runways below RVR minimums?\""
        "  \n*Requires LLM provider configured in .env (Anthropic, Gemini via OpenAI-compatible, or NIM).*"
    )
    q_col, btn_col = st.columns([5, 1])
    with q_col:
        planner_question = st.text_input(
            "Planner query",
            placeholder="What is the current status of PCT TRACON and what should the duty manager do?",
            label_visibility="collapsed",
            key="planner_input",
        )
    with btn_col:
        planner_run = st.button("Ask", type="primary", use_container_width=True)

    if planner_run and planner_question.strip():
        with st.spinner("Analyzing NAS status ..."):
            agent = PlannerAgent()
            pr = run(agent.query(planner_question.strip()))
            run(save_planner_result(pr))

        conf_val = pr.confidence if pr.confidence is not None else 0.0
        c1, c2 = st.columns(2)
        c1.metric(
            "Planner Confidence",
            f"{conf_val:.0%}" if pr.confidence is not None else "N/A",
        )
        c2.metric("Actions Recommended", len(pr.recommended_actions))

        st.markdown(f"**{pr.answer}**")

        if pr.recommended_actions:
            st.subheader("Recommended Actions")
            for i, action in enumerate(pr.recommended_actions, 1):
                st.markdown(f"**{i}.** {action}")

        if pr.sub_queries:
            with st.expander("Sub-queries analyzed"):
                for q in pr.sub_queries:
                    st.markdown(f"- {q}")

        if pr.citations:
            with st.expander(f"Evidence citations ({len(pr.citations)})"):
                st.code(", ".join(pr.citations))

    recent_briefs = run(storage.get_recent_planner_results(limit=5))
    if recent_briefs:
        with st.expander("Recent planner briefs"):
            for brief in recent_briefs:
                # evaluated_at comes back from storage as a real datetime
                # (asyncpg TIMESTAMPTZ), not a string -- format_ts() handles
                # both so this doesn't crash the moment there's real data.
                ts = format_ts(brief.get("evaluated_at"))
                q = brief.get("question", "")
                conf = brief.get("confidence")
                conf_str = f"{conf:.0%}" if conf is not None else "N/A"
                st.markdown(f"**{ts} UTC** · conf={conf_str} · _{q}_")
                st.markdown(brief.get("answer", ""))
                st.divider()

st.divider()

# ── PCT Watch Officer Brief ──────────────────────────────────────────────────
st.markdown('<div id="watch-officer"></div>', unsafe_allow_html=True)
st.subheader("🧠 PCT Watch Officer Brief — *What changed and what needs review?*")
st.caption(
    "**Answers:** *In one screen, what is the operational story right now?* "
    "The Watch Officer reads current PostgreSQL state, runs deterministic signal "
    "detection, and (when an LLM is configured) drafts a narrative brief. "
    "This brief is the **explanation layer** between the banner (fast signal) "
    "and the escalation package (durable record)."
)

_wo_latest = run(storage.get_latest_watch_officer_brief())

if _wo_latest is None:
    _wo_ctx = run(build_watch_officer_context())
    _wo_latest = generate_watch_officer_brief(_wo_ctx, use_llm=True)
    run(storage.save_watch_officer_brief(_wo_latest))

_wo_b1, _wo_b2, _wo_b3 = st.columns([1, 1, 4])
if _wo_b1.button("🔄 Refresh Brief", use_container_width=True):
    _new_ctx = run(build_watch_officer_context())
    _wo_latest = generate_watch_officer_brief(_new_ctx, use_llm=True)
    run(storage.save_watch_officer_brief(_wo_latest))
    st.rerun()
if _wo_b2.button("⚙ Deterministic Only", use_container_width=True,
                 help="Force the rule-based fallback brief (skips LLM)"):
    _new_ctx = run(build_watch_officer_context())
    _wo_latest = generate_watch_officer_brief(_new_ctx, use_llm=False)
    run(storage.save_watch_officer_brief(_wo_latest))
    st.rerun()

_POSTURE_COLOR = {"ACTION": "red", "ATTENTION": "orange", "NORMAL": "blue",
                  "GOOD": "green", "UNKNOWN": "grey"}
_TRUST_COLOR   = {"TRUSTWORTHY": "green", "DEGRADED": "orange",
                  "INCOMPLETE": "red", "UNKNOWN": "grey"}
_pcol = _POSTURE_COLOR.get(_wo_latest.posture, "grey")
_tcol = _TRUST_COLOR.get(_wo_latest.trust_assessment, "grey")

with st.container(border=True):
    bh1, bh2, bh3, bh4 = st.columns([1.2, 1.5, 1, 2])
    bh1.markdown(f"### :{_pcol}[**{_wo_latest.posture}**]")
    bh2.markdown(f"trust: :{_tcol}[**{_wo_latest.trust_assessment}**]")
    bh3.markdown(f"confidence: **{_wo_latest.confidence:.0%}**")
    badge = "🤖 LLM-assisted" if _wo_latest.llm_used else "⚙ Deterministic"
    bh4.markdown(f"_{badge}_ · `{_wo_latest.generated_at.strftime('%H:%M:%S')} UTC`")

    st.markdown(f"**{_wo_latest.headline}**")

    wc1, wc2 = st.columns(2)
    with wc1:
        st.markdown("**📋 What changed**")
        for item in _wo_latest.what_changed[:6]:
            st.markdown(f"- {item}")
        st.markdown("**👀 Recommended human review**")
        for item in _wo_latest.recommended_human_review[:5]:
            st.markdown(f"- {item}")
    with wc2:
        st.markdown("**🎯 Why it matters**")
        for item in _wo_latest.why_it_matters[:5]:
            st.markdown(f"- {item}")
        st.markdown("**🔬 Evidence**")
        for item in _wo_latest.evidence[:5]:
            st.markdown(f"- {item}")

    if _wo_latest.affected_airports:
        st.caption(f"Affected airports: `{', '.join(_wo_latest.affected_airports)}`")
    if _wo_latest.limitations:
        with st.expander("Brief limitations / caveats"):
            for lim in _wo_latest.limitations:
                st.markdown(f"- {lim}")

st.divider()

# ── Operational Readiness Snapshot ────────────────────────────────────────────
st.subheader("🎯 PCT TRACON Operational Readiness")
st.caption(
    "**Answers:** *Can the duty manager trust the SWIM feed right now to make "
    "safe, real-time operational decisions for KDCA, KIAD, KBWI, and KADW?*"
)

val_summary = run(storage.get_validation_summary())
lat_stats = run(storage.get_latency_stats())
alerts_raw = run(storage.get_recent_alerts(limit=500))

total = val_summary.get("total", 0)
passed = val_summary.get("passed", 0) or 0
failed = val_summary.get("failed", 0) or 0
pass_rate_pct = (passed / total * 100) if total else None
avg_lat = lat_stats.get("avg_ms")
critical_alerts = sum(1 for a in alerts_raw if a.get("severity") == "CRITICAL")
warning_alerts = sum(1 for a in alerts_raw if a.get("severity") == "WARNING")

k1, k2, k3, k4 = st.columns(4)

k1.metric(
    label="📡 SWIM Messages Ingested",
    value=f"{total:,}",
    delta="total events in DB",
    delta_color="off",
)

emoji_pass, status_pass = readiness_label(pass_rate_pct, good_if=95, warn_if=85, bad_if=70)
pass_display = f"{pass_rate_pct:.1f}%" if pass_rate_pct is not None else "—"
k2.metric(
    label=f"{emoji_pass} Schema Conformance",
    value=pass_display,
    delta=f"{status_pass} · target ≥ 95%",
    delta_color="normal" if (pass_rate_pct or 0) >= 95 else "inverse",
)

total_alerts = len(alerts_raw)
emoji_alert, status_alert = readiness_label(
    total_alerts, good_if=0, warn_if=10, bad_if=50, lower_is_better=True
)
k3.metric(
    label=f"{emoji_alert} Open Operational Alerts",
    value=f"{total_alerts:,}",
    delta=f"{critical_alerts} CRITICAL · {warning_alerts} WARNING",
    delta_color="inverse" if critical_alerts > 0 else "off",
)

emoji_lat, status_lat = readiness_label(
    avg_lat or 0, good_if=5_000, warn_if=30_000, bad_if=300_000, lower_is_better=True
)
if avg_lat is None:
    lat_display = "—"
elif avg_lat < 1000:
    lat_display = f"{avg_lat:,.0f} ms"
elif avg_lat < 60_000:
    lat_display = f"{avg_lat/1000:.1f} s"
else:
    lat_display = f"{avg_lat/60_000:.1f} min"
k4.metric(
    label=f"{emoji_lat} Avg Event-to-Ingest Lag",
    value=lat_display,
    delta=f"{status_lat} · target < 5 s live",
    delta_color="normal" if (avg_lat or 0) < 5000 else "inverse",
)

if pass_rate_pct is not None and avg_lat is not None:
    if (pass_rate_pct or 0) >= 95 and (avg_lat or 0) < 5000 and critical_alerts == 0:
        st.success("**Operational status: GREEN** — feed is healthy, data quality nominal, no critical alerts. Safe to rely on the feed for live operational decisions.")
    elif critical_alerts > 0 or (pass_rate_pct or 0) < 70:
        st.error(
            f"**Operational status: RED** — {critical_alerts} CRITICAL alerts active "
            f"and/or {pass_rate_pct:.0f}% schema conformance is below 70%. "
            "Supervisor should validate against secondary sources before using feed data for ops decisions."
        )
    else:
        st.warning(
            "**Operational status: AMBER** — feed is functional but degraded. "
            "Review individual metrics below and the Critic intelligence panel for context."
        )

st.divider()
st.caption(
    "NAS FeedOps Sentinel · FAA SWIM/STDDS PCT Automation Demonstration · "
    "All data is simulated unless the FAA SCDS Jumpstart Kit is connected."
)
