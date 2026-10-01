"""
The Dashboard's review breakout: each review awaiting a decision, one line
each, one click from its drawer on the Review Queue.

A count ("3 awaiting") says there is work; this says what the work is --
where, which rule, how many readings, what the AI suggests, how long it has
waited -- so the watch officer picks the one that matters without opening a
page to find out. The rows are src/review_queue.py's, the same ones the
Review Queue counts and lists. Reviews someone is monitoring are answered:
they are a count with a link here, not rows.
"""
from __future__ import annotations

import streamlit as st

from src import review_queue
from src.review_queue import OpenReview

from lib import theme
from lib.data import waited

MAX_SHOWN = 6
_SUGGESTS_TONE = {review_queue.ESCALATE: "bad", review_queue.NO_ESCALATION: "ok", review_queue.NO_AI_REVIEW: "neutral"}


def _row(review: OpenReview, first: bool) -> str:
    action = review.action
    tone = _SUGGESTS_TONE[review.suggests]
    return (
        f'<a href="{theme.drill_href("critic", review=action.action_id)}" target="_self" '
        f'style="display:flex;align-items:center;gap:14px;padding:9px 14px;'
        + ("" if first else f"border-top:1px solid {theme.DARK_GRAY3};")
        + f'text-decoration:none;color:{theme.LIGHT_GRAY5};font-size:15px;">'
        f'<span class="mono" style="min-width:52px;font-weight:700;">{theme._esc(action.airport_code or "PCT")}</span>'
        f'<span style="min-width:190px;">{theme._esc((action.alert_type or "—").removeprefix("ALERT_"))}</span>'
        f'<span class="mono" style="min-width:110px;color:{theme.GRAY3};font-size:14px;">'
        f"{action.occurrences:,} reading(s)</span>"
        f'<span style="color:{theme.TONES[tone][0]};font-size:14px;">'
        f"{theme._esc(review.ai_line)}</span>"
        + ("" if review.live else
           f'<span class="chip" style="color:{theme.ORANGE5};border-color:{theme.TONES["warn"][1]};'
           f'padding:1px 7px;font-size:13px;">NOT LIVE DATA</span>')
        + f'<span class="mono" style="margin-left:auto;color:{theme.GRAY1};font-size:14px;white-space:nowrap;">'
        f"waiting {theme._esc(waited(review.waiting_since))}</span>"
        f'<span style="color:{theme.BLUE5};font-size:14px;white-space:nowrap;">Decide &rarr;</span></a>'
    )


def render(reviews: list[OpenReview]) -> None:
    waiting, watched = review_queue.awaiting(reviews), review_queue.monitored(reviews)
    theme.section(
        "Review queue",
        "Each CRITICAL condition awaiting a decision.",
        tone="bad" if waiting else ("warn" if watched else "ok"),
        right=f"{len(waiting)} awaiting · {len(watched)} monitoring",
    )
    # Most urgent first: what the AI would escalate, then the longest wait.
    ordered = sorted(waiting, key=lambda r: (r.suggests != review_queue.ESCALATE, r.waiting_since))
    rows = "".join(_row(r, first=i == 0) for i, r in enumerate(ordered[:MAX_SHOWN]))
    if not waiting:
        rows = (f'<div style="padding:10px 14px;color:{theme.GRAY3};font-size:15px;">'
                "Nothing is awaiting a decision.</div>")
    foot = []
    if len(waiting) > MAX_SHOWN:
        foot.append(f'<a href="{theme.drill_href("critic", show="awaiting")}" target="_self" '
                    f'style="color:{theme.BLUE5};text-decoration:none;">All {len(waiting)} awaiting &rarr;</a>')
    if watched:
        foot.append(f'<a href="{theme.drill_href("critic", show="monitoring")}" target="_self" '
                    f'style="color:{theme.BLUE5};text-decoration:none;">{len(watched)} being monitored &rarr;</a>')
    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-radius:4px;background:{theme.DARK_GRAY1};'
        f'margin:0 0 6px 0;">{rows}'
        + (f'<div style="display:flex;gap:22px;padding:8px 14px;border-top:1px solid {theme.DARK_GRAY3};'
           f'font-size:14px;font-weight:600;">{"".join(foot)}</div>' if foot else "")
        + "</div>",
        unsafe_allow_html=True,
    )
