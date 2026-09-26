"""
Transactional Execution Gate — Human-in-the-Loop Layer.

The Execution Gate is the single deterministic control-plane boundary that
decides whether a CRITICAL alert becomes a staged human-review action.

Architectural contract:

    CRITICAL ALERT
          |
          v
    EXECUTION GATE
          |
          +---- PENDING staged_action
          |
          +---- audit record
                    |
                    v
              WATCH OFFICER
              advisory only
                    |
                    v
            OPERATIONAL TICKET
                    |
                    v
              HUMAN REVIEW
              /    |     \
             /     |      \
       APPROVE  OVERRIDE  REJECT
                (edit text/
                 severity,
                 then sign off)

The gate NEVER:
    - auto-executes an operational action, whether APPROVED or OVERRIDDEN
    - allows an LLM to approve, override, or reject an action
    - allows an LLM to determine the CRITICAL/non-CRITICAL boundary
    - treats an AI recommendation as human authorization

The Watch Officer is advisory and downstream of staging.

Public API:
    stage_critical_alert(alert, critic_result=None)
        -> StagedAction | None

    approve_staged_action(action_id, actor, note)
        -> StagedAction | None

    reject_staged_action(action_id, actor, note)
        -> StagedAction | None

    override_staged_action(action_id, overridden_recommendation, overridden_severity, actor, note)
        -> StagedAction | None
        Human-edited sign-off: the operator changes the AI's recommendation
        text and/or severity before approving. Still purely advisory/record-
        keeping -- see the module docstring's "gate NEVER" list below. No
        automated execution is triggered by this any more than by approve/reject.

    get_pending_staged_actions()
        -> list[StagedAction]

    get_recent_staged_actions(limit)
        -> list[StagedAction]

    get_action_audit_log(action_id=None, limit=None)
        -> list[dict]
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from src import storage
from src.agent_tools import guard_human_gate
from src.auth import Decision, Principal, authorize, format_actor
from src.config import settings
from src.logging_config import get_logger
from src.metrics import HITL_QUEUE_DEPTH
from src.models import (
    AlertEvent,
    CriticResult,
    OperationalTicket,
    StagedAction,
)

# Re-export storage-layer HITL operations through the gate's public API.
approve_staged_action = storage.approve_staged_action
reject_staged_action = storage.reject_staged_action
override_staged_action = storage.override_staged_action
get_pending_staged_actions = storage.get_pending_staged_actions
get_recent_staged_actions = storage.get_recent_staged_actions
get_action_audit_log = storage.get_action_audit_log

_log = get_logger(__name__)

def _strict_bool(value: Any) -> bool:
    """
    Parse boolean-like values without the bool("false") trap.

    bool("false") == True in Python, which is unsafe for escalation logic.
    """

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in {
            "true",
            "1",
            "yes",
            "y",
        }

    if isinstance(value, (int, float)):
        return value != 0

    return False


def condition_key(alert: AlertEvent) -> str:
    """
    The condition a CRITICAL alert reports: alert type, airport, service.
    One review covers a condition while it keeps firing (see
    CONDITION_QUIET_MINUTES). Matches the migration's backfill,
    concat_ws('|', alert_type, airport_code, service_name).
    """
    return "|".join(str(v) for v in (alert.alert_type, alert.airport_code, alert.service_name) if v is not None)


def _idempotency_key(alert: AlertEvent) -> str:
    """
    Generate a deterministic identity for a staged alert.

    Reprocessing the same alert produces the same key and therefore cannot
    create multiple staged actions.
    """

    seed = "|".join(
        [
            str(alert.alert_id or ""),
            str(alert.alert_type or ""),
            str(alert.airport_code or ""),
            str(alert.service_name or ""),
        ]
    )

    return hashlib.sha256(
        seed.encode("utf-8")
    ).hexdigest()[:24]


def _derive_recommended_action(
    alert: AlertEvent,
    critic: CriticResult | None,
) -> str:
    """
    Produce an operator-facing recommendation.

    Critic content is advisory only. It does not determine whether the
    action is staged.

    The CRITICAL determination has already happened before this function
    is called.
    """

    airport = alert.airport_code or "PCT"
    service = alert.service_name or "unknown service"
    alert_kind = alert.alert_type or "CRITICAL alert"

    critic_escalates = (
        _strict_bool(critic.escalate)
        if critic is not None
        else False
    )

    if critic_escalates and critic.confidence is not None:
        pattern = (
            critic.pattern
            or "UNKNOWN"
        ).lower().replace("_", " ")

        return (
            f"Review escalation recommendation for "
            f"{alert_kind} at {airport} ({service}). "
            f"AI critic classified the pattern as {pattern} "
            f"with {critic.confidence:.0%} confidence. "
            "Verify against authoritative operational systems "
            "before any human disposition."
        )

    return (
        f"Review CRITICAL alert {alert_kind} at {airport}; "
        f"verify {service} feed status, latency, and related "
        "airport conditions before human disposition."
    )


def _derive_rationale(
    alert: AlertEvent,
    critic: CriticResult | None,
) -> str:
    """
    Build rationale while keeping deterministic alert identity authoritative.

    AI reasoning is explicitly labeled as advisory.
    """

    deterministic = (
        f"Deterministic CRITICAL rule fired: "
        f"{alert.alert_type}. "
        f"Description: {alert.description}"
    )

    if critic and critic.reasoning:
        return (
            deterministic
            + " "
            + "AI advisory reasoning: "
            + critic.reasoning
        )

    return deterministic


async def _refresh_hitl_queue_metric() -> None:
    """
    Refresh the current pending-action gauge.

    Observability failure must never break the transactional gate.
    """

    try:
        pending = await storage.get_pending_staged_actions()
        HITL_QUEUE_DEPTH.set(len(pending))
    except Exception as exc:  # noqa: BLE001
        _log.warning("hitl_queue_metric_refresh_failed", extra={"error": str(exc)})
        return


async def stage_critical_alert(
    alert: AlertEvent,
    critic_result: CriticResult | None = None,
) -> StagedAction | None:
    """
    Stage a CRITICAL alert for human review.

    Deterministic rule:

        severity != CRITICAL -> no staged action
        severity == CRITICAL -> covered by a PENDING review of its condition

    One review per condition, not per reading: an alert whose condition
    (condition_key) fired within CONDITION_QUIET_MINUTES joins that review;
    otherwise it opens a new one. The critic/LLM cannot change the rule.

    Returns the review only when this call opened it -- the caller builds its
    ticket and investigation once -- and None when the alert is not CRITICAL,
    joined an open review, or was already staged.

    No execution occurs here.
    """
    # Agents recommend; the rules stage. Code running as an agent is refused
    # by the tool-use policy here (src/agent_tools.py).
    guard_human_gate("stage_for_human_review")

    # This is the primary execution-control boundary.
    if alert.severity != "CRITICAL":
        return None

    idempotency_key = _idempotency_key(alert)

    action = StagedAction(
        alert_id=alert.alert_id,
        event_id=alert.event_id,
        airport_code=alert.airport_code,
        service_name=alert.service_name,
        severity=alert.severity,
        alert_type=alert.alert_type,

        recommended_action=_derive_recommended_action(
            alert,
            critic_result,
        ),

        rationale=_derive_rationale(
            alert,
            critic_result,
        ),

        critic_confidence=(
            critic_result.confidence
            if critic_result
            else None
        ),

        critic_pattern=(
            critic_result.pattern
            if critic_result
            else None
        ),

        # The only valid initial state.
        status="PENDING",

        idempotency_key=idempotency_key,
        condition_key=condition_key(alert),
    )

    # Storage opens the review, or returns the one already covering this
    # alert's condition (or this alert).
    result = await storage.create_staged_action(
        action, condition_window=timedelta(minutes=settings.CONDITION_QUIET_MINUTES))
    if result.action_id != action.action_id:
        return None

    await _refresh_hitl_queue_metric()

    # Advisory ticket generation happens AFTER the action has been staged.
    #
    # If Watch Officer fails, the transactional HITL action remains intact.
    try:
        await _build_escalation_package(
            result,
            alert,
            critic_result,
        )

    except Exception as exc:  # noqa: BLE001

        get_logger(__name__).warning(
            "ticket_creation_failed",
            extra={
                "action_id": result.action_id,
                "alert_id": alert.alert_id,
                "error": str(exc),
            },
        )

    return result


async def _build_escalation_package(
    staged: StagedAction,
    alert: AlertEvent,
    critic_result: CriticResult | None,
) -> OperationalTicket | None:
    """
    Build the advisory Operational Ticket after staging.

    Important ordering:

        1. Execution Gate stages PENDING action.
        2. Watch Officer creates advisory narrative.
        3. Operational Ticket packages the narrative.
        4. Human reviews.

    Watch Officer failure never reverses or bypasses the staged action.
    """

    from src.watch_officer_agent import (
        agenerate_watch_officer_brief,
        build_watch_officer_context,
    )

    existing = await storage.get_ticket_by_action_id(
        staged.action_id
    )

    if existing:
        return existing

    # Explicitly bind the Watch Officer context to the alert that caused
    # this staged action.
    context = await build_watch_officer_context(
        alert=alert,
    )

    # Rule-based, deliberately. This runs inside staging for every CRITICAL
    # alert, so a model call here was unbudgeted and serial: a burst of 15
    # alerts meant 15 model round-trips (with provider fallback and retries)
    # before the pass finished. The model-written narrative now comes from
    # the investigation, which runs after staging under
    # MAX_LLM_INVESTIGATIONS_PER_PASS; staging itself never waits on a model.
    brief = await agenerate_watch_officer_brief(
        context,
        use_llm=False,
    )

    await storage.save_watch_officer_brief(
        brief
    )

    title = (
        f"{alert.alert_type or 'CRITICAL'} "
        f"at {alert.airport_code or 'PCT'}"
    )

    critic_summary = ""

    if (
        critic_result
        and critic_result.reasoning
    ):
        critic_summary = (
            " AI critic advisory: "
            + critic_result.reasoning[:160]
        )

    summary = (
        brief.headline
        + critic_summary
    )

    ticket = OperationalTicket(
        action_id=staged.action_id,
        alert_id=staged.alert_id,
        brief_id=brief.brief_id,

        title=title,

        severity=staged.severity,
        affected_airport=staged.airport_code,
        affected_service=staged.service_name,

        # These are deterministic/guardrailed values from Watch Officer.
        posture=brief.posture,
        trust_assessment=brief.trust_assessment,

        summary=summary,
        what_changed=brief.what_changed,
        why_it_matters=brief.why_it_matters,
        evidence=brief.evidence,
        recommended_review=brief.recommended_human_review,

        critic_rationale=(
            critic_result.reasoning
            if critic_result
            else None
        ),

        # Ticket itself remains advisory/draft.
        status="DRAFT",

        created_by_agent="watch_officer",

        idempotency_key=(
            f"ticket:{staged.action_id}"
        ),
    )

    return await storage.create_operational_ticket(
        ticket
    )


# (staged action status, ticket status) each decision records.
_DECISION_STATUSES: dict[Decision, tuple[str, str]] = {
    Decision.APPROVE: ("APPROVED", "APPROVED_FOR_ESCALATION"),
    Decision.REJECT: ("REJECTED", "REJECTED"),
    Decision.OVERRIDE: ("OVERRIDDEN", "OVERRIDDEN"),
}


async def decide_staged_action(
    action_id: str,
    decision: Decision,
    principal: Principal,
    note: str = "",
    *,
    overridden_recommendation: str | None = None,
    overridden_severity: str | None = None,
) -> StagedAction | None:
    """
    The one entry point for a human disposition of a staged action.

    Authorizes first (src/auth.authorize: verified sign-in + realm role),
    then records the decision under format_actor(principal), then moves the
    linked ticket. Raises AuthorizationError without writing anything if the
    principal may not decide. MONITOR leaves the action PENDING and only
    marks the ticket.

    Before any of that, code running as an agent is refused by the tool-use
    policy (src/agent_tools.py): whatever principal it presents, an agent
    never decides.
    """
    guard_human_gate("decide_staged_action")
    try:
        authorize(principal, decision, allow_unverified=settings.ALLOW_UNVERIFIED_DECISIONS)
    except Exception:
        _log.warning("staged_action_decision_denied", extra={
            "action_id": action_id, "decision": decision.value,
            "principal_id": principal.principal_id, "source": principal.source.value,
            "roles": sorted(principal.roles),
        })
        raise

    actor = format_actor(principal)
    ticket = await storage.get_ticket_by_action_id(action_id)
    ticket_id = ticket.ticket_id if ticket else None
    result: StagedAction | None = None

    if decision == Decision.MONITOR:
        if ticket_id:
            await storage.mark_operational_ticket_monitoring(ticket_id, actor=actor, note=note)
    else:
        new_status, ticket_status = _DECISION_STATUSES[decision]
        result = await storage.decide_staged_action_with_ticket(
            action_id, ticket_id, new_status, ticket_status, actor, note,
            overridden_recommendation=overridden_recommendation,
            overridden_severity=overridden_severity,
        )

    await _refresh_hitl_queue_metric()
    _log.info("staged_action_decided", extra={
        "action_id": action_id, "decision": decision.value, "actor": actor,
    })
    return result