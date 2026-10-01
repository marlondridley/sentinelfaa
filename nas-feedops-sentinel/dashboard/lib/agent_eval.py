"""
Agent evaluation on the dashboard, in three layers of progressive disclosure:
mission first, AI second, telemetry on demand.

  Layer 1  Dashboard          one "AI investigation" card, five indicators,
                              for the most recent investigation
  Layer 2  Review page        the investigation's evaluation and its
                              governance gates, next to the record itself
  Layer 3  Agent Evaluation   the whole evaluation for engineers, TPMs and
                              governance (pages/6_Agent_Evaluation.py)

Every number comes from src/evaluation.py, which measures from the durable
record -- never from an agent's account of itself -- and computes with the
Agent Governance Toolkit wherever AGT computes correctly. What cannot be
measured is shown as "no data", never as a placeholder.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from lib import safe_text, theme
from src.async_bridge import run_async as run

NO_DATA = "no data"
GATE_TONE = {"PASS": "ok", "WARN": "warn", "FAIL": "bad", "PENDING": "info", "N/A": "neutral"}
GATE_ICON = {"PASS": "🟢", "WARN": "🟡", "FAIL": "🔴", "PENDING": "🔵", "N/A": "⚪"}


def pct(value: float | None, digits: int = 0) -> str:
    return NO_DATA if value is None else f"{value:.{digits}%}"


def seconds(value: float | None) -> str:
    if value is None:
        return NO_DATA
    return f"{value:.1f} s" if value < 120 else f"{value / 60:.1f} min"


def usd(value: float | None) -> str:
    if value is None:
        return NO_DATA
    return f"${value:.4f}" if value < 0.01 else f"${value:.2f}"


# ── Who calculated a number, in words ────────────────────────────────────────
# The evaluation tags every value with its kind (src/agt_metrics.py CATEGORIES). The tags are precise and mean
# nothing to someone reading the page, so the page says what each one means instead.
_CALCULATED_BY = (
    ("AGT-native", "Agent Governance Toolkit"),
    ("AGT-compatible", "Sentinel's calculation of a toolkit metric"),
    ("Sentinel-specific", "Sentinel's own check"),
)
CALCULATED_BY_HELP = (
    "Who computed the number, not the data it was computed from. "
    "Agent Governance Toolkit: Microsoft's open-source toolkit for governing AI agents computes it. "
    "Sentinel's calculation of a toolkit metric: the toolkit defines the metric, but the installed version "
    "computes it wrongly (a test shows each defect), so Sentinel computes it until that is fixed. "
    "Sentinel's own check: the toolkit has no such metric; it is specific to this mission."
)


def calculated_by(source: str) -> str:
    """'AGT-native: agent-sre ResponseLatency' as 'Agent Governance Toolkit (agent-sre ResponseLatency)'."""
    text = str(source or "").strip()
    for tag, words in _CALCULATED_BY:
        if text.startswith(tag):
            rest = (text[len(tag):].lstrip(" :").replace("ECE", "expected calibration error")
                    .replace("AGT bug", "the toolkit's own has a known defect: bug"))
            rest = rest[1:-1] if rest.startswith("(") and rest.endswith(")") else rest
            return f"{words} ({rest})" if rest else words
    return text


def overall(ev: Any) -> str:
    """The card's one status: red on any failed gate, amber on any warning, green otherwise."""
    statuses = set(ev.gate_summary.values())
    return "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"


# ── Loading (never let an evaluation problem take the page down) ─────────────

def load_latest():
    from src.evaluation import evaluate_latest

    try:
        return run(evaluate_latest())
    except Exception as exc:  # noqa: BLE001
        st.caption(safe_text.failure("The AI investigation evaluation is unavailable", exc))
        return None


def load_for_action(action_id: str):
    from src.evaluation import evaluate_action

    try:
        return run(evaluate_action(action_id))
    except Exception as exc:  # noqa: BLE001
        st.caption(safe_text.failure("The evaluation is unavailable", exc))
        return None


# ── Layer 1: the Dashboard card ───────────────────────────────────────────────

def render_card(ev: Any, *, sla_s: float | None, budget_usd: float | None) -> None:
    """Five indicators for the latest investigation, each one click from its detail."""
    if ev is None:
        st.caption("No investigation recorded yet. CRITICAL alerts are investigated when they are staged.")
        return
    status = overall(ev)
    review = theme.drill_href("critic", review=ev.action_id)
    evaluation = theme.drill_href("evaluation")
    grounded = ev.grounding
    quality_tone = ("neutral" if grounded is None else "ok" if grounded >= 0.95
                    else "warn" if grounded >= 0.8 else "bad")
    time_tone = ("neutral" if ev.duration_s is None or sla_s is None
                 else "ok" if ev.duration_s <= sla_s else "bad")
    cost_tone = ("neutral" if ev.cost_usd is None or budget_usd is None
                 else "ok" if ev.cost_usd <= budget_usd else "bad")
    decided = ev.action_status in ("APPROVED", "REJECTED", "OVERRIDDEN")
    st.markdown(
        '<div style="display:flex;gap:10px;align-items:baseline;margin:2px 0 6px 0;">'
        f'<span style="font-size:15px;color:{theme.LIGHT_GRAY5};font-weight:600;">'
        f'{GATE_ICON[status]} AI investigation</span>'
        + theme.status_chip(status, size="sm")
        + f'<span style="font-size:14px;color:{theme.GRAY3};">'
        + theme._esc(f"{ev.alert_type or 'alert'} at {ev.airport or 'PCT'} · "
                     f"{ev.opened_at:%Y-%m-%d %H:%M} UTC · advisory only")
        + f'</span><a href="{theme._attr_href(evaluation)}" target="_self" style="margin-left:auto;font-size:14px;'
        f'color:{theme.BLUE5};text-decoration:none;font-weight:600;">Agent evaluation →</a></div>',
        unsafe_allow_html=True,
    )
    theme.kpi_row([
        theme.kpi("Evidence quality", pct(grounded) if grounded is not None else "rules only",
                  tone=quality_tone, href=review, drill="EVIDENCE →",
                  foot=(f"{ev.claims_accepted} of {ev.claims_accepted + ev.claims_rejected} model claims grounded"
                        if grounded is not None else "no model output to judge")),
        theme.kpi("Confidence", pct(ev.confidence), tone="info" if ev.confidence is not None else "neutral",
                  href=evaluation, drill="CALIBRATION →", foot="The Critic's own figure; calibration measured"),
        theme.kpi("Investigation", seconds(ev.duration_s), tone=time_tone, href=review, drill="STAGES →",
                  foot=f"SLA {seconds(sla_s)}" if sla_s is not None else "opened to sealed"),
        theme.kpi("Cost", usd(ev.cost_usd) if ev.used_model else "$0 · rules", tone=cost_tone, href=evaluation,
                  drill="COST →", foot=f"{ev.model_calls} model call(s)"
                  + (f" · budget {usd(budget_usd)}" if budget_usd is not None else "")),
        theme.kpi("Human review", "Required", tone="ok" if decided else "warn", href=review, drill="REVIEW →",
                  foot=f"{ev.action_status.title()} by a person" if decided
                  else f"Staged action {(ev.action_status or 'unknown').lower()}"),
    ], columns=5)


# ── Layer 2: the investigation's evaluation and gates (Review page) ──────────

def render_evaluation(ev: Any) -> None:
    if ev is None:
        return
    chips = "".join(
        f'<span style="margin-right:10px;font-size:13px;color:{theme.GRAY3};">{group} '
        + theme.status_chip(status, size="sm") + "</span>"
        for group, status in ev.gate_summary.items()
    )
    stats = (f"evidence quality {pct(ev.grounding) if ev.grounding is not None else 'rules only'} · "
             f"confidence {pct(ev.confidence)} · {seconds(ev.duration_s)} · "
             f"{usd(ev.cost_usd) if ev.used_model else '$0, rules only'} · "
             f"decision quality {pct(ev.decision_score) if ev.decision_score is not None else NO_DATA}")
    st.markdown(
        f'<div style="margin:6px 0 2px 0;">{chips}</div>'
        f'<div class="mono" style="font-size:13px;color:{theme.GRAY1};margin-bottom:4px;">'
        f'{theme._esc(stats)}</div>',
        unsafe_allow_html=True,
    )
    with st.expander("How this investigation was evaluated: governance gates, time, cost"):
        st.caption("Measured from the record, never reported by the agents: the grounding gate, the operator's "
                   "decision, the gateway's usage and cost, the sealed record's hash, the audit log.")
        st.dataframe(pd.DataFrame([
            {"": GATE_ICON.get(g.status, ""), "Gate": g.group, "Check": g.name, "Status": g.status,
             "Detail": g.detail, "Decided by": calculated_by(g.source)}
            for g in ev.gates
        ]), hide_index=True, width="stretch")
        stages = {**ev.stages_s, **({"handoffs": ev.handoff_s} if ev.handoff_s is not None else {})}
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Where the time went**")
            rows = ([{"stage": "alert → investigation (queue)", "seconds": round(ev.queue_s, 2)}]
                    if ev.queue_s is not None else [])
            rows += [{"stage": name.replace("_", " "), "seconds": round(v, 2)} for name, v in stages.items()]
            if ev.time_to_decision_s is not None:
                rows.append({"stage": "sealed → human decision", "seconds": round(ev.time_to_decision_s, 1)})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        with c2:
            st.markdown("**Model use**")
            st.dataframe(pd.DataFrame([
                {"measure": "model calls", "value": str(ev.model_calls)},
                {"measure": "re-sent requests", "value": str(ev.retries)},
                {"measure": "tokens in / out", "value": f"{ev.prompt_tokens} / {ev.completion_tokens}"},
                {"measure": "cost", "value": usd(ev.cost_usd) if ev.used_model else "$0, rules only"},
                {"measure": "Critic cost (this alert)", "value": usd(ev.critic_cost_usd)},
                {"measure": "cost per useful evidence item", "value": usd(ev.cost_per_useful_item)},
                {"measure": "served by", "value": ", ".join(ev.served_models) or NO_DATA},
                {"measure": "outcomes", "value": ", ".join(f"{k} {v}" for k, v in ev.outcomes.items()) or "none"},
            ]), hide_index=True, width="stretch")


# ── Layer 3: what is behind one Health number (Agent Evaluation page) ────────

EVIDENCE_SHOWN = 25
_REVIEW_WORD = {"PENDING": "open"}


def render_evidence(items: list, unit: str) -> None:
    """
    The investigations, model calls and recommendations one dimension was computed from
    (src/evaluation_evidence.py), what counts against it first. Each row names its agent and links to the
    review it belongs to, so the number leads to the work.
    """
    samples, against = sum(i.samples for i in items), sum(i.against for i in items)
    if not items:
        st.caption(f"No {unit} in the window: nothing was measured.")
        return
    shown = items[:EVIDENCE_SHOWN]
    cells = f"padding:7px 10px;border-bottom:1px solid {theme.DARK_GRAY2};vertical-align:top;font-size:14px;"
    rows = []
    for item in shown:
        tone = theme.TONES["bad" if item.against else "ok"][0]
        review = "—"
        if item.action_id:
            where = " · ".join(x for x in (item.airport, (item.alert_type or "").removeprefix("ALERT_")) if x)
            status = _REVIEW_WORD.get(item.review_status or "", (item.review_status or "").lower())
            review = (f'<a href="{theme._attr_href(theme.drill_href("critic", review=item.action_id))}" target="_self" '
                      f'style="color:{theme.BLUE5};text-decoration:none;font-weight:600;">'
                      f'{theme._esc(where or "review")} &rarr;</a>'
                      + (f' <span style="color:{theme.GRAY1};">{theme._esc(status)}</span>' if status else ""))
        when = f"{item.at:%b %d %H:%M}" if hasattr(item.at, "strftime") else "—"
        rows.append(
            f'<tr><td style="{cells}color:{tone};width:18px;">{"■" if item.against else "●"}</td>'
            f'<td class="mono" style="{cells}color:{theme.GRAY3};white-space:nowrap;">{theme._esc(when)}</td>'
            f'<td style="{cells}color:{theme.GRAY3};white-space:nowrap;">{theme._esc(item.kind)}</td>'
            f'<td style="{cells}color:{theme.GRAY5};">{theme._esc(item.reading)}</td>'
            f'<td style="{cells}color:{theme.GRAY5};white-space:nowrap;">'
            f'{theme._esc(", ".join(a.replace("_", " ") for a in item.agents) or "—")}</td>'
            f'<td style="{cells}white-space:nowrap;">{review}</td></tr>')
    head = f"padding:6px 10px;text-align:left;font-size:13px;font-weight:700;letter-spacing:0.07em;" \
           f"text-transform:uppercase;color:{theme.GRAY1};border-bottom:1px solid {theme.DARK_GRAY3};"
    st.markdown(
        f'<div style="font-size:14px;color:{theme.GRAY3};margin:2px 0 6px 0;">'
        f'<b style="color:{theme.LIGHT_GRAY5};">{against:,} of {samples:,}</b> {theme._esc(unit)} count against it'
        + (f" · the {len(shown)} rows that matter most of {len(items):,}" if len(items) > len(shown) else "")
        + "</div>"
        f'<table style="width:100%;border-collapse:collapse;">'
        f'<thead><tr><th style="{head}"></th><th style="{head}">When (UTC)</th><th style="{head}">What</th>'
        f'<th style="{head}">Its reading</th><th style="{head}">Agent</th><th style="{head}">Review</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>',
        unsafe_allow_html=True,
    )
