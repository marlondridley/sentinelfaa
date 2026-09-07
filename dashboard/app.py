"""
NAS FeedOps Sentinel — Streamlit Dashboard

Displays live/replayed feed health, RVR conditions, SMES counts,
validation summaries, latency metrics, and alert logs for the
PCT TRACON (KDCA, KIAD, KBWI, KADW).

Run with:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

# Allow running from repo root or from the dashboard/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config, storage
from src.alert_engine import check_alerts
from src.feed_health import compute_feed_health
from src.maintenance import purge_old_kit_files
from src.models import NASEvent
from src.news_agent import build_ticker_items
from src.planner_agent import PlannerAgent
from src.replay import replay_from_directory
from src.sample_data import generate_sample_batch
from src.storage import save_alerts, save_events, save_planner_result
from src.validators import validate_event
from src.watch_officer_agent import (
    build_watch_officer_context,
    generate_watch_officer_brief,
)

# Kit log folder path (resolved via the data/raw/swim_live junction)
KIT_LOG_PATH = config.SWIM_RAW_FOLDER

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PCT TAIS FeedOps Sentinel",
    page_icon="✈",
    layout="wide",
)

# ── Status colour mapping ─────────────────────────────────────────────────────
STATUS_EMOJI = {
    "OK": "🟢",
    "STALE": "🟡",
    "DEGRADED": "🟠",
    "UNAVAILABLE": "🔴",
    "DISCONNECTED": "🔴",
    "NO DATA": "⚫",
}
SEVERITY_EMOJI = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}


def _status_label(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '⚪')} {status}"


# ── Breaking News Ticker (top-of-page, CNN-style) ─────────────────────────────
# Pre-compute the data the ticker needs (done before main "Load persisted data"
# block so it can render at the very top of the page).
_news_events_raw = storage.get_recent_events(limit=500)
_news_alerts_raw = storage.get_recent_alerts(limit=200)
_news_rvr_rows   = storage.get_latest_rvr()
_news_phase      = storage.get_flight_phase_counts(window_minutes=15)
_news_active     = storage.get_active_aircraft(window_minutes=5, limit=300)
_news_lat        = storage.get_latency_stats()

# Build NASEvent list for feed-health computation (worst-wins per airport)
_news_live: list[NASEvent] = []
for _row in _news_events_raw[:300]:
    try:
        _r = dict(_row)
        _r["event_time_utc"] = datetime.fromisoformat(_r["event_time_utc"])
        _r["received_time_utc"] = datetime.fromisoformat(_r["received_time_utc"])
        if isinstance(_r.get("validation_errors"), str):
            _r["validation_errors"] = json.loads(_r["validation_errors"])
        _news_live.append(NASEvent(**_r))
    except Exception:
        pass

_news_health = compute_feed_health(_news_live) if _news_live else {"airport_status": {}}

ticker_items = build_ticker_items(
    airport_status=_news_health.get("airport_status", {}),
    alerts=_news_alerts_raw,
    rvr_rows=_news_rvr_rows,
    phase_counts=_news_phase,
    active_aircraft_total=len(_news_active),
    avg_latency_ms=_news_lat.get("avg_ms"),
)

# Severity → CSS class
_SEV_CLASS = {
    "GOOD":      "tk-green",
    "NORMAL":    "tk-grey",
    "ATTENTION": "tk-yellow",
    "ACTION":    "tk-red",
}
# Build the inline-HTML ticker string with clearer separator and severity prefix
_SEV_GLYPH = {"GOOD": "●", "NORMAL": "●", "ATTENTION": "▲", "ACTION": "■"}
_ticker_html_items = "".join(
    f'<a class="tk-item {_SEV_CLASS.get(it["severity"], "tk-grey")}" '
    f'href="#{it.get("anchor","page-top")}" title="Click to jump to section">'
    f'<span class="tk-glyph">{_SEV_GLYPH.get(it["severity"], "●")}</span> '
    f'{it["text"]}'
    f'</a>'
    for it in ticker_items
)
# Duplicate the items so the marquee scrolls seamlessly
_ticker_payload = f"{_ticker_html_items}{_ticker_html_items}"

# Tuned: ~9 seconds per item, minimum 90s so a short list still scrolls smoothly
_scroll_seconds = max(90, len(ticker_items) * 9)

st.markdown(
    f"""
    <a id="page-top"></a>
    <style>
    .ticker-wrap {{
        background: #1a1a1a;
        border-top:    3px solid #ff3030;
        border-bottom: 3px solid #ff3030;
        overflow: hidden;
        white-space: nowrap;
        padding: 14px 0;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        font-size: 20px;
        font-weight: 600;
        line-height: 1.3;
        margin: 0 0 14px 0;
        position: relative;
        box-shadow: 0 2px 8px rgba(0,0,0,0.35);
    }}
    .ticker-label {{
        background: #ff3030;
        color: #ffffff;
        padding: 8px 18px;
        font-weight: 900;
        letter-spacing: 0.1em;
        font-size: 18px;
        margin-right: 24px;
        display: inline-block;
        vertical-align: middle;
        text-shadow: 0 1px 2px rgba(0,0,0,0.4);
        border-radius: 2px;
    }}
    .ticker {{
        display: inline-block;
        padding-left: 100%;
        animation: ticker-scroll {_scroll_seconds}s linear infinite;
    }}
    /* Pause on hover (desktop) AND when actively touched/clicked (touch + desktop tap-hold) */
    .ticker-wrap:hover .ticker,
    .ticker-wrap:active .ticker,
    .ticker-wrap:focus-within .ticker {{
        animation-play-state: paused;
    }}
    @keyframes ticker-scroll {{
        0%   {{ transform: translate3d(0, 0, 0); }}
        100% {{ transform: translate3d(-50%, 0, 0); }}
    }}
    .tk-item {{
        display: inline-block;
        margin: 0 32px 0 0;
        padding: 4px 6px;
        vertical-align: middle;
        text-shadow: 0 1px 1px rgba(0,0,0,0.5);
        text-decoration: none;
        cursor: pointer;
        border-radius: 3px;
        transition: background-color 0.15s ease-out;
    }}
    .tk-item:hover, .tk-item:focus {{
        background: rgba(255,255,255,0.12);
        text-decoration: underline;
        outline: none;
    }}
    .tk-glyph {{
        font-size: 14px;
        margin-right: 6px;
        vertical-align: middle;
    }}
    .tk-green  {{ color: #4ade80; }}
    .tk-grey   {{ color: #e5e7eb; }}
    .tk-yellow {{ color: #fbbf24; font-weight: 800; }}
    .tk-red    {{ color: #ff5252; font-weight: 900; }}
    /* scroll-margin so anchored sections don't get hidden under Streamlit's header */
    [id^="anchor-"], #airport-health, #airspace-ops, #alert-log,
    #rvr-section, #critic-intel, #latency-section {{
        scroll-margin-top: 80px;
    }}
    </style>
    <div class="ticker-wrap" title="Hover or tap to pause · click any headline to jump to its section">
        <span class="ticker-label">PCT&nbsp;LIVE</span>
        <div class="ticker">{_ticker_payload}</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Mission Questions Answered (table of contents for the operator) ─────────
def _render_mission_panel() -> None:
    """
    7-row decision support table that doubles as a nav index — every row's
    "Dashboard evidence" cell deep-links to the section that answers it.
    """
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
              <td><a href="#latency-section">Message rate, last message time, files processed</a></td>
              <td><span class="mission-decision">Confirms the SWIM feed is connected and usable</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">2.</span><span class="mission-q">Can we trust the current aircraft picture?</span></td>
              <td><a href="#airspace-ops">Live positions, stale status, no-data status</a></td>
              <td><span class="mission-decision">Determines whether the airspace view is operationally reliable</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">3.</span><span class="mission-q">Which airport is driving the traffic load?</span></td>
              <td><a href="#airspace-ops">KDCA, KIAD, KBWI, KADW departure / arrival / en route counts</a></td>
              <td><span class="mission-decision">Helps prioritize airport-level monitoring</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">4.</span><span class="mission-q">What phase of flight is most active?</span></td>
              <td><a href="#airspace-ops">Departures, arrivals, en route, ground counts</a></td>
              <td><span class="mission-decision">Shows demand by operational phase</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">5.</span><span class="mission-q">Are tracks stale, missing, or abnormal?</span></td>
              <td><a href="#airport-health">Stale flags, altitude distribution, speed distribution</a></td>
              <td><span class="mission-decision">Detects data-quality or movement anomalies</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">6.</span><span class="mission-q">Is this isolated or sector-wide?</span></td>
              <td><a href="#airport-health">Per-airport comparison across all PCT airports</a></td>
              <td><span class="mission-decision">Supports escalation and root-cause triage</span></td>
            </tr>
            <tr>
              <td><span class="mission-num">7.</span><span class="mission-q">What should the operator do next?</span></td>
              <td><a href="#critic-intel">AI Planner + AI Critic recommendation</a></td>
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
    "Click any row's evidence to jump to the dashboard section that answers it. "
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
            pr = agent.query(planner_question.strip())
            save_planner_result(pr)

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

    # Recent planner history
    recent_briefs = storage.get_recent_planner_results(limit=5)
    if recent_briefs:
        with st.expander("Recent planner briefs"):
            for brief in recent_briefs:
                ts = brief.get("evaluated_at", "")[:19]
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
    "The Watch Officer reads current SQLite state, runs deterministic signal "
    "detection, and (when an LLM is configured) drafts a narrative brief. "
    "This brief is the **explanation layer** between the banner (fast signal) "
    "and the escalation package (durable record)."
)

_wo_latest = storage.get_latest_watch_officer_brief()

# If no brief exists yet, generate one on first dashboard load
if _wo_latest is None:
    _wo_ctx = build_watch_officer_context()
    _wo_latest = generate_watch_officer_brief(_wo_ctx, use_llm=True)
    storage.save_watch_officer_brief(_wo_latest)

# Buttons
_wo_b1, _wo_b2, _wo_b3 = st.columns([1, 1, 4])
if _wo_b1.button("🔄 Refresh Brief", use_container_width=True):
    _new_ctx = build_watch_officer_context()
    _wo_latest = generate_watch_officer_brief(_new_ctx, use_llm=True)
    storage.save_watch_officer_brief(_wo_latest)
    st.rerun()
if _wo_b2.button("⚙ Deterministic Only", use_container_width=True,
                 help="Force the rule-based fallback brief (skips LLM)"):
    _new_ctx = build_watch_officer_context()
    _wo_latest = generate_watch_officer_brief(_new_ctx, use_llm=False)
    storage.save_watch_officer_brief(_wo_latest)
    st.rerun()

# Posture + trust badges
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

# ── Load persisted data (used by airspace + downstream panels) ────────────────
events_raw = storage.get_recent_events(limit=1000)
alerts_raw = storage.get_recent_alerts(limit=500)
val_summary = storage.get_validation_summary()
lat_stats = storage.get_latency_stats()
rvr_rows = storage.get_latest_rvr()

events_df = pd.DataFrame(events_raw) if events_raw else pd.DataFrame()
alerts_df = pd.DataFrame(alerts_raw) if alerts_raw else pd.DataFrame()

# Rebuild NASEvent objects for feed_health computation
_live_events: list[NASEvent] = []
for row in events_raw[:500]:
    try:
        r = dict(row)
        r["event_time_utc"] = datetime.fromisoformat(r["event_time_utc"])
        r["received_time_utc"] = datetime.fromisoformat(r["received_time_utc"])
        if isinstance(r.get("validation_errors"), str):
            r["validation_errors"] = json.loads(r["validation_errors"])
        _live_events.append(NASEvent(**r))
    except Exception:
        pass

health = compute_feed_health(_live_events) if _live_events else {
    "airport_status": {}, "service_status": {}, "latency_stats": {}, "smes_counts": {}
}

# ── Live Airspace Operations (TAIS) ───────────────────────────────────────────
st.markdown('<div id="airspace-ops"></div>', unsafe_allow_html=True)
st.subheader("🛩 Live PCT Airspace Operations")
st.caption(
    "**Answers:** *Where is every aircraft in PCT airspace right now, and how "
    "does flow break down by airport and phase?* "
    "Source: FAA SWIM TAIS feed (live STARS track and flight-plan records). "
    "Aircraft counts use a 15-minute window; live positions use the last 5 minutes."
)

active_aircraft = storage.get_active_aircraft(window_minutes=5, limit=500)
phase_counts = storage.get_flight_phase_counts(window_minutes=15)
altitude_dist = storage.get_altitude_distribution(window_minutes=15)

# Top-row airspace KPIs — each label is the operational question it answers
ap1, ap2, ap3, ap4, ap5 = st.columns(5)
total_active = len(active_aircraft)
total_dep = sum(c.get("DEPARTURE", 0) for c in phase_counts.values())
total_arr = sum(c.get("ARRIVAL", 0) for c in phase_counts.values())
total_enr = sum(c.get("ENROUTE", 0) for c in phase_counts.values())
total_gnd = sum(c.get("GROUND", 0) for c in phase_counts.values())

ap1.metric(
    label="Are live aircraft positions being received?",
    value=total_active,
    delta="active in last 5 min" if total_active else "no live positions",
    delta_color="off" if total_active else "inverse",
    help="Distinct aircraft for which TAIS sent a position in the last 5 minutes. "
         "Zero = TAIS feed has stopped delivering tracks; investigate the Kit + broker.",
)
ap2.metric(
    label="🛫 How many departure records were observed?",
    value=total_dep,
    delta="climbing out of PCT airports" if total_dep else "no departures detected",
    delta_color="off",
    help="Aircraft classified as DEPARTURE phase (climbing AND originating at a PCT primary airport) "
         "in the last 15 minutes.",
)
ap3.metric(
    label="🛬 How much arrival traffic is flowing through PCT?",
    value=total_arr,
    delta="descending into PCT airports" if total_arr else "no arrivals detected",
    delta_color="off",
    help="Aircraft classified as ARRIVAL phase (descending AND destination is a PCT primary airport) "
         "in the last 15 minutes.",
)
ap4.metric(
    label="✈ How much transition / overflight traffic is active?",
    value=total_enr,
    delta="level flight through sector" if total_enr else "no enroute traffic",
    delta_color="off",
    help="Aircraft classified as ENROUTE (level flight at cruise or transiting PCT) in the last 15 minutes. "
         "Includes overflights that neither depart nor land in PCT primaries.",
)
ap5.metric(
    label="🅿 Are surface / ground records present?",
    value=total_gnd,
    delta="on taxi/ramp/runway" if total_gnd else "no ground records",
    delta_color="off",
    help="Aircraft below 500 ft (surface, taxi, ramp, runway ground movement). Sourced from TAIS "
         "low-altitude tracks; for richer ground state, subscribe to SMES.",
)

# Airspace map — Palantir-style overlay of airports + live aircraft
PCT_AIRPORTS = {
    "KDCA": {"name": "Reagan National",       "lat": 38.8521, "lon": -77.0377},
    "KIAD": {"name": "Dulles International",  "lat": 38.9531, "lon": -77.4565},
    "KBWI": {"name": "Baltimore-Washington",  "lat": 39.1754, "lon": -76.6683},
    "KADW": {"name": "Joint Base Andrews",    "lat": 38.8108, "lon": -76.8669},
}

airport_layer_data = []
for code, info in PCT_AIRPORTS.items():
    overall = health["airport_status"].get(code, "NO DATA")
    color = {
        "OK":           [50, 200, 50, 200],
        "STALE":        [255, 230, 0, 200],
        "DEGRADED":     [255, 140, 0, 220],
        "UNAVAILABLE":  [255, 0, 0, 240],
        "DISCONNECTED": [255, 0, 0, 255],
        "NO DATA":      [120, 120, 120, 180],
    }.get(overall, [120, 120, 120, 180])
    airport_layer_data.append({
        "code": code,
        "name": info["name"],
        "lat": info["lat"],
        "lon": info["lon"],
        "status": overall,
        "color": color,
        "label": f"{code} · {overall}",
    })

aircraft_layer_data = []
for ac in active_aircraft:
    if ac.get("lat") is None or ac.get("lon") is None:
        continue
    phase = ac.get("flight_phase") or "UNKNOWN"
    color = {
        "DEPARTURE": [30, 144, 255, 220],
        "ARRIVAL":   [255, 100, 30, 220],
        "ENROUTE":   [144, 238, 144, 200],
        "GROUND":    [180, 180, 180, 200],
        "UNKNOWN":   [220, 220, 220, 180],
    }.get(phase, [220, 220, 220, 180])
    aircraft_layer_data.append({
        "aircraft_id": ac.get("aircraft_id") or "—",
        "lat":   float(ac["lat"]),
        "lon":   float(ac["lon"]),
        "alt":   ac.get("altitude_ft") or 0,
        "speed": ac.get("ground_speed_kt") or 0,
        "phase": phase,
        "airport": ac.get("airport_code") or "—",
        "runway":  ac.get("runway") or "—",
        "color":   color,
        "label":   f"{ac.get('aircraft_id','?')} {phase} {ac.get('altitude_ft', 0)}ft",
    })

map_col, ops_col = st.columns([3, 2])

with map_col:
    st.markdown("**Airspace Map — *Where are aircraft visible in the PCT sector?***")
    if airport_layer_data or aircraft_layer_data:
        layers = []
        if airport_layer_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=airport_layer_data,
                get_position="[lon, lat]",
                get_fill_color="color",
                get_radius=4000,
                radius_min_pixels=10,
                radius_max_pixels=22,
                pickable=True,
            ))
            layers.append(pdk.Layer(
                "TextLayer",
                data=airport_layer_data,
                get_position="[lon, lat]",
                get_text="label",
                get_color=[20, 20, 20, 240],
                get_size=14,
                get_alignment_baseline="'top'",
                get_pixel_offset=[0, 12],
            ))
        if aircraft_layer_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=aircraft_layer_data,
                get_position="[lon, lat]",
                get_fill_color="color",
                get_radius=600,
                radius_min_pixels=3,
                radius_max_pixels=7,
                pickable=True,
            ))

        view_state = pdk.ViewState(
            latitude=38.92,
            longitude=-77.05,
            zoom=8.4,
            pitch=30,
            bearing=0,
        )
        st.pydeck_chart(pdk.Deck(
            map_style=None,  # Use default Carto basemap (no Mapbox token needed)
            initial_view_state=view_state,
            layers=layers,
            tooltip={
                "html": "<b>{label}</b><br/>"
                        "Airport: {airport} · Runway: {runway}<br/>"
                        "Alt: {alt} ft · GS: {speed} kt",
                "style": {"backgroundColor": "#0e1117", "color": "white"},
            },
        ))
        st.caption(
            f"Showing {len(aircraft_layer_data)} live aircraft positions over "
            f"{len(airport_layer_data)} PCT primary airports. "
            "🟢 OK · 🟡 STALE · 🟠 DEGRADED · 🔴 UNAVAILABLE · "
            "🔵 Departure · 🟠 Arrival · 🟢 Enroute · ⚪ Ground."
        )
    else:
        st.info("No aircraft positions in window. Verify TAIS feed is flowing.")

with ops_col:
    st.markdown("**Per-Airport Activity — *Which airport has healthy, stale, or missing data?***")
    rows = []
    for code, info in PCT_AIRPORTS.items():
        pc = phase_counts.get(code, {})
        rows.append({
            "Airport": code,
            "Status": _status_label(health["airport_status"].get(code, "NO DATA")),
            "🛫 Dep": pc.get("DEPARTURE", 0),
            "🛬 Arr": pc.get("ARRIVAL", 0),
            "✈ Enr":  pc.get("ENROUTE", 0),
            "🅿 Gnd":  pc.get("GROUND", 0),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # Flight matrix — most-recent flights with status
    if active_aircraft:
        st.markdown("**Live Flight Matrix** (most recent)")
        fm_rows = []
        for ac in active_aircraft[:20]:
            fm_rows.append({
                "Flight": ac.get("aircraft_id") or "—",
                "Airport": ac.get("airport_code"),
                "Phase": ac.get("flight_phase") or "—",
                "Alt (ft)": ac.get("altitude_ft"),
                "GS (kt)": ac.get("ground_speed_kt"),
                "Runway": ac.get("runway") or "—",
                "Rules": ac.get("flight_rules") or "—",
            })
        st.dataframe(pd.DataFrame(fm_rows), hide_index=True, use_container_width=True, height=300)

# Altitude + ground-speed distribution histograms
if altitude_dist:
    dist_df = pd.DataFrame(altitude_dist)
    alt_col, gs_col = st.columns(2)

    with alt_col:
        st.markdown("**Altitude Distribution — *Are aircraft appearing in expected altitude bands?***")
        alt_chart = alt.Chart(dist_df).mark_bar(opacity=0.85).encode(
            alt.X("altitude_ft:Q", bin=alt.Bin(maxbins=20), title="Altitude (ft)"),
            alt.Y("count()", title="Aircraft"),
            alt.Color("airport_code:N", title="Airport"),
            tooltip=["airport_code", "count()"],
        ).properties(height=240)
        st.altair_chart(alt_chart, use_container_width=True)

    with gs_col:
        st.markdown("**Ground Speed Distribution — *Do speed patterns look normal by airport?***")
        gs_df = dist_df[dist_df["ground_speed_kt"].notna()]
        if not gs_df.empty:
            gs_chart = alt.Chart(gs_df).mark_bar(opacity=0.85).encode(
                alt.X("ground_speed_kt:Q", bin=alt.Bin(maxbins=20), title="Ground speed (kt)"),
                alt.Y("count()", title="Aircraft"),
                alt.Color("airport_code:N", title="Airport"),
                tooltip=["airport_code", "count()"],
            ).properties(height=240)
            st.altair_chart(gs_chart, use_container_width=True)
        else:
            st.info("No ground-speed samples in window.")

st.divider()

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙ Controls")

    gen_count = st.slider("Sample events to generate", min_value=10, max_value=500,
                          value=100, step=10)

    if st.button("Generate & Ingest Sample Data", use_container_width=True, type="primary"):
        with st.spinner("Generating …"):
            events = generate_sample_batch(gen_count)
            for e in events:
                r = validate_event(e)
                e.validation_status = "PASSED" if r.passed else "FAILED"
                e.validation_errors = r.errors
            save_events(events)
            alerts = check_alerts(events)
            save_alerts(alerts)
        st.success(f"Ingested {len(events)} events · {len(alerts)} alerts triggered.")

    st.divider()

    # Replay section
    st.subheader("Replay")
    replay_files = list(config.RAW_DIR.glob("*.json")) if config.RAW_DIR.exists() else []
    st.caption(f"{len(replay_files)} file(s) in data/raw/")
    if st.button("Run Replay from data/raw", use_container_width=True,
                 disabled=len(replay_files) == 0):
        with st.spinner("Replaying …"):
            r_events, r_errors = replay_from_directory()
            for e in r_events:
                res = validate_event(e)
                e.validation_status = "PASSED" if res.passed else "FAILED"
                e.validation_errors = res.errors
            save_events(r_events)
            r_alerts = check_alerts(r_events)
            save_alerts(r_alerts)
        if r_errors:
            st.warning(f"{len(r_errors)} parse error(s). Check console.")
        st.success(
            f"Replay complete · {len(r_events)} events · {len(r_alerts)} alerts."
        )

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
    st.caption(f"DB: `{config.DB_PATH}`")
    st.caption(f"Refreshed: {datetime.utcnow().strftime('%H:%M:%S')} UTC")
    if st.button("Refresh dashboard (auto-cleanup)", use_container_width=True, type="primary"):
        removed, remaining = purge_old_kit_files(KIT_LOG_PATH, max_age_minutes=cleanup_minutes)
        if removed > 0:
            st.toast(f"Cleaned {removed} Kit file(s) older than {cleanup_minutes} min")
        st.rerun()

# ── Operational Readiness Snapshot ────────────────────────────────────────────
st.subheader("🎯 PCT TRACON Operational Readiness")
st.caption(
    "**Answers:** *Can the duty manager trust the SWIM feed right now to make "
    "safe, real-time operational decisions for KDCA, KIAD, KBWI, and KADW?*"
)

# Raw values
total = val_summary.get("total", 0)
passed = val_summary.get("passed", 0) or 0
failed = val_summary.get("failed", 0) or 0
pass_rate_pct = (passed / total * 100) if total else None
avg_lat = lat_stats.get("avg_ms")
critical_alerts = sum(1 for a in alerts_raw if a.get("severity") == "CRITICAL")
warning_alerts = sum(1 for a in alerts_raw if a.get("severity") == "WARNING")

# Helper — color a metric label with status emoji + target context
def _readiness_label(value, good_if, warn_if, bad_if, lower_is_better=False):
    """Return (emoji, status_text) based on thresholds."""
    if value is None:
        return ("⚪", "no data")
    if lower_is_better:
        if value <= good_if: return ("🟢", "healthy")
        if value <= warn_if: return ("🟡", "watch")
        if value <= bad_if:  return ("🟠", "degraded")
        return ("🔴", "critical")
    if value >= good_if: return ("🟢", "healthy")
    if value >= warn_if: return ("🟡", "watch")
    if value >= bad_if:  return ("🟠", "degraded")
    return ("🔴", "critical")

k1, k2, k3, k4 = st.columns(4)

# 1. Sector activity — how much SWIM data is flowing
k1.metric(
    label="📡 SWIM Messages Ingested",
    value=f"{total:,}",
    delta="total events in DB",
    delta_color="off",
    help="Total NAS events processed since pipeline start. PCT-wide normal "
         "rate is roughly 1,000–20,000 messages per active operational hour. "
         "Zero or near-zero = SWIM broker connectivity is broken; investigate "
         "the Jumpstart Kit and broker auth.",
)

# 2. Data quality — schema conformance
emoji_pass, status_pass = _readiness_label(pass_rate_pct, good_if=95, warn_if=85, bad_if=70)
pass_display = f"{pass_rate_pct:.1f}%" if pass_rate_pct is not None else "—"
k2.metric(
    label=f"{emoji_pass} Schema Conformance",
    value=pass_display,
    delta=f"{status_pass} · target ≥ 95%",
    delta_color="normal" if (pass_rate_pct or 0) >= 95 else "inverse",
    help="% of incoming SWIM messages that pass schema and business-rule "
         "validation. Below 95% indicates upstream FAA data-quality issues "
         "or a parser drift after a SWIM schema revision. Sustained failures "
         "= open a ticket with the SWIM service desk.",
)

# 3. Active alerts — operational triage queue
total_alerts = len(alerts_raw)
emoji_alert, status_alert = _readiness_label(
    total_alerts, good_if=0, warn_if=10, bad_if=50, lower_is_better=True
)
k3.metric(
    label=f"{emoji_alert} Open Operational Alerts",
    value=f"{total_alerts:,}",
    delta=f"{critical_alerts} CRITICAL · {warning_alerts} WARNING",
    delta_color="inverse" if critical_alerts > 0 else "off",
    help="Alerts triggered by the deterministic rule engine awaiting "
         "supervisor triage. CRITICAL alerts (low RVR, ISMC disconnect, "
         "feed loss) require immediate attention. The AI Critic panel below "
         "scores which CRITICALs deserve escalation vs which are spurious.",
)

# 4. Processing freshness — gap between event time and ingest time
emoji_lat, status_lat = _readiness_label(
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
    help="Time between when the event occurred at the FAA facility and "
         "when Sentinel received the message. Live target is sub-second. "
         "Large values (minutes) indicate the system is processing a "
         "BACKLOG rather than live data — normal during catch-up after "
         "the Kit was offline. Falls quickly once the live feed catches up.",
)

# Interpretation summary line
if pass_rate_pct is not None and avg_lat is not None:
    if (pass_rate_pct or 0) >= 95 and (avg_lat or 0) < 5000 and critical_alerts == 0:
        st.success("**Operational status: GREEN** — feed is healthy, data quality nominal, no critical alerts. Safe to rely on the feed for live operational decisions.")
    elif critical_alerts > 0 or (pass_rate_pct or 0) < 70:
        st.error(
            f"**Operational status: RED** — {critical_alerts} CRITICAL alerts active "
            f"and/or {pass_rate_pct:.0f}% schema conformance is below 70%. "
            "Supervisor should validate against secondary sources (STARS console, "
            "ATIS, telephone coordination) before using feed data for ops decisions."
        )
    else:
        st.warning(
            "**Operational status: AMBER** — feed is functional but degraded. "
            "Review individual metrics below and the Critic intelligence panel "
            "for context before acting on aggregate numbers."
        )

st.divider()

# ── Airport & service status ──────────────────────────────────────────────────
col_left, col_right = st.columns(2)

st.markdown('<div id="airport-health"></div>', unsafe_allow_html=True)
with col_left:
    st.subheader("🏢 Airport Feed Health")
    st.caption(
        "**Answers:** *Which airports have a SWIM feed I can trust right now?* "
        "🟢 OK · 🟡 STALE · 🟠 DEGRADED · 🔴 UNAVAILABLE/DISCONNECTED. "
        "Worst-wins across all services per airport."
    )
    if health["airport_status"]:
        airport_rows = [
            {
                "Airport": ap,
                "Status": _status_label(health["airport_status"].get(ap, "NO DATA")),
            }
            for ap in sorted(config.SUPPORTED_AIRPORTS)
        ]
        st.dataframe(pd.DataFrame(airport_rows), hide_index=True, use_container_width=True)
    else:
        st.info("No data yet. Generate sample events using the sidebar.")

with col_right:
    st.subheader("📡 Service Health by Airport")
    st.caption(
        "**Answers:** *Which specific SWIM service at which specific airport is "
        "the problem?* A single degraded service can be tolerated; multiple "
        "services degraded at the same airport indicates infrastructure issues."
    )
    svc_rows = []
    for service, airport_map in health.get("service_status", {}).items():
        for airport, status in sorted(airport_map.items()):
            svc_rows.append({
                "Service": service,
                "Airport": airport,
                "Status": _status_label(status),
            })
    if svc_rows:
        st.dataframe(pd.DataFrame(svc_rows), hide_index=True, use_container_width=True)
    else:
        st.info("No service health data yet.")

st.divider()

# ── Latest RVR table ──────────────────────────────────────────────────────────
st.markdown('<div id="rvr-section"></div>', unsafe_allow_html=True)
st.subheader("👁 Runway Visual Range (RVR)")
st.caption(
    "**Answers:** *Is the visibility on each runway safe for normal instrument "
    "approach procedures?* RVR below **1,200 ft** triggers low-visibility ops "
    "procedures (CAT II/III ILS, increased separation, possible runway closure). "
    "Highlighted red cells are safety-critical and the duty manager should "
    "verify against tower-reported conditions."
)
st.caption("Latest reading per runway, per airport:")
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

    # Highlight low RVR values
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

# ── SMES counts & latency metrics ─────────────────────────────────────────────
col_smes, col_lat = st.columns(2)

with col_smes:
    st.subheader("🛬 Surface Movement Volume")
    st.caption(
        "**Answers:** *How busy is the airport surface (taxi, ramp, runway "
        "ground movement) at each facility?* Unusual spikes can indicate a "
        "data loop, equipment fault, or genuine ground-traffic surge that "
        "warrants a tower coordination call."
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

st.markdown('<div id="latency-section"></div>', unsafe_allow_html=True)
with col_lat:
    st.subheader("⏱ Delivery Latency by Service")
    st.caption(
        "**Answers:** *Is each SWIM service delivering messages fast enough to "
        "support real-time decisions?* Sustained values above 30 s indicate "
        "broker, network, or upstream processing issues — investigate ISMC "
        "link health first."
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

# ── Validation summary ────────────────────────────────────────────────────────
st.subheader("✅ Data Quality Posture (Schema Validation)")
st.caption(
    "**Answers:** *Are upstream FAA systems sending us well-formed data?* "
    "Each incoming SWIM message is checked against service-specific schema "
    "and business rules. Sustained failures on a specific service point to "
    "an upstream data-quality issue worth a SWIM service-desk ticket."
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

# ── AI Critic Intelligence (promoted to its own section, always rendered) ────
st.markdown('<div id="critic-intel"></div>', unsafe_allow_html=True)
st.subheader("🤖 AI Critic — *Should we escalate?*")
st.caption(
    "**Answers:** *Which CRITICAL alerts are genuinely safety-significant vs which are "
    "data artifacts or transients?* Each alert is scored 0.0–1.0 confidence with a "
    "pattern classification (CASCADE_FAILURE / ISOLATED_FAULT / WEATHER_CORRELATED / "
    "DATA_ANOMALY / TRANSIENT) and an explicit escalation recommendation. The Critic "
    "is an LLM advisory layer — the deterministic gate (HITL) is what blocks unsafe "
    "automated actions."
)

_critic_rows_all = storage.get_recent_critic_results(limit=50)
_critic_by_id = {r["alert_id"]: r for r in _critic_rows_all}

# KPI strip — each label is the question it answers for the supervisor
_cri_total = len(_critic_rows_all)
_cri_escalate = sum(1 for r in _critic_rows_all if r.get("escalate"))
_cri_cascade = sum(1 for r in _critic_rows_all if r.get("pattern") == "CASCADE_FAILURE")
_cri_isolated = sum(1 for r in _critic_rows_all if r.get("pattern") == "ISOLATED_FAULT")
_cri_weather = sum(1 for r in _critic_rows_all if r.get("pattern") == "WEATHER_CORRELATED")
_cri_avg_conf = (sum(r.get("confidence", 0) or 0 for r in _critic_rows_all) / _cri_total) if _cri_total else None

ck1, ck2, ck3, ck4, ck5 = st.columns(5)
ck1.metric(
    label="How many alerts has the AI critic reviewed?",
    value=f"{_cri_total:,}",
    delta="CRITICAL alerts evaluated",
    delta_color="off",
    help="Total CRITICAL alerts the Critic has evaluated since the pipeline started.",
)
ck2.metric(
    label="🔺 How many demand a supervisor look NOW?",
    value=_cri_escalate,
    delta="escalation recommended" if _cri_escalate else "no escalations",
    delta_color="inverse" if _cri_escalate else "off",
    help="Alerts the Critic flagged with escalate=true. Apply the rule: confidence ≥ 0.7 AND "
         "severity CRITICAL, OR CASCADE_FAILURE pattern across multiple services/airports.",
)
ck3.metric(
    label="How sure is the AI of its verdicts?",
    value=f"{_cri_avg_conf:.0%}" if _cri_avg_conf is not None else "—",
    delta="mean confidence across verdicts",
    delta_color="off",
    help="Mean confidence (0–1) across all Critic verdicts. Below 0.5 means evidence is mixed "
         "and the Critic itself is uncertain — supervisor should validate manually.",
)
ck4.metric(
    label="Are multi-service degradations happening?",
    value=_cri_cascade,
    delta="CASCADE_FAILURE patterns",
    delta_color="inverse" if _cri_cascade else "off",
    help="Critic verdicts classified as CASCADE_FAILURE — multiple services or multiple airports "
         "degrading together. These are the highest-severity operational events.",
)
ck5.metric(
    label="Or just single-service issues?",
    value=_cri_isolated,
    delta="ISOLATED_FAULT patterns",
    delta_color="off",
    help="Critic verdicts classified as ISOLATED_FAULT — single service at single airport. "
         "Less urgent than cascades but still worth investigation.",
)

if not _critic_rows_all:
    st.info(
        "**No Critic verdicts yet.** The AI Critic runs only on CRITICAL alerts, and only "
        "when `LLM_PROVIDER` + a valid API key are configured in `.env`. Once Gemini "
        "(or Anthropic) is wired and a CRITICAL alert fires, each verdict shows up "
        "here within seconds."
    )
else:
    # Card-style render of recent Critic verdicts, joined to their alerts where possible
    _alert_id_to_row = {a.get("alert_id"): a for a in alerts_raw}
    _SEV_PATTERN_COLOR = {
        "CASCADE_FAILURE":    "red",
        "ISOLATED_FAULT":     "orange",
        "WEATHER_CORRELATED": "orange",
        "DATA_ANOMALY":       "blue",
        "TRANSIENT":          "grey",
        "UNKNOWN":            "grey",
    }
    for cr in _critic_rows_all[:10]:
        alert_row = _alert_id_to_row.get(cr.get("alert_id"), {})
        conf = cr.get("confidence")
        conf_str = f"{conf:.0%}" if conf is not None else "N/A"
        conf_color = "red" if (conf or 0) >= 0.7 else ("orange" if (conf or 0) >= 0.5 else "grey")
        pattern = cr.get("pattern", "UNKNOWN")
        pattern_color = _SEV_PATTERN_COLOR.get(pattern, "grey")
        escalate = bool(cr.get("escalate"))
        escalate_badge = "🔺 ESCALATE TO SUPERVISOR" if escalate else "—"
        reasoning = cr.get("reasoning", "")
        alert_type = alert_row.get("alert_type", "—")
        airport = alert_row.get("airport_code", "—")
        service = alert_row.get("service_name", "—")

        with st.container(border=True):
            h1, h2, h3, h4 = st.columns([3, 1, 2, 2])
            h1.markdown(f"**{alert_type}** · `{airport}` · `{service}`")
            h2.markdown(f"confidence: :{conf_color}[**{conf_str}**]")
            h3.markdown(f"pattern: :{pattern_color}[**{pattern}**]")
            h4.markdown(f":red[**{escalate_badge}**]" if escalate else escalate_badge)
            st.markdown(reasoning if reasoning else "_No reasoning recorded._")
            # Citations
            cits_raw = cr.get("citations", "[]")
            try:
                cits = json.loads(cits_raw) if isinstance(cits_raw, str) else cits_raw
            except Exception:
                cits = []
            if cits:
                st.caption(f"Evidence event_ids: `{', '.join(cits[:6])}`")

st.divider()

# ── Human Review Queue (HITL Execution Gate) ─────────────────────────────────
from src.execution_gate import (
    approve_staged_action as gate_approve,
    reject_staged_action as gate_reject,
    get_pending_staged_actions,
    get_recent_staged_actions,
    get_action_audit_log,
)
from src.storage import (
    approve_operational_ticket,
    reject_operational_ticket,
    mark_operational_ticket_monitoring,
    get_ticket_by_action_id,
    get_recent_operational_tickets,
)

st.markdown('<div id="hitl-queue"></div>', unsafe_allow_html=True)
st.subheader("🧑‍✈️ Human Review Queue — *What needs operator approval?*")
st.caption(
    "**Answers:** *Which CRITICAL alerts are staged as PENDING actions that "
    "cannot execute until a human approves them?* Every CRITICAL alert from "
    "the deterministic engine produces a StagedAction here. The AI Critic can "
    "recommend escalation but cannot move an action out of PENDING. Every "
    "approve / reject writes an immutable row in `action_audit_log`."
)

_pending_actions = get_pending_staged_actions()

if not _pending_actions:
    st.success(
        "✅ **No pending actions.** Either no CRITICAL alerts are active, or all "
        "staged actions have been reviewed. Pipeline is operating without "
        "human intervention required."
    )
else:
    st.warning(
        f"⚠️ **{len(_pending_actions)} staged action(s) await operator approval.** "
        "None will execute until decided here."
    )
    for _action in _pending_actions:
        _ticket = get_ticket_by_action_id(_action.action_id)
        with st.container(border=True):
            # Title row from the ticket if available, else from the action
            title = (_ticket.title if _ticket else
                     f"{_action.alert_type} at {_action.airport_code or '—'}")
            h1, h2, h3 = st.columns([3, 1, 2])
            h1.markdown(f"### {title}")
            conf_str = (
                f"{_action.critic_confidence:.0%}"
                if _action.critic_confidence is not None else "—"
            )
            h2.markdown(f"AI conf: **{conf_str}**")
            posture = _ticket.posture if _ticket else "ACTION"
            posture_color = {"ACTION": "red", "ATTENTION": "orange",
                             "NORMAL": "blue", "GOOD": "green"}.get(posture, "grey")
            h3.markdown(f"posture: :{posture_color}[**{posture}**]")

            # Summary from the Watch Officer Brief
            if _ticket and _ticket.summary:
                st.markdown(f"**Summary:** {_ticket.summary}")
            else:
                st.markdown(f"**Recommended action:** {_action.recommended_action}")

            # Watch Officer detail (the Escalation Package narrative)
            if _ticket:
                pc1, pc2 = st.columns(2)
                with pc1:
                    if _ticket.what_changed:
                        st.markdown("**📋 What changed**")
                        for it in _ticket.what_changed[:4]:
                            st.markdown(f"- {it}")
                    if _ticket.recommended_review:
                        st.markdown("**👀 Recommended review**")
                        for it in _ticket.recommended_review[:4]:
                            st.markdown(f"- {it}")
                with pc2:
                    if _ticket.why_it_matters:
                        st.markdown("**🎯 Why it matters**")
                        for it in _ticket.why_it_matters[:4]:
                            st.markdown(f"- {it}")
                    if _ticket.evidence:
                        st.markdown("**🔬 Evidence**")
                        for it in _ticket.evidence[:4]:
                            st.markdown(f"- {it}")

                if _ticket.critic_rationale:
                    with st.expander("🤖 AI Critic rationale"):
                        st.markdown(_ticket.critic_rationale)

                st.caption(
                    f"ticket_id: `{_ticket.ticket_id[:18]}` · "
                    f"action_id: `{_action.action_id[:18]}` · "
                    f"brief_id: `{(_ticket.brief_id or '')[:18]}` · "
                    f"status: `{_ticket.status}` · "
                    f"drafted: {_ticket.generated_at.strftime('%H:%M:%S')} UTC"
                )

            # Decision note + three-button row
            note_key = f"note_{_action.action_id}"
            ap_key = f"approve_{_action.action_id}"
            mo_key = f"monitor_{_action.action_id}"
            rj_key = f"reject_{_action.action_id}"

            note = st.text_input(
                "Decision note (will be recorded in audit trail)",
                key=note_key,
                placeholder="e.g., verified vs STARS console, contacting KDCA tower",
            )
            b1, b2, b3, _sp = st.columns([1.4, 1.4, 1.4, 2])
            if b1.button("✅ Approve Escalation", key=ap_key, type="primary", use_container_width=True):
                gate_approve(_action.action_id, actor="dashboard_operator", note=note or "")
                if _ticket:
                    approve_operational_ticket(_ticket.ticket_id, actor="dashboard_operator", note=note or "")
                st.toast(f"Escalation APPROVED for {_action.action_id[:8]}", icon="✅")
                st.rerun()
            if b2.button("👁 Continue Monitoring", key=mo_key, use_container_width=True,
                         help="Keep staged action PENDING; mark ticket MONITORING"):
                if _ticket:
                    mark_operational_ticket_monitoring(_ticket.ticket_id, actor="dashboard_operator", note=note or "")
                    st.toast(f"Ticket {_ticket.ticket_id[:8]} -> MONITORING", icon="👁")
                else:
                    st.warning("No ticket linked to this action; cannot mark monitoring.")
                st.rerun()
            if b3.button("❌ Reject", key=rj_key, use_container_width=True):
                gate_reject(_action.action_id, actor="dashboard_operator", note=note or "")
                if _ticket:
                    reject_operational_ticket(_ticket.ticket_id, actor="dashboard_operator", note=note or "")
                st.toast(f"Action {_action.action_id[:8]} REJECTED", icon="❌")
                st.rerun()

# Decision Audit Trail (always rendered)
st.markdown('<div id="audit-trail"></div>', unsafe_allow_html=True)
st.subheader("🗂 Decision Audit Trail")
st.caption(
    "**Answers:** *Who approved or rejected which action, when, and why?* "
    "Immutable append-only log of every staged-action state transition. "
    "Sourced from `action_audit_log` table."
)

_audit_rows = get_action_audit_log(limit=50)
_recent_actions = get_recent_staged_actions(limit=50)
_action_map = {a.action_id: a for a in _recent_actions}

if not _audit_rows:
    st.info("No staged actions have been decided yet. As you approve/reject above, decisions land here.")
else:
    audit_display = []
    for r in _audit_rows:
        act = _action_map.get(r.get("action_id"))
        audit_display.append({
            "Decided (UTC)":   r.get("created_at", "")[:19].replace("T", " "),
            "Action":          (r.get("action_id") or "")[:18] + "…",
            "Airport":         act.airport_code if act else "—",
            "Alert Type":      act.alert_type if act else "—",
            "From → To":       f"{r.get('previous_status','?')} → {r.get('new_status','?')}",
            "Actor":           r.get("actor", "—"),
            "Note":            r.get("note", "") or "—",
        })
    st.dataframe(
        pd.DataFrame(audit_display),
        hide_index=True,
        use_container_width=True,
        height=240,
    )

st.divider()

# ── Alert log ─────────────────────────────────────────────────────────────────
st.markdown('<div id="alert-log"></div>', unsafe_allow_html=True)
st.subheader("🚨 Operational Alert Log")
st.caption(
    "**Answers:** *What does the duty manager need to act on?* "
    "Deterministic alerts (never bypassed by AI) for safety-critical conditions "
    "and feed-health issues. The **AI Critic Intelligence** panel below scores "
    "each CRITICAL alert with confidence, pattern classification, and escalation "
    "recommendation so the supervisor knows which alerts demand immediate human "
    "attention vs which are likely spurious."
)
if not alerts_df.empty:
    # Load critic results and index by alert_id
    critic_rows = storage.get_recent_critic_results(limit=200)
    critic_by_id = {r["alert_id"]: r for r in critic_rows}

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

    # Critic Intelligence panel — only CRITICAL alerts with critic results
    critical_with_critic = [
        (row, critic_by_id[row["alert_id"]])
        for _, row in alerts_df.iterrows()
        if row.get("severity") == "CRITICAL" and row.get("alert_id") in critic_by_id
    ]
    # (Inline Critic block in alert log is intentionally removed.
    # The promoted "AI Critic Intelligence" section above handles all rendering.)
    if False and critical_with_critic:
        st.markdown('<div id="critic-intel-inline"></div>', unsafe_allow_html=True)
        st.subheader("🤖 AI Critic Intelligence")
        for alert_row, cr in critical_with_critic[:10]:
            conf = cr.get("confidence")
            conf_str = f"{conf:.0%}" if conf is not None else "N/A"
            pattern = cr.get("pattern", "UNKNOWN")
            escalate = bool(cr.get("escalate"))
            reasoning = cr.get("reasoning", "")

            badge = "🔺 ESCALATE" if escalate else ""
            conf_color = "red" if (conf or 0) >= 0.7 else "orange"

            with st.container(border=True):
                h1, h2, h3, h4 = st.columns([3, 1, 2, 1])
                h1.markdown(f"**{alert_row.get('alert_type')}** · {alert_row.get('airport_code','')}")
                h2.markdown(f"conf: **:{conf_color}[{conf_str}]**")
                h3.markdown(f"pattern: `{pattern}`")
                h4.markdown(f"**{badge}**" if badge else "")
                st.caption(reasoning)

                cit_raw = cr.get("citations", "[]")
                try:
                    import json as _json
                    cits = _json.loads(cit_raw) if isinstance(cit_raw, str) else cit_raw
                except Exception:
                    cits = []
                if cits:
                    st.caption(f"Citations: {', '.join(cits[:5])}")

    # Alert type breakdown
    with st.expander("Alert type breakdown"):
        type_counts = alerts_df["alert_type"].value_counts().reset_index()
        type_counts.columns = ["Alert Type", "Count"]
        st.dataframe(type_counts, hide_index=True, use_container_width=True)
        st.bar_chart(type_counts.set_index("Alert Type"))
else:
    st.success("No alerts in the log. System appears healthy.")

st.divider()

# ── Replay test results panel ─────────────────────────────────────────────────
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

# ── Live SWIM Folder Status panel ─────────────────────────────────────────────
st.subheader("Live SWIM Folder Status")
st.caption(
    "This panel reflects the output folder written by the "
    "FAA SCDS Jumpstart JMS consumer. "
    "Configure the Jumpstart Kit to write to `data/raw/swim_live/`, "
    "then run `python -m src.main --mode swim-folder` to ingest."
)

swim_live_dir = config.SWIM_RAW_FOLDER
failed_dir = config.DATA_DIR / "failed"
processed_dir = config.PROCESSED_DIR

sf1, sf2, sf3, sf4 = st.columns(4)

pending_files = list([p for p in swim_live_dir.iterdir() if p.is_file()]) if swim_live_dir.exists() else []
processed_files = list([p for p in processed_dir.iterdir() if p.is_file()]) if processed_dir.exists() else []
# Exclude the ingest log itself from the count
processed_files = [f for f in processed_files if f.name != "ingest_log.jsonl"]
failed_files = list([p for p in failed_dir.iterdir() if p.is_file()]) if failed_dir.exists() else []

sf1.metric("Pending Files", len(pending_files), help="Files waiting in swim_live/")
sf2.metric("Processed Files", len(processed_files), help="Successfully ingested")
sf3.metric("Failed Files", len(failed_files), help="Parse or normalisation errors")

# Latest file timestamp from ingest log
from src.swim_file_ingest import read_ingest_log
ingest_log_entries = read_ingest_log(limit=50)
processed_entries = [e for e in ingest_log_entries if e.get("status") == "processed"]
if processed_entries:
    latest_ts = processed_entries[0].get("timestamp", "")
    latest_svc = processed_entries[0].get("services", [])
    sf4.metric("Latest Ingest", latest_ts[11:19] + " UTC" if latest_ts else "--")
    st.caption(f"Latest service(s) detected: {', '.join(latest_svc) if latest_svc else '--'}")
else:
    sf4.metric("Latest Ingest", "--")

# Trigger ingest from dashboard
col_ingest, col_hint = st.columns([1, 2])
with col_ingest:
    if st.button("Scan swim_live/ Now", use_container_width=True,
                 disabled=len(pending_files) == 0):
        from src.swim_file_ingest import scan_swim_folder
        with st.spinner("Scanning swim_live/ ..."):
            sw_events, sw_stats = scan_swim_folder(swim_live_dir)
            for e in sw_events:
                res = validate_event(e)
                e.validation_status = "PASSED" if res.passed else "FAILED"
                e.validation_errors = res.errors
            save_events(sw_events)
            sw_alerts = check_alerts(sw_events)
            save_alerts(sw_alerts)
        st.success(
            f"Ingested {sw_stats.events_parsed} events from "
            f"{sw_stats.files_processed} file(s). "
            f"{sw_stats.files_failed} failed. "
            f"{len(sw_alerts)} alert(s) triggered."
        )
        st.rerun()

with col_hint:
    if len(pending_files) == 0:
        st.info(
            "No files in `data/raw/swim_live/`. "
            "Drop .json / .xml / .txt STDDS messages here, or "
            "configure the FAA SCDS Jumpstart Kit to write to this folder."
        )

# Ingest audit log table
if ingest_log_entries:
    with st.expander("Ingest audit log (latest 50 entries)"):
        log_df = pd.DataFrame(ingest_log_entries)
        st.dataframe(log_df, hide_index=True, use_container_width=True)

st.divider()
st.caption(
    "NAS FeedOps Sentinel · FAA SWIM/STDDS PCT Automation Demonstration · "
    "All data is simulated unless the FAA SCDS Jumpstart Kit is connected."
)
