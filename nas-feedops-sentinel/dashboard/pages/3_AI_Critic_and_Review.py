"""
AI Critic & Human Review: Critic verdicts, the HITL execution-gate review
queue, and the decision audit trail.

Answers mission question 7: what should the operator do next -- and turns
that into an attributed approve / monitor / override / reject workflow.

The one invariant on this page: nothing an AI produces executes anything.
The Critic's verdict is advisory, the staged action sits PENDING until a
human decides, and every transition is written to `action_audit_log` with
the actor string resolved by bootstrap_page(). This rewrite changed the
presentation of that workflow and nothing about its semantics -- the gate
calls, their arguments, and their ordering are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import pandas as pd
import streamlit as st

from lib import agent_eval, theme
from lib.data import format_ts, parse_json_str_list
from lib.investigation_view import load_investigation, render_investigation
from lib.page_setup import bootstrap_page
from src import storage
from src.async_bridge import run_async as run
from src.auth import AuthorizationError, Decision, authorize, can_decide, format_actor
from src.config import settings
from src.execution_gate import (
    decide_staged_action,
    get_action_audit_log,
    get_pending_staged_actions,
    get_recent_staged_actions,
)
from src.storage import get_ticket_by_action_id

_principal = bootstrap_page(page_title="AI Critic & Review — PCT FeedOps Sentinel", page_icon="\U0001F916")



def _allowed(decision: Decision) -> bool:
    return can_decide(_principal, decision, allow_unverified=settings.ALLOW_UNVERIFIED_DECISIONS)


def _why_not(decision: Decision) -> str | None:
    """The authorization failure, shown as the disabled button's tooltip."""
    try:
        authorize(_principal, decision, allow_unverified=settings.ALLOW_UNVERIFIED_DECISIONS)
    except AuthorizationError as exc:
        return str(exc)
    return None


def _decide(action_id: str, decision: Decision, note: str | None, **override) -> None:
    """Every disposition goes through the gate, which authorizes before it writes."""
    try:
        run(decide_staged_action(action_id, decision, _principal, note or "", **override))
    except AuthorizationError as exc:
        st.error(str(exc))
        return
    st.toast(f"{decision.value.upper()} recorded for {action_id[:8]} by {format_actor(_principal)}")
    st.rerun()


alerts_raw = run(storage.get_recent_alerts(limit=500))

theme.page_header(
    "AI Critic & Human Review",
    "Which CRITICAL alerts are genuinely safety-significant, and which are data artifacts?",
    detail=(
        "The Critic reads alerts and their surrounding events and proposes an "
        "escalation verdict. It cannot act on that verdict. Anything it recommends is "
        "staged as a PENDING action that stays inert until a named human approves, "
        "overrides or rejects it, and every one of those decisions is appended to the "
        "immutable audit log at the bottom of this page."
    ),
)

# ── Critic verdicts ───────────────────────────────────────────────────────────
_critic_rows_all = run(storage.get_recent_critic_results(limit=50))

_cri_total = len(_critic_rows_all)
_cri_escalate = sum(1 for r in _critic_rows_all if r.get("escalate"))
_cri_cascade = sum(1 for r in _critic_rows_all if r.get("pattern") == "CASCADE_FAILURE")
_cri_isolated = sum(1 for r in _critic_rows_all if r.get("pattern") == "ISOLATED_FAULT")
_cri_avg_conf = (
    sum(r.get("confidence", 0) or 0 for r in _critic_rows_all) / _cri_total
) if _cri_total else None

theme.section(
    "Critic verdicts",
    "Advisory only — no verdict here has executed anything.",
    tone="bad" if _cri_escalate else "info",
)

theme.kpi_row(
    [
        theme.kpi(
            "Alerts evaluated", f"{_cri_total:,}", tone="info",
            foot="Most recent 50 Critic runs",
            href=theme.drill_href("alerts", severity="CRITICAL"),
            drill="SOURCE →",
        ),
        theme.kpi(
            "Escalations suggested", f"{_cri_escalate:,}",
            tone="bad" if _cri_escalate else "ok",
            foot="Awaiting a human decision below" if _cri_escalate else "None suggested",
        ),
        theme.kpi(
            "Mean confidence",
            f"{_cri_avg_conf:.0%}" if _cri_avg_conf is not None else "—",
            tone="neutral",
            foot="Model-reported — not a measured accuracy",
        ),
        theme.kpi(
            "Cascade failures", f"{_cri_cascade:,}",
            tone="bad" if _cri_cascade else "ok",
            foot="Multiple services failing together",
        ),
        theme.kpi(
            "Isolated faults", f"{_cri_isolated:,}", tone="info",
            foot="Contained to one service or facility",
        ),
    ],
    columns=5,
)

if not _critic_rows_all:
    st.info(
        "No Critic verdicts yet. Configure LLM_PROVIDER in .env and trigger a CRITICAL "
        "alert to populate this view."
    )
else:
    _alert_id_to_row = {a.get("alert_id"): a for a in alerts_raw}
    _PATTERN_TONE = {
        "CASCADE_FAILURE": "bad",
        "ISOLATED_FAULT": "warn",
        "WEATHER_CORRELATED": "warn",
        "DATA_ANOMALY": "info",
        "TRANSIENT": "neutral",
        "UNKNOWN": "neutral",
    }
    for cr in _critic_rows_all[:10]:
        alert_row = _alert_id_to_row.get(cr.get("alert_id"), {})
        conf = cr.get("confidence")
        conf_str = f"{conf:.0%}" if conf is not None else "N/A"
        pattern = str(cr.get("pattern", "UNKNOWN"))
        escalate = bool(cr.get("escalate"))
        reasoning = cr.get("reasoning", "")
        alert_type = alert_row.get("alert_type", "—")
        airport = alert_row.get("airport_code", "—")
        service = alert_row.get("service_name", "—")
        _ptone = _PATTERN_TONE.get(pattern, "neutral")

        with st.container(border=True):
            st.markdown(
                '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
                '<span style="font-size:14px;font-weight:650;color:' + theme.LIGHT_GRAY5 + ';">'
                + theme._esc(alert_type) + "</span>"
                '<span class="mono" style="font-size:11.5px;color:' + theme.GRAY1 + ';">'
                + theme._esc(airport) + " · " + theme._esc(service) + "</span>"
                '<span class="chip" style="color:' + theme.TONES[_ptone][0]
                + ";border-color:" + theme.TONES[_ptone][1]
                + ';padding:2px 8px;font-size:10.5px;">' + theme._esc(pattern) + "</span>"
                '<span class="mono" style="font-size:11.5px;color:' + theme.GRAY1 + ';">'
                "confidence " + theme._esc(conf_str) + "</span>"
                + (
                    '<span class="chip" style="color:' + theme.RED5
                    + ";border-color:" + theme.TONES["bad"][1]
                    + ";background:" + theme.TONES["bad"][2]
                    + ';margin-left:auto;padding:2px 9px;font-size:10.5px;">'
                    "ESCALATE TO SUPERVISOR</span>"
                    if escalate else
                    '<span class="mono" style="margin-left:auto;font-size:11px;color:'
                    + theme.GRAY1 + ';">no escalation proposed</span>'
                )
                + "</div>",
                unsafe_allow_html=True,
            )
            st.markdown(reasoning if reasoning else "_No reasoning recorded._")
            cits = parse_json_str_list(cr.get("citations", "[]"), "citations")
            if cits:
                st.markdown(
                    f'<div class="mono" style="font-size:11px;color:{theme.GRAY1};">'
                    f'Evidence event_ids: {theme._esc(", ".join(cits[:6]))}</div>',
                    unsafe_allow_html=True,
                )

# ── Human review queue (HITL execution gate) ──────────────────────────────────
_pending_actions = run(get_pending_staged_actions())

theme.section(
    "Human review queue",
    "Staged actions that cannot execute until a named operator decides.",
    tone="bad" if _pending_actions else "ok",
    right=f"{len(_pending_actions)} pending",
)

if not _pending_actions:
    st.markdown(
        f'<div style="border:1px solid {theme.TONES["ok"][1]};border-left:3px solid '
        f'{theme.GREEN5};background:{theme.TONES["ok"][2]};border-radius:4px;padding:14px 16px;">'
        f'<div style="color:{theme.GREEN5};font-size:14px;font-weight:700;">Queue empty</div>'
        f'<div style="color:{theme.GRAY3};font-size:12.5px;margin-top:4px;">'
        "All staged actions have been decided, or no CRITICAL alert has produced one."
        "</div></div>",
        unsafe_allow_html=True,
    )
else:
    _alert_origin = {a.get("alert_id"): a.get("data_origin", "unknown") for a in alerts_raw}
    for _action in _pending_actions:
        _ticket = run(get_ticket_by_action_id(_action.action_id))
        _origin = _alert_origin.get(_action.alert_id, "unknown")
        with st.container(border=True):
            title = (
                _ticket.title if _ticket
                else f"{_action.alert_type} at {_action.airport_code or '—'}"
            )
            conf_str = (
                f"{_action.critic_confidence:.0%}"
                if _action.critic_confidence is not None else "—"
            )
            posture = _ticket.posture if _ticket else "ACTION"

            st.markdown(
                '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;'
                'margin-bottom:8px;">'
                + theme.status_chip(posture)
                + '<span style="font-size:15px;font-weight:650;color:' + theme.LIGHT_GRAY5 + ';">'
                + theme._esc(title) + "</span>"
                # Provenance: anything not from the live feed is labelled on its face.
                + (
                    '<span class="chip" style="color:' + theme.ORANGE5
                    + ";border-color:" + theme.TONES["warn"][1]
                    + ';padding:2px 8px;font-size:10.5px;">'
                    + theme._esc(_origin.upper()) + " DATA</span>"
                    if _origin != "live" else ""
                )
                + '<span class="mono" style="margin-left:auto;font-size:11.5px;color:'
                + theme.GRAY1 + ';">AI confidence ' + theme._esc(conf_str) + "</span>"
                "</div>",
                unsafe_allow_html=True,
            )

            if _ticket and _ticket.summary:
                st.markdown(f"**Summary:** {_ticket.summary}")
            else:
                st.markdown(f"**Recommended action:** {_action.recommended_action}")

            render_investigation(load_investigation(_action.action_id))
            # Layer 2: how this investigation was evaluated, and its gates.
            agent_eval.render_evaluation(agent_eval.load_for_action(_action.action_id))

            if _ticket:
                pc1, pc2 = st.columns(2)
                with pc1:
                    theme.evidence_list("What changed", _ticket.what_changed or [], limit=4)
                    theme.evidence_list("Recommended review", _ticket.recommended_review or [], limit=4)
                with pc2:
                    theme.evidence_list("Why it matters", _ticket.why_it_matters or [], limit=4)
                    theme.evidence_list("Evidence", _ticket.evidence or [], limit=4)

                if _ticket.critic_rationale:
                    with st.expander("AI Critic rationale"):
                        st.markdown(_ticket.critic_rationale)

                st.markdown(
                    f'<div class="mono" style="font-size:10.5px;color:{theme.GRAY1};'
                    f'margin-top:8px;">'
                    f"ticket {theme._esc(_ticket.ticket_id[:18])} · "
                    f"action {theme._esc(_action.action_id[:18])} · "
                    f"brief {theme._esc((_ticket.brief_id or '')[:18])} · "
                    f"status {theme._esc(_ticket.status)} · "
                    f"drafted {_ticket.generated_at.strftime('%H:%M:%S')} UTC</div>",
                    unsafe_allow_html=True,
                )

            # Set in the sidebar identity block (bootstrap_page(), which every page
            # calls), so every decision recorded here carries the same actor string
            # -- including whether the identity was Keycloak-verified or
            # self-declared -- as the sidebar banner the operator actually saw when
            # they started this session, regardless of which page they were on.
            _active_actor = format_actor(_principal)
            st.markdown(
                f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid '
                f'{theme.BLUE5};background:{theme.TONES["info"][2]};border-radius:3px;'
                f'padding:7px 10px;margin:10px 0;font-size:11.5px;color:{theme.GRAY3};">'
                f"This decision will be signed and attributed in the audit log as "
                f'<b class="mono" style="color:{theme.BLUE5};">{theme._esc(_active_actor)}</b>'
                "</div>",
                unsafe_allow_html=True,
            )

            note_key = f"note_{_action.action_id}"
            ap_key = f"approve_{_action.action_id}"
            mo_key = f"monitor_{_action.action_id}"
            rj_key = f"reject_{_action.action_id}"

            note = st.text_input(
                "Decision note (recorded in the audit trail)",
                key=note_key,
                placeholder="e.g. verified against STARS console; contacting KDCA tower",
            )

            # Override: the operator edits the AI's recommendation text and/or
            # severity before signing off, instead of accepting it verbatim (Approve)
            # or discarding it (Reject). Still purely advisory/record-keeping --
            # override_staged_action() never triggers any automated execution any
            # more than approve/reject do; it only changes what gets written to the
            # audit trail as the human-authorized recommendation.
            _sev_options = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
            with st.expander("Edit & override before sign-off"):
                override_text_key = f"override_text_{_action.action_id}"
                override_sev_key = f"override_sev_{_action.action_id}"
                ov_key = f"override_{_action.action_id}"

                edited_recommendation = st.text_area(
                    "Recommended action (editable)",
                    value=_action.recommended_action,
                    key=override_text_key,
                    help="The AI's original recommendation is preserved unchanged in the "
                         "audit trail; this is what you're replacing it with.",
                )
                _sev_default = _action.severity if _action.severity in _sev_options else "CRITICAL"
                edited_severity = st.selectbox(
                    "Severity (editable)",
                    _sev_options,
                    index=_sev_options.index(_sev_default),
                    key=override_sev_key,
                )

                if st.button("Override & sign off", key=ov_key, use_container_width=True,
                             disabled=not _allowed(Decision.OVERRIDE),
                             help=_why_not(Decision.OVERRIDE)):
                    _decide(_action.action_id, Decision.OVERRIDE, note,
                            overridden_recommendation=edited_recommendation,
                            overridden_severity=edited_severity)

            b1, b2, b3, _sp = st.columns([1.5, 1.5, 1.2, 2])

            if b1.button("Approve escalation", key=ap_key, type="primary", use_container_width=True,
                         disabled=not _allowed(Decision.APPROVE), help=_why_not(Decision.APPROVE)):
                _decide(_action.action_id, Decision.APPROVE, note)

            if b2.button("Continue monitoring", key=mo_key, use_container_width=True,
                         disabled=not _allowed(Decision.MONITOR),
                         help=_why_not(Decision.MONITOR)
                         or "Keep the staged action PENDING; mark the ticket MONITORING"):
                _decide(_action.action_id, Decision.MONITOR, note)

            if b3.button("Reject", key=rj_key, use_container_width=True,
                         disabled=not _allowed(Decision.REJECT), help=_why_not(Decision.REJECT)):
                _decide(_action.action_id, Decision.REJECT, note)

# ── Decision audit trail ──────────────────────────────────────────────────────
theme.section(
    "Decision audit trail",
    "Who approved or rejected which action, when, and why.",
    tone="neutral",
    right="append-only · action_audit_log",
)

_audit_rows = run(get_action_audit_log(limit=50))
_recent_actions = run(get_recent_staged_actions(limit=50))
_action_map = {a.action_id: a for a in _recent_actions}

if not _audit_rows:
    st.info("No staged actions have been decided yet.")
else:
    audit_display = []
    for r in _audit_rows:
        act = _action_map.get(r.get("action_id"))
        audit_display.append({
            # created_at comes back from storage as a real datetime (asyncpg
            # TIMESTAMPTZ), not a string -- format_ts() handles both so this
            # doesn't crash the moment there's a real decision in the log.
            "Decided (UTC)": format_ts(r.get("created_at")).replace("T", " "),
            "Action": (r.get("action_id") or "")[:18],
            "Airport": act.airport_code if act else "—",
            "Alert type": act.alert_type if act else "—",
            "Transition": f"{r.get('previous_status','?')} → {r.get('new_status','?')}",
            "Actor": r.get("actor", "—"),
            "Note": r.get("note", "") or "—",
            # Only populated for OVERRIDDEN rows -- shows what the operator
            # actually signed off on in place of the AI's original
            # recommended_action, which stays untouched on the staged_actions row.
            "Overridden to": (
                (act.overridden_recommendation or "—")
                if act and r.get("new_status") == "OVERRIDDEN"
                else "—"
            ),
        })
    audit_df = pd.DataFrame(audit_display)
    st.dataframe(audit_df, hide_index=True, use_container_width=True, height=260)
    st.download_button(
        "Download the audit trail as CSV",
        data=audit_df.to_csv(index=False).encode("utf-8"),
        file_name="pct_decision_audit_trail.csv",
        mime="text/csv",
        help="For an incident record or an external review.",
    )

theme.back_link()
