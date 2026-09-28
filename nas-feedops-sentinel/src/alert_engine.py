"""
Alert engine for NAS FeedOps Sentinel.

Evaluates a batch of NASEvents against seven alert rules and returns
a list of AlertEvents for persistence and dashboard display.

Alert types and their operational meaning:
  ALERT_FEED_DISCONNECTED   — A feed link has gone fully offline.
  ALERT_FEED_STALE          — No message received in 5 minutes; possible silent failure.
                              Raised by the reconciler from the last message stored for each
                              live feed (stale_feed_alerts), not from a batch: a silent feed
                              sends no batch, so a per-batch rule can never see it.
  ALERT_HIGH_LATENCY        — Message delivery exceeded the 30-second threshold.
  ALERT_SCHEMA_VALIDATION_FAILED — A message failed structural/business-rule validation.
  ALERT_LOW_RVR             — Runway visual range fell below 1,200 ft; ops impact likely.
  ALERT_ISMC_LINK_DEGRADED  — ISMC reports its own link as degraded or unavailable.
  ALERT_SURFACE_EVENT_SPIKE — Unusual burst of SMES events; possible data loop or incident.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src import config
from src.models import AlertEvent, NASEvent, is_surface_event, stable_id


def check_alerts(events: list[NASEvent]) -> list[AlertEvent]:
    """Evaluate all alert rules against the event list. Returns triggered alerts."""
    # Retain timezone awareness for safe datetime arithmetic
    now = datetime.now(timezone.utc)
    
    alerts: list[AlertEvent] = []

    for event in events:
        alerts.extend(_per_event_alerts(event))

    alerts.extend(_surface_spike_alerts(events, now))

    return [_with_origin(_with_stable_id(a, now), events) for a in alerts]


def _with_origin(alert: AlertEvent, events: list[NASEvent]) -> AlertEvent:
    """
    An alert inherits the provenance of what raised it: its event's origin, or
    for an aggregate alert the origins of the events at that airport/service
    ("mixed" when they differ). A synthetic event must never produce an alert
    that looks live.
    """
    if alert.event_id:
        origin = next((e.data_origin for e in events if e.event_id == alert.event_id), "unknown")
    else:
        scope = {e.data_origin for e in events
                 if e.airport_code == alert.airport_code
                 and (alert.service_name is None or e.service_name == alert.service_name)}
        origin = scope.pop() if len(scope) == 1 else ("mixed" if scope else "unknown")
    return alert.model_copy(update={"data_origin": origin})


def _with_stable_id(alert: AlertEvent, now: datetime) -> AlertEvent:
    """
    Give an alert an identity derived from what triggered it.

    Per-event alerts: the rule, the triggering event, and the description
    (one RVR event can raise LOW_RVR at touchdown and at rollout). Aggregate
    alerts (stale feed, surface spike) have no single event: the rule, airport,
    service and the evaluation window they fell in. Re-running the same events
    -- a retry, a double scan -- therefore yields the same alerts, which upsert
    in place and stage at most once.
    """
    if alert.event_id:
        key = stable_id("alert", alert.alert_type, alert.event_id, alert.description)
    else:
        window = max(config.SURFACE_SPIKE_WINDOW_SECONDS, config.STALE_FEED_SECONDS)
        bucket = int(now.timestamp() // window)
        key = stable_id("alert", alert.alert_type, alert.airport_code, alert.service_name, bucket)
    return alert.model_copy(update={"alert_id": key})


# ── Per-event rules ───────────────────────────────────────────────────────────

def _per_event_alerts(event: NASEvent) -> list[AlertEvent]:
    alerts: list[AlertEvent] = []

    # ALERT_FEED_DISCONNECTED
    if event.feed_status == "disconnected":
        alerts.append(AlertEvent(
            alert_type="ALERT_FEED_DISCONNECTED",
            severity="CRITICAL",
            airport_code=event.airport_code,
            service_name=event.service_name,
            description=(
                f"Feed DISCONNECTED reported by {event.service_name} "
                f"at {event.airport_code}."
            ),
            event_id=event.event_id,
        ))

    # ALERT_HIGH_LATENCY
    if event.latency_ms is not None and event.latency_ms > config.LATENCY_ALERT_MS:
        alerts.append(AlertEvent(
            alert_type="ALERT_HIGH_LATENCY",
            severity="WARNING",
            airport_code=event.airport_code,
            service_name=event.service_name,
            description=(
                f"High latency {event.latency_ms:.0f} ms for {event.service_name} "
                f"at {event.airport_code} (threshold {config.LATENCY_ALERT_MS} ms)."
            ),
            event_id=event.event_id,
        ))

    # ALERT_SCHEMA_VALIDATION_FAILED
    if event.validation_status == "FAILED":
        alerts.append(AlertEvent(
            alert_type="ALERT_SCHEMA_VALIDATION_FAILED",
            severity="WARNING",
            airport_code=event.airport_code,
            service_name=event.service_name,
            description=(
                f"Validation FAILED for {event.service_name} at {event.airport_code}: "
                + "; ".join(event.validation_errors)
            ),
            event_id=event.event_id,
        ))

    # ALERT_LOW_RVR
    if event.service_name == "APDS_RVR":
        rvr_positions: dict[str, int | None] = {
            "touchdown": event.rvr_touchdown,
            "midpoint": event.rvr_midpoint,
            "rollout": event.rvr_rollout,
        }
        for position, value in rvr_positions.items():
            if value is not None and value < config.RVR_LOW_THRESHOLD_FT:
                alerts.append(AlertEvent(
                    alert_type="ALERT_LOW_RVR",
                    severity="CRITICAL",
                    airport_code=event.airport_code,
                    service_name=event.service_name,
                    description=(
                        f"Low RVR {value} ft at {position} on runway {event.runway} "
                        f"at {event.airport_code} "
                        f"(threshold {config.RVR_LOW_THRESHOLD_FT} ft)."
                    ),
                    event_id=event.event_id,
                ))

    # ALERT_ISMC_LINK_DEGRADED
    if event.service_name == "ISMC" and event.feed_status in ("degraded", "unavailable"):
        alerts.append(AlertEvent(
            alert_type="ALERT_ISMC_LINK_DEGRADED",
            severity="CRITICAL",
            airport_code=event.airport_code,
            service_name=event.service_name,
            description=(
                f"ISMC link status '{event.feed_status}' at {event.airport_code}."
            ),
            event_id=event.event_id,
        ))

    return alerts


# ── Batch-level rules ─────────────────────────────────────────────────────────

def stale_feed_alerts(
    last_seen: dict[tuple[str | None, str | None], datetime], now: datetime | None = None,
) -> list[AlertEvent]:
    """
    ALERT_FEED_STALE — no message received within STALE_FEED_SECONDS.

    `last_seen` maps each live (airport, service) feed to the time of the last
    message stored for it, so a feed that has gone quiet is judged against the
    clock rather than against whatever the current batch happens to contain.
    The alerts are of the live feed by definition, and their identity is the
    feed and the evaluation window, so re-evaluating every minute upserts one
    alert per window instead of one per pass.
    """
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=config.STALE_FEED_SECONDS)
    alerts: list[AlertEvent] = []
    for (airport, service), seen in last_seen.items():
        if seen < stale_cutoff:
            age_s = (now - seen).total_seconds()
            alert = AlertEvent(
                alert_type="ALERT_FEED_STALE",
                severity="WARNING",
                airport_code=airport,
                service_name=service,
                description=(
                    f"Feed STALE: no message from {service} at {airport} "
                    f"for {age_s:.0f} s (threshold {config.STALE_FEED_SECONDS} s)."
                ),
            )
            alerts.append(_with_stable_id(alert, now).model_copy(update={"data_origin": "live"}))
    return alerts


def _surface_spike_alerts(events: list[NASEvent], now: datetime) -> list[AlertEvent]:
    """ALERT_SURFACE_EVENT_SPIKE — unusual burst of discrete SMES surface events per airport.

    Counts discrete events only. Equipment status (hold bars, ASDE-X system
    status) is not traffic, and position updates arrive once per target per
    second -- counting either turned routine traffic into spike alerts.
    """
    window_start = now - timedelta(seconds=config.SURFACE_SPIKE_WINDOW_SECONDS)
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        if is_surface_event(event) and event.received_time_utc >= window_start:
            counts[event.airport_code] += 1

    alerts: list[AlertEvent] = []
    for airport, count in counts.items():
        if count >= config.SURFACE_SPIKE_COUNT:
            alerts.append(AlertEvent(
                alert_type="ALERT_SURFACE_EVENT_SPIKE",
                severity="WARNING",
                airport_code=airport,
                service_name="SMES",
                description=(
                    f"Surface event spike: {count} SMES events at {airport} "
                    f"within {config.SURFACE_SPIKE_WINDOW_SECONDS} s window "
                    f"(threshold {config.SURFACE_SPIKE_COUNT})."
                ),
            ))
    return alerts
