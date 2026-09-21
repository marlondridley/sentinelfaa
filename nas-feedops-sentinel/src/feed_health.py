"""
Feed health computation for NAS FeedOps Sentinel.

Aggregates a batch of NASEvents into an operational health summary
consumed by the Streamlit dashboard and CLI output.

Status priority (worst wins): OK < STALE < DEGRADED < UNAVAILABLE < DISCONNECTED
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src import config
from src.models import NASEvent

# Numeric rank used to pick the "worst" status
_STATUS_RANK: dict[str, int] = {
    "OK": 0,
    "STALE": 1,
    "DEGRADED": 2,
    "UNAVAILABLE": 3,
    "DISCONNECTED": 4,
}


def _worst(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


def compute_feed_health(events: list[NASEvent]) -> dict[str, Any]:
    """
    Derive an operational health snapshot from a batch of NASEvents.

    Returns a dict with:
      computed_at      — ISO timestamp of when the snapshot was taken
      airport_status   — {airport: worst_status_string}
      service_status   — {service: {airport: status_string}}
      latency_stats    — {service: {avg_ms, min_ms, max_ms, count}}
      smes_counts      — {airport: int}
      total_events     — int
    """
    now = datetime.now(timezone.utc)

    # NASEvent.received_time_utc is always tz-aware UTC (enforced by
    # models.NASEvent.ensure_utc_awareness), including when it comes back
    # from Postgres via asyncpg (TIMESTAMPTZ columns deserialize as aware
    # datetimes, not strings). now/stale_cutoff must stay aware too, or the
    # 'latest.received_time_utc < stale_cutoff' comparison below raises
    # TypeError: can't compare offset-naive and offset-aware datetimes the
    # moment this runs against any real event batch.

    stale_cutoff = now - timedelta(seconds=config.STALE_FEED_SECONDS)

    # Group events by (airport, service)
    by_key: dict[tuple, list[NASEvent]] = {}
    for event in events:
        key = (event.airport_code, event.service_name)
        by_key.setdefault(key, []).append(event)

    airport_status: dict[str, str] = {}
    service_status: dict[str, dict[str, str]] = {}

    for (airport, service), evts in by_key.items():
        latest = max(evts, key=lambda e: e.received_time_utc)
        status = "STALE" if latest.received_time_utc < stale_cutoff else "OK"

        # Feed_status values reported in the messages can override staleness
        feed_statuses = [e.feed_status for e in evts if e.feed_status]
        if "disconnected" in feed_statuses:
            status = _worst(status, "DISCONNECTED")
        elif "unavailable" in feed_statuses:
            status = _worst(status, "UNAVAILABLE")
        elif "degraded" in feed_statuses:
            status = _worst(status, "DEGRADED")

        airport_status[airport] = _worst(airport_status.get(airport, "OK"), status)
        service_status.setdefault(service, {})[airport] = status

    # Latency statistics per service
    latency_buckets: dict[str, list[float]] = {}
    for event in events:
        if event.latency_ms is not None:
            latency_buckets.setdefault(event.service_name, []).append(event.latency_ms)

    latency_stats: dict[str, dict[str, float | int]] = {}
    for service, lats in latency_buckets.items():
        latency_stats[service] = {
            "avg_ms": sum(lats) / len(lats),
            "min_ms": min(lats),
            "max_ms": max(lats),
            "count": len(lats),
        }

    # SMES surface movement event count per airport
    smes_counts: dict[str, int] = {}
    for event in events:
        if event.service_name == "SMES":
            smes_counts[event.airport_code] = smes_counts.get(event.airport_code, 0) + 1

    return {
        "computed_at": now.isoformat(),
        "airport_status": airport_status,
        "service_status": service_status,
        "latency_stats": latency_stats,
        "smes_counts": smes_counts,
        "total_events": len(events),
    }
