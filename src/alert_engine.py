"""
Alert engine for NAS FeedOps Sentinel.

Evaluates a batch of NASEvents against seven alert rules and returns
a list of AlertEvents for persistence and dashboard display.

Alert types and their operational meaning:
  ALERT_FEED_DISCONNECTED   — A feed link has gone fully offline.
  ALERT_FEED_STALE          — No message received in 5 minutes; possible silent failure.
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
from src.models import AlertEvent, NASEvent


def check_alerts(events: list[NASEvent]) -> list[AlertEvent]:
    """Evaluate all alert rules against the event list. Returns triggered alerts."""
    now = datetime.now(timezone.utc)
    alerts: list[AlertEvent] = []

    for event in events:
        alerts.extend(_per_event_alerts(event))

    alerts.extend(_stale_feed_alerts(events, now))
    alerts.extend(_surface_spike_alerts(events, now))

    return alerts


# ── Per-event rules ───────────────────────────────────────────────────────────


def _per_event_alerts(event: NASEvent) -> list[AlertEvent]:
    alerts: list[AlertEvent] = []

    # ALERT_FEED_DISCONNECTED
    if event.feed_status == "disconnected":
        alerts.append(
            AlertEvent(
                alert_type="ALERT_FEED_DISCONNECTED",
                severity="CRITICAL",
                airport_code=event.airport_code,
                service_name=event.service_name,
                description=(
                    f"Feed DISCONNECTED reported by {event.service_name} "
                    f"at {event.airport_code}."
                ),
                event_id=event.event_id,
            )
        )

    # ALERT_HIGH_LATENCY
    if event.latency_ms is not None and event.latency_ms > config.LATENCY_ALERT_MS:
        alerts.append(
            AlertEvent(
                alert_type="ALERT_HIGH_LATENCY",
                severity="WARNING",
                airport_code=event.airport_code,
                service_name=event.service_name,
                description=(
                    f"High latency {event.latency_ms:.0f} ms for {event.service_name} "
                    f"at {event.airport_code} (threshold {config.LATENCY_ALERT_MS} ms)."
                ),
                event_id=event.event_id,
            )
        )

    # ALERT_SCHEMA_VALIDATION_FAILED
    if event.validation_status == "FAILED":
        alerts.append(
            AlertEvent(
                alert_type="ALERT_SCHEMA_VALIDATION_FAILED",
                severity="WARNING",
                airport_code=event.airport_code,
                service_name=event.service_name,
                description=(
                    f"Validation FAILED for {event.service_name} at {event.airport_code}: "
                    + "; ".join(event.validation_errors)
                ),
                event_id=event.event_id,
            )
        )

    # ALERT_LOW_RVR
    if event.service_name == "APDS_RVR":
        rvr_positions: dict[str, int | None] = {
            "touchdown": event.rvr_touchdown,
            "midpoint": event.rvr_midpoint,
            "rollout": event.rvr_rollout,
        }
        for position, value in rvr_positions.items():
            if value is not None and value < config.RVR_LOW_THRESHOLD_FT:
                alerts.append(
                    AlertEvent(
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
                    )
                )

    # ALERT_ISMC_LINK_DEGRADED
    if event.service_name == "ISMC" and event.feed_status in (
        "degraded",
        "unavailable",
    ):
        alerts.append(
            AlertEvent(
                alert_type="ALERT_ISMC_LINK_DEGRADED",
                severity="CRITICAL",
                airport_code=event.airport_code,
                service_name=event.service_name,
                description=(
                    f"ISMC link status '{event.feed_status}' at {event.airport_code}."
                ),
                event_id=event.event_id,
            )
        )

    return alerts


# ── Batch-level rules ─────────────────────────────────────────────────────────


def _stale_feed_alerts(events: list[NASEvent], now: datetime) -> list[AlertEvent]:
    """ALERT_FEED_STALE — no message received within STALE_FEED_SECONDS."""
    latest: dict[tuple, datetime] = {}
    for event in events:
        key = (event.airport_code, event.service_name)
        ts = event.received_time_utc
        if key not in latest or ts > latest[key]:
            latest[key] = ts

    stale_cutoff = now - timedelta(seconds=config.STALE_FEED_SECONDS)
    alerts: list[AlertEvent] = []
    for (airport, service), last_seen in latest.items():
        if last_seen < stale_cutoff:
            age_s = (now - last_seen).total_seconds()
            alerts.append(
                AlertEvent(
                    alert_type="ALERT_FEED_STALE",
                    severity="WARNING",
                    airport_code=airport,
                    service_name=service,
                    description=(
                        f"Feed STALE: no message from {service} at {airport} "
                        f"for {age_s:.0f} s (threshold {config.STALE_FEED_SECONDS} s)."
                    ),
                )
            )
    return alerts


def _surface_spike_alerts(events: list[NASEvent], now: datetime) -> list[AlertEvent]:
    """ALERT_SURFACE_EVENT_SPIKE — unusual burst of SMES events per airport."""
    window_start = now - timedelta(seconds=config.SURFACE_SPIKE_WINDOW_SECONDS)
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        if event.service_name == "SMES" and event.received_time_utc >= window_start:
            counts[event.airport_code] += 1

    alerts: list[AlertEvent] = []
    for airport, count in counts.items():
        if count >= config.SURFACE_SPIKE_COUNT:
            alerts.append(
                AlertEvent(
                    alert_type="ALERT_SURFACE_EVENT_SPIKE",
                    severity="WARNING",
                    airport_code=airport,
                    service_name="SMES",
                    description=(
                        f"Surface event spike: {count} SMES events at {airport} "
                        f"within {config.SURFACE_SPIKE_WINDOW_SECONDS} s window "
                        f"(threshold {config.SURFACE_SPIKE_COUNT})."
                    ),
                )
            )
    return alerts
