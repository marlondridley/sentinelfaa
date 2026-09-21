"""
The single consolidated ingest & breaking-news ticker shown at the top of
the Overview page.

This used to render inline at the top of the one-file dashboard and its
headline links used same-page "#anchor" fragments to jump to whichever
section answered that headline. Now that those sections live on separate
pages, a same-page fragment for a section that moved would silently do
nothing when clicked -- _ANCHOR_PAGE_MAP is what keeps those links working:
an anchor whose section stayed on the Overview page still gets a plain
in-page "#anchor" link, and an anchor whose section moved gets rewritten to
that page's URL instead.
"""
from __future__ import annotations

import streamlit as st

from src.async_bridge import run_async as run
from src.news_agent import build_ticker_items
from src.feed_health import compute_feed_health
from src import storage

from lib.data import reconstruct_nas_events

# Anchors whose section moved off the Overview page during the multipage
# split, mapped to the page that now owns them. "page-top" and
# "watch-officer" are intentionally absent -- both are still on Overview,
# so their original "#anchor" fragment link is left untouched below.
_ANCHOR_PAGE_MAP = {
    "airspace-ops": "Live_Airspace",
    "airport-health": "Feed_Health",
    "rvr-section": "Feed_Health",
    "latency-section": "Feed_Health",
    "critic-intel": "AI_Critic_and_Review",
    "hitl-queue": "AI_Critic_and_Review",
    "audit-trail": "AI_Critic_and_Review",
    "alert-log": "Alert_Log",
}


def _ticker_href(anchor: str) -> str:
    target_page = _ANCHOR_PAGE_MAP.get(anchor)
    return target_page if target_page else f"#{anchor}"


def render_ticker() -> None:
    """Fetch current status and render the auto-scrolling ticker banner."""
    events_raw = run(storage.get_recent_events(limit=1000))
    news_alerts_raw = run(storage.get_recent_alerts(limit=200))
    news_rvr_rows = run(storage.get_latest_rvr())
    news_phase = run(storage.get_flight_phase_counts(window_minutes=15))
    news_active = run(storage.get_active_aircraft(window_minutes=5, limit=300))
    news_lat = run(storage.get_latency_stats())

    # reconstruct_nas_events() rather than fetch_live_events() here: we
    # already have events_raw from the fetch above, so re-fetching via
    # fetch_live_events() would hit storage twice for the same query.
    live_events = reconstruct_nas_events(events_raw)

    news_live = live_events[:300]
    news_health = compute_feed_health(news_live) if news_live else {"airport_status": {}}

    ticker_items = run(build_ticker_items(
        airport_status=news_health.get("airport_status", {}),
        alerts=news_alerts_raw,
        rvr_rows=news_rvr_rows,
        phase_counts=news_phase,
        active_aircraft_total=len(news_active),
        avg_latency_ms=news_lat.get("avg_ms"),
    ))

    sev_class = {
        "GOOD":      "tk-green",
        "NORMAL":    "tk-grey",
        "ATTENTION": "tk-yellow",
        "ACTION":    "tk-red",
    }
    sev_glyph = {"GOOD": "●", "NORMAL": "●", "ATTENTION": "▲", "ACTION": "■"}
    ticker_html_items = "".join(
        f'<a class="tk-item {sev_class.get(it["severity"], "tk-grey")}" '
        f'href="{_ticker_href(it.get("anchor","page-top"))}" target="_self" '
        f'title="Click to jump to section">'
        f'<span class="tk-glyph">{sev_glyph.get(it["severity"], "●")}</span> '
        f'{it["text"]}'
        f'</a>'
        for it in ticker_items
    )
    ticker_payload = f"{ticker_html_items}{ticker_html_items}"
    scroll_seconds = max(90, len(ticker_items) * 9)

    st.session_state.setdefault("ticker_paused", False)
    ticker_pause_col, _ticker_spacer_col = st.columns([1, 11])
    with ticker_pause_col:
        if st.button(
            "▶ Resume ticker" if st.session_state["ticker_paused"] else "⏸ Pause ticker",
            key="ticker_pause_toggle",
            help="Pause or resume the auto-scrolling news ticker below.",
        ):
            st.session_state["ticker_paused"] = not st.session_state["ticker_paused"]
    ticker_wrap_class = "ticker-wrap tk-paused" if st.session_state["ticker_paused"] else "ticker-wrap"

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
            animation: ticker-scroll {scroll_seconds}s linear infinite;
        }}
        .ticker-wrap:hover .ticker,
        .ticker-wrap:active .ticker,
        .ticker-wrap:focus-within .ticker,
        .ticker-wrap.tk-paused .ticker {{
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
        [id^="anchor-"], #airport-health, #airspace-ops, #alert-log,
        #rvr-section, #critic-intel, #latency-section {{
            scroll-margin-top: 80px;
        }}
        </style>
        <div class="{ticker_wrap_class}" role="region" aria-label="PCT live status ticker" aria-live="off" title="Hover, tap, or use the Pause ticker button above to pause · click any headline to jump to its section">
            <span class="ticker-label">PCT&nbsp;LIVE</span>
            <div class="ticker">{ticker_payload}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
