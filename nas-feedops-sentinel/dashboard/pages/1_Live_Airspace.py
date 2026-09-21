"""
Live PCT Airspace Operations page.

Answers mission questions 2-4 from the Overview page's mission panel:
can we trust the current aircraft picture, which airport is driving the
traffic load, and what phase of flight is most active. Split out of the
original single-file dashboard's "Live Airspace Operations (TAIS)"
section.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from lib.data import PCT_AIRPORTS, fetch_live_events, status_label
from lib.page_setup import bootstrap_page
from src.async_bridge import run_async as run
from src.feed_health import compute_feed_health
from src import storage

bootstrap_page(page_title="Live Airspace — PCT FeedOps Sentinel", page_icon="🛩")

st.title("🛩 Live PCT Airspace Operations")
st.caption(
    "**Answers:** *Where is every aircraft in PCT airspace right now, and how "
    "does flow break down by airport and phase?* "
    "Source: FAA SWIM TAIS feed (live STARS track and flight-plan records). "
    "Aircraft counts use a 15-minute window; live positions use the last 5 minutes."
)

live_events = fetch_live_events(limit=1000)
health = compute_feed_health(live_events[:500]) if live_events else {
    "airport_status": {}, "service_status": {}, "latency_stats": {}, "smes_counts": {}
}

active_aircraft = run(storage.get_active_aircraft(window_minutes=5, limit=500))
phase_counts = run(storage.get_flight_phase_counts(window_minutes=15))
altitude_dist = run(storage.get_altitude_distribution(window_minutes=15))

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
    help="Distinct aircraft for which TAIS sent a position in the last 5 minutes.",
)
ap2.metric(
    label="🛫 How many departure records were observed?",
    value=total_dep,
    delta="climbing out of PCT airports" if total_dep else "no departures detected",
    delta_color="off",
)
ap3.metric(
    label="🛬 How much arrival traffic is flowing through PCT?",
    value=total_arr,
    delta="descending into PCT airports" if total_arr else "no arrivals detected",
    delta_color="off",
)
ap4.metric(
    label="✈ How much transition / overflight traffic is active?",
    value=total_enr,
    delta="level flight through sector" if total_enr else "no enroute traffic",
    delta_color="off",
)
ap5.metric(
    label="🅿 Are surface / ground records present?",
    value=total_gnd,
    delta="on taxi/ramp/runway" if total_gnd else "no ground records",
    delta_color="off",
)

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
            map_style=None,
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
            "Status": status_label(health["airport_status"].get(code, "NO DATA")),
            "🛫 Dep": pc.get("DEPARTURE", 0),
            "🛬 Arr": pc.get("ARRIVAL", 0),
            "✈ Enr":  pc.get("ENROUTE", 0),
            "🅿 Gnd":  pc.get("GROUND", 0),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

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
st.page_link("app.py", label="◀ Back to Overview", icon="✈")
