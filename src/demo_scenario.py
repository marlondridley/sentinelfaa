"""
Demo scenario generator for NAS FeedOps Sentinel.

Produces a controlled CASCADE_FAILURE sequence at KDCA so the Critic
and Planner agents have rich, realistic evidence to reason over.

Scenario timeline (all within the last 20 minutes):
  T-20m  ISMC reports DEGRADED at KDCA
  T-18m  APDS_RVR: RVR 900 ft on runway 01 (below 1200 ft threshold)
  T-15m  ISMC escalates to UNAVAILABLE at KDCA
  T-13m  SMES spike: 14 surface events in rapid succession at KDCA
  T-10m  APDS_RVR: RVR drops further to 600 ft on runway 01
  T-8m   ISMC disconnects at KDCA
  T-6m   High latency (38000 ms) on APDS_RVR and SMES at KDCA
  T-5m   Additional APDS_RVR: RVR 800 ft on runway 19 (both runways affected)
  T-2m   Healthy baseline events: KIAD, KBWI, KADW (all OK — contrast)

This makes the Critic score CASCADE_FAILURE with high confidence and
the Planner identify multi-service, multi-factor KDCA degradation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List

from src.models import NASEvent


def _now_minus(minutes: float) -> datetime:
    return datetime.utcnow() - timedelta(minutes=minutes)


def _event(**kwargs) -> NASEvent:
    base = dict(
        tracon="PCT",
        message_type="demo",
        validation_status="PASSED",
    )
    base.update(kwargs)
    if "latency_ms" not in base:
        base["latency_ms"] = 220.0
    if "received_time_utc" not in base:
        base["received_time_utc"] = base.get("event_time_utc", datetime.utcnow())
    return NASEvent(**base)


def generate_cascade_failure_scenario() -> List[NASEvent]:
    """Return the full CASCADE_FAILURE demo event sequence."""
    events: List[NASEvent] = []

    # T-20m: ISMC DEGRADED at KDCA
    t = _now_minus(20)
    for _ in range(3):
        events.append(_event(
            airport_code="KDCA", service_name="ISMC", message_type="link_status",
            event_time_utc=t, received_time_utc=t,
            feed_status="degraded", latency_ms=280.0,
        ))

    # T-18m: RVR 900 ft on runway 01 at KDCA
    t = _now_minus(18)
    events.append(_event(
        airport_code="KDCA", service_name="APDS_RVR", message_type="rvr_report",
        event_time_utc=t, received_time_utc=t,
        runway="01", rvr_touchdown=900, rvr_midpoint=1100, rvr_rollout=1400,
        latency_ms=310.0,
    ))

    # T-15m: ISMC UNAVAILABLE at KDCA
    t = _now_minus(15)
    for _ in range(2):
        events.append(_event(
            airport_code="KDCA", service_name="ISMC", message_type="link_status",
            event_time_utc=t, received_time_utc=t,
            feed_status="unavailable", latency_ms=420.0,
        ))

    # T-30s: SMES spike — 14 rapid surface events at KDCA (within 60-second detection window)
    t = _now_minus(0.5)
    for i in range(14):
        events.append(_event(
            airport_code="KDCA", service_name="SMES", message_type="surface_event",
            event_time_utc=t + timedelta(seconds=i * 2),
            received_time_utc=t + timedelta(seconds=i * 2),
            aircraft_id=f"AAL{100 + i}", surface_event_type="MOVEMENT",
            latency_ms=195.0,
        ))

    # T-10m: RVR drops to 600 ft — well below minimum
    t = _now_minus(10)
    events.append(_event(
        airport_code="KDCA", service_name="APDS_RVR", message_type="rvr_report",
        event_time_utc=t, received_time_utc=t,
        runway="01", rvr_touchdown=600, rvr_midpoint=700, rvr_rollout=900,
        latency_ms=390.0,
    ))

    # T-8m: ISMC DISCONNECTED at KDCA
    t = _now_minus(8)
    events.append(_event(
        airport_code="KDCA", service_name="ISMC", message_type="link_status",
        event_time_utc=t, received_time_utc=t,
        feed_status="disconnected", latency_ms=510.0,
    ))

    # T-6m: High latency across APDS_RVR and SMES
    t = _now_minus(6)
    for svc in ("APDS_RVR", "SMES"):
        events.append(_event(
            airport_code="KDCA", service_name=svc, message_type="heartbeat",
            event_time_utc=t, received_time_utc=t,
            latency_ms=38000.0,
            aircraft_id="SWA501" if svc == "SMES" else None,
            surface_event_type="MOVEMENT" if svc == "SMES" else None,
            runway="01" if svc == "APDS_RVR" else None,
            rvr_touchdown=650 if svc == "APDS_RVR" else None,
        ))

    # T-5m: Second runway affected — RVR 800 ft on runway 19
    t = _now_minus(5)
    events.append(_event(
        airport_code="KDCA", service_name="APDS_RVR", message_type="rvr_report",
        event_time_utc=t, received_time_utc=t,
        runway="19", rvr_touchdown=800, rvr_midpoint=950, rvr_rollout=1100,
        latency_ms=405.0,
    ))

    # T-2m: Healthy baseline at KIAD, KBWI, KADW (creates contrast)
    t = _now_minus(2)
    for ap in ("KIAD", "KBWI", "KADW"):
        for svc in ("ISMC", "SMES"):
            events.append(_event(
                airport_code=ap, service_name=svc, message_type="heartbeat",
                event_time_utc=t, received_time_utc=t,
                feed_status="operational" if svc == "ISMC" else None,
                aircraft_id="UAL200" if svc == "SMES" else None,
                surface_event_type="MOVEMENT" if svc == "SMES" else None,
                latency_ms=180.0,
            ))
        events.append(_event(
            airport_code=ap, service_name="APDS_RVR", message_type="rvr_report",
            event_time_utc=t, received_time_utc=t,
            runway="01", rvr_touchdown=4500, rvr_midpoint=5000, rvr_rollout=5500,
            latency_ms=175.0,
        ))

    return events
