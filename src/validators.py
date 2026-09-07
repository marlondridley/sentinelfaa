"""
Schema and business-rule validation for NAS FeedOps Sentinel.

Each rule reflects real FAA SWIM/STDDS message integrity requirements:
- APDS_RVR messages must contain runway and at least one RVR measurement.
- SMES messages must reference a moving entity (aircraft, vehicle, or track).
- ISMC messages must carry a feed_status so operators can assess link health.
"""
from __future__ import annotations

from src import config
from src.models import NASEvent, ValidationResult


def validate_event(event: NASEvent) -> ValidationResult:
    """Run all validation rules against a NASEvent. Returns ValidationResult."""
    errors: list[str] = []

    # Rule 1 — Airport code must be one of the PCT airports
    if event.airport_code not in config.SUPPORTED_AIRPORTS:
        errors.append(
            f"Invalid airport_code '{event.airport_code}'. "
            f"Must be one of {sorted(config.SUPPORTED_AIRPORTS)}."
        )

    # Rule 2 — TRACON must be PCT
    if event.tracon != config.SUPPORTED_TRACON:
        errors.append(
            f"Invalid tracon '{event.tracon}'. Must be '{config.SUPPORTED_TRACON}'."
        )

    # Rule 3 — Service name must be a supported STDDS service
    if event.service_name not in config.SUPPORTED_SERVICES:
        errors.append(
            f"Invalid service_name '{event.service_name}'. "
            f"Must be one of {sorted(config.SUPPORTED_SERVICES)}."
        )

    # Rule 4 — event_time_utc required (Pydantic enforces type; guard against None)
    if event.event_time_utc is None:
        errors.append("event_time_utc is required.")

    # Rule 5 — received_time_utc required
    if event.received_time_utc is None:
        errors.append("received_time_utc is required.")

    # Rule 6 — latency_ms must have been calculated
    if event.latency_ms is None:
        errors.append("latency_ms must be calculated and present.")

    # Rule 7 — RVR messages: runway + at least one RVR measurement
    if event.service_name == "APDS_RVR":
        if not event.runway:
            errors.append("RVR message must include runway.")
        rvr_present = [
            v for v in [event.rvr_touchdown, event.rvr_midpoint, event.rvr_rollout]
            if v is not None
        ]
        if not rvr_present:
            errors.append(
                "RVR message must include at least one RVR value "
                "(rvr_touchdown, rvr_midpoint, or rvr_rollout)."
            )

    # Rule 8 — SMES messages: must reference a moving entity
    if event.service_name == "SMES":
        if not any([event.aircraft_id, event.vehicle_id, event.track_id]):
            errors.append(
                "SMES message must include aircraft_id, vehicle_id, or track_id."
            )

    # Rule 9 — ISMC messages: feed_status is mandatory for link health assessment
    if event.service_name == "ISMC":
        if not event.feed_status:
            errors.append("ISMC message must include feed_status.")

    return ValidationResult(passed=len(errors) == 0, errors=errors)
