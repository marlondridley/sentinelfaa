"""
Escalation-integrity reconciler: no CRITICAL alert goes unstaged.

The pipeline stages every CRITICAL alert it produces, but "the code path
does it" is not a control -- it is how 84 CRITICAL alerts ended up with no
staged action while a divergent copy of the pipeline ran. This job checks the
invariant directly against the database and repairs it:

  * An unstaged CRITICAL alert inside RECONCILE_STAGE_WINDOW_MINUTES is staged
    PENDING through the normal gate (ticket included) and investigated on
    rules, so it reaches the review queue like any other.
  * An older one is recorded as EXPIRED -- a staged action that says, in the
    audit trail, that the alert was never put in front of a reviewer in time.
    Surfacing a months-old alert as PENDING would bury today's queue; leaving
    it unrecorded would hide the gap. EXPIRED records it without doing either.

It also exports the invariant as metrics (nas_critical_alerts_unstaged,
nas_hitl_oldest_pending_age_seconds) for the alert rules.

    python -m src.reconcile          # one pass, prints a summary
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src import storage
from src.config import settings
from src.execution_gate import _idempotency_key, stage_critical_alert
from src.logging_config import get_logger
from src.metrics import CRITICAL_ALERTS_UNSTAGED, HITL_OLDEST_PENDING_SECONDS, HITL_QUEUE_DEPTH, RECONCILED_ALERTS
from src.models import AlertEvent, StagedAction

_log = get_logger(__name__)

_UNSTAGED_SQL = """
    SELECT a.alert_id, a.alert_type, a.severity, a.airport_code, a.service_name,
           a.description, a.triggered_at, a.event_id, a.data_origin
    FROM alert_events a
    LEFT JOIN staged_action_alerts x ON x.alert_id = a.alert_id
    WHERE a.severity = 'CRITICAL' AND x.alert_id IS NULL
    ORDER BY a.triggered_at
    LIMIT $1
"""


async def reconcile_unstaged(*, limit: int = 1000) -> dict[str, int]:
    from src.investigation import investigate_staged
    from src.storage.pool import get_db_pool

    pool = await get_db_pool()
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=settings.RECONCILE_STAGE_WINDOW_MINUTES)
    staged, expired = [], 0

    for row in await pool.fetch(_UNSTAGED_SQL, limit):
        alert = AlertEvent(**dict(row))
        triggered = alert.triggered_at if alert.triggered_at.tzinfo else alert.triggered_at.replace(tzinfo=timezone.utc)
        if now - triggered <= window:
            action = await stage_critical_alert(alert)
            if action is not None:
                staged.append((action, alert))
                RECONCILED_ALERTS.labels(outcome="staged_pending").inc()
        else:
            await storage.create_staged_action(
                StagedAction(
                    alert_id=alert.alert_id, event_id=alert.event_id, airport_code=alert.airport_code,
                    service_name=alert.service_name, severity=alert.severity, alert_type=alert.alert_type,
                    recommended_action="None: the alert was never reviewed and is past the staging window.",
                    rationale=f"Deterministic CRITICAL rule fired: {alert.alert_type}. {alert.description}",
                    status="EXPIRED", idempotency_key=_idempotency_key(alert),
                    decided_at=now, decided_by="system",
                    decision_note="Recorded by the reconciler: never staged for review in time.",
                ),
                note=(f"reconciler: CRITICAL alert from {triggered.isoformat()} was never staged; "
                      f"past the {settings.RECONCILE_STAGE_WINDOW_MINUTES}-min window, recorded EXPIRED"),
            )
            expired += 1
            RECONCILED_ALERTS.labels(outcome="recorded_expired").inc()

    if staged:
        try:
            await investigate_staged(staged, llm_budget=0)
        except Exception as exc:  # noqa: BLE001 -- advisory; the staging above stands
            _log.error("reconcile_investigation_failed", extra={"error": str(exc)})

    # Reviews staged before one-review-per-condition fold into one per condition.
    merged = await storage.merge_pending_duplicates(timedelta(minutes=settings.CONDITION_QUIET_MINUTES))

    remaining = len(await pool.fetch(_UNSTAGED_SQL, limit))
    CRITICAL_ALERTS_UNSTAGED.set(remaining)
    # Both review-queue gauges from one read, on every pass. The depth was set
    # only where the queue was listed, so the worker reported 0 beside a
    # 20-hour-old pending review.
    pending = await storage.get_pending_staged_actions()
    oldest = min((p.created_at for p in pending), default=None)
    HITL_QUEUE_DEPTH.set(len(pending))
    waited = (datetime.now(timezone.utc) - oldest).total_seconds() if oldest else 0.0
    HITL_OLDEST_PENDING_SECONDS.set(max(0.0, waited))

    summary = {"staged_pending": len(staged), "recorded_expired": expired, "still_unstaged": remaining,
               "merged": merged}
    if staged or expired or remaining or merged:
        _log.warning("reconcile_repaired", extra=summary)
    return summary


def main() -> None:
    from src.logging_config import configure_logging

    configure_logging()
    print(asyncio.run(reconcile_unstaged()))


if __name__ == "__main__":
    main()
