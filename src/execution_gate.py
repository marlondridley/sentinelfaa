"""
Transactional Execution Gate — Human-in-the-Loop Layer.

The gate is the *single* place where the system decides whether a CRITICAL alert
becomes a staged action awaiting human approval. It enforces three rules:

  1. CRITICAL alerts STAGE an action. They never auto-execute.
  2. Each staged action is idempotent — re-running the same alert creates ONE
     action, not many (driven by a deterministic idempotency_key).
  3. Only a human can move the action from PENDING to APPROVED or REJECTED.
     Both transitions write an immutable row in `action_audit_log`.

This file deliberately does NOT contain any "execute the action" code path.
The system's contract is: the AI can recommend; the human disposes.

Public API:
    stage_critical_alert(alert, critic_result=None)   -> StagedAction | None
    approve_staged_action(action_id, actor, note)      -> StagedAction | None
    reject_staged_action(action_id, actor, note)       -> StagedAction | None
    get_pending_staged_actions()                       -> list[StagedAction]
    get_recent_staged_actions(limit)                   -> list[StagedAction]
    get_action_audit_log(action_id=None, limit)        -> list[dict]
"""

from __future__ import annotations

import hashlib

from src import storage
from src.metrics import GATE_DECISIONS, HITL_QUEUE_DEPTH
from src.models import (
    AlertEvent,
    CriticResult,
    GateDecision,
    OperationalTicket,
    PlannerResult,
    StagedAction,
    WatchOfficerBrief,
)


# Re-export the storage-layer helpers so callers can use a single import path.
approve_staged_action = storage.approve_staged_action
reject_staged_action = storage.reject_staged_action
get_pending_staged_actions = storage.get_pending_staged_actions
get_recent_staged_actions = storage.get_recent_staged_actions
get_action_audit_log = storage.get_action_audit_log


def _idempotency_key(alert: AlertEvent) -> str:
    """
    Deterministic key based on the alert identity. Re-staging the same alert
    (e.g., on pipeline retry) returns the existing action, not a duplicate.
    """
    seed = (
        f"{alert.alert_id}|{alert.alert_type}|{alert.airport_code}|{alert.service_name}"
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _derive_recommended_action(alert: AlertEvent, critic: CriticResult | None) -> str:
    """
    Compose the operator-facing recommended action. If the Critic recommended
    escalation, lead with its language; otherwise produce a deterministic
    triage instruction tied to the specific alert type and airport.
    """
    airport = alert.airport_code or "PCT"
    service = alert.service_name or "unknown service"
    alert_kind = alert.alert_type or "CRITICAL alert"

    if critic and critic.escalate and critic.confidence is not None:
        return (
            f"Escalate to PCT TRACON supervisor in charge: "
            f"AI critic flags this {alert_kind} at {airport} ({service}) "
            f"with {critic.confidence:.0%} confidence as a "
            f"{critic.pattern.lower().replace('_', ' ')}. "
            f"Verify against STARS console and tower coordination before action."
        )
    # Deterministic fallback — keeps the gate working with no LLM.
    return (
        f"Review CRITICAL alert {alert_kind} at {airport}; "
        f"verify {service} feed status, latency, and related airport "
        f"conditions before escalation."
    )


def _derive_rationale(alert: AlertEvent, critic: CriticResult | None) -> str:
    """Compose the rationale text — what evidence supports staging this action."""
    if critic and critic.reasoning:
        return critic.reasoning
    return (
        f"Deterministic rule fired: {alert.alert_type}. "
        f"Description: {alert.description}"
    )


def stage_critical_alert(
    alert: AlertEvent,
    critic_result: CriticResult | None = None,
) -> StagedAction | None:
    """
    Stage a CRITICAL alert as a PENDING action awaiting human approval.

    - Returns the StagedAction (newly created OR pre-existing for the same idempotency_key).
    - Returns None and does nothing for non-CRITICAL alerts.
    - Never raises on duplicate; idempotency is enforced at the storage layer.

    The LLM is NEVER given the ability to set status=APPROVED or to execute
    the action. It can only contribute its critic_result to inform the
    rationale a human will then read.
    """
    if alert.severity != "CRITICAL":
        return None

    action = StagedAction(
        alert_id=alert.alert_id,
        event_id=alert.event_id,
        airport_code=alert.airport_code,
        service_name=alert.service_name,
        severity=alert.severity,
        alert_type=alert.alert_type,
        recommended_action=_derive_recommended_action(alert, critic_result),
        rationale=_derive_rationale(alert, critic_result),
        critic_confidence=critic_result.confidence if critic_result else None,
        critic_pattern=critic_result.pattern if critic_result else None,
        status="PENDING",
        idempotency_key=_idempotency_key(alert),
    )
    result = storage.create_staged_action(action)
    # Refresh the HITL queue gauge — count of currently-PENDING actions.
    try:
        HITL_QUEUE_DEPTH.set(len(storage.get_pending_staged_actions()))
    except Exception:
        pass

    # Build an Operational Ticket package that carries the Watch Officer
    # narrative + the staged action context into the Human Review Queue.
    # Imported lazily so watch_officer_agent's storage queries pick up the
    # newly-created staged action.
    try:
        _build_escalation_package(result, alert, critic_result)
    except Exception as exc:
        # Ticket creation is advisory — if it fails the action still stages.
        from src.logging_config import get_logger

        get_logger(__name__).warning(
            "ticket_creation_failed",
            extra={"action_id": result.action_id, "error": str(exc)},
        )

    return result


def _build_escalation_package(
    staged: StagedAction,
    alert: AlertEvent,
    critic_result: CriticResult | None,
) -> OperationalTicket | None:
    """
    Generate (or retrieve) the OperationalTicket attached to this staged action.
    The Watch Officer Brief becomes the ticket narrative.
    Idempotent on action_id.
    """
    # Lazy import to avoid circular dependency at module load
    from src.watch_officer_agent import (
        build_watch_officer_context,
        generate_watch_officer_brief,
    )

    idem = f"ticket:{staged.action_id}"
    existing = storage.get_ticket_by_action_id(staged.action_id)
    if existing:
        return existing

    # Brief: use LLM if available, otherwise deterministic
    context = build_watch_officer_context()
    brief = generate_watch_officer_brief(context, use_llm=True)
    storage.save_watch_officer_brief(brief)

    # Compose ticket from brief + alert + critic
    title = f"{alert.alert_type or 'CRITICAL'} at {alert.airport_code or 'PCT'}"
    summary = brief.headline + (
        f" Critic: {critic_result.reasoning[:160]}"
        if critic_result and critic_result.reasoning
        else ""
    )

    ticket = OperationalTicket(
        action_id=staged.action_id,
        alert_id=staged.alert_id,
        brief_id=brief.brief_id,
        title=title,
        severity=staged.severity,
        affected_airport=staged.airport_code,
        affected_service=staged.service_name,
        posture=brief.posture,
        trust_assessment=brief.trust_assessment,
        summary=summary,
        what_changed=brief.what_changed,
        why_it_matters=brief.why_it_matters,
        evidence=brief.evidence,
        recommended_review=brief.recommended_human_review,
        critic_rationale=critic_result.reasoning if critic_result else None,
        status="DRAFT",
        created_by_agent="watch_officer",
        idempotency_key=idem,
    )
    return storage.create_operational_ticket(ticket)


# ── Legacy compatibility shim ─────────────────────────────────────────────────
# The previous gate implementation derived a risk tier from pattern + confidence
# + alert count and produced a GateDecision. Some tests and the agent_evaluator
# still call that API. The new HITL system is simpler — CRITICAL stages, WARNING
# doesn't — but we keep the old surface working to avoid breaking eval scenarios.

_PATTERN_BASE_TIER: dict[str, str] = {
    "CASCADE_FAILURE": "CRITICAL",
    "ISOLATED_FAULT": "HIGH",
    "WEATHER_CORRELATED": "HIGH",
    "DATA_ANOMALY": "MEDIUM",
    "TRANSIENT": "LOW",
    "UNKNOWN": "MEDIUM",
}


def _derive_tier(
    pattern: str,
    confidence: float | None,
    escalate: bool,
    critical_alert_count: int,
) -> tuple[str, list[str]]:
    """Legacy risk-tier derivation. Kept for backward-compat with agent_evaluator."""
    factors: list[str] = []
    tier = _PATTERN_BASE_TIER.get(pattern, "MEDIUM")
    if escalate:
        factors.append(f"escalation_flag=True (pattern={pattern})")
        if tier in ("MEDIUM", "LOW"):
            tier = "HIGH"
    if critical_alert_count > 0:
        factors.append(f"critical_alert_count={critical_alert_count}")
        if tier == "MEDIUM":
            tier = "HIGH"
        elif tier == "LOW":
            tier = "MEDIUM"
    if confidence is not None and confidence < 0.5:
        factors.append(f"low_confidence={confidence:.2f} (uncertainty penalty)")
        if tier == "MEDIUM":
            tier = "HIGH"
        elif tier == "LOW":
            tier = "MEDIUM"
    if not factors:
        factors.append(f"pattern={pattern} confidence={confidence}")
    return tier, factors


class ExecutionGate:
    """
    Legacy shim. Derives a risk tier from a PlannerResult + optional CriticResult.
    Returns a GateDecision and stages an HITL ticket for CRITICAL / HIGH tiers
    via the new staged_actions table.

    Prefer stage_critical_alert() for new code. This class exists so the existing
    agent_evaluator eval scenarios continue to work.
    """

    def evaluate(
        self,
        planner_result: PlannerResult,
        critic_result: CriticResult | None = None,
        idempotency_key: str | None = None,
        critical_alert_count: int = 0,
        has_warning_alerts: bool = False,  # accepted but not used; kept for back-compat
    ) -> GateDecision:
        if critic_result:
            pattern = critic_result.pattern
            escalate = bool(critic_result.escalate)
            confidence = (
                critic_result.confidence
                if critic_result.confidence is not None
                else planner_result.confidence
            )
        else:
            escalate = False
            confidence = planner_result.confidence
            if critical_alert_count >= 3:
                pattern = "CASCADE_FAILURE"
            elif critical_alert_count > 0:
                pattern = "ISOLATED_FAULT"
            else:
                pattern = "TRANSIENT"

        tier, factors = _derive_tier(
            pattern, confidence, escalate, critical_alert_count
        )
        idem_key = idempotency_key or (
            (planner_result.question or "")[:64]
            + (critic_result.alert_id if critic_result else "")
        )
        blocked = tier in ("CRITICAL", "HIGH")
        ticket_id = None
        if blocked:
            # Stage via the new system using a synthetic AlertEvent so the
            # HITL queue + audit trail reflect the legacy eval too.
            synthetic = AlertEvent(
                alert_id=idem_key,
                alert_type=f"LEGACY_{pattern}",
                severity="CRITICAL",
                airport_code=critic_result.alert_id.split("-")[0]
                if critic_result
                else None,
                service_name=None,
                description=f"Legacy gate evaluation: tier={tier} factors={factors}",
                event_id=None,
            )
            staged = stage_critical_alert(synthetic, critic_result)
            if staged:
                ticket_id = f"HITL-{staged.action_id[:8].upper()}"
        return GateDecision(
            allowed=not blocked,
            risk_tier=tier,
            hitl_required=blocked,
            idempotency_key=idem_key,
            risk_factors=factors,
            ticket_id=ticket_id,
        )


def get_staged_actions(status: str = "PENDING") -> list[dict]:
    """Legacy alias — returns staged_actions rows as plain dicts."""
    conn = storage._get_conn()
    rows = conn.execute(
        "SELECT * FROM staged_actions WHERE status = ? ORDER BY created_at DESC",
        (status,),
    ).fetchall()
    return [
        {**dict(r), "ticket_id": f"HITL-{r['action_id'][:8].upper()}"} for r in rows
    ]
