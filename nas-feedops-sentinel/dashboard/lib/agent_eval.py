"""
Agent evaluation on the dashboard, in three layers of progressive disclosure:
mission first, AI second, telemetry on demand.

  Layer 1  Overview           one "AI investigation" card, five indicators,
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

from lib import theme
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
        st.caption(f"AI investigation evaluation unavailable: {type(exc).__name__}: {exc}")
        return None


def load_for_action(action_id: str):
    from src.evaluation import evaluate_action

    try:
        return run(evaluate_action(action_id))
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Evaluation unavailable: {type(exc).__name__}: {exc}")
        return None


# ── Layer 1: the Overview card ───────────────────────────────────────────────

def render_card(ev: Any, *, sla_s: float | None, budget_usd: float | None) -> None:
    """Five indicators for the latest investigation, each one click from its detail."""
    if ev is None:
        st.caption("No investigation recorded yet. CRITICAL alerts are investigated when they are staged.")
        return
    status = overall(ev)
    review = theme.drill_href("critic")
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
        f'<span style="font-size:13px;color:{theme.LIGHT_GRAY5};font-weight:600;">'
        f'{GATE_ICON[status]} AI investigation</span>'
        + theme.status_chip(status, size="sm")
        + f'<span style="font-size:12px;color:{theme.GRAY3};">'
        + theme._esc(f"{ev.alert_type or 'alert'} at {ev.airport or 'PCT'} · "
                     f"{ev.opened_at:%Y-%m-%d %H:%M} UTC · advisory only")
        + f'</span><a href="{evaluation}" target="_self" style="margin-left:auto;font-size:12px;'
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
        f'<span style="margin-right:10px;font-size:11.5px;color:{theme.GRAY3};">{group} '
        + theme.status_chip(status, size="sm") + "</span>"
        for group, status in ev.gate_summary.items()
    )
    stats = (f"evidence quality {pct(ev.grounding) if ev.grounding is not None else 'rules only'} · "
             f"confidence {pct(ev.confidence)} · {seconds(ev.duration_s)} · "
             f"{usd(ev.cost_usd) if ev.used_model else '$0, rules only'} · "
             f"decision quality {pct(ev.decision_score) if ev.decision_score is not None else NO_DATA}")
    st.markdown(
        f'<div style="margin:6px 0 2px 0;">{chips}</div>'
        f'<div class="mono" style="font-size:11px;color:{theme.GRAY1};margin-bottom:4px;">'
        f'{theme._esc(stats)}</div>',
        unsafe_allow_html=True,
    )
    with st.expander("How this investigation was evaluated: governance gates, time, cost"):
        st.caption("Measured from the record, never reported by the agents: the grounding gate, the operator's "
                   "decision, the gateway's usage and cost, the sealed record's hash, the audit log.")
        st.dataframe(pd.DataFrame([
            {"": GATE_ICON.get(g.status, ""), "Gate": g.group, "Check": g.name, "Status": g.status,
             "Detail": g.detail, "Decided by": g.source}
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
