"""
The dashboard's one design system.

Why this exists
---------------
Before this module every page invented its own look: app.py carried an
inline <style> block for a single table, the ticker carried another, and
each page's numbers were plain st.metric() calls. The result is visible in
any screenshot of the old dashboard -- "319 messages ingested", "100%
conformance", "205 open alerts" and "1112 min lag" are all rendered at the
same size, in the same weight, in the same colour, so nothing on the page
tells an operator which of those four numbers is the one that should ruin
their morning. (It is the last one.)

A common operating picture works by *ranking* information, not by
displaying it. This module is where that ranking is defined once:

  * `inject_theme()`   -- global CSS: canvas, panels, typography, spacing.
  * `page_header()`    -- the identical title/answers block every page opens with.
  * `section()`        -- a labelled band, so the eye can find "Alerts" without reading.
  * `kpi_row()`        -- the ranked number strip; every tile can be a drill-down link.
  * `status_chip()`    -- one status vocabulary (OK/STALE/DEGRADED/...) with one colour map.
  * `drill_href()`     -- the single place page-to-page filter links are built.

Palette
-------
The colours are Palantir's own Blueprint 4 dark tokens rather than
something hand-picked, because the operator-facing convention they encode
(a near-black canvas, one accent blue, and status colours reserved
exclusively for status) is the thing being copied here -- not the
aesthetic. Reserving saturation for meaning is what makes a red number
read as "act on this" instead of as decoration. Nothing in this file
picks a colour for prettiness: every non-grey value below is bound to a
severity or a health state.

All helpers return or emit HTML through st.markdown(unsafe_allow_html=True).
Every caller-supplied string that lands in that HTML is escaped with
html.escape() in `_esc()` below -- alert descriptions and airport codes
come out of the database and out of LLM output, and neither is trusted to
be free of angle brackets.
"""
from __future__ import annotations

import html as _html
from typing import Any, Sequence

import streamlit as st

# ── Blueprint 4 dark tokens ───────────────────────────────────────────────────
# https://blueprintjs.com/docs/#core/colors -- greys carry structure, the
# five accent colours carry meaning and nothing else.
BLACK = "#111418"
DARK_GRAY1 = "#1C2127"
DARK_GRAY2 = "#252A31"
DARK_GRAY3 = "#2F343C"
DARK_GRAY4 = "#383E47"
DARK_GRAY5 = "#404854"
GRAY1 = "#8592A3"
GRAY3 = "#8F99A8"
GRAY5 = "#C5CBD3"
LIGHT_GRAY5 = "#F6F7F9"

BLUE3 = "#2D72D2"
BLUE5 = "#8ABBFF"
GREEN3 = "#238551"
GREEN5 = "#72CA9B"
ORANGE3 = "#C87619"
ORANGE5 = "#FBB360"
RED3 = "#CD4246"
RED5 = "#FA999C"
VIOLET5 = "#D0B0FF"

# Tone -> (foreground, border, wash). A "tone" is the answer to "how
# worried should the operator be about this number?", and it is the only
# thing that drives colour anywhere in the dashboard.
TONES: dict[str, tuple[str, str, str]] = {
    "ok":      (GREEN5,  "#1C4D35", "rgba(35,133,81,0.10)"),
    "warn":    (ORANGE5, "#63421A", "rgba(200,118,25,0.10)"),
    "bad":     (RED5,    "#5E2326", "rgba(205,66,70,0.12)"),
    "info":    (BLUE5,   "#1B3D6B", "rgba(45,114,210,0.10)"),
    "neutral": (GRAY5,   DARK_GRAY4, "rgba(255,255,255,0.02)"),
}

# One status vocabulary for the whole app. lib/data.py's STATUS_EMOJI is
# the text-only equivalent used inside st.dataframe cells (where HTML
# cannot render); this is the rendered-chip equivalent. They deliberately
# agree on which states exist.
STATUS_TONE: dict[str, str] = {
    "OK": "ok",
    "GOOD": "ok",
    "TRUSTWORTHY": "ok",
    "NORMAL": "info",
    "STALE": "warn",
    "WATCH": "warn",
    "ATTENTION": "warn",
    "DEGRADED": "warn",
    "UNAVAILABLE": "bad",
    "DISCONNECTED": "bad",
    "ACTION": "bad",
    "INCOMPLETE": "bad",
    "CRITICAL": "bad",
    "WARNING": "warn",
    "INFO": "info",
    "NO DATA": "neutral",
    "UNKNOWN": "neutral",
    # Evaluation and governance (dashboard/lib/agent_eval.py): gate results,
    # objective status and the readiness verdict.
    "PASS": "ok",
    "WARN": "warn",
    "FAIL": "bad",
    "PENDING": "info",
    "N/A": "neutral",
    "MET": "ok",
    "AT_RISK": "warn",
    "AT RISK": "warn",
    "MISSED": "bad",
    "NO_DATA": "neutral",
    "READY": "ok",
    "NOT READY": "bad",
    "INSUFFICIENT DATA": "neutral",
}

# Page URL slugs. Streamlit derives these from the filenames under
# dashboard/pages/ (4_Alert_Log.py -> /Alert_Log). Centralised here so a
# page rename breaks one dict instead of a dozen hand-written hrefs.
PAGES = {
    "overview": "/",
    "airspace": "Live_Airspace",
    "health": "Feed_Health",
    "critic": "AI_Critic_and_Review",
    "alerts": "Alert_Log",
    "ingest": "Ingest_and_Replay",
    "evaluation": "Agent_Evaluation",
}


def _esc(value: Any) -> str:
    """Escape anything bound for the HTML helpers below.

    Descriptions, airport codes and headlines all originate from the
    database or from LLM output. Neither is trusted to be free of markup,
    and every one of them reaches the browser through
    unsafe_allow_html=True, so escaping is not optional here.
    """
    return _html.escape("" if value is None else str(value), quote=True)


def drill_href(page: str, **params: Any) -> str:
    """
    Build a same-tab link into another page, carrying filter state.

    This is what makes a number on the Overview page a *drill-down*
    instead of a dead end: "205 open alerts" links to
    `Alert_Log?severity=CRITICAL`, and the Alert Log reads those params
    back out of st.query_params to arrive pre-filtered. Values that are
    None or empty are dropped so callers can pass optional filters
    unconditionally.
    """
    slug = PAGES.get(page, page)
    pairs = [
        f"{_html.escape(str(k))}={_html.escape(str(v))}"
        for k, v in params.items()
        if v is not None and v != ""
    ]
    return f"{slug}?{'&'.join(pairs)}" if pairs else slug


def sparkline(values: Sequence[float], tone: str = "info", width: int = 120, height: int = 28) -> str:
    """
    Inline SVG sparkline -- returns markup, draws nothing on its own.

    Inline SVG rather than a chart library because these sit *inside* KPI
    tiles: an st.altair_chart() cannot be nested in a markdown block, and
    a 120x28 trend line does not need axes, tooltips or a legend. It needs
    to answer "is this getting worse?" in peripheral vision.
    """
    pts = [float(v) for v in values if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = width / (len(pts) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - 3 - ((v - lo) / span) * (height - 6):.1f}"
        for i, v in enumerate(pts)
    )
    stroke = TONES.get(tone, TONES["info"])[0]
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{coords}" fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/></svg>'
    )


def status_chip(status: str, *, size: str = "md") -> str:
    """One status word, rendered as a bordered chip in its tone's colour."""
    key = (status or "UNKNOWN").upper()
    tone = STATUS_TONE.get(key, "neutral")
    fg, border, wash = TONES[tone]
    pad = "1px 7px" if size == "sm" else "3px 10px"
    fs = "13px" if size == "sm" else "14px"
    return (
        f'<span class="chip" style="color:{fg};border-color:{border};background:{wash};'
        f'padding:{pad};font-size:{fs};">{_esc(key)}</span>'
    )


def inject_theme() -> None:
    """
    Emit the global stylesheet. Called once per page run, from
    page_setup.bootstrap_page(), so no page can forget it.

    Streamlit re-renders the whole DOM on every rerun, so this has to be
    part of the page body rather than a one-time setup step -- guarding it
    behind a session_state flag would make the styling vanish on the
    second interaction.
    """
    st.markdown(
        f"""
        <style>
        /* ── Canvas ─────────────────────────────────────────────── */
        .stApp {{ background: {BLACK}; }}
        .block-container {{
            /* Clear Streamlit's fixed header, or it covers each page's title. */
            padding-top: 4.25rem; padding-bottom: 3rem;
            max-width: 1680px;
        }}
        section[data-testid="stSidebar"] {{
            background: {DARK_GRAY1};
            border-right: 1px solid {DARK_GRAY3};
        }}
        section[data-testid="stSidebar"] .stMarkdown h3 {{
            font-size: 15px; letter-spacing: 0.06em; text-transform: uppercase;
            color: {GRAY3}; font-weight: 700;
        }}

        /* ── Typography ─────────────────────────────────────────── */
        html, body, [class*="css"] {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                         "Helvetica Neue", Arial, sans-serif;
        }}
        h1 {{ font-size: 30px !important; font-weight: 700 !important;
             letter-spacing: -0.01em; color: {LIGHT_GRAY5}; }}
        h2 {{ font-size: 22px !important; font-weight: 650 !important; color: {LIGHT_GRAY5}; }}
        h3 {{ font-size: 18px !important; font-weight: 650 !important; color: {GRAY5}; }}
        .stCaption, [data-testid="stCaptionContainer"] p {{
            color: {GRAY1} !important; font-size: 14px !important; line-height: 1.5;
        }}
        hr {{ border-color: {DARK_GRAY3} !important; margin: 1.1rem 0 !important; }}

        /* ── Panels ─────────────────────────────────────────────── */
        div[data-testid="stVerticalBlockBorderWrapper"] {{
            background: {DARK_GRAY1};
            border: 1px solid {DARK_GRAY3} !important;
            border-radius: 4px;
        }}
        div[data-testid="stExpander"] details {{
            background: {DARK_GRAY1};
            border: 1px solid {DARK_GRAY3};
            border-radius: 4px;
        }}
        div[data-testid="stExpander"] summary {{ font-size: 15px; font-weight: 600; }}

        /* ── Section bands ──────────────────────────────────────── */
        .sec {{
            display: flex; align-items: baseline; gap: 6px 14px; flex-wrap: wrap;
            margin: 26px 0 12px 0; padding: 0 0 10px 0;
            border-bottom: 1px solid {DARK_GRAY3};
        }}
        .sec-bar {{ width: 4px; height: 22px; border-radius: 2px; flex: none;
                    position: relative; top: 4px; }}
        .sec-title {{ font-size: 20px; font-weight: 700; color: {LIGHT_GRAY5};
                      letter-spacing: 0.005em; }}
        .sec-q {{ font-size: 16px; color: {GRAY3}; line-height: 1.4; }}
        .sec-right {{ margin-left: auto; font-size: 14px; color: {GRAY3};
                      font-variant-numeric: tabular-nums; }}

        /* ── Tooltips ───────────────────────────────────────────────
           Instant, readable, and reachable by keyboard and touch; the browser's own title=""
           tooltip is delayed, tiny, and unavailable without a mouse. Put the text in data-tip. */
        .tip {{ position: relative; cursor: help; outline: none; }}
        .tip::after {{
            content: attr(data-tip);
            position: absolute; left: 0; top: calc(100% + 8px); z-index: 10000;
            width: max-content; max-width: min(440px, 80vw); box-sizing: border-box;
            padding: 10px 13px; border-radius: 5px;
            background: {DARK_GRAY4}; color: {LIGHT_GRAY5}; border: 1px solid {DARK_GRAY5};
            font-size: 14px; font-weight: 400; font-style: normal; letter-spacing: 0;
            text-transform: none; line-height: 1.5; white-space: normal; text-align: left;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
            opacity: 0; visibility: hidden; pointer-events: none;
            transition: opacity 90ms ease-out;
        }}
        .tip:hover::after, .tip:focus::after, .tip:focus-visible::after {{ opacity: 1; visibility: visible; }}
        .tip:focus-visible {{ outline: 2px solid {BLUE3}; outline-offset: 2px; border-radius: 3px; }}
        .tip--end::after {{ left: auto; right: 0; }}

        /* ── KPI strip ──────────────────────────────────────────── */
        .kpis {{ display: grid; gap: 10px; margin: 4px 0 6px 0; }}
        .kpi {{
            background: {DARK_GRAY1};
            border: 1px solid {DARK_GRAY3};
            border-left-width: 3px;
            border-radius: 4px;
            padding: 12px 14px 11px 13px;
            text-decoration: none !important;
            display: block; position: relative;
            transition: background 120ms ease-out, border-color 120ms ease-out;
        }}
        a.kpi:hover {{ background: {DARK_GRAY2}; border-color: {DARK_GRAY5}; }}
        a.kpi:hover .kpi-drill {{ opacity: 1; }}
        a.kpi .kpi-label {{ padding-right: 64px; }}   /* room for the "OPEN →" badge, even on a narrow tile */
        .kpi-label {{
            font-size: 13px; font-weight: 700; letter-spacing: 0.07em;
            text-transform: uppercase; color: {GRAY1};
            display: flex; align-items: center; gap: 6px;
        }}
        .kpi-value {{
            font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
            font-size: 30px; font-weight: 600; line-height: 1.15;
            margin-top: 6px; font-variant-numeric: tabular-nums;
            letter-spacing: -0.02em;
        }}
        .kpi-unit {{ font-size: 17px; font-weight: 500; opacity: 0.65; margin-left: 3px; }}
        .kpi-foot {{ font-size: 13px; color: {GRAY1}; margin-top: 5px; line-height: 1.4; }}
        .kpi-spark {{ position: absolute; right: 12px; bottom: 10px; opacity: 0.75; }}
        .kpi-drill {{
            position: absolute; right: 11px; top: 10px;
            font-size: 13px; font-weight: 700; letter-spacing: 0.05em;
            color: {BLUE5}; opacity: 0.45; transition: opacity 120ms ease-out;
        }}

        /* ── Chips & inline bits ────────────────────────────────── */
        .chip {{
            display: inline-block; border: 1px solid; border-radius: 3px;
            font-weight: 700; letter-spacing: 0.05em;
            font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
            white-space: nowrap;
        }}
        .mono {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
                 font-variant-numeric: tabular-nums; }}

        /* ── Matrix (airport x service) ─────────────────────────── */
        .mx {{ border-collapse: separate; border-spacing: 3px; width: 100%; }}
        .mx th {{
            font-size: 13px; font-weight: 700; letter-spacing: 0.07em;
            text-transform: uppercase; color: {GRAY1}; padding: 4px 8px;
            text-align: left; white-space: nowrap;
        }}
        .mx td {{
            padding: 9px 10px; border-radius: 3px; font-size: 14px;
            border: 1px solid {DARK_GRAY3}; background: {DARK_GRAY1};
            font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
            font-weight: 700; letter-spacing: 0.04em; text-align: center;
        }}
        .mx td.rowhead {{
            text-align: left; color: {GRAY5}; background: {DARK_GRAY2};
            letter-spacing: 0.06em;
        }}

        /* ── Posture banner ─────────────────────────────────────── */
        .posture {{
            display: flex; align-items: stretch; gap: 0;
            border: 1px solid {DARK_GRAY3}; border-radius: 4px;
            overflow: hidden; margin: 2px 0 14px 0;
        }}
        .posture-verdict {{
            padding: 16px 22px; min-width: 210px;
            display: flex; flex-direction: column; justify-content: center;
            border-right: 1px solid {DARK_GRAY3};
        }}
        .posture-word {{
            font-size: 30px; font-weight: 800; letter-spacing: 0.01em; line-height: 1.05;
        }}
        .posture-sub {{ font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase;
                        color: {GRAY1}; font-weight: 700; margin-top: 3px; }}
        .posture-body {{ padding: 15px 20px; flex: 1; background: {DARK_GRAY1}; }}
        .posture-head {{ font-size: 17px; font-weight: 600; color: {LIGHT_GRAY5};
                         line-height: 1.45; }}
        .posture-meta {{ font-size: 13px; color: {GRAY1}; margin-top: 7px;
                         display: flex; gap: 16px; flex-wrap: wrap; }}

        /* ── Evidence list ──────────────────────────────────────── */
        .ev {{ list-style: none; padding: 0; margin: 6px 0 0 0; }}
        .ev li {{
            font-size: 14px; color: {GRAY5}; line-height: 1.5;
            padding: 5px 0 5px 14px; border-bottom: 1px solid {DARK_GRAY2};
            position: relative;
        }}
        .ev li:last-child {{ border-bottom: none; }}
        .ev li::before {{
            content: ""; position: absolute; left: 2px; top: 11px;
            width: 4px; height: 4px; border-radius: 50%; background: {DARK_GRAY5};
        }}
        .ev-title {{
            font-size: 13px; font-weight: 700; letter-spacing: 0.07em;
            text-transform: uppercase; color: {GRAY1}; margin-top: 2px;
        }}

        /* ── Streamlit widget tuning ────────────────────────────── */
        div[data-testid="stDataFrame"] {{ border: 1px solid {DARK_GRAY3}; border-radius: 4px; }}
        .stButton button {{ border-radius: 3px; font-size: 15px; font-weight: 600; }}
        div[data-testid="stMetricValue"] {{
            font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
            font-size: 26px;
        }}
        [data-testid="stHeaderActionElements"] {{ display: none; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def help_hint(text: str, label: str = "How to read this") -> str:
    """An unobtrusive "i" whose tooltip holds the explanation, so the page shows data, not prose."""
    return (f'<span class="tip" tabindex="0" role="note" aria-label="{_esc(text)}" data-tip="{_esc(text)}" '
            f'style="font-size:14px;color:{GRAY3};font-style:normal;white-space:nowrap;margin-left:10px;">'
            f'&#9432; {_esc(label)}</span>')


def page_header(title: str, question: str, *, detail: str | None = None) -> None:
    """
    The identical opening block on every page: what this page is, and the
    one operational question it answers.

    Every page states its question because the dashboard's contract with
    the operator is question-shaped -- the mission panel on the Overview
    page lists seven of them, and a page that cannot name which one it
    answers has no reason to exist.
    """
    st.markdown(
        f'<div style="margin:0 0 4px 0;">'
        f'<span style="font-size:30px;font-weight:700;color:{LIGHT_GRAY5};'
        f'letter-spacing:-0.01em;">{_esc(title)}</span></div>'
        f'<div style="font-size:16px;color:{GRAY3};line-height:1.45;margin-bottom:16px;">'
        f'{_esc(question)}{help_hint(detail) if detail else ""}</div>',
        unsafe_allow_html=True,
    )


def section(title: str, question: str = "", *, tone: str = "info", right: str = "") -> None:
    """A labelled band separating one block of evidence from the next."""
    bar = TONES.get(tone, TONES["info"])[0]
    st.markdown(
        f'<div class="sec">'
        f'<div class="sec-bar" style="background:{bar};"></div>'
        f'<span class="sec-title">{_esc(title)}</span>'
        + (f'<span class="sec-q">{_esc(question)}</span>' if question else "")
        + (f'<span class="sec-right">{_esc(right)}</span>' if right else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def kpi(
    label: str,
    value: str,
    *,
    tone: str = "neutral",
    unit: str = "",
    foot: str = "",
    href: str | None = None,
    drill: str = "",
    spark: Sequence[float] | None = None,
) -> str:
    """
    Build one KPI tile. Returns markup; use kpi_row() to render a strip.

    `href` is what separates this from st.metric(): a number an operator
    cannot click is a number they have to go hunting for. Every tile that
    represents a countable set of records (alerts, failures, aircraft)
    should pass one.
    """
    fg, border, _wash = TONES.get(tone, TONES["neutral"])
    tag = "a" if href else "div"
    attrs = f' href="{href}" target="_self"' if href else ""
    drill_badge = (
        f'<span class="kpi-drill">{_esc(drill or "OPEN →")}</span>' if href else ""
    )
    spark_html = (
        f'<span class="kpi-spark">{sparkline(spark, tone)}</span>' if spark else ""
    )
    return (
        f'<{tag} class="kpi"{attrs} style="border-left-color:{border if tone == "neutral" else fg};">'
        f'{drill_badge}'
        f'<div class="kpi-label">{_esc(label)}</div>'
        f'<div class="kpi-value" style="color:{fg};">{_esc(value)}'
        + (f'<span class="kpi-unit">{_esc(unit)}</span>' if unit else "")
        + "</div>"
        + (f'<div class="kpi-foot">{_esc(foot)}</div>' if foot else "")
        + spark_html
        + f"</{tag}>"
    )


def event_chips(event_ids: Sequence[str], events: dict[str, dict], limit: int = 8) -> str:
    """
    Evidence event ids as chips; hovering one shows the SWIM event it names
    (service, airport, time, and what the message said), or that it has aged
    out of the event store.
    """
    chips = []
    for event_id in list(event_ids)[:limit]:
        e = events.get(event_id)
        if e is None:
            about = "No longer in the event store (retention)."
        else:
            when = e.get("event_time_utc")
            parts = [str(e.get("service_name") or "?"), str(e.get("airport_code") or "?"),
                     f"{when:%Y-%m-%d %H:%M:%S} UTC" if hasattr(when, "strftime") else str(when or "")]
            detail = {
                "runway": e.get("runway"), "RVR td/mid/ro": "/".join(
                    str(e[k]) for k in ("rvr_touchdown", "rvr_midpoint", "rvr_rollout") if e.get(k) is not None),
                "aircraft": e.get("aircraft_id"), "phase": e.get("flight_phase"), "alt ft": e.get("altitude_ft"),
                "surface": e.get("surface_event_type"), "feed": e.get("feed_status"),
            }
            about = " · ".join(parts + [f"{k} {v}" for k, v in detail.items() if v not in (None, "")])
        chips.append(f'<span class="chip mono tip" tabindex="0" role="note" aria-label="{_esc(about)}" '
                     f'data-tip="{_esc(about)}" style="font-size:13px;padding:2px 8px;">{_esc(event_id[:14])}</span>')
    more = f" +{len(event_ids) - limit}" if len(event_ids) > limit else ""
    return ('<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:14px;color:'
            f'{GRAY3};">Evidence (hover or focus a chip for the event): ' + "".join(chips) + _esc(more) + "</div>")


def kpi_row(tiles: Sequence[str], *, columns: int | None = None) -> None:
    """Render KPI tiles as one CSS grid so they share a baseline and gutter."""
    if not tiles:
        return
    cols = columns or len(tiles)
    st.markdown(
        f'<div class="kpis" style="grid-template-columns:repeat({cols}, minmax(0,1fr));">'
        + "".join(tiles)
        + "</div>",
        unsafe_allow_html=True,
    )


def posture_banner(
    verdict: str,
    headline: str,
    *,
    tone: str,
    subtitle: str = "",
    meta: Sequence[str] = (),
) -> None:
    """
    The single most important element on the Overview page: one word, at a
    size nothing else on the page competes with, plus the sentence that
    justifies it. Everything below it is evidence for this verdict.
    """
    fg, border, wash = TONES.get(tone, TONES["neutral"])
    meta_html = "".join(f"<span>{_esc(m)}</span>" for m in meta)
    st.markdown(
        f'<div class="posture" style="border-color:{border};">'
        f'<div class="posture-verdict" style="background:{wash};border-right-color:{border};">'
        f'<div class="posture-word" style="color:{fg};">{_esc(verdict)}</div>'
        f'<div class="posture-sub">{_esc(subtitle)}</div>'
        f"</div>"
        f'<div class="posture-body">'
        f'<div class="posture-head">{_esc(headline)}</div>'
        f'<div class="posture-meta">{meta_html}</div>'
        f"</div></div>",
        unsafe_allow_html=True,
    )


def evidence_list(title: str, items: Sequence[str], *, limit: int = 6) -> None:
    """A titled bullet list that does not look like markdown bullets."""
    shown = [i for i in items if i][:limit]
    if not shown:
        return
    st.markdown(
        f'<div class="ev-title">{_esc(title)}</div><ul class="ev">'
        + "".join(f"<li>{_esc(i)}</li>" for i in shown)
        + "</ul>",
        unsafe_allow_html=True,
    )


def status_matrix(
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    cell_status: dict[tuple[str, str], str],
    *,
    href_for: Any = None,
) -> None:
    """
    Airport x service health as a matrix rather than a list of rows.

    A 4x4 grid of coloured cells answers "is this isolated or sector-wide?"
    (mission question 6) in one glance; the same data as sixteen dataframe
    rows does not, because the reader has to hold the whole table in their
    head to see a pattern. `href_for(row, col)` may return a drill-down
    link for a cell.
    """
    head = "".join(f"<th>{_esc(c)}</th>" for c in col_labels)
    body = []
    for r in row_labels:
        cells = [f'<td class="rowhead">{_esc(r)}</td>']
        for c in col_labels:
            status = cell_status.get((r, c), "NO DATA")
            tone = STATUS_TONE.get(str(status).upper(), "neutral")
            fg, border, wash = TONES[tone]
            inner = _esc(status)
            if href_for:
                link = href_for(r, c)
                if link:
                    inner = (
                        f'<a href="{link}" target="_self" '
                        f'style="color:inherit;text-decoration:none;">{inner}</a>'
                    )
            cells.append(
                f'<td style="color:{fg};border-color:{border};background:{wash};">{inner}</td>'
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    st.markdown(
        f'<table class="mx"><thead><tr><th></th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>',
        unsafe_allow_html=True,
    )


def back_link(label: str = "Overview") -> None:
    """Consistent footer navigation on every sub-page."""
    st.markdown(
        f'<div style="margin-top:18px;padding-top:12px;border-top:1px solid {DARK_GRAY3};">'
        f'<a href="/" target="_self" style="color:{BLUE5};text-decoration:none;'
        f'font-size:14px;font-weight:600;">&larr; Back to {_esc(label)}</a></div>',
        unsafe_allow_html=True,
    )
