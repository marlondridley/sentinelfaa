"""
AI Critic & Human Review: Critic verdicts, the HITL execution-gate review
queue, and the decision audit trail.

Answers mission question 7: what should the operator do next -- and turns
that into an attributed approve / monitor / override / reject workflow.

The one invariant on this page: nothing an AI produces executes anything.
The Critic's verdict is advisory, the staged action sits PENDING until a
human decides, and every transition is written to `action_audit_log` with
the actor string resolved by bootstrap_page().

The queue is a triage table, one row per review (one review per condition:
src/execution_gate.py), filtered by facility, AI suggestion, alert type and
confidence. A review's full detail -- investigation, evaluation, ticket, the
readings it covers -- opens in a drawer, where it is approved, overridden,
monitored or rejected one at a time. Reject and Continue monitoring can also
be applied to selected rows at once; each review still gets its own audit
row, decided by the same gate call.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
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

# ── Reviews: loaded once, opened from the verdicts and from the queue ──────────
_pending_actions = run(get_pending_staged_actions())
_by_id = {a.action_id: a for a in _pending_actions}

_alert_by_id = {a.get("alert_id"): a for a in alerts_raw}
# Newest verdict per alert: the storage call returns newest first.
_verdict = {}
for _v in run(storage.get_critic_verdicts_for_alerts([a.alert_id for a in _pending_actions if a.alert_id])):
    _verdict.setdefault(_v["alert_id"], _v)
_tickets = {a.action_id: run(get_ticket_by_action_id(a.action_id)) for a in _pending_actions}


def _suggests(action) -> str:
    v = _verdict.get(action.alert_id) or {}
    if v.get("confidence") is None:
        return "No AI review"
    return "Escalate" if v.get("escalate") else "Don't escalate"


def _waited(since: datetime | None) -> str:
    if since is None:
        return "—"
    minutes = int((datetime.now(timezone.utc) - since).total_seconds() // 60)
    return f"{minutes // 1440} d {minutes % 1440 // 60} h" if minutes >= 1440 else (
        f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min")


def _review_detail(action) -> None:
    """Everything behind one review: summary, readings, investigation, evaluation, ticket, decision."""
    ticket = _tickets.get(action.action_id)
    verdict = _verdict.get(action.alert_id) or {}
    origin = (_alert_by_id.get(action.alert_id) or {}).get("data_origin", "unknown")
    conf = verdict.get("confidence")
    st.markdown(
        '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">'
        + theme.status_chip(ticket.posture if ticket else "ACTION")
        + '<span style="font-size:15px;font-weight:650;color:' + theme.LIGHT_GRAY5 + ';">'
        + theme._esc(ticket.title if ticket else f"{action.alert_type} at {action.airport_code or '—'}") + "</span>"
        + ('<span class="chip" style="color:' + theme.ORANGE5 + ";border-color:" + theme.TONES["warn"][1]
           + ';padding:2px 8px;font-size:10.5px;">' + theme._esc(origin.upper()) + " DATA</span>"
           if origin != "live" else "")
        + '<span class="mono" style="margin-left:auto;font-size:11.5px;color:' + theme.GRAY1 + ';">AI suggests '
        + theme._esc(_suggests(action).lower()) + (f" · {conf:.0%}" if conf is not None else "") + "</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"**AI summary:** {verdict.get('bluf') or '_No AI summary yet._'}")
    covered = run(storage.get_covered_alert_ids(action.action_id))
    first, last = action.first_seen_at or action.created_at, action.last_seen_at or action.created_at
    st.markdown(
        f"**{action.occurrences:,} reading(s)** of this condition, {format_ts(first)[:16].replace('T', ' ')}"
        f" to {format_ts(last)[:16].replace('T', ' ')} UTC."
    )
    known = [_alert_by_id[a] for a in covered if a in _alert_by_id]
    if known:
        with st.expander(f"The readings ({len(covered):,})"):
            st.dataframe(pd.DataFrame([{
                "Triggered (UTC)": format_ts(r.get("triggered_at"))[:19].replace("T", " "),
                "Description": r.get("description", ""),
            } for r in known[-200:]]), hide_index=True, use_container_width=True, height=220)

    render_investigation(load_investigation(action.action_id))
    agent_eval.render_evaluation(agent_eval.load_for_action(action.action_id))
    if ticket:
        pc1, pc2 = st.columns(2)
        with pc1:
            theme.evidence_list("What changed", ticket.what_changed or [], limit=4)
            theme.evidence_list("Recommended review", ticket.recommended_review or [], limit=4)
        with pc2:
            theme.evidence_list("Why it matters", ticket.why_it_matters or [], limit=4)
            theme.evidence_list("Evidence", ticket.evidence or [], limit=4)
        if ticket.critic_rationale:
            with st.expander("AI Critic rationale"):
                st.markdown(ticket.critic_rationale)

    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {theme.BLUE5};'
        f'background:{theme.TONES["info"][2]};border-radius:3px;padding:7px 10px;margin:10px 0;'
        f'font-size:11.5px;color:{theme.GRAY3};">This decision will be signed and attributed in the '
        f'audit log as <b class="mono" style="color:{theme.BLUE5};">{theme._esc(format_actor(_principal))}</b></div>',
        unsafe_allow_html=True,
    )
    note = st.text_input("Decision note (recorded in the audit trail)", key=f"note_{action.action_id}",
                         placeholder="e.g. verified against STARS console; contacting KDCA tower")
    _sev_options = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    with st.expander("Edit & override before sign-off"):
        edited_recommendation = st.text_area(
            "Recommended action (editable)", value=action.recommended_action, key=f"override_text_{action.action_id}",
            help="The AI's original recommendation is preserved unchanged in the audit trail; "
                 "this is what you're replacing it with.")
        edited_severity = st.selectbox(
            "Severity (editable)", _sev_options, key=f"override_sev_{action.action_id}",
            index=_sev_options.index(action.severity if action.severity in _sev_options else "CRITICAL"))
        if st.button("Override & sign off", key=f"override_{action.action_id}", use_container_width=True,
                     disabled=not _allowed(Decision.OVERRIDE), help=_why_not(Decision.OVERRIDE)):
            _decide(action.action_id, Decision.OVERRIDE, note,
                    overridden_recommendation=edited_recommendation, overridden_severity=edited_severity)
    b1, b2, b3, _sp = st.columns([1.5, 1.5, 1.2, 2])
    if b1.button("Approve escalation", key=f"approve_{action.action_id}", type="primary", use_container_width=True,
                 disabled=not _allowed(Decision.APPROVE), help=_why_not(Decision.APPROVE)):
        _decide(action.action_id, Decision.APPROVE, note)
    if b2.button("Continue monitoring", key=f"monitor_{action.action_id}", use_container_width=True,
                 disabled=not _allowed(Decision.MONITOR),
                 help=_why_not(Decision.MONITOR) or "Keep the staged action PENDING; mark the ticket MONITORING"):
        _decide(action.action_id, Decision.MONITOR, note)
    if b3.button("Reject", key=f"reject_{action.action_id}", use_container_width=True,
                 disabled=not _allowed(Decision.REJECT), help=_why_not(Decision.REJECT)):
        _decide(action.action_id, Decision.REJECT, note)


@st.dialog("Review", width="large")
def _open_review(action_id: str) -> None:
    _review_detail(_by_id[action_id])


@st.dialog("Confirm decision")
def _confirm_bulk(decision: Decision, action_ids: list[str]) -> None:
    chosen = [_by_id[a] for a in action_ids if a in _by_id]
    verb = {Decision.REJECT: "Reject", Decision.MONITOR: "Continue monitoring"}[decision]
    st.markdown(f"**{verb}** {len(chosen)} review(s), covering {sum(a.occurrences for a in chosen):,} reading(s):")
    st.markdown("\n".join(f"- {a.airport_code or '—'} · {a.alert_type} · {a.occurrences:,} reading(s)"
                          for a in chosen[:12]) + ("\n- …" if len(chosen) > 12 else ""))
    note = st.text_input("Decision note (recorded on each review's audit row)", key="bulk_note",
                         placeholder="e.g. RVR 0 ft = sensor not reporting (fixed 2026-09-26)")
    st.caption(f"Signed as {format_actor(_principal)}; one audit entry per review.")
    if st.button(f"{verb} {len(chosen)} review(s)", type="primary", use_container_width=True,
                 disabled=not note.strip()):
        refused = []
        for a in chosen:
            try:
                run(decide_staged_action(a.action_id, decision, _principal, note.strip()))
            except AuthorizationError as exc:
                refused.append(str(exc))
        if refused:
            st.error(refused[0])
            return
        st.toast(f"{verb.upper()} recorded for {len(chosen)} review(s) by {format_actor(_principal)}")
        st.rerun()


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
    _latest_verdicts = st.expander(f"Latest Critic verdicts ({min(10, _cri_total)})", expanded=False)
    _covering = run(storage.get_covering_actions([r.get("alert_id") for r in _critic_rows_all[:10]]))
    _cited_events = run(storage.get_events_by_ids(sorted({
        e for r in _critic_rows_all[:10] for e in parse_json_str_list(r.get("citations", "[]"), "citations")})))
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

        with _latest_verdicts, st.container(border=True):
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
                st.markdown(theme.event_chips(cits, _cited_events), unsafe_allow_html=True)
            # The verdict is advisory; what a person acts on is the review covering its alert.
            review = _covering.get(cr.get("alert_id"))
            if review and review["status"] == "PENDING" and review["action_id"] in _by_id:
                if st.button("Review escalation" if escalate else "Open review", key=f"verdict_{cr.get('alert_id')}",
                             type="primary" if escalate else "secondary"):
                    _open_review(review["action_id"])
            elif review:
                st.caption(f"Review {review['status'].lower()}"
                           + (f" by {review['decided_by']}" if review.get("decided_by") else "") + ".")
            else:
                st.caption("No review: only CRITICAL alerts are staged for a decision.")

# ── Human review queue (HITL execution gate) ──────────────────────────────────

theme.section(
    "Human review queue",
    "One review per condition; nothing executes until a named operator decides.",
    tone="bad" if _pending_actions else "ok",
    right=f"{len(_pending_actions)} review(s) · {sum(a.occurrences for a in _pending_actions):,} reading(s)",
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
    _rows = pd.DataFrame([{
        "action_id": a.action_id,
        "Last reading (UTC)": a.last_seen_at or a.created_at,
        "Facility": a.airport_code or "—",
        "Alert": (a.alert_type or "—").removeprefix("ALERT_"),
        "Readings": a.occurrences,
        "AI suggests": _suggests(a),
        "Confidence": round(100 * c) if (c := (_verdict.get(a.alert_id) or {}).get("confidence")) is not None else None,
        "AI summary": (_verdict.get(a.alert_id) or {}).get("bluf") or "No AI summary yet",
        "Waiting": _waited(a.first_seen_at or a.created_at),
        "Ticket": _tickets[a.action_id].status if _tickets.get(a.action_id) else "—",
    } for a in _pending_actions]).sort_values("Last reading (UTC)", ascending=False)

    def _counted(column: str):
        return lambda value: f"{value} ({int((_rows[column] == value).sum())})"

    f1, f2, f3, f4 = st.columns([1.1, 1.6, 1.5, 0.9])
    facilities = f1.pills("Facility", sorted(_rows["Facility"].unique()), selection_mode="multi",
                          format_func=_counted("Facility"), key="q_facility")
    suggests = f2.pills("AI suggests", [s for s in ("Escalate", "Don't escalate", "No AI review")
                                        if s in set(_rows["AI suggests"])],
                        selection_mode="multi", format_func=_counted("AI suggests"), key="q_suggests")
    alerts = f3.pills("Alert", sorted(_rows["Alert"].unique()), selection_mode="multi",
                      format_func=_counted("Alert"), key="q_alert")
    confident = f4.toggle("AI ≥ 80%", key="q_confident", help="Only reviews the AI rated at 80% confidence or more")
    _keep = pd.Series(True, index=_rows.index)
    for column, chosen in (("Facility", facilities), ("AI suggests", suggests), ("Alert", alerts)):
        if chosen:
            _keep &= _rows[column].isin(chosen)
    if confident:
        _keep &= _rows["Confidence"].fillna(0) >= 80
    _view = _rows[_keep].reset_index(drop=True)

    _picked = st.dataframe(
        _view.drop(columns=["action_id"]), hide_index=True, use_container_width=True, key="q_table",
        on_select="rerun", selection_mode="multi-row", height=min(38 + 35 * len(_view), 420),
        column_config={
            "Last reading (UTC)": st.column_config.DatetimeColumn(format="MMM D HH:mm"),
            "Readings": st.column_config.NumberColumn(format="%d"),
            "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%"),
            "AI summary": st.column_config.TextColumn(width="large"),
        },
    )
    _chosen = [_view.loc[i, "action_id"] for i in _picked.selection.rows]

    a1, a2, a3, a4 = st.columns([1.2, 1.5, 2.1, 2.4])
    if a1.button("Open review", type="primary", use_container_width=True, disabled=len(_chosen) != 1,
                 help="Select one row: its investigation, evaluation, readings and decision"):
        _open_review(_chosen[0])
    if a2.button(f"Reject selected ({len(_chosen)})", use_container_width=True,
                 disabled=not _chosen or not _allowed(Decision.REJECT), help=_why_not(Decision.REJECT)):
        _confirm_bulk(Decision.REJECT, _chosen)
    if a3.button(f"Continue monitoring selected ({len(_chosen)})", use_container_width=True,
                 disabled=not _chosen or not _allowed(Decision.MONITOR), help=_why_not(Decision.MONITOR)):
        _confirm_bulk(Decision.MONITOR, _chosen)
    a4.caption("Select rows to reject or monitor several reviews at once. "
               "Approving an escalation is one review at a time, from its detail.")

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
