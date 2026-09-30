"""
NAS FeedOps Sentinel -- Common Operating Picture (Overview).

This is the landing page of a multipage Streamlit app. It answers the
top-level question -- "does PCT TRACON have a trustworthy aircraft
movement picture right now, and what should the duty manager do about
it?" -- and it answers it in a deliberate order:

  1. A deterministic verdict. GREEN/AMBER/RED is computed from measured
     facts about the last hour (schema conformance, ingest lag, reviews of
     CRITICAL alerts still awaiting a decision) and from whether each
     airport's feed is current, by rules in `_verdict()` below. No model is
     consulted. This is the line an operator is entitled to act on.
  2. The numbers that produced it, as a ranked KPI strip. Every tile is
     a link into the page that explains it: the alert count opens a
     pre-filtered Alert Log, the lag figure opens the latency view. A
     number an operator cannot click is a number they have to go hunting
     for, and the previous version of this page had six of them.
  3. Per-airport status, so "is this isolated or sector-wide?" is
     answered before anyone has to read a table.
  4. One "AI investigation" card: five indicators for the latest
     investigation (evidence quality, confidence, time, cost, human
     review), measured from the record. Detail is one click deeper.
  5. The Watch Officer brief -- the narrative layer, which may be
     LLM-assisted and is labelled as such, kept visually subordinate to
     the deterministic verdict above it so the two are never confused.
  6. The AI Planner, for questions the fixed panels do not answer.

Everything else (the live airspace map, feed/service health, the AI
Critic and human review queue, the alert log, and ingest/replay tooling)
lives on its own page under dashboard/pages/ -- see the docstring in
dashboard/lib/__init__.py for why this was split out of what used to be a
single ~1500-line script.

Run with:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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

from lib import agent_eval, brief_view, theme
from lib.data import (
    AIRPORTS,
    bucket_counts,
    format_ts,
    reconstruct_nas_events,
)
from lib.page_setup import bootstrap_page
from lib.attention import render_attention
from src import storage
from src.async_bridge import run_async as run
from src.auth import Decision, can_decide
from src.config import settings
from src.evaluation import load_gate_config
from src.feed_health import compute_feed_health
from src.planner_agent import PlannerAgent
from src.rate_limit import model_action_cooldown
from src.storage import save_planner_result
from src.watch_officer_agent import build_watch_officer_context, generate_watch_officer_brief
from src.watch_officer_freshness import current_view

_principal = bootstrap_page(page_title="PCT TAIS FeedOps Sentinel", page_icon="✈")

# Refresh brief, Deterministic only and Ask run paid model calls and write to the database, so they are for the
# people who operate the console: a signed-in watch_officer or supervisor. An auditor reads, and a visitor with
# no verified identity changes nothing. Same rule as the Ingest page.
_MAY_OPERATE = can_decide(_principal, Decision.MONITOR, allow_unverified=settings.ALLOW_UNVERIFIED_DECISIONS)
_OPERATORS_ONLY = "Requires the watch_officer or supervisor role and a verified sign-in."


def _may_start(kind: str) -> bool:
    """
    Whether this person's click starts a model-backed action now: an operator, not inside the cooldown.
    The buttons are disabled for everyone else, but the disabled state is only the browser's: this is the check.
    """
    if not _MAY_OPERATE:
        return False
    wait = model_action_cooldown().try_start(f"{_principal.principal_id}|{kind}")
    if wait > 0:
        st.warning(f"That was started a moment ago. Wait {wait:.0f} s before starting it again.")
        return False
    return True

# ── Data ──────────────────────────────────────────────────────────────────────
# The verdict is about the feed now, so its aggregates cover the last hour: a
# lifetime figure never notices the feed has gone quiet, and an old bad day
# would hold it AMBER for good. And it is about the LIVE feed: a replay lands in
# the same tables with a fresh receipt time, so counting it would let a dead feed
# read GREEN off replayed captures, or a replayed fault read RED over a healthy one.
# The provenance banner says when non-live data is around; the verdict ignores it.
WINDOW = timedelta(minutes=60)
_since = datetime.now(timezone.utc) - WINDOW
val_summary = run(storage.get_validation_summary(_since, data_origin="live"))
lat_stats = run(storage.get_latency_stats(_since, data_origin="live"))
alerts_raw = run(storage.get_recent_alerts(limit=500, data_origin="live"))
events_raw = run(storage.get_recent_events(limit=1000, data_origin="live"))
not_live_counts = {k: v for k, v in run(storage.get_origin_counts(window_minutes=60)).items() if k != "live" and v}
active_aircraft = run(storage.get_active_aircraft(window_minutes=5, limit=500))
phase_counts = run(storage.get_flight_phase_counts(window_minutes=15))

live_events = reconstruct_nas_events(events_raw)
health = compute_feed_health(live_events[:500]) if live_events else {"airport_status": {}}

render_attention(health.get("airport_status", {}), alerts_raw)

total_validated = val_summary.get("total", 0) or 0
passed = val_summary.get("passed", 0) or 0
failed = val_summary.get("failed", 0) or 0
pass_rate = (passed / total_validated * 100) if total_validated else None
avg_lat_ms = lat_stats.get("avg_ms")

# A CRITICAL alert is open while the review that covers it awaits a decision.
critical_alerts = len(run(storage.get_pending_staged_actions(data_origin="live")))
warning_alerts = sum(1 for a in alerts_raw if a.get("severity") == "WARNING")
total_alerts = len(alerts_raw)


# ── Deterministic verdict ─────────────────────────────────────────────────────
def _verdict() -> tuple[str, str, str, str]:
    """
    Compute the operational verdict from measured facts alone.

    Returns (word, tone, subtitle, justification). The thresholds are the
    same ones the KPI tiles are coloured by, so the banner can never
    disagree with the numbers directly beneath it -- a discrepancy there
    would be the fastest possible way to lose an operator's trust in the
    page. Nothing here consults a model: this is the claim the system is
    prepared to defend.
    """
    if total_validated == 0:
        elsewhere = ", ".join(f"{n:,} {origin}" for origin, n in sorted(not_live_counts.items()))
        return (
            "NO DATA",
            "neutral",
            "no live messages in the last hour",
            "No live SWIM messages were ingested in the last hour, so no claim can be made about the "
            "aircraft movement picture."
            + (f" {elsewhere} event(s) from other sources arrived in that time and are not counted here."
               if elsewhere else "")
            + " Start the watcher; a replay does not change this verdict.",
        )
    # The same feed statuses the attention strip acts on: a feed that is down is an
    # action, one that is stale or degraded is attention.
    airport_status = health.get("airport_status", {})
    feed_down = sorted(a for a, s in airport_status.items() if s in ("UNAVAILABLE", "DISCONNECTED"))
    feed_quiet = sorted(a for a, s in airport_status.items() if s in ("STALE", "DEGRADED"))
    if critical_alerts > 0 or feed_down or (pass_rate is not None and pass_rate < 70):
        return (
            "RED",
            "bad",
            "do not rely on feed",
            f"{critical_alerts} CRITICAL review(s) awaiting a decision, "
            + (f"feed down at {', '.join(feed_down)}, " if feed_down else "")
            + f"schema conformance at {pass_rate:.0f}% over the last hour. Validate against a "
            "secondary source before using feed data for operational decisions.",
        )
    if feed_quiet or (pass_rate is not None and pass_rate < 95) or (avg_lat_ms or 0) >= 5_000:
        return (
            "AMBER",
            "warn",
            "degraded but usable",
            "The feed is delivering, but "
            + (f"the {', '.join(feed_quiet)} feed is stale or degraded, or " if feed_quiet else "either ")
            + "conformance is below the 95% target or "
            "messages are arriving too late to be treated as real time. Usable for "
            "trend and situational awareness; corroborate before acting on a single "
            "aircraft.",
        )
    return (
        "GREEN",
        "ok",
        "feed trustworthy",
        "Every airport feed is current, conformance is at target, ingest lag is inside the "
        "5-second real-time budget, and no CRITICAL review awaits a decision. Safe to rely "
        "on the feed for live operational decisions.",
    )


verdict_word, verdict_tone, verdict_sub, verdict_text = _verdict()

st.markdown(
    f'<div style="display:flex;align-items:baseline;gap:8px 16px;flex-wrap:wrap;margin-bottom:14px;">'
    f'<span style="font-size:30px;font-weight:700;color:{theme.LIGHT_GRAY5};'
    f'letter-spacing:-0.01em;">PCT TAIS FeedOps Sentinel</span>'
    f'<span style="font-size:16px;color:{theme.GRAY3};">'
    f"Can PCT TRACON trust the current aircraft movement picture across "
    f"<b>{' &middot; '.join(AIRPORTS)}</b>?</span></div>",
    unsafe_allow_html=True,
)

theme.posture_banner(
    verdict_word,
    verdict_text,
    tone=verdict_tone,
    subtitle=verdict_sub,
    meta=[
        f"{total_validated:,} messages evaluated in the last hour",
        f"{len(active_aircraft)} aircraft in the last 5 min",
        f"{critical_alerts} critical review(s) / {total_alerts} recent alerts",
        "Deterministic rules — no model involved",
    ],
)

# ── Ranked KPI strip ──────────────────────────────────────────────────────────
theme.section(
    "Operational readiness",
    "Every figure opens the page that explains it.",
    tone=verdict_tone,
)

alerts_per_hour = bucket_counts(alerts_raw, "triggered_at", buckets=12)
crit_per_hour = bucket_counts(
    alerts_raw, "triggered_at", buckets=12,
    predicate=lambda r: r.get("severity") == "CRITICAL",
)
events_per_hour = bucket_counts(events_raw, "received_time_utc", buckets=12)

if avg_lat_ms is None:
    lag_display, lag_unit = "—", ""
elif avg_lat_ms < 1_000:
    lag_display, lag_unit = f"{avg_lat_ms:,.0f}", "ms"
elif avg_lat_ms < 60_000:
    lag_display, lag_unit = f"{avg_lat_ms / 1_000:.1f}", "s"
else:
    lag_display, lag_unit = f"{avg_lat_ms / 60_000:,.0f}", "min"

lag_tone = (
    "neutral" if avg_lat_ms is None
    else "ok" if avg_lat_ms < 5_000
    else "warn" if avg_lat_ms < 300_000
    else "bad"
)
conf_tone = (
    "neutral" if pass_rate is None
    else "ok" if pass_rate >= 95
    else "warn" if pass_rate >= 70
    else "bad"
)

theme.kpi_row(
    [
        theme.kpi(
            "Open alerts",
            f"{total_alerts:,}",
            tone="bad" if critical_alerts else ("warn" if total_alerts > 10 else "ok"),
            foot=f"{sum(1 for a in alerts_raw if a.get('severity') == 'CRITICAL')} critical · {warning_alerts} warning",
            href=theme.drill_href("alerts"),
            drill="TRIAGE →",
            spark=alerts_per_hour,
        ),
        theme.kpi(
            "Critical now",
            f"{critical_alerts:,}",
            tone="bad" if critical_alerts else "ok",
            foot="Reviews awaiting a decision" if critical_alerts else "None open",
            href=theme.drill_href("critic"),
            drill="OPEN →",
            spark=crit_per_hour,
        ),
        theme.kpi(
            "Schema conformance",
            f"{pass_rate:.1f}" if pass_rate is not None else "—",
            unit="%" if pass_rate is not None else "",
            tone=conf_tone,
            foot=f"{failed:,} failed of {total_validated:,} in the last hour · target ≥ 95%",
            href=theme.drill_href("health", focus="validation"),
            drill="BREAKDOWN →",
        ),
        theme.kpi(
            "Event → ingest lag",
            lag_display,
            unit=lag_unit,
            tone=lag_tone,
            foot="Target < 5 s for live decisions",
            href=theme.drill_href("health", focus="latency"),
            drill="BY SERVICE →",
        ),
        theme.kpi(
            "Aircraft tracked",
            f"{len(active_aircraft):,}",
            tone="ok" if active_aircraft else "neutral",
            foot="Distinct aircraft, last 5 min",
            href=theme.drill_href("airspace"),
            drill="MAP →",
        ),
        theme.kpi(
            "Messages ingested",
            f"{total_validated:,}",
            tone="info" if total_validated else "neutral",
            foot="SWIM events in the last hour",
            href=theme.drill_href("health", focus="volume"),
            drill="VOLUME →",
            spark=events_per_hour,
        ),
    ],
    columns=6,
)

# ── Per-airport status ────────────────────────────────────────────────────────
theme.section(
    "Facility status",
    "Is this isolated to one airport, or sector-wide?",
    right="15-minute movement window",
)

_cards = []
for _code, _info in AIRPORTS.items():
    _status = health.get("airport_status", {}).get(_code, "NO DATA")
    _tone = theme.STATUS_TONE.get(str(_status).upper(), "neutral")
    _fg, _border, _wash = theme.TONES[_tone]
    _pc = phase_counts.get(_code, {})
    _movements = (
        f"{_pc.get('DEPARTURE', 0)} dep · {_pc.get('ARRIVAL', 0)} arr · "
        f"{_pc.get('ENROUTE', 0)} enr · {_pc.get('GROUND', 0)} gnd"
    )
    _cards.append(
        '<a class="kpi" href="' + theme.drill_href("health", airport=_code) + '" target="_self" '
        'style="border-left-color:' + _fg + ';">'
        '<div class="kpi-label">' + theme._esc(_code) + "</div>"
        '<div style="font-size:14px;color:' + theme.GRAY5 + ';margin-top:3px;">'
        + theme._esc(_info["name"]) + "</div>"
        '<div style="margin-top:9px;">' + theme.status_chip(str(_status), size="sm") + "</div>"
        '<div class="kpi-foot mono">' + theme._esc(_movements) + "</div>"
        "</a>"
    )
theme.kpi_row(_cards, columns=4)

# ── AI investigation (Layer 1 of the agent evaluation) ────────────────────────
# Five indicators for the latest investigation, no more: the operator's
# picture stays mission-first. Detail is one click deeper (Review page), and
# the full evaluation is on Agent Evaluation. Every number is measured from
# the record by src/evaluation.py, never reported by an agent about itself.
theme.section(
    "AI investigation",
    "What did the agents add to the latest CRITICAL alert, and was it sound?",
    tone="info",
    right="advisory only · a person decides",
)
_gates = load_gate_config()
agent_eval.render_card(
    agent_eval.load_latest(),
    sla_s=_gates.budget.max_duration_seconds,
    budget_usd=_gates.budget.max_cost_usd,
)

# ── Watch Officer brief ───────────────────────────────────────────────────────
theme.section(
    "Watch Officer brief",
    "Can we trust our own data right now, and what should someone do next?",
    tone="info",
)

_wo_latest = run(storage.get_latest_watch_officer_brief())
# The first brief is generated by an operator's visit, not anyone's: it spends model calls and writes a row.
if _wo_latest is None and _may_start("brief"):
    _wo_ctx = run(build_watch_officer_context())
    _wo_latest = generate_watch_officer_brief(_wo_ctx, use_llm=True)
    run(storage.save_watch_officer_brief(_wo_latest))

_wo_b1, _wo_b2, _wo_b3 = st.columns([1, 1.2, 5])
_gate = {"disabled": not _MAY_OPERATE, **({} if _MAY_OPERATE else {"help": _OPERATORS_ONLY})}
if _wo_b1.button("Refresh brief", use_container_width=True, **_gate) and _may_start("brief"):
    _new_ctx = run(build_watch_officer_context())
    _wo_latest = generate_watch_officer_brief(_new_ctx, use_llm=True)
    run(storage.save_watch_officer_brief(_wo_latest))
    st.rerun()
if _wo_b2.button(
    "Deterministic only",
    use_container_width=True,
    disabled=not _MAY_OPERATE,
    help="Force the rule-based fallback brief, with no LLM involvement." if _MAY_OPERATE else _OPERATORS_ONLY,
) and _may_start("brief"):
    _new_ctx = run(build_watch_officer_context())
    _wo_latest = generate_watch_officer_brief(_new_ctx, use_llm=False)
    run(storage.save_watch_officer_brief(_wo_latest))
    st.rerun()

# The brief has one fixed shape (lib/brief_view.py): status, bottom line, at most three owned actions,
# what to watch, and the detail behind one expander. The provenance is inside it, not decoration.
with st.container(border=True):
    if _wo_latest is None:
        st.info("No brief has been generated yet. A watch officer or supervisor generates the first one.")
    else:
        # The status shown is today's, not the one stored when the brief was written (src/watch_officer_freshness.py).
        _wo_shown, _wo_notice = current_view(_wo_latest, run(build_watch_officer_context()))
        if _wo_notice:
            st.warning(_wo_notice)
        brief_view.render(_wo_shown)

# ── AI Planner ────────────────────────────────────────────────────────────────
theme.section("Ask the planner", "Questions the fixed panels above do not answer.", tone="info")

with st.expander("AI Planner — plain-English questions over live SWIM data", expanded=False):
    st.caption(
        "Answered in the voice of a senior PCT TRACON duty manager. Examples: "
        "\"What's happening at KIAD right now?\" · \"Is the IAD arrival flow normal?\" · "
        "\"Are any runways below RVR minimums?\" Requires an LLM provider in .env; "
        "without one the planner answers deterministically and says so."
    )
    _q_col, _btn_col = st.columns([6, 1])
    with _q_col:
        planner_question = st.text_input(
            "Planner query",
            placeholder="What is the current status of PCT TRACON and what should the duty manager do?",
            label_visibility="collapsed",
            key="planner_input",
            # The question goes into the prompt twice; an unbounded one is an unbounded bill.
            max_chars=settings.PLANNER_QUESTION_MAX_CHARS,
            disabled=not _MAY_OPERATE,
        )
    with _btn_col:
        planner_run = st.button("Ask", type="primary", use_container_width=True, **_gate)

    if planner_run and planner_question.strip() and _may_start("planner"):
        with st.spinner("Analyzing NAS status ..."):
            agent = PlannerAgent()
            pr = run(agent.query(planner_question.strip()))
            run(save_planner_result(pr))

        theme.kpi_row(
            [
                theme.kpi(
                    "Planner confidence",
                    f"{pr.confidence:.0%}" if pr.confidence is not None else "N/A",
                    tone="info" if pr.confidence is not None else "neutral",
                    foot=(
                        "Model-reported" if pr.confidence is not None
                        else "Deterministic answer — no confidence claimed"
                    ),
                ),
                theme.kpi(
                    "Actions recommended",
                    f"{len(pr.recommended_actions)}",
                    tone="warn" if pr.recommended_actions else "neutral",
                    foot="Advisory only — not staged for execution",
                ),
            ],
            columns=2,
        )
        st.markdown(f"**{pr.answer}**")

        if pr.recommended_actions:
            theme.evidence_list("Recommended actions", pr.recommended_actions, limit=10)
        if pr.sub_queries:
            with st.expander("Sub-queries analyzed"):
                theme.evidence_list("Sub-queries", pr.sub_queries, limit=20)
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
                _ts = format_ts(brief.get("evaluated_at"))
                _conf = brief.get("confidence")
                _conf_str = f"{_conf:.0%}" if _conf is not None else "N/A"
                st.markdown(
                    f'<div class="mono" style="font-size:13px;color:{theme.GRAY1};">'
                    f"{theme._esc(_ts)} UTC · conf={_conf_str}</div>"
                    f'<div style="font-size:14px;color:{theme.GRAY3};font-style:italic;">'
                    f'{theme._esc(brief.get("question", ""))}</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(brief.get("answer", ""))
                st.divider()

# ── Mission contract ──────────────────────────────────────────────────────────
theme.section(
    "Mission questions answered",
    "The contract between this dashboard and the decisions it supports.",
    tone="neutral",
)

_MISSION_ROWS = [
    ("Is the PCT TAIS feed alive right now?",
     "Message rate, last message time, files processed",
     "Confirms the SWIM feed is connected and usable",
     theme.drill_href("health")),
    ("Can we trust the current aircraft picture?",
     "Live positions, stale status, no-data status",
     "Determines whether the airspace view is operationally reliable",
     theme.drill_href("airspace")),
    ("Which airport is driving the traffic load?",
     f"{', '.join(AIRPORTS)} departure / arrival / en route counts",
     "Helps prioritize airport-level monitoring",
     theme.drill_href("airspace")),
    ("What phase of flight is most active?",
     "Departures, arrivals, en route, ground counts",
     "Shows demand by operational phase",
     theme.drill_href("airspace")),
    ("Are tracks stale, missing, or abnormal?",
     "Stale flags, altitude distribution, speed distribution",
     "Detects data-quality or movement anomalies",
     theme.drill_href("health", focus="validation")),
    ("Is this isolated or sector-wide?",
     "Per-airport comparison across all PCT airports",
     "Supports escalation and root-cause triage",
     theme.drill_href("health")),
    ("What should the operator do next?",
     "AI Planner + AI Critic recommendation, gated by human approval",
     "Turns monitoring into action",
     theme.drill_href("critic")),
]

_rows_html = "".join(
    '<tr>'
    '<td class="mq-n">' + str(i) + "</td>"
    '<td class="mq-q">' + theme._esc(q) + "</td>"
    '<td><a href="' + href + '" target="_self">' + theme._esc(ev) + "</a></td>"
    '<td class="mq-d">' + theme._esc(dec) + "</td>"
    "</tr>"
    for i, (q, ev, dec, href) in enumerate(_MISSION_ROWS, 1)
)

st.markdown(
    f"""
    <style>
    .mq {{ width:100%; border-collapse:collapse; font-size:14px; }}
    .mq th {{
        text-align:left; padding:6px 10px; font-size:13px; font-weight:700;
        letter-spacing:0.07em; text-transform:uppercase; color:{theme.GRAY1};
        border-bottom:1px solid {theme.DARK_GRAY3};
    }}
    .mq td {{
        padding:8px 10px; border-bottom:1px solid {theme.DARK_GRAY2};
        color:{theme.GRAY5}; vertical-align:top; line-height:1.45;
    }}
    .mq tr:hover td {{ background:{theme.DARK_GRAY1}; }}
    .mq .mq-n {{ color:{theme.GRAY1}; width:22px; font-weight:700;
                 font-family:ui-monospace,Menlo,Consolas,monospace; }}
    .mq .mq-q {{ color:{theme.LIGHT_GRAY5}; font-weight:600; width:27%; }}
    .mq .mq-d {{ color:{theme.GRAY1}; font-style:italic; width:30%; }}
    .mq a {{ color:{theme.BLUE5}; text-decoration:none; font-weight:600; }}
    .mq a:hover {{ text-decoration:underline; }}
    </style>
    <table class="mq">
      <thead><tr><th></th><th>Mission question</th><th>Evidence (click to open)</th>
      <th>Decision supported</th></tr></thead>
      <tbody>{_rows_html}</tbody>
    </table>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    f'<div style="margin-top:20px;padding-top:12px;border-top:1px solid {theme.DARK_GRAY3};'
    f'font-size:13px;color:{theme.GRAY1};line-height:1.6;">'
    "NAS FeedOps Sentinel &middot; FAA SWIM/STDDS PCT automation demonstration.<br/>"
    "The GREEN/AMBER/RED verdict above is computed from measured conformance, lag and "
    "alert counts by fixed rules. Narrative sections are labelled with their provenance."
    "</div>",
    unsafe_allow_html=True,
)
