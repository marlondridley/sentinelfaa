"""
Operational Alert Log -- the triage console.

Answers "what does the duty manager need to act on, and in what order?"

This page is the far end of every alert drill-down on the Overview page.
"205 open alerts" there is a link to here; "14 critical" is a link to here
with `?severity=CRITICAL` already applied. That is the whole reason the
filter state is read from the URL rather than only from widgets: a number
on a status board is useless if seeing what is behind it means arriving on
a fresh page and rebuilding the filter by hand.

Accepted query parameters, all optional and all case-insensitive:
    ?severity=CRITICAL        one or more, comma-separated
    ?airport=KIAD             one or more, comma-separated
    ?service=TAIS             one or more, comma-separated
    ?type=ALERT_LOW_RVR       one or more, comma-separated
    ?focus=records            the drill-down: the matching alerts open right
                              under the Triage tiles, not at the bottom
    ?focus=newest             the same, led by the newest matching alert

Every tile on this page is itself a drill-down. The Triage tiles narrow to
their severity, a facility tile to its airport, and both keep the rest of
the current filter and open the records under the tiles: a click shows the
alerts behind the number, where the eye already is.
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
from lib.data import as_utc, bucket_counts
from lib.page_setup import bootstrap_page
from src import storage
from src.async_bridge import run_async as run

bootstrap_page(page_title="Alert Log — PCT FeedOps Sentinel", page_icon="\U0001F6A8")

theme.page_header(
    "Operational Alert Log",
    "What does the duty manager need to act on, and in what order?",
    detail=(
        "Every alert here was raised by a deterministic rule in the alert engine "
        "against an ingested SWIM message -- none is model-generated. Severity is "
        "assigned by the rule that fired, not inferred."
    ),
)

alerts_raw = run(storage.get_recent_alerts(limit=500))

if not alerts_raw:
    st.markdown(
        f'<div style="border:1px solid {theme.TONES["ok"][1]};border-left:3px solid '
        f'{theme.GREEN5};background:{theme.TONES["ok"][2]};border-radius:4px;'
        f'padding:16px 18px;">'
        f'<div style="color:{theme.GREEN5};font-size:18px;font-weight:700;">'
        "No alerts in the log</div>"
        f'<div style="color:{theme.GRAY3};font-size:14px;margin-top:5px;">'
        "Either the system is healthy or nothing has been ingested yet. Check "
        "messages ingested on the Overview page to tell the two apart."
        "</div></div>",
        unsafe_allow_html=True,
    )
    theme.back_link()
    st.stop()

df = pd.DataFrame(alerts_raw)
for _col in ("severity", "alert_type", "airport_code", "service_name", "description"):
    if _col not in df.columns:
        df[_col] = None
df["triggered_at"] = df["triggered_at"].map(as_utc)
df = df.sort_values("triggered_at", ascending=False, na_position="last")


# ── Filter state: URL first, widgets second ───────────────────────────────────
def _from_query(param: str, allowed: list[str]) -> list[str]:
    """
    Read a comma-separated filter out of the URL, keeping only values that
    actually occur in the data.

    Silently dropping unknown values rather than erroring is deliberate: a
    stale bookmark pointing at an airport that has since gone quiet should
    open an unfiltered page, not an error page.
    """
    raw = st.query_params.get(param)
    if not raw:
        return []
    wanted = {v.strip().upper() for v in str(raw).split(",") if v.strip()}
    return [a for a in allowed if str(a).upper() in wanted]


_severities = [s for s in ["CRITICAL", "WARNING", "INFO"] if s in set(df["severity"].dropna())]
_types = sorted(str(t) for t in df["alert_type"].dropna().unique())
_airports = sorted(str(a) for a in df["airport_code"].dropna().unique())
_services = sorted(str(s) for s in df["service_name"].dropna().unique())

theme.section(
    "Filter",
    "Arrives pre-filtered when opened from an Overview tile.",
    right=f"{len(df):,} alerts in store",
)

f1, f2, f3, f4 = st.columns(4)
sel_sev = f1.multiselect(
    "Severity", _severities,
    default=_from_query("severity", _severities) or _severities,
)
sel_type = f2.multiselect("Alert type", _types, default=_from_query("type", _types))
sel_airport = f3.multiselect("Airport", _airports, default=_from_query("airport", _airports))
sel_service = f4.multiselect("Service", _services, default=_from_query("service", _services))

view = df[df["severity"].isin(sel_sev)] if sel_sev else df
if sel_type:
    view = view[view["alert_type"].isin(sel_type)]
if sel_airport:
    view = view[view["airport_code"].isin(sel_airport)]
if sel_service:
    view = view[view["service_name"].isin(sel_service)]

_focus = str(st.query_params.get("focus") or "").lower()
_focus = _focus if _focus in ("records", "newest") else ""


def _link(**changes: str | None) -> str:
    """
    This page with the current filter, some of it changed. Severity is left
    out when every severity is selected (the default), so links stay short.
    """
    params: dict[str, str | None] = {
        "severity": ",".join(sel_sev) if sel_sev and set(sel_sev) != set(_severities) else None,
        "type": ",".join(sel_type) or None,
        "airport": ",".join(sel_airport) or None,
        "service": ",".join(sel_service) or None,
    }
    params.update(changes)
    return theme.drill_href("alerts", **params)


def _describe(frame: pd.DataFrame) -> str:
    """'110 warning alerts at KBWI', in words, for the drill-down's heading."""
    sevs = sorted(set(frame["severity"].dropna()))
    what = (sevs[0].lower() + " " if len(sevs) == 1 else "") + ("alert" if len(frame) == 1 else "alerts")
    where = sorted(set(frame["airport_code"].dropna()))
    rules = sorted(set(frame["alert_type"].dropna()))
    return (f"{len(frame):,} {what}"
            + (f" at {', '.join(where)}" if 0 < len(where) <= 3 else "")
            + (f" · {rules[0]}" if len(rules) == 1 else ""))

# ── Shape of the filtered set ─────────────────────────────────────────────────
n_crit = int((view["severity"] == "CRITICAL").sum())
n_warn = int((view["severity"] == "WARNING").sum())
n_info = int((view["severity"] == "INFO").sum())

newest = view["triggered_at"].max() if not view.empty else None
if newest is not None and pd.notna(newest):
    _age_min = (pd.Timestamp.now(tz="UTC") - newest).total_seconds() / 60
    if _age_min < 60:
        age_display, age_unit = f"{_age_min:.0f}", "min"
    elif _age_min < 1440:
        age_display, age_unit = f"{_age_min / 60:.1f}", "h"
    else:
        age_display, age_unit = f"{_age_min / 1440:.1f}", "d"
    # An alert log whose newest entry is days old is not a quiet system, it
    # is a stopped one -- worth colouring, because the count alone reads the
    # same either way.
    age_tone = "ok" if _age_min < 60 else "warn" if _age_min < 1440 else "bad"
else:
    age_display, age_unit, age_tone = "—", "", "neutral"

theme.section("Triage", "Everything below reflects the filter above.", tone="bad" if n_crit else "info")

_SHOW = "SHOW ↓"   # the tiles' drill badge: the alerts open right below
theme.kpi_row(
    [
        theme.kpi("Matching alerts", f"{len(view):,}", tone="info",
                  foot=f"of {len(df):,} in the store",
                  href=_link(focus="records") if len(view) else None, drill=_SHOW),
        theme.kpi("Critical", f"{n_crit:,}", tone="bad" if n_crit else "ok",
                  foot="Supervisor action required" if n_crit else "None in this view",
                  href=_link(severity="CRITICAL", focus="records") if n_crit else None, drill=_SHOW),
        theme.kpi("Warning", f"{n_warn:,}", tone="warn" if n_warn else "ok",
                  foot="Monitor and corroborate",
                  href=_link(severity="WARNING", focus="records") if n_warn else None, drill=_SHOW),
        theme.kpi("Informational", f"{n_info:,}", tone="neutral", foot="Context only",
                  href=_link(severity="INFO", focus="records") if n_info else None, drill=_SHOW),
        theme.kpi("Newest alert", age_display, unit=age_unit, tone=age_tone,
                  foot="Age of the most recent matching alert",
                  spark=bucket_counts(view.to_dict("records"), "triggered_at", buckets=12),
                  href=_link(focus="newest") if len(view) else None, drill="OPEN ↓"),
    ],
    columns=5,
)

if view.empty:
    st.info("No alerts match this filter. Widen the selection above.")
    theme.back_link()
    st.stop()


def _record_card(record: pd.Series) -> None:
    """One alert in full: what fired, where, when, and where to look next."""
    _sev = str(record.get("severity") or "UNKNOWN")
    with st.container(border=True):
        st.markdown(
            '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
            + theme.status_chip(_sev)
            + '<span style="font-size:16px;font-weight:650;color:' + theme.LIGHT_GRAY5 + ';">'
            + theme._esc(record.get("alert_type")) + "</span>"
            + '<span class="mono" style="margin-left:auto;font-size:13px;color:'
            + theme.GRAY1 + ';">'
            + theme._esc(
                record["triggered_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
                if pd.notna(record.get("triggered_at")) else "no timestamp"
            )
            + "</span></div>"
            '<div style="font-size:15px;color:' + theme.GRAY5
            + ';margin-top:10px;line-height:1.55;">'
            + theme._esc(record.get("description")) + "</div>",
            unsafe_allow_html=True,
        )
        d1, d2, d3 = st.columns(3)
        d1.markdown(
            f'<div class="ev-title">Airport</div><div class="mono" style="color:{theme.GRAY5};">'
            f'{theme._esc(record.get("airport_code") or "—")}</div>',
            unsafe_allow_html=True,
        )
        d2.markdown(
            f'<div class="ev-title">Service</div><div class="mono" style="color:{theme.GRAY5};">'
            f'{theme._esc(record.get("service_name") or "—")}</div>',
            unsafe_allow_html=True,
        )
        d3.markdown(
            f'<div class="ev-title">Source event</div><div class="mono" '
            f'style="color:{theme.GRAY5};font-size:13px;">'
            f'{theme._esc(record.get("event_id") or "—")}</div>',
            unsafe_allow_html=True,
        )
        if record.get("airport_code"):
            st.markdown(
                f'<a href="{theme.drill_href("health", airport=str(record["airport_code"]))}" '
                f'target="_self" style="color:{theme.BLUE5};font-size:14px;'
                f'text-decoration:none;font-weight:600;">'
                f'Check {theme._esc(record["airport_code"])} feed health &rarr;</a>',
                unsafe_allow_html=True,
            )


_COLS = ["Triggered (UTC)", "Severity", "Alert type", "Airport", "Service", "Description"]
_table = view.copy()
_table["Triggered (UTC)"] = _table["triggered_at"].dt.strftime("%Y-%m-%d %H:%M:%S")
_table = _table.rename(columns={"severity": "Severity", "alert_type": "Alert type", "airport_code": "Airport",
                                "service_name": "Service", "description": "Description"})


def _records(title: str, question: str) -> None:
    """The alert records: select a row to read it in full."""
    theme.section(title, question, right=f"{len(view):,} shown · newest first")
    selection = st.dataframe(
        _table[_COLS],
        hide_index=True,
        use_container_width=True,
        height=380,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Description": st.column_config.TextColumn(width="large"),
            "Severity": st.column_config.TextColumn(width="small"),
        },
    )
    rows = selection.get("selection", {}).get("rows", []) if isinstance(selection, dict) else []
    if rows:
        # .iloc, not .loc: the row index the selection reports is positional
        # within the *filtered, re-sorted* view, and the DataFrame still carries
        # its original labels from the full alert set.
        _record_card(view.iloc[rows[0]])
    st.download_button(
        "Download this view as CSV",
        data=_table[_COLS].to_csv(index=False).encode("utf-8"),
        file_name="pct_alert_log.csv",
        mime="text/csv",
        help="Exports exactly the filtered rows shown above, for an incident record.",
    )


# ── The drill-down: what was clicked, right under the tiles ───────────────────
if _focus:
    if _focus == "newest":
        theme.section("Newest alert", _describe(view.head(1)), tone="info")
        _record_card(view.iloc[0])
    _records("Drill-down: " + _describe(view), "Select a row to read the full description.")
    st.markdown(
        f'<a href="{_link()}" target="_self" style="color:{theme.BLUE5};font-size:14px;'
        f'text-decoration:none;font-weight:600;">Close the drill-down &times;</a>',
        unsafe_allow_html=True,
    )

# ── Time and type structure ───────────────────────────────────────────────────
theme.section("Where the alerts cluster", "In time, and by rule.", tone="info")

c_left, c_right = st.columns([3, 2])

_SEV_SCALE = alt.Scale(
    domain=["CRITICAL", "WARNING", "INFO"],
    range=[theme.RED5, theme.ORANGE5, theme.BLUE5],
)

with c_left:
    st.markdown(
        f'<div class="ev-title">Alerts over time</div>'
        f'<div style="font-size:13px;color:{theme.GRAY1};margin-bottom:4px;">'
        "Hourly buckets. A single tall bar is an incident; a flat band is a rule "
        "firing continuously, which usually means a threshold needs revisiting."
        "</div>",
        unsafe_allow_html=True,
    )
    hist_df = view.dropna(subset=["triggered_at"]).copy()
    if hist_df.empty:
        st.info("No timestamped alerts in this view.")
    else:
        hist_df["hour"] = hist_df["triggered_at"].dt.floor("h")
        chart = (
            alt.Chart(hist_df)
            .mark_bar()
            .encode(
                x=alt.X("hour:T", title=None,
                        axis=alt.Axis(grid=False, labelColor=theme.GRAY1,
                                      domainColor=theme.DARK_GRAY4, tickColor=theme.DARK_GRAY4)),
                y=alt.Y("count():Q", title="alerts",
                        axis=alt.Axis(grid=True, gridColor=theme.DARK_GRAY2,
                                      labelColor=theme.GRAY1, titleColor=theme.GRAY1,
                                      domainColor=theme.DARK_GRAY4)),
                color=alt.Color("severity:N", scale=_SEV_SCALE,
                                legend=alt.Legend(orient="top", title=None,
                                                  labelColor=theme.GRAY3)),
                tooltip=["hour:T", "severity:N", alt.Tooltip("count():Q", title="alerts")],
            )
            .properties(height=230)
            .configure_view(strokeWidth=0)
            .configure(background="transparent")
        )
        st.altair_chart(chart, use_container_width=True)

with c_right:
    st.markdown(
        f'<div class="ev-title">Which rules are firing</div>'
        f'<div style="font-size:13px;color:{theme.GRAY1};margin-bottom:4px;">'
        "Alerts per rule in this view. One rule far ahead of the rest points at a single root cause."
        "</div>",
        unsafe_allow_html=True,
    )
    # Counted here, then plotted as sum(count): each row of type_df is already
    # a count, so the chart must add them up, not count the rows (which drew
    # every rule at exactly 1).
    type_df = (
        view.groupby(["alert_type", "severity"], dropna=False)
        .size().reset_index(name="count")
    )
    totals = view.groupby("alert_type").size().reset_index(name="total")
    totals["share"] = totals["total"] / max(len(view), 1)
    order = totals.sort_values("total", ascending=False)["alert_type"].tolist()
    _y = alt.Y("alert_type:N", sort=order, title=None,
               axis=alt.Axis(labelColor=theme.GRAY3, labelLimit=220,
                             domainColor=theme.DARK_GRAY4, tickColor=theme.DARK_GRAY4))
    bars = alt.Chart(type_df).mark_bar().encode(
        y=_y,
        x=alt.X("sum(count):Q", title="alerts",
                scale=alt.Scale(domain=[0, int(totals["total"].max() * 1.18) + 1]),
                axis=alt.Axis(grid=True, gridColor=theme.DARK_GRAY2, format="d", tickMinStep=1,
                              labelColor=theme.GRAY1, titleColor=theme.GRAY1, domainColor=theme.DARK_GRAY4)),
        color=alt.Color("severity:N", scale=_SEV_SCALE, legend=None),
        tooltip=["alert_type:N", "severity:N", alt.Tooltip("sum(count):Q", title="alerts")],
    )
    counts = alt.Chart(totals).mark_text(align="left", dx=6, fontSize=15, fontWeight="bold",
                                         color=theme.LIGHT_GRAY5).encode(
        y=_y, x=alt.X("total:Q"), text=alt.Text("total:Q", format=","),
        tooltip=["alert_type:N", alt.Tooltip("total:Q", title="alerts", format=","),
                 alt.Tooltip("share:Q", title="share of this view", format=".0%")],
    )
    type_chart = (
        alt.layer(bars, counts)
        .properties(height=max(120, 46 * len(order)))
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )
    st.altair_chart(type_chart, use_container_width=True)

# ── Per-airport spread ────────────────────────────────────────────────────────
_ap = view.dropna(subset=["airport_code"])
if not _ap.empty:
    theme.section("Spread across facilities", "Isolated, or sector-wide? Select a facility to see its alerts.",
                  tone="info")
    _tiles = []
    for _code, _grp in sorted(_ap.groupby("airport_code")):
        _c = int((_grp["severity"] == "CRITICAL").sum())
        _w = int((_grp["severity"] == "WARNING").sum())
        _tiles.append(
            theme.kpi(
                str(_code),
                f"{len(_grp):,}",
                tone="bad" if _c else ("warn" if _w else "info"),
                foot=f"{_c} critical · {_w} warning",
                # This facility's alerts, keeping severity, rule and service:
                # the drill-down opens under the Triage tiles.
                href=_link(airport=str(_code), focus="records"),
                drill=_SHOW,
            )
        )
    theme.kpi_row(_tiles, columns=min(len(_tiles), 4))

# ── The log itself (already open at the top when drilled into) ────────────────
if not _focus:
    _records("Alert records", "Select a row to read the full description.")

theme.back_link()
