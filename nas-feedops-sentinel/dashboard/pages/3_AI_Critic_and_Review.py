"""
Review Queue: the CRITICAL conditions that need a person's decision, what the
AI suggests for each, and the audit trail of what was decided.

This is the page where work is done. The Dashboard says what needs someone;
the Alert Log is the record of every reading; here a named operator approves,
overrides, rejects or keeps monitoring one review at a time.

The one invariant on this page: nothing an AI produces executes anything.
The Critic's verdict is advisory, the staged action sits PENDING until a
human decides, and every transition is written to `action_audit_log` with
the actor string resolved by bootstrap_page().

Everything on it counts reviews (one per condition: src/execution_gate.py),
never readings, and its three numbers are its own rows (src/review_queue.py):

  Awaiting decision       nobody has answered yet -- the queue
  AI suggests escalation  those of the queue the Critic would escalate
  Monitoring              answered "keep watching": out of the queue, still
                          open, still decidable from its detail

A tile opens the rows it counts (?show=awaiting|escalate|monitoring). The
address names the open list: a link arrives on the list it names, whatever
this session chose before, and the Awaiting and Monitoring pills write their
list back to ?show=, so a refresh or a copied link shows the same one. (Back
between two pill choices is the exception: Streamlit reruns with the address
it last wrote, so the list stays as it was until a reload.)
A review's full detail -- investigation, evaluation, ticket, the readings it
covers -- opens in a drawer (?review=<action id> opens one by link; a
decided one opens read-only, as the record of who decided and why). Reject
and Continue monitoring can also be applied to selected rows at once; each
review still gets its own audit row, decided by the same gate call.

The Critic's own activity, reading by reading, is at the bottom: it is
evidence about the AI, not a second queue.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import pandas as pd
import streamlit as st

from lib import agent_eval, safe_text, theme
from lib.data import format_ts, parse_json_str_list, waited
from lib.investigation_view import load_investigation, render_investigation
from lib.page_setup import bootstrap_page
from src import review_queue, storage
from src.async_bridge import run_async as run
from src.auth import AuthorizationError, Decision, authorize, can_decide, format_actor
from src.config import settings
from src.execution_gate import (
    DecisionNotRecorded,
    decide_staged_action,
    get_action_audit_log,
    get_recent_staged_actions,
)
from src.review_queue import OpenReview

_principal = bootstrap_page(page_title="Review Queue — PCT FeedOps Sentinel", page_icon="\U0001F916")


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
    except DecisionNotRecorded as exc:
        # Someone decided it first (or it was merged): nothing was written for this operator.
        st.toast(f"{decision.value.upper()} not recorded for {action_id[:8]}: {exc}")
        st.rerun()
    st.toast(f"{decision.value.upper()} recorded for {action_id[:8]} by {format_actor(_principal)}")
    st.rerun()


theme.page_header(
    "Review Queue",
    "Which CRITICAL conditions need a decision, and what does the AI suggest?",
    detail=(
        "One review per condition, however many readings it has. The Critic reads a condition's "
        "alerts and events and suggests whether to escalate; it cannot act on that. Each review "
        "stays inert until a named person approves, overrides or rejects it. Continue monitoring "
        "takes it out of the queue and keeps it open. Every decision is appended to the audit "
        "trail below."
    ),
)

# ── Open reviews: loaded once, the same rows behind every number on the page ───
alerts_raw = run(storage.get_recent_alerts(limit=500))
_alert_by_id = {a.get("alert_id"): a for a in alerts_raw}

_reviews = run(review_queue.open_reviews())
_by_id = {r.action.action_id: r for r in _reviews}
_awaiting = review_queue.awaiting(_reviews)
_monitored = review_queue.monitored(_reviews)
_escalations = review_queue.escalations(_reviews)


def _summary(review: OpenReview) -> str:
    """The Critic's one-line summary; a verdict with none (older, or the model left it out) shows its verdict and confidence."""
    if (review.verdict or {}).get("bluf"):
        return review.verdict["bluf"]
    if review.confidence is not None:
        return (f"No written summary for this review. The AI says {review.suggests.lower()}, "
                f"{review.confidence:.0%} confident.")
    return "Not reviewed by the AI yet."


def _review_detail(review: OpenReview) -> None:
    """Everything behind one review: summary, readings, investigation, evaluation, ticket, decision."""
    action, ticket, verdict = review.action, review.ticket, review.verdict or {}
    origin = (_alert_by_id.get(action.alert_id) or {}).get("data_origin", "unknown")
    st.markdown(
        '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">'
        + theme.status_chip(ticket.posture if ticket else "ACTION")
        + '<span style="font-size:17px;font-weight:650;color:' + theme.LIGHT_GRAY5 + ';">'
        + theme._esc(ticket.title if ticket else f"{action.alert_type} at {action.airport_code or '—'}") + "</span>"
        + ('<span class="chip" style="color:' + theme.ORANGE5 + ";border-color:" + theme.TONES["warn"][1]
           + ';padding:2px 8px;font-size:13px;">' + theme._esc(origin.upper()) + " DATA</span>"
           if origin != "live" else "")
        + '<span class="mono" style="margin-left:auto;font-size:13px;color:' + theme.GRAY1 + ';">'
        + theme._esc(review.ai_line) + "</span></div>",
        unsafe_allow_html=True,
    )
    if review.monitoring:
        st.caption(
            f"Being monitored since {format_ts(ticket.decided_at)[:16].replace('T', ' ')} UTC"
            + (f" by {safe_text.plain(ticket.decided_by)}" if ticket.decided_by else "")
            + (f": {safe_text.plain(ticket.decision_note)}" if ticket.decision_note else ".")
        )
    st.markdown(f"**AI summary:** {safe_text.plain(_summary(review))}")
    covered = run(storage.get_covered_alert_ids(action.action_id))
    first, last = action.first_seen_at or action.created_at, action.last_seen_at or action.created_at
    assessed = verdict.get("verdicts") or 0
    st.markdown(
        f"**{action.occurrences:,} reading(s)** of this condition, {format_ts(first)[:16].replace('T', ' ')}"
        f" to {format_ts(last)[:16].replace('T', ' ')} UTC."
        + (f" The AI assessed {assessed:,} of them; {verdict.get('escalations') or 0:,} said escalate."
           if assessed > 1 else "")
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
                st.markdown(safe_text.plain(ticket.critic_rationale))

    if action.status != "PENDING":
        # No longer open: the record of what happened, with nothing left to decide.
        when = f" on {format_ts(action.decided_at)[:16].replace('T', ' ')} UTC" if action.decided_at else ""
        st.markdown(
            f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {theme.GRAY3};'
            f'background:{theme.DARK_GRAY2};border-radius:3px;padding:8px 11px;margin:10px 0;'
            f'font-size:14px;color:{theme.GRAY5};"><b>{theme._esc(action.status.title())}</b>'
            + (f" by {theme._esc(action.decided_by)}" if action.decided_by else "") + theme._esc(when)
            + (f'<div style="margin-top:4px;color:{theme.GRAY3};">Note: {theme._esc(action.decision_note)}</div>'
               if action.decision_note else "")
            + (f'<div style="margin-top:4px;color:{theme.GRAY3};">Signed off as: '
               f'{theme._esc(action.overridden_recommendation)}</div>' if action.overridden_recommendation else "")
            + "</div>",
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {theme.BLUE5};'
        f'background:{theme.TONES["info"][2]};border-radius:3px;padding:7px 10px;margin:10px 0;'
        f'font-size:13px;color:{theme.GRAY3};">This decision will be signed and attributed in the '
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
                 help=_why_not(Decision.MONITOR) or "Take it out of the queue and keep it open, under Monitoring"):
        _decide(action.action_id, Decision.MONITOR, note)
    if b3.button("Reject", key=f"reject_{action.action_id}", use_container_width=True,
                 disabled=not _allowed(Decision.REJECT), help=_why_not(Decision.REJECT)):
        _decide(action.action_id, Decision.REJECT, note)


@st.dialog("Review", width="large")
def _open_review(action_id: str) -> None:
    _review_detail(_by_id[action_id])


@st.dialog("Confirm decision")
def _confirm_bulk(decision: Decision, action_ids: list[str]) -> None:
    chosen = [_by_id[a].action for a in action_ids if a in _by_id]
    verb = {Decision.REJECT: "Reject", Decision.MONITOR: "Continue monitoring"}[decision]
    st.markdown(f"**{verb}** {len(chosen)} review(s), covering {sum(a.occurrences for a in chosen):,} reading(s):")
    st.markdown("\n".join(f"- {a.airport_code or '—'} · {a.alert_type} · {a.occurrences:,} reading(s)"
                          for a in chosen[:12]) + ("\n- …" if len(chosen) > 12 else ""))
    note = st.text_input("Decision note (recorded on each review's audit row)", key="bulk_note",
                         placeholder="e.g. RVR 0 ft = sensor not reporting (fixed 2026-09-26)")
    st.caption(f"Signed as {format_actor(_principal)}; one audit entry per review.")
    if st.button(f"{verb} {len(chosen)} review(s)", type="primary", use_container_width=True,
                 disabled=not note.strip()):
        refused, skipped, recorded = [], 0, 0
        for a in chosen:
            try:
                run(decide_staged_action(a.action_id, decision, _principal, note.strip()))
                recorded += 1
            except AuthorizationError as exc:
                refused.append(str(exc))
            except DecisionNotRecorded:
                skipped += 1
        if refused:
            st.error(refused[0])
            return
        st.toast(f"{verb.upper()} recorded for {recorded} of {len(chosen)} review(s) by {format_actor(_principal)}"
                 + (f"; {skipped} already decided by someone else, not changed" if skipped else ""))
        st.rerun()


# A link that names a review (the Dashboard, the attention strip, the Alert Log,
# a number on Agent Evaluation) opens it, once: the parameter is dropped so
# closing the drawer is final. One that is no longer open opens read-only.
_linked = st.query_params.get("review")
if _linked:
    del st.query_params["review"]
    if _linked not in _by_id:
        _closed = run(review_queue.find(_linked))
        if _closed is not None:
            _by_id[_linked] = _closed
    if _linked in _by_id:
        _open_review(_linked)
    else:
        st.info("No review has that id; the queue below shows what is open.")

# ── The numbers: each one is rows of the table below, and opens them ──────────
AWAITING, MONITORING = "awaiting", "monitoring"
# ?show=escalate is the awaiting view with the AI's escalations picked out.
_show = str(st.query_params.get("show") or "").lower()
_SHOW = "SHOW ↓"


def _choose_list() -> None:
    """A pill writes its list into the address. Clicking the highlighted one clears the pill, not the list."""
    if st.session_state["q_show"]:
        st.query_params["show"] = st.session_state["q_show"]


theme.kpi_row(
    [
        theme.kpi("Awaiting decision", f"{len(_awaiting):,}", tone="bad" if _awaiting else "ok",
                  foot=(f"{sum(r.action.occurrences for r in _awaiting):,} reading(s) behind them"
                        if _awaiting else "Nothing waiting for anyone"),
                  href=theme.drill_href("critic", show=AWAITING) if _awaiting else None, drill=_SHOW),
        theme.kpi("AI suggests escalation", f"{len(_escalations):,}", tone="bad" if _escalations else "ok",
                  foot=f"of the {len(_awaiting):,} awaiting a decision" if _awaiting else "Nothing to assess",
                  href=theme.drill_href("critic", show="escalate") if _escalations else None, drill=_SHOW),
        theme.kpi("Monitoring", f"{len(_monitored):,}", tone="warn" if _monitored else "neutral",
                  foot="Answered “keep watching”; still open" if _monitored else "None",
                  href=theme.drill_href("critic", show=MONITORING) if _monitored else None, drill=_SHOW),
        theme.kpi("Longest wait", waited(min((r.waiting_since for r in _awaiting), default=None)),
                  tone="warn" if _awaiting else "neutral",
                  foot="Oldest review awaiting a decision" if _awaiting else "No review is waiting"),
    ],
    columns=4,
)

# ── The queue (HITL execution gate) ───────────────────────────────────────────
theme.section(
    "Reviews",
    "One per condition; nothing executes until a named operator decides.",
    tone="bad" if _awaiting else ("warn" if _monitored else "ok"),
    right=f"{len(_awaiting)} awaiting · {len(_monitored)} monitoring",
)

if not _reviews:
    st.markdown(
        f'<div style="border:1px solid {theme.TONES["ok"][1]};border-left:3px solid '
        f'{theme.GREEN5};background:{theme.TONES["ok"][2]};border-radius:4px;padding:14px 16px;">'
        f'<div style="color:{theme.GREEN5};font-size:16px;font-weight:700;">Queue empty</div>'
        f'<div style="color:{theme.GRAY3};font-size:14px;margin-top:4px;">'
        "Every review has been decided, or no CRITICAL alert has produced one."
        "</div></div>",
        unsafe_allow_html=True,
    )
else:
    _labels = {AWAITING: f"Awaiting decision ({len(_awaiting)})", MONITORING: f"Monitoring ({len(_monitored)})"}
    # The address names the list on every run, whatever pill this session held: a
    # held pill used to beat a link's ?show=, so the Monitoring tile could open the
    # empty Awaiting list. With no ?show=, the queue if anything awaits.
    _view_name = (MONITORING if _show == MONITORING or (not _awaiting and _show not in (AWAITING, "escalate"))
                  else AWAITING)
    st.session_state["q_show"] = _view_name
    st.segmented_control("Show", list(_labels), format_func=_labels.get, key="q_show",
                         label_visibility="collapsed", on_change=_choose_list)
    _shown = _monitored if _view_name == MONITORING else _awaiting

    if not _shown:
        st.info("Nothing is awaiting a decision." if _view_name == AWAITING else "Nothing is being monitored.")
    else:
        def _row(review: OpenReview) -> dict:
            a = review.action
            row = {
                "action_id": a.action_id,
                "Last reading (UTC)": a.last_seen_at or a.created_at,
                "Facility": a.airport_code or "—",
                "Alert": (a.alert_type or "—").removeprefix("ALERT_"),
                "Readings": a.occurrences,
                "AI suggests": review.suggests,
                "Confidence": round(100 * review.confidence) if review.confidence is not None else None,
                "AI summary": _summary(review),
            }
            if review.monitoring:
                return row | {"Monitoring since (UTC)": review.ticket.decided_at,
                              "By": review.ticket.decided_by or "—",
                              "Note": review.ticket.decision_note or "—"}
            return row | {"Waiting": waited(review.waiting_since)}

        _rows = pd.DataFrame([_row(r) for r in _shown]).sort_values("Last reading (UTC)", ascending=False)

        def _counted(column: str):
            return lambda value: f"{value} ({int((_rows[column] == value).sum())})"

        _suggestions = [s for s in (review_queue.ESCALATE, review_queue.NO_ESCALATION, review_queue.NO_AI_REVIEW)
                        if s in set(_rows["AI suggests"])]
        f1, f2, f3, f4 = st.columns([1.1, 1.6, 1.5, 0.9])
        facilities = f1.pills("Facility", sorted(_rows["Facility"].unique()), selection_mode="multi",
                              format_func=_counted("Facility"), key=f"q_facility_{_view_name}")
        suggests = f2.pills("AI suggests", _suggestions, selection_mode="multi",
                            format_func=_counted("AI suggests"), key=f"q_suggests_{_view_name}",
                            default=[review_queue.ESCALATE] if _show == "escalate" and _view_name == AWAITING
                            and review_queue.ESCALATE in _suggestions else None)
        alerts = f3.pills("Alert", sorted(_rows["Alert"].unique()), selection_mode="multi",
                          format_func=_counted("Alert"), key=f"q_alert_{_view_name}")
        confident = f4.toggle("AI ≥ 80%", key=f"q_confident_{_view_name}",
                              help="Only reviews the AI rated at 80% confidence or more")
        _keep = pd.Series(True, index=_rows.index)
        for column, chosen in (("Facility", facilities), ("AI suggests", suggests), ("Alert", alerts)):
            if chosen:
                _keep &= _rows[column].isin(chosen)
        if confident:
            _keep &= _rows["Confidence"].fillna(0) >= 80
        _view = _rows[_keep].reset_index(drop=True)

        _picked = st.dataframe(
            _view.drop(columns=["action_id"]), hide_index=True, use_container_width=True,
            # A selection is only meaningful for the rows it was made on: a decision
            # or a filter that changes them starts a fresh table (and selection).
            key="q_table_" + hashlib.sha256("|".join(_view["action_id"]).encode()).hexdigest()[:12],
            on_select="rerun", selection_mode="multi-row", height=min(38 + 35 * len(_view), 420),
            column_config={column: config for column, config in {
                "Last reading (UTC)": st.column_config.DatetimeColumn(format="MMM D HH:mm"),
                "Monitoring since (UTC)": st.column_config.DatetimeColumn(format="MMM D HH:mm"),
                "Readings": st.column_config.NumberColumn(format="%d"),
                "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d%%"),
                "AI summary": st.column_config.TextColumn(width="large"),
            }.items() if column in _view.columns},
        )
        _chosen = [_view.loc[i, "action_id"] for i in _picked.selection.rows if 0 <= i < len(_view)]

        a1, a2, a3, a4 = st.columns([1.2, 1.5, 2.1, 2.4])
        if a1.button("Open review", type="primary", use_container_width=True, disabled=len(_chosen) != 1,
                     help="Select one row: its investigation, evaluation, readings and decision"):
            _open_review(_chosen[0])
        if a2.button(f"Reject selected ({len(_chosen)})", use_container_width=True,
                     disabled=not _chosen or not _allowed(Decision.REJECT), help=_why_not(Decision.REJECT)):
            _confirm_bulk(Decision.REJECT, _chosen)
        if _view_name == AWAITING:
            if a3.button(f"Continue monitoring selected ({len(_chosen)})", use_container_width=True,
                         disabled=not _chosen or not _allowed(Decision.MONITOR), help=_why_not(Decision.MONITOR)):
                _confirm_bulk(Decision.MONITOR, _chosen)
            a4.caption("A decided or monitored review leaves this list. "
                       "Approving an escalation is one review at a time, from its detail.")
        else:
            a4.caption("Still open: approve, override or reject one from its detail.")

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
        # An operator's note or an actor name that starts with = + - @ would run as a formula in a spreadsheet.
        data=safe_text.csv_safe(audit_df).to_csv(index=False).encode("utf-8"),
        file_name="pct_decision_audit_trail.csv",
        mime="text/csv",
        help="For an incident record or an external review.",
    )

# ── The Critic's own activity: evidence about the AI, not a second queue ──────
# Counted per reading it assessed, decided or not, so these numbers are not the
# queue's: one review covers many readings, and most of these are long decided.
_critic_rows_all = run(storage.get_recent_critic_results(limit=50))
_cri_total = len(_critic_rows_all)

theme.section(
    "AI Critic activity",
    "Reading by reading. Advisory only: no verdict here has executed anything.",
    tone="neutral",
    right=f"last {_cri_total} assessed reading(s)" if _cri_total else "",
)

if not _critic_rows_all:
    st.info(
        "No Critic verdicts yet. Configure LLM_PROVIDER in .env and trigger a CRITICAL "
        "alert to populate this view."
    )
else:
    _confidences = [r["confidence"] for r in _critic_rows_all if r.get("confidence") is not None]
    st.caption(
        f"Of the last {_cri_total} readings the Critic assessed: "
        f"{sum(1 for r in _critic_rows_all if r.get('escalate'))} said escalate · "
        + (f"mean confidence {sum(_confidences) / len(_confidences):.0%} (model-reported) · " if _confidences else "")
        + f"{sum(1 for r in _critic_rows_all if r.get('pattern') == 'CASCADE_FAILURE')} cascade failure · "
        f"{sum(1 for r in _critic_rows_all if r.get('pattern') == 'ISOLATED_FAULT')} isolated fault."
    )
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
        alert_row = _alert_by_id.get(cr.get("alert_id"), {})
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
                '<span style="font-size:16px;font-weight:650;color:' + theme.LIGHT_GRAY5 + ';">'
                + theme._esc(alert_type) + "</span>"
                '<span class="mono" style="font-size:13px;color:' + theme.GRAY1 + ';">'
                + theme._esc(airport) + " · " + theme._esc(service) + "</span>"
                '<span class="chip" style="color:' + theme.TONES[_ptone][0]
                + ";border-color:" + theme.TONES[_ptone][1]
                + ';padding:2px 8px;font-size:13px;">' + theme._esc(pattern) + "</span>"
                '<span class="mono" style="font-size:13px;color:' + theme.GRAY1 + ';">'
                "confidence " + theme._esc(conf_str) + "</span>"
                + (
                    '<span class="chip" style="color:' + theme.RED5
                    + ";border-color:" + theme.TONES["bad"][1]
                    + ";background:" + theme.TONES["bad"][2]
                    + ';margin-left:auto;padding:2px 9px;font-size:13px;">'
                    "ESCALATE TO SUPERVISOR</span>"
                    if escalate else
                    '<span class="mono" style="margin-left:auto;font-size:13px;color:'
                    + theme.GRAY1 + ';">no escalation proposed</span>'
                )
                + "</div>",
                unsafe_allow_html=True,
            )
            st.markdown(safe_text.plain(reasoning) if reasoning else "_No reasoning recorded._")
            cits = parse_json_str_list(cr.get("citations", "[]"), "citations")
            if cits:
                st.markdown(theme.event_chips(cits, _cited_events), unsafe_allow_html=True)
            # The verdict is advisory; what a person acts on is the review covering its alert.
            covering = _covering.get(cr.get("alert_id"))
            if covering and covering["status"] == "PENDING" and covering["action_id"] in _by_id:
                if st.button("Review escalation" if escalate else "Open review", key=f"verdict_{cr.get('alert_id')}",
                             type="primary" if escalate else "secondary"):
                    _open_review(covering["action_id"])
            elif covering:
                st.caption(f"Review {covering['status'].lower()}"
                           + (f" by {covering['decided_by']}" if covering.get("decided_by") else "") + ".")
            else:
                st.caption("No review: only CRITICAL alerts are staged for a decision.")

theme.back_link()
