"""Shared ingest → validate → alert → critic pipeline (CLI + Temporal)."""
from __future__ import annotations

import asyncio

from src.alert_engine import check_alerts
from src.config import settings
from src.critic_agent import CriticAgent
from src.execution_gate import stage_critical_alert
from src.feed_health import compute_feed_health
from src.investigation import investigate_staged
from src.logging_config import bind_correlation_id, get_logger
from src.metrics import ALERTS_TRIGGERED, EVENTS_INGESTED, EVENTS_VALIDATED, set_airport_status
from src.models import NASEvent
from src.storage import save_alerts, save_critic_result, save_events
from src.validators import validate_event

_log = get_logger(__name__)


async def run_pipeline(
    events: list[NASEvent],
    label: str = "PIPELINE",
    correlation_id: str | None = None,
) -> dict:
    """Validate, persist, alert, and summarise a batch of NASEvents."""
    cid = bind_correlation_id(correlation_id)
    _log.info(
        "pipeline_start",
        extra={"label": label, "event_count": len(events), "correlation_id": cid},
    )

    for event in events:
        result = validate_event(event)
        event.validation_status = "PASSED" if result.passed else "FAILED"
        event.validation_errors = result.errors
        EVENTS_INGESTED.labels(
            source=label.lower(),
            airport=event.airport_code or "unknown",
            service=event.service_name or "unknown",
        ).inc()
        EVENTS_VALIDATED.labels(status=event.validation_status).inc()

    await save_events(events)

    alerts = check_alerts(events)
    await save_alerts(alerts)

    for a in alerts:
        ALERTS_TRIGGERED.labels(
            alert_type=a.alert_type,
            severity=a.severity,
            airport=a.airport_code or "unknown",
        ).inc()

    critic = CriticAgent()
    critical_alerts = [a for a in alerts if a.severity == "CRITICAL"]
    critic_results = {}
    if critical_alerts:
        to_evaluate = critical_alerts[: settings.MAX_CRITIC_PER_PASS]
        skipped = len(critical_alerts) - len(to_evaluate)
        _log.info(
        "critic_evaluation_start",
        extra={
            "evaluating_count": len(to_evaluate),
            "skipped_count": skipped,
            "total_critical": len(critical_alerts),
            },
        )
        async def _critique_and_save(alert):
            result = await critic.critique(alert)
            await save_critic_result(result)
            return alert.alert_id, result

        pairs = await asyncio.gather(
            *(_critique_and_save(alert) for alert in to_evaluate)
        )
        critic_results.update(pairs)

    staged_count = 0
    staged_pairs = []
    for alert in critical_alerts:
        critic_for_alert = critic_results.get(alert.alert_id)
        action = await stage_critical_alert(alert, critic_for_alert)
        if action is not None:
            staged_count += 1
            staged_pairs.append((action, alert))

    # Advisory, after staging: an investigation failure is logged and never
    # reaches the staged actions above.
    investigated = 0
    if settings.INVESTIGATIONS_ENABLED and staged_pairs:
        try:
            investigated = len(await investigate_staged(staged_pairs))
        except Exception as exc:  # noqa: BLE001
            _log.error("investigations_failed", extra={"error": f"{type(exc).__name__}: {exc}", "staged_count": staged_count})
    if staged_count:
        _log.info(
        "hitl_actions_staged",
        extra={"staged_count": staged_count},
        )

    health = compute_feed_health(events)
    for airport, status in health["airport_status"].items():
        set_airport_status(airport, status)

    passed = sum(1 for e in events if e.validation_status == "PASSED")
    failed = len(events) - passed

    _log.info(
    "pipeline_execution_summary",
    extra={
        "label": label,
        "events_processed": len(events),
        "passed": passed,
        "failed": failed,
        "alert_count": len(alerts),
        "staged_count": staged_count,
        "airport_status": health["airport_status"],
        "smes_counts": health["smes_counts"],
    },
)

    return {
        "correlation_id": cid,
        "event_count": len(events),
        "passed": passed,
        "failed": failed,
        "alert_count": len(alerts),
        "staged_count": staged_count,
        "investigated_count": investigated,
        "airport_status": health["airport_status"],
    }
