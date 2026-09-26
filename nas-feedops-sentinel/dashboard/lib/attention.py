"""
The Overview's attention strip: what needs the watch officer now, most
urgent first, each item one click from its subject (src/attention.py).

A still list, not a scrolling ticker: a moving target is hard to read and
harder to click, and every item opens its page already on what it names --
the review, the airport's RVR, the airport's feed status, the alerts.
"""
from __future__ import annotations

import streamlit as st

from src import storage
from src.async_bridge import run_async as run
from src.attention import ACTION, ATTENTION, GOOD, build_attention_items

from lib import theme

_TONE = {ACTION: "bad", ATTENTION: "warn", GOOD: "ok"}
_GLYPH = {ACTION: "■", ATTENTION: "▲", GOOD: "●"}
MAX_SHOWN = 5


def render_attention(airport_status: dict[str, str], alerts: list[dict]) -> None:
    items = run(build_attention_items(airport_status=airport_status, alerts=alerts,
                                      rvr_rows=run(storage.get_latest_rvr())))
    lead = theme.TONES[_TONE[items[0]["severity"]]][0]
    rows = "".join(
        f'<a href="{theme.drill_href(i["page"], **i["params"])}" target="_self" '
        f'style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-top:1px solid '
        f'{theme.DARK_GRAY3};text-decoration:none;color:{theme.LIGHT_GRAY5};font-size:13.5px;">'
        f'<span style="color:{theme.TONES[_TONE[i["severity"]]][0]};font-size:11px;">{_GLYPH[i["severity"]]}</span>'
        f'<span>{theme._esc(i["text"])}</span>'
        f'<span style="margin-left:auto;color:{theme.BLUE5};font-size:12px;white-space:nowrap;">Open &rarr;</span></a>'
        for i in items[:MAX_SHOWN]
    )
    more = len(items) - MAX_SHOWN
    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {lead};border-radius:4px;'
        f'background:{theme.DARK_GRAY1};margin:0 0 16px 0;">'
        f'<div style="padding:8px 14px;font-size:10.5px;font-weight:700;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:{theme.GRAY1};">'
        + ("Needs attention" if items[0]["severity"] != GOOD else "Status")
        + (f" &middot; {more} more below the top {MAX_SHOWN}" if more > 0 else "")
        + f"</div>{rows}</div>",
        unsafe_allow_html=True,
    )
