"""
Feed & Service Health -- is the pipe delivering, and is the data any good?

Answers mission questions 1, 5 and 6: is the feed alive, are tracks
stale/missing/abnormal, and is a problem isolated to one facility or
sector-wide.

This is the landing page for four different drill-downs from the Overview
KPI strip, which is why it opens on a view chosen by the URL rather than
always on the same one:

    ?focus=validation   schema conformance, by service
    ?focus=latency      delivery lag, by service
    ?focus=volume       ingest volume over time
    ?focus=rvr          runway visual range
    ?airport=KIAD       narrows every view to one facility

Arriving from "schema conformance 100%" on the Overview and landing on a
page that opens on an airport table would make the operator hunt for the
thing they just clicked. They land on the breakdown instead.
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
import streamlit as st

from lib import theme
from lib.data import as_utc, fetch_live_events
from lib.page_setup import bootstrap_page
from src import config, storage
from src.async_bridge import run_async as run
from src.feed_health import compute_feed_health

bootstrap_page(page_title="Feed Health — PCT FeedOps Sentinel", page_icon="\U0001F4E1")

theme.page_header(
    "Feed & Service Health",
    "Is the PCT TAIS feed alive, is the data well-formed, and is a fault isolated or sector-wide?",
    detail=(
        "Status is derived from message recency against the STALE_FEED_SECONDS budget "
        "and from feed_status values the upstream services report about themselves. A "
        "facility with no messages at all reads NO DATA, which is a different claim "
        "from OK and is never rendered as one."
    ),
)

live_events = fetch_live_events(limit=1000)
health = compute_feed_health(live_events[:500]) if live_events else {
    "airport_status": {}, "service_status": {}, "latency_stats": {}, "smes_counts": {}
}
events_raw = run(storage.get_recent_events(limit=1000))
rvr_rows = run(storage.get_latest_rvr())
val_summary = run(storage.get_validation_summary())

events_df = pd.DataFrame(events_raw) if events_raw else pd.DataFrame()
total = val_summary.get("total", 0) or 0
passed = val_summary.get("passed", 0) or 0
failed = val_summary.get("failed", 0) or 0
pass_rate = (passed / total * 100) if total else None

airports = sorted(config.SUPPORTED_AIRPORTS)
services = sorted(health.get("service_status", {}).keys()) or sorted(config.SUPPORTED_SERVICES)

# ── Filter / view state from the URL ──────────────────────────────────────────
_FOCUS_VIEWS = {
    "validation": "Data quality",
    "latency": "Latency",
    "volume": "Volume",
    "rvr": "RVR",
    "status": "Status",
}
VIEWS = ["Status", "Data quality", "Latency", "Volume", "RVR"]

_q_focus = str(st.query_params.get("focus", "")).lower()
_default_view = _FOCUS_VIEWS.get(_q_focus, "Status")
_q_airport = str(st.query_params.get("airport", "")).upper()
_default_airports = [a for a in airports if a in {v.strip() for v in _q_airport.split(",")}]

fc1, fc2 = st.columns([3, 2])
with fc1:
    view = st.segmented_control(
        "View", VIEWS, default=_default_view, label_visibility="collapsed"
    ) or _default_view
with fc2:
    sel_airports = st.multiselect(
        "Airports", airports, default=_default_airports,
        placeholder="All PCT airports", label_visibility="collapsed",
    )

focus_airports = sel_airports or airports

# ── Always-on health summary ──────────────────────────────────────────────────
_statuses = {a: health.get("airport_status", {}).get(a, "NO DATA") for a in focus_airports}
n_ok = sum(1 for s in _statuses.values() if s == "OK")
n_nodata = sum(1 for s in _statuses.values() if s == "NO DATA")
n_bad = sum(1 for s in _statuses.values() if s in ("UNAVAILABLE", "DISCONNECTED"))
n_degraded = sum(1 for s in _statuses.values() if s in ("STALE", "DEGRADED"))

_lat_all = [s["avg_ms"] for s in health.get("latency_stats", {}).values() if s.get("avg_ms")]
_avg_lat = (sum(_lat_all) / len(_lat_all)) if _lat_all else None

_rvr_values = [
    float(v)
    for row in rvr_rows or []
    for v in (row.get("rvr_touchdown"), row.get("rvr_midpoint"), row.get("rvr_rollout"))
    if v is not None
]
_rvr_min = min(_rvr_values) if _rvr_values else None

theme.kpi_row(
    [
        theme.kpi(
            "Facilities reporting OK", f"{n_ok}/{len(focus_airports)}",
            tone="ok" if n_ok == len(focus_airports) else ("bad" if n_bad else "warn"),
            foot=f"{n_degraded} degraded · {n_nodata} with no data",
        ),
        theme.kpi(
            "Schema conformance",
            f"{pass_rate:.1f}" if pass_rate is not None else "—",
            unit="%" if pass_rate is not None else "",
            tone="neutral" if pass_rate is None else ("ok" if pass_rate >= 95 else "warn"),
            foot=f"{failed:,} failed of {total:,}",
        ),
        theme.kpi(
            "Avg delivery lag",
            f"{_avg_lat / 1000:,.1f}" if _avg_lat else "—",
            unit="s" if _avg_lat else "",
            tone="neutral" if _avg_lat is None else ("ok" if _avg_lat < 5000 else "warn"),
            foot=f"Alert threshold {config.LATENCY_ALERT_MS:,} ms",
        ),
        theme.kpi(
            "Lowest RVR reported",
            f"{_rvr_min:,.0f}" if _rvr_min is not None else "—",
            unit="ft" if _rvr_min is not None else "",
            tone=(
                "neutral" if _rvr_min is None
                else "bad" if _rvr_min < config.RVR_LOW_THRESHOLD_FT else "ok"
            ),
            foot=f"Low-visibility ops below {config.RVR_LOW_THRESHOLD_FT:,} ft",
        ),
        theme.kpi(
            "Open alerts on these feeds", f"{len(run(storage.get_recent_alerts(limit=500))):,}",
            tone="info",
            foot="Full triage in the Alert Log",
            href=theme.drill_href("alerts", airport=",".join(sel_airports) if sel_airports else None),
            drill="TRIAGE →",
        ),
    ],
    columns=5,
)


def _dark_axis(title: str | None = None, grid: bool = True) -> alt.Axis:
    """Axis styling shared by every chart on this page."""
    return alt.Axis(
        title=title, grid=grid, gridColor=theme.DARK_GRAY2,
        labelColor=theme.GRAY1, titleColor=theme.GRAY1,
        domainColor=theme.DARK_GRAY4, tickColor=theme.DARK_GRAY4,
    )


def _finish(chart: alt.Chart) -> alt.Chart:
    return chart.configure_view(strokeWidth=0).configure(background="transparent")


# ── Status ────────────────────────────────────────────────────────────────────
if view == "Status":
    theme.section(
        "Service by facility",
        "One cell per SWIM service per airport. A red column is a service problem; "
        "a red row is a facility problem.",
        tone="info",
    )
    cells = {}
    for svc in services:
        per_airport = health.get("service_status", {}).get(svc, {})
        for ap in focus_airports:
            cells[(svc, ap)] = per_airport.get(ap, "NO DATA")
    theme.status_matrix(
        services, focus_airports, cells,
        href_for=lambda svc, ap: theme.drill_href("alerts", airport=ap, service=svc),
    )
    st.markdown('<div style="margin-top:8px;">' + theme.help_hint(
        "OK: a message arrived inside the freshness budget. STALE: the last message is older "
        "than that budget. DEGRADED, UNAVAILABLE, DISCONNECTED: states the upstream service "
        "declared about itself. NO DATA: nothing has ever arrived for that pair -- not a health "
        "claim. Click a cell for the alerts raised for it.", "How to read the matrix") + "</div>",
        unsafe_allow_html=True)

    theme.section("Surface movement volume", "How busy is each airport surface?", tone="neutral")
    smes = {k: v for k, v in health.get("smes_counts", {}).items() if k in focus_airports}
    if smes:
        smes_df = pd.DataFrame([{"Airport": k, "SMES events": v} for k, v in sorted(smes.items())])
        st.altair_chart(
            _finish(
                alt.Chart(smes_df).mark_bar(color=theme.BLUE3, size=28).encode(
                    x=alt.X("Airport:N", title=None, axis=_dark_axis(grid=False)),
                    y=alt.Y("SMES events:Q", axis=_dark_axis("events")),
                    tooltip=["Airport", "SMES events"],
                ).properties(height=200)
            ),
            use_container_width=True,
        )
    else:
        st.info("No SMES surface-movement events recorded for the selected airports.")

# ── Data quality ──────────────────────────────────────────────────────────────
elif view == "Data quality":
    theme.section(
        "Schema conformance",
        "Are upstream FAA systems sending well-formed data?",
        tone="ok" if (pass_rate or 0) >= 95 else "warn",
    )
    theme.kpi_row(
        [
            theme.kpi("Validated", f"{total:,}", tone="info", foot="Messages checked against schema"),
            theme.kpi("Passed", f"{passed:,}", tone="ok", foot="Conformed on first pass"),
            theme.kpi("Failed", f"{failed:,}", tone="bad" if failed else "ok",
                      foot="Rejected or flagged by validation"),
        ],
        columns=3,
    )

    if not events_df.empty and "validation_status" in events_df.columns:
        by_service = (
            events_df.groupby(["service_name", "validation_status"], dropna=False)
            .size().reset_index(name="count")
        )
        st.altair_chart(
            _finish(
                alt.Chart(by_service).mark_bar().encode(
                    y=alt.Y("service_name:N", title=None, sort="-x", axis=_dark_axis(grid=False)),
                    x=alt.X("count:Q", stack="normalize", axis=_dark_axis("share of messages")),
                    color=alt.Color(
                        "validation_status:N",
                        scale=alt.Scale(range=[theme.GREEN5, theme.RED5, theme.ORANGE5]),
                        legend=alt.Legend(orient="top", title=None, labelColor=theme.GRAY3),
                    ),
                    tooltip=["service_name", "validation_status", "count"],
                ).properties(height=max(160, 42 * by_service["service_name"].nunique()))
            ),
            use_container_width=True,
        )
        st.markdown(
            f'<div style="font-size:11.5px;color:{theme.GRAY1};">'
            "Bars are normalised so a low-volume service with a systematic problem is "
            "as visible as a high-volume one with an occasional glitch. Absolute counts "
            "are in the tooltip.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.info("No validated events yet. Replay a capture from Ingest & Replay.")

# ── Latency ───────────────────────────────────────────────────────────────────
elif view == "Latency":
    theme.section(
        "Delivery lag by service",
        "Is each SWIM service delivering fast enough to support real-time decisions?",
        tone="info",
        right=f"alert threshold {config.LATENCY_ALERT_MS:,} ms",
    )
    lat_rows = [
        {
            "Service": svc,
            "Avg (ms)": stats["avg_ms"],
            "Min (ms)": stats["min_ms"],
            "Max (ms)": stats["max_ms"],
            "Samples": stats["count"],
        }
        for svc, stats in sorted(health.get("latency_stats", {}).items())
    ]
    if lat_rows:
        lat_df = pd.DataFrame(lat_rows)
        base = alt.Chart(lat_df).encode(
            y=alt.Y("Service:N", title=None, sort="-x", axis=_dark_axis(grid=False))
        )
        st.altair_chart(
            _finish(
                (
                    base.mark_rule(color=theme.DARK_GRAY5, size=2).encode(
                        x=alt.X("Min (ms):Q", axis=_dark_axis("milliseconds"), scale=alt.Scale(type="symlog")),
                        x2="Max (ms):Q",
                    )
                    + base.mark_circle(size=140, color=theme.BLUE5).encode(
                        x="Avg (ms):Q",
                        tooltip=["Service", "Avg (ms)", "Min (ms)", "Max (ms)", "Samples"],
                    )
                ).properties(height=max(150, 44 * len(lat_df)))
            ),
            use_container_width=True,
        )
        st.markdown(
            f'<div style="font-size:11.5px;color:{theme.GRAY1};margin-bottom:8px;">'
            "The bar is the observed min-to-max range; the dot is the mean. A wide bar "
            "with a low mean is bursty delivery, which a mean alone hides. The axis is "
            "log-scaled because live and replayed captures differ by orders of magnitude."
            "</div>",
            unsafe_allow_html=True,
        )
        st.dataframe(
            lat_df.style.format(
                {"Avg (ms)": "{:,.0f}", "Min (ms)": "{:,.0f}", "Max (ms)": "{:,.0f}"}
            ),
            hide_index=True, use_container_width=True,
        )
        st.markdown(
            f'<div style="font-size:11.5px;color:{theme.GRAY1};line-height:1.6;">'
            "<b>Reading a very large lag:</b> this figure is event time to ingest time. "
            "Replaying an archived capture legitimately produces lags of hours or days, "
            "because the events really are that old. It is evidence of stale data only "
            "when the source is the live feed.</div>",
            unsafe_allow_html=True,
        )
    else:
        st.info("No latency samples recorded yet.")

# ── Volume ────────────────────────────────────────────────────────────────────
elif view == "Volume":
    theme.section(
        "Ingest volume over time",
        "Is the feed delivering at a steady rate, and which services carry it?",
        tone="info",
    )
    if events_df.empty or "received_time_utc" not in events_df.columns:
        st.info("No events ingested yet.")
    else:
        vol = events_df.copy()
        vol["received"] = vol["received_time_utc"].map(as_utc)
        vol = vol.dropna(subset=["received"])
        if "airport_code" in vol.columns and sel_airports:
            vol = vol[vol["airport_code"].isin(sel_airports)]
        if vol.empty:
            st.info("No events for the selected airports.")
        else:
            vol["bucket"] = vol["received"].dt.floor("h")
            st.altair_chart(
                _finish(
                    alt.Chart(vol).mark_area(opacity=0.8, line=False).encode(
                        x=alt.X("bucket:T", title=None, axis=_dark_axis(grid=False)),
                        y=alt.Y("count():Q", axis=_dark_axis("messages / hour"), stack=True),
                        color=alt.Color(
                            "service_name:N",
                            scale=alt.Scale(range=[theme.BLUE3, theme.GREEN3, theme.ORANGE3,
                                                   theme.VIOLET5, theme.BLUE5]),
                            legend=alt.Legend(orient="top", title=None, labelColor=theme.GRAY3),
                        ),
                        tooltip=["bucket:T", "service_name:N", alt.Tooltip("count():Q", title="messages")],
                    ).properties(height=260)
                ),
                use_container_width=True,
            )
            counts = (
                vol.groupby("service_name").size().reset_index(name="Messages")
                .sort_values("Messages", ascending=False)
            )
            theme.kpi_row(
                [
                    theme.kpi(str(r["service_name"]), f"{int(r['Messages']):,}",
                              tone="info", foot="messages in store")
                    for _, r in counts.iterrows()
                ],
                columns=max(1, min(len(counts), 5)),
            )

# ── RVR ───────────────────────────────────────────────────────────────────────
else:
    theme.section(
        "Runway visual range",
        "Is visibility on each runway safe for normal instrument approach procedures?",
        tone="info",
        right=f"low-visibility ops below {config.RVR_LOW_THRESHOLD_FT:,} ft",
    )
    if not rvr_rows:
        st.info("No RVR data yet.")
    else:
        rvr_df = pd.DataFrame(rvr_rows)
        if sel_airports:
            rvr_df = rvr_df[rvr_df["airport_code"].isin(sel_airports)]
        if rvr_df.empty:
            st.info("No RVR data for the selected airports.")
        else:
            long = rvr_df.melt(
                id_vars=["airport_code", "runway"],
                value_vars=["rvr_touchdown", "rvr_midpoint", "rvr_rollout"],
                var_name="position", value_name="rvr_ft",
            ).dropna(subset=["rvr_ft"])
            long["position"] = long["position"].str.replace("rvr_", "", regex=False)
            long["runway_label"] = long["airport_code"] + " " + long["runway"].astype(str)

            bars = alt.Chart(long).mark_bar(size=13).encode(
                y=alt.Y("runway_label:N", title=None, axis=_dark_axis(grid=False)),
                x=alt.X("rvr_ft:Q", axis=_dark_axis("RVR (ft)")),
                color=alt.condition(
                    alt.datum.rvr_ft < config.RVR_LOW_THRESHOLD_FT,
                    alt.value(theme.RED5), alt.value(theme.GREEN3),
                ),
                yOffset="position:N",
                tooltip=["airport_code", "runway", "position", "rvr_ft"],
            )
            threshold = alt.Chart(
                pd.DataFrame({"x": [config.RVR_LOW_THRESHOLD_FT]})
            ).mark_rule(color=theme.ORANGE5, strokeDash=[5, 4], size=1.5).encode(x="x:Q")
            st.altair_chart(
                _finish((bars + threshold).properties(
                    height=max(180, 46 * long["runway_label"].nunique())
                )),
                use_container_width=True,
            )
            st.markdown(
                f'<div style="font-size:11.5px;color:{theme.GRAY1};line-height:1.6;">'
                "Three bars per runway: touchdown, midpoint and rollout. The dashed line "
                f"is the {config.RVR_LOW_THRESHOLD_FT:,} ft low-visibility threshold; any "
                "bar crossing to its left is the operationally significant one. "
                "Values are scaled from the APDS feed by "
                f"APDS_RVR_SCALE_FT={config.APDS_RVR_SCALE_FT} and are rejected above "
                f"{config.APDS_RVR_MAX_PLAUSIBLE_FT:,} ft as implausible.</div>",
                unsafe_allow_html=True,
            )

theme.back_link()
