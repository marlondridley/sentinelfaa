"""
Live PCT Airspace Operations.

Answers mission questions 2-4: can we trust the current aircraft picture,
which airport is driving the traffic load, and what phase of flight is
most active.

Design notes that matter for correctness rather than looks:

  * The map draws only what the feed actually reports. TAIS track records
    carry position, altitude, ground speed and vertical speed, but no
    heading, so aircraft are drawn as points rather than as
    heading-oriented symbols -- a symbol pointing in a direction the data
    does not contain would be a fabrication an operator could act on.
  * The 10 NM rings around each airport are plain geometry at a stated
    radius, not a claim about airspace boundaries, approach gates or
    sector limits. They exist to give the eye a distance reference.
  * Selecting a flight strip highlights that aircraft on the map. The
    selection is read from session state at the top of the run, before
    the map is built, because Streamlit executes a page top to bottom and
    the table is below the map.
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

from lib import theme
from lib.data import AIRPORTS, fetch_live_events
from lib.page_setup import bootstrap_page
from src import storage
from src.async_bridge import run_async as run
from src.feed_health import compute_feed_health

bootstrap_page(page_title="Live Airspace — PCT FeedOps Sentinel", page_icon="\U0001F6E9")

# Carto's Dark Matter basemap: a public, token-free style URL. Mapbox
# styles would need an access token this deployment does not have, and
# map_style=None (the previous setting) renders aircraft over an empty
# void, which makes position impossible to judge.
_MAP_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"

_PHASE_COLOR = {
    "DEPARTURE": [45, 114, 210],
    "ARRIVAL": [200, 118, 25],
    "ENROUTE": [114, 202, 155],
    "GROUND": [143, 153, 168],
    "UNKNOWN": [95, 107, 124],
}
_PHASE_HEX = {
    "DEPARTURE": theme.BLUE3,
    "ARRIVAL": theme.ORANGE3,
    "ENROUTE": theme.GREEN5,
    "GROUND": theme.GRAY3,
    "UNKNOWN": theme.GRAY1,
}
_STATUS_RGB = {
    "OK": [35, 133, 81],
    "STALE": [200, 118, 25],
    "DEGRADED": [200, 118, 25],
    "UNAVAILABLE": [205, 66, 70],
    "DISCONNECTED": [205, 66, 70],
    "NO DATA": [95, 107, 124],
}

theme.page_header(
    "Live PCT Airspace Operations",
    "Where is every aircraft in PCT airspace right now, and how does flow break down?",
    detail=(
        "Source: FAA SWIM TAIS (live STARS track and flight-plan records). Positions "
        "are from the last 5 minutes; movement counts use a 15-minute window. An empty "
        "map means no positions were received in that window, which is a feed finding, "
        "not quiet airspace."
    ),
)

live_events = fetch_live_events(limit=1000)
health = compute_feed_health(live_events[:500]) if live_events else {"airport_status": {}}
active_aircraft = run(storage.get_active_aircraft(window_minutes=5, limit=500))
phase_counts = run(storage.get_flight_phase_counts(window_minutes=15))
altitude_dist = run(storage.get_altitude_distribution(window_minutes=15))

# Read the flight-strip selection before anything is drawn: the table that
# produces it renders lower down the page, so on the rerun its selection is
# already in session state and the map can honour it on this pass rather
# than one interaction later.
_sel_state = st.session_state.get("flight_strips")
_sel_rows = []
if isinstance(_sel_state, dict):
    _sel_rows = _sel_state.get("selection", {}).get("rows", []) or []

positioned = [
    ac for ac in active_aircraft
    if ac.get("lat") is not None and ac.get("lon") is not None
]
focus_id = None
if _sel_rows and _sel_rows[0] < len(positioned):
    focus_id = positioned[_sel_rows[0]].get("aircraft_id")

total_dep = sum(c.get("DEPARTURE", 0) for c in phase_counts.values())
total_arr = sum(c.get("ARRIVAL", 0) for c in phase_counts.values())
total_enr = sum(c.get("ENROUTE", 0) for c in phase_counts.values())
total_gnd = sum(c.get("GROUND", 0) for c in phase_counts.values())

theme.kpi_row(
    [
        theme.kpi(
            "Aircraft tracked", f"{len(active_aircraft):,}",
            tone="ok" if active_aircraft else "bad",
            foot="Distinct aircraft with a position in the last 5 min"
            if active_aircraft else "No live positions — check the feed",
            href=None if active_aircraft else theme.drill_href("health"),
            drill="DIAGNOSE →",
        ),
        theme.kpi("Departures", f"{total_dep:,}", tone="info", foot="Climbing out of PCT airports"),
        theme.kpi("Arrivals", f"{total_arr:,}", tone="info", foot="Descending into PCT airports"),
        theme.kpi("En route", f"{total_enr:,}", tone="info", foot="Level flight through the sector"),
        theme.kpi("On the surface", f"{total_gnd:,}", tone="neutral", foot="Taxi, ramp or runway"),
    ],
    columns=5,
)

# ── Map ───────────────────────────────────────────────────────────────────────
theme.section(
    "Airspace picture",
    "Colour is phase of flight; airport markers carry feed status.",
    right=f"{len(positioned)} positions plotted",
)

map_col, side_col = st.columns([3, 2])

with map_col:
    airport_data = []
    for code, info in AIRPORTS.items():
        if info["lat"] is None:      # not in src/airports.py: counted below, but nowhere to put a marker
            continue
        status = health.get("airport_status", {}).get(code, "NO DATA")
        airport_data.append({
            "code": code,
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "status": str(status),
            "color": _STATUS_RGB.get(str(status), _STATUS_RGB["NO DATA"]) + [230],
            "ring": _STATUS_RGB.get(str(status), _STATUS_RGB["NO DATA"]) + [90],
            "label": f"{code}",
            "tip": theme._esc(f"{code} — {info['name']} — feed {status}"),
        })

    aircraft_data = []
    for ac in positioned:
        phase = str(ac.get("flight_phase") or "UNKNOWN")
        vs = ac.get("vertical_speed_fpm")
        trend = "climbing" if (vs or 0) > 200 else "descending" if (vs or 0) < -200 else "level"
        is_focus = focus_id is not None and ac.get("aircraft_id") == focus_id
        aircraft_data.append({
            "aircraft_id": ac.get("aircraft_id") or "—",
            "lat": float(ac["lat"]),
            "lon": float(ac["lon"]),
            "alt": ac.get("altitude_ft") or 0,
            "speed": ac.get("ground_speed_kt") or 0,
            "phase": phase,
            "trend": trend,
            "airport": ac.get("airport_code") or "—",
            "runway": ac.get("runway") or "—",
            "rules": ac.get("flight_rules") or "—",
            "color": _PHASE_COLOR.get(phase, _PHASE_COLOR["UNKNOWN"]) + [235],
            "radius": 900 if is_focus else 520,
            # The deck.gl tooltip is HTML assigned to innerHTML, and aircraft_id is whatever the
            # feed or an API client sent, so it is escaped like every other stored string.
            "tip": theme._esc(
                f"{ac.get('aircraft_id', '?')} — {phase} — "
                f"{ac.get('altitude_ft') or 0:,} ft {trend} — "
                f"{ac.get('ground_speed_kt') or 0} kt"
            ),
        })

    layers = [
        # 10 NM reference rings, drawn first so everything else sits on top.
        pdk.Layer(
            "ScatterplotLayer",
            data=airport_data,
            get_position="[lon, lat]",
            get_radius=18520,  # 10 NM in metres
            stroked=True,
            filled=False,
            get_line_color="ring",
            line_width_min_pixels=1,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            data=airport_data,
            get_position="[lon, lat]",
            get_fill_color="color",
            get_radius=2600,
            radius_min_pixels=7,
            radius_max_pixels=14,
            stroked=True,
            get_line_color=[255, 255, 255, 200],
            line_width_min_pixels=1,
            pickable=True,
        ),
        pdk.Layer(
            "TextLayer",
            data=airport_data,
            get_position="[lon, lat]",
            get_text="label",
            get_color=[246, 247, 249, 235],
            get_size=13,
            get_alignment_baseline="'top'",
            get_pixel_offset=[0, 14],
        ),
    ]
    if aircraft_data:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=aircraft_data,
                get_position="[lon, lat]",
                get_fill_color="color",
                get_radius="radius",
                radius_min_pixels=3,
                radius_max_pixels=9,
                stroked=True,
                get_line_color=[17, 20, 24, 255],
                line_width_min_pixels=1,
                pickable=True,
            )
        )
        # Callsigns only when the map is sparse enough to read them.
        if len(aircraft_data) <= 45:
            layers.append(
                pdk.Layer(
                    "TextLayer",
                    data=aircraft_data,
                    get_position="[lon, lat]",
                    get_text="aircraft_id",
                    get_color=[197, 203, 211, 210],
                    get_size=10,
                    get_alignment_baseline="'bottom'",
                    get_pixel_offset=[0, -9],
                )
            )

    st.pydeck_chart(
        pdk.Deck(
            map_style=_MAP_STYLE,
            initial_view_state=pdk.ViewState(
                latitude=38.92, longitude=-77.05, zoom=8.3, pitch=0, bearing=0
            ),
            layers=layers,
            tooltip={
                "html": "<b>{tip}</b>",
                "style": {
                    "backgroundColor": theme.DARK_GRAY2,
                    "color": theme.LIGHT_GRAY5,
                    "fontSize": "12px",
                    "border": f"1px solid {theme.DARK_GRAY4}",
                    "borderRadius": "3px",
                },
            },
        ),
        use_container_width=True,
    )

    _legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;">'
        f'<span style="width:9px;height:9px;border-radius:50%;background:{hexcol};'
        f'display:inline-block;"></span>'
        f'<span style="font-size:11px;color:{theme.GRAY3};">{name.title()}</span></span>'
        for name, hexcol in _PHASE_HEX.items()
    )
    st.markdown(
        f'<div style="margin-top:6px;">{_legend}</div>'
        f'<div style="font-size:11px;color:{theme.GRAY1};margin-top:6px;line-height:1.6;">'
        f"Rings are 10 NM from the field. Aircraft are drawn as points because TAIS "
        f"track records carry no heading &mdash; a directional symbol here would be "
        f"invented. Airport marker colour is feed status, not traffic."
        "</div>",
        unsafe_allow_html=True,
    )

with side_col:
    st.markdown('<div class="ev-title">Phase of flight</div>', unsafe_allow_html=True)
    phase_df = pd.DataFrame(
        [
            {"Phase": p, "Aircraft": n}
            for p, n in [
                ("DEPARTURE", total_dep), ("ARRIVAL", total_arr),
                ("ENROUTE", total_enr), ("GROUND", total_gnd),
            ]
            if n
        ]
    )
    if phase_df.empty:
        st.info("No movements in the 15-minute window.")
    else:
        st.altair_chart(
            alt.Chart(phase_df)
            .mark_arc(innerRadius=52, outerRadius=82, stroke=theme.BLACK, strokeWidth=2)
            .encode(
                theta=alt.Theta("Aircraft:Q", stack=True),
                color=alt.Color(
                    "Phase:N",
                    scale=alt.Scale(
                        domain=list(phase_df["Phase"]),
                        range=[_PHASE_HEX.get(p, theme.GRAY1) for p in phase_df["Phase"]],
                    ),
                    legend=alt.Legend(orient="right", title=None, labelColor=theme.GRAY3),
                ),
                tooltip=["Phase", "Aircraft"],
            )
            .properties(height=190)
            .configure_view(strokeWidth=0)
            .configure(background="transparent"),
            use_container_width=True,
        )

    st.markdown('<div class="ev-title">Movements by facility</div>', unsafe_allow_html=True)
    _tiles = []
    for code, info in AIRPORTS.items():
        pc = phase_counts.get(code, {})
        total_mv = sum(pc.values())
        status = str(health.get("airport_status", {}).get(code, "NO DATA"))
        _tiles.append(
            theme.kpi(
                code,
                f"{total_mv:,}",
                tone=theme.STATUS_TONE.get(status.upper(), "neutral"),
                foot=(
                    f"{pc.get('DEPARTURE', 0)} dep · {pc.get('ARRIVAL', 0)} arr · "
                    f"{pc.get('ENROUTE', 0)} enr"
                ),
                href=theme.drill_href("health", airport=code),
                drill="HEALTH →",
            )
        )
    theme.kpi_row(_tiles, columns=2)

# ── Flight strips ─────────────────────────────────────────────────────────────
theme.section(
    "Flight strips",
    "Select a row to highlight that aircraft on the map above.",
    right=f"{len(positioned)} aircraft with a position",
)

if not positioned:
    st.info(
        "No aircraft positions in the window. This is a feed condition: check "
        "Feed & Service Health before concluding the airspace is quiet."
    )
else:
    strips = pd.DataFrame(
        [
            {
                "Flight": ac.get("aircraft_id") or "—",
                "Airport": ac.get("airport_code") or "—",
                "Phase": ac.get("flight_phase") or "—",
                "Alt (ft)": ac.get("altitude_ft"),
                "GS (kt)": ac.get("ground_speed_kt"),
                "V/S (fpm)": ac.get("vertical_speed_fpm"),
                "Runway": ac.get("runway") or "—",
                "Rules": ac.get("flight_rules") or "—",
                "Squawk": ac.get("beacon_code") or "—",
            }
            for ac in positioned
        ]
    )
    st.dataframe(
        strips,
        hide_index=True,
        use_container_width=True,
        height=320,
        key="flight_strips",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "V/S (fpm)": st.column_config.NumberColumn(
                help="Positive is climbing, negative is descending.", format="%d"
            ),
        },
    )
    if focus_id:
        st.markdown(
            f'<div style="font-size:12px;color:{theme.BLUE5};">'
            f"Highlighted on the map: <b>{theme._esc(focus_id)}</b></div>",
            unsafe_allow_html=True,
        )

# ── Distributions ─────────────────────────────────────────────────────────────
if altitude_dist:
    theme.section(
        "Movement profile",
        "Do altitude and speed sit in the bands these airports should produce?",
        tone="neutral",
    )
    dist_df = pd.DataFrame(altitude_dist)
    _airport_scale = alt.Scale(
        range=[theme.BLUE3, theme.GREEN5, theme.ORANGE3, theme.VIOLET5]
    )

    def _axis(title: str, grid: bool = True) -> alt.Axis:
        return alt.Axis(
            title=title, grid=grid, gridColor=theme.DARK_GRAY2,
            labelColor=theme.GRAY1, titleColor=theme.GRAY1,
            domainColor=theme.DARK_GRAY4, tickColor=theme.DARK_GRAY4,
        )

    alt_col, gs_col = st.columns(2)
    with alt_col:
        st.markdown('<div class="ev-title">Altitude distribution</div>', unsafe_allow_html=True)
        st.altair_chart(
            alt.Chart(dist_df).mark_bar(opacity=0.9).encode(
                alt.X("altitude_ft:Q", bin=alt.Bin(maxbins=20), axis=_axis("altitude (ft)", False)),
                alt.Y("count()", axis=_axis("aircraft")),
                alt.Color("airport_code:N", scale=_airport_scale,
                          legend=alt.Legend(orient="top", title=None, labelColor=theme.GRAY3)),
                tooltip=["airport_code", alt.Tooltip("count()", title="aircraft")],
            ).properties(height=230).configure_view(strokeWidth=0).configure(background="transparent"),
            use_container_width=True,
        )
    with gs_col:
        st.markdown('<div class="ev-title">Ground speed distribution</div>', unsafe_allow_html=True)
        gs_df = dist_df[dist_df["ground_speed_kt"].notna()] if "ground_speed_kt" in dist_df else pd.DataFrame()
        if gs_df.empty:
            st.info("No ground-speed samples in the window.")
        else:
            st.altair_chart(
                alt.Chart(gs_df).mark_bar(opacity=0.9).encode(
                    alt.X("ground_speed_kt:Q", bin=alt.Bin(maxbins=20),
                          axis=_axis("ground speed (kt)", False)),
                    alt.Y("count()", axis=_axis("aircraft")),
                    alt.Color("airport_code:N", scale=_airport_scale,
                              legend=alt.Legend(orient="top", title=None, labelColor=theme.GRAY3)),
                    tooltip=["airport_code", alt.Tooltip("count()", title="aircraft")],
                ).properties(height=230).configure_view(strokeWidth=0).configure(background="transparent"),
                use_container_width=True,
            )

theme.back_link()
