"""
AI Critic & Human Review page: Critic verdicts, the HITL execution-gate
review queue, and the decision audit trail.

Answers mission question 7 from the Overview page's mission panel: what
should the operator do next, and turns that into an actionable,
attributed approve/monitor/reject workflow. Split out of the original
single-file dashboard's "AI Critic Intelligence", "Human Review Queue
(HITL Execution Gate)", and "Decision Audit Trail" sections.
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

from lib.data import format_ts, parse_json_str_list
from lib.page_setup import bootstrap_page
from src import storage
from src.async_bridge import run_async as run
from src.execution_gate import (
    approve_staged_action as gate_approve,
)
from src.execution_gate import (
    get_action_audit_log,
    get_pending_staged_actions,
    get_recent_staged_actions,
)
from src.execution_gate import (
    reject_staged_action as gate_reject,
)
from src.execution_gate import (
    override_staged_action as gate_override,
)
from src.storage import (
    approve_operational_ticket,
    get_ticket_by_action_id,
    mark_operational_ticket_monitoring,
    mark_operational_ticket_overridden,
    reject_operational_ticket,
)

bootstrap_page(page_title="AI Critic & Review — PCT FeedOps Sentinel", page_icon="🤖")

alerts_raw = run(storage.get_recent_alerts(limit=500))

# ── AI Critic Intelligence ───────────────────────────────────────────────────
st.title("🤖 AI Critic — *Should we escalate?*")
st.caption(
    "**Answers:** *Which CRITICAL alerts are genuinely safety-significant vs which are data artifacts?*"
)

_critic_rows_all = run(storage.get_recent_critic_results(limit=50))

_cri_total = len(_critic_rows_all)
_cri_escalate = sum(1 for r in _critic_rows_all if r.get("escalate"))
_cri_cascade = sum(1 for r in _critic_rows_all if r.get("pattern") == "CASCADE_FAILURE")
_cri_isolated = sum(1 for r in _critic_rows_all if r.get("pattern") == "ISOLATED_FAULT")
_cri_avg_conf = (sum(r.get("confidence", 0) or 0 for r in _critic_rows_all) / _cri_total) if _cri_total else None

ck1, ck2, ck3, ck4, ck5 = st.columns(5)
ck1.metric("Alerts Evaluated", f"{_cri_total:,}")
ck2.metric("🔺 Escalations Suggested", _cri_escalate, delta_color="inverse" if _cri_escalate else "off")
ck3.metric("Mean Confidence", f"{_cri_avg_conf:.0%}" if _cri_avg_conf is not None else "—")
ck4.metric("Cascade Failures", _cri_cascade, delta_color="inverse" if _cri_cascade else "off")
ck5.metric("Isolated Faults", _cri_isolated)

if not _critic_rows_all:
    st.info("No Critic verdicts yet. Configure `LLM_PROVIDER` and trigger a CRITICAL alert to populate.")
else:
    _alert_id_to_row = {a.get("alert_id"): a for a in alerts_raw}
    _SEV_PATTERN_COLOR = {
        "CASCADE_FAILURE":    "red",
        "ISOLATED_FAULT":     "orange",
        "WEATHER_CORRELATED": "orange",
        "DATA_ANOMALY":       "blue",
        "TRANSIENT":          "grey",
        "UNKNOWN":            "grey",
    }
    for cr in _critic_rows_all[:10]:
        alert_row = _alert_id_to_row.get(cr.get("alert_id"), {})
        conf = cr.get("confidence")
        conf_str = f"{conf:.0%}" if conf is not None else "N/A"
        conf_color = "red" if (conf or 0) >= 0.7 else ("orange" if (conf or 0) >= 0.5 else "grey")
        pattern = cr.get("pattern", "UNKNOWN")
        pattern_color = _SEV_PATTERN_COLOR.get(pattern, "grey")
        escalate = bool(cr.get("escalate"))
        escalate_badge = "🔺 ESCALATE TO SUPERVISOR" if escalate else "—"
        reasoning = cr.get("reasoning", "")
        alert_type = alert_row.get("alert_type", "—")
        airport = alert_row.get("airport_code", "—")
        service = alert_row.get("service_name", "—")

        with st.container(border=True):
            h1, h2, h3, h4 = st.columns([3, 1, 2, 2])
            h1.markdown(f"**{alert_type}** · `{airport}` · `{service}`")
            h2.markdown(f"confidence: :{conf_color}[**{conf_str}**]")
            h3.markdown(f"pattern: :{pattern_color}[**{pattern}**]")
            h4.markdown(f":red[**{escalate_badge}**]" if escalate else escalate_badge)
            st.markdown(reasoning if reasoning else "_No reasoning recorded._")
            cits_raw = cr.get("citations", "[]")
            cits = parse_json_str_list(cits_raw, "citations")
            if cits:
                st.caption(f"Evidence event_ids: `{', '.join(cits[:6])}`")

st.divider()

# ── Human Review Queue (HITL Execution Gate) ─────────────────────────────────
st.subheader("🧑‍✈️ Human Review Queue — *What needs operator approval?*")
st.caption(
    "**Answers:** *Which CRITICAL alerts are staged as PENDING actions that cannot execute until a human approves them?*"
)

_pending_actions = run(get_pending_staged_actions())

if not _pending_actions:
    st.success("✅ **No pending actions.** All staged actions have been reviewed or no CRITICAL alerts are active.")
else:
    st.warning(f"⚠️ **{len(_pending_actions)} staged action(s) await operator approval.**")
    for _action in _pending_actions:
        _ticket = run(get_ticket_by_action_id(_action.action_id))
        with st.container(border=True):
            title = (_ticket.title if _ticket else f"{_action.alert_type} at {_action.airport_code or '—'}")
            h1, h2, h3 = st.columns([3, 1, 2])
            h1.markdown(f"### {title}")
            conf_str = f"{_action.critic_confidence:.0%}" if _action.critic_confidence is not None else "—"
            h2.markdown(f"AI conf: **{conf_str}**")
            posture = _ticket.posture if _ticket else "ACTION"
            posture_color = {"ACTION": "red", "ATTENTION": "orange", "NORMAL": "blue", "GOOD": "green"}.get(posture, "grey")
            h3.markdown(f"posture: :{posture_color}[**{posture}**]")

            if _ticket and _ticket.summary:
                st.markdown(f"**Summary:** {_ticket.summary}")
            else:
                st.markdown(f"**Recommended action:** {_action.recommended_action}")

            if _ticket:
                pc1, pc2 = st.columns(2)
                with pc1:
                    if _ticket.what_changed:
                        st.markdown("**📋 What changed**")
                        for it in _ticket.what_changed[:4]:
                            st.markdown(f"- {it}")
                    if _ticket.recommended_review:
                        st.markdown("**👀 Recommended review**")
                        for it in _ticket.recommended_review[:4]:
                            st.markdown(f"- {it}")
                with pc2:
                    if _ticket.why_it_matters:
                        st.markdown("**🎯 Why it matters**")
                        for it in _ticket.why_it_matters[:4]:
                            st.markdown(f"- {it}")
                    if _ticket.evidence:
                        st.markdown("**🔬 Evidence**")
                        for it in _ticket.evidence[:4]:
                            st.markdown(f"- {it}")

                if _ticket.critic_rationale:
                    with st.expander("🤖 AI Critic rationale"):
                        st.markdown(_ticket.critic_rationale)

                st.caption(
                    f"ticket_id: `{_ticket.ticket_id[:18]}` · "
                    f"action_id: `{_action.action_id[:18]}` · "
                    f"brief_id: `{(_ticket.brief_id or '')[:18]}` · "
                    f"status: `{_ticket.status}` · "
                    f"drafted: {_ticket.generated_at.strftime('%H:%M:%S')} UTC"
                )

            # Set in the sidebar identity block (bootstrap_page(), which every page
            # calls), so every decision recorded here carries the same actor string
            # -- including whether the identity was OIDC-verified or self-declared --
            # as the sidebar banner the operator actually saw when they started this
            # session, regardless of which page they were on when they signed in.
            _active_actor = st.session_state.get(
                "operator_principal_actor",
                f"{st.session_state.get('operator_id', 'watch_officer_primary')}"
                f" ({st.session_state.get('operator_role', 'Watch Officer')}) [self-declared]",
            )
            st.caption(f"🛡 Decisions will be signed & attributed in audit log as: **{_active_actor}**")

            note_key = f"note_{_action.action_id}"
            ap_key = f"approve_{_action.action_id}"
            mo_key = f"monitor_{_action.action_id}"
            rj_key = f"reject_{_action.action_id}"

            note = st.text_input(
                "Decision note (will be recorded in audit trail)",
                key=note_key,
                placeholder="e.g., verified vs STARS console, contacting KDCA tower",
            )

            # Override: the operator edits the AI's recommendation text and/or
            # severity before signing off, instead of accepting it verbatim (Approve)
            # or discarding it (Reject). Still purely advisory/record-keeping --
            # override_staged_action() never triggers any automated execution any
            # more than approve/reject do; it only changes what gets written to the
            # audit trail as the human-authorized recommendation.
            _sev_options = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
            with st.expander("\u270f\ufe0f Edit & override before sign-off"):
                override_text_key = f"override_text_{_action.action_id}"
                override_sev_key = f"override_sev_{_action.action_id}"
                ov_key = f"override_{_action.action_id}"

                edited_recommendation = st.text_area(
                    "Recommended action (editable)",
                    value=_action.recommended_action,
                    key=override_text_key,
                    help="The AI's original recommendation is preserved unchanged in the audit trail; "
                    "this is what you're replacing it with.",
                )
                _sev_default = _action.severity if _action.severity in _sev_options else "CRITICAL"
                edited_severity = st.selectbox(
                    "Severity (editable)",
                    _sev_options,
                    index=_sev_options.index(_sev_default),
                    key=override_sev_key,
                )

                if st.button("\u270f\ufe0f Override & Sign Off", key=ov_key, use_container_width=True):
                    run(gate_override(
                        _action.action_id,
                        overridden_recommendation=edited_recommendation,
                        overridden_severity=edited_severity,
                        actor=_active_actor,
                        note=note or "",
                    ))
                    if _ticket:
                        run(mark_operational_ticket_overridden(_ticket.ticket_id, actor=_active_actor, note=note or ""))
                    st.toast(f"Action {_action.action_id[:8]} OVERRIDDEN by {_active_actor}", icon="\u270f\ufe0f")
                    st.rerun()

            b1, b2, b3, _sp = st.columns([1.4, 1.4, 1.4, 2])

            if b1.button("✅ Approve Escalation", key=ap_key, type="primary", use_container_width=True):
                run(gate_approve(_action.action_id, actor=_active_actor, note=note or ""))
                if _ticket:
                    run(approve_operational_ticket(_ticket.ticket_id, actor=_active_actor, note=note or ""))
                st.toast(f"Escalation APPROVED for {_action.action_id[:8]} by {_active_actor}", icon="✅")
                st.rerun()

            if b2.button("👁 Continue Monitoring", key=mo_key, use_container_width=True,
                         help="Keep staged action PENDING; mark ticket MONITORING"):
                if _ticket:
                    run(mark_operational_ticket_monitoring(_ticket.ticket_id, actor=_active_actor, note=note or ""))
                    st.toast(f"Ticket {_ticket.ticket_id[:8]} -> MONITORING by {_active_actor}", icon="👁")
                else:
                    st.warning("No ticket linked to this action; cannot mark monitoring.")
                st.rerun()

            if b3.button("❌ Reject", key=rj_key, use_container_width=True):
                run(gate_reject(_action.action_id, actor=_active_actor, note=note or ""))
                if _ticket:
                    run(reject_operational_ticket(_ticket.ticket_id, actor=_active_actor, note=note or ""))
                st.toast(f"Action {_action.action_id[:8]} REJECTED by {_active_actor}", icon="❌")
                st.rerun()

# ── Decision Audit Trail ──────────────────────────────────────────────────────
st.subheader("🗂 Decision Audit Trail")
st.caption(
    "**Answers:** *Who approved or rejected which action, when, and why?* "
    "Immutable append-only log sourced from `action_audit_log`."
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
            "Decided (UTC)":   format_ts(r.get("created_at")).replace("T", " "),
            "Action":          (r.get("action_id") or "")[:18] + "…",
            "Airport":         act.airport_code if act else "—",
            "Alert Type":      act.alert_type if act else "—",
            "From → To":       f"{r.get('previous_status','?')} → {r.get('new_status','?')}",
            "Actor":           r.get("actor", "—"),
            "Note":            r.get("note", "") or "—",
            # Only populated for OVERRIDDEN rows -- shows what the operator
            # actually signed off on in place of the AI's original recommended_action,
            # which stays untouched on the staged_actions row for comparison.
            "Overridden To":   (
                (act.overridden_recommendation or "—")
                if act and r.get("new_status") == "OVERRIDDEN"
                else "—"
            ),
        })
    st.dataframe(
        pd.DataFrame(audit_display),
        hide_index=True,
        use_container_width=True,
        height=240,
    )

st.divider()
st.page_link("app.py", label="◀ Back to Overview", icon="✈")
