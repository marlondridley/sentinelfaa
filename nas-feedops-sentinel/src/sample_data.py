"""
Realistic sample message generators for all five STDDS service types.

Messages reflect actual PCT TRACON operational parameters:
- Runways are per-airport (real KDCA, KIAD, KBWI, KADW configurations).
- RVR values use feet (FAA standard), with a 15% chance of low-visibility conditions.
- ISMC feed_status probabilities skew toward operational (75%) with occasional anomalies.
- Latency is randomized across a wide range to exercise the high-latency alert rule.
"""
from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone


from src import config
from src.models import NASEvent

# ── Lookup tables ─────────────────────────────────────────────────────────────

AIRPORTS: list[str] = sorted(config.SUPPORTED_AIRPORTS)

# Real runway configurations for each PCT airport
RUNWAYS: dict[str, list[str]] = {
    "KDCA": ["01", "04", "15", "19", "22", "33"],
    "KIAD": ["01L", "01R", "12", "19L", "19R", "30"],
    "KBWI": ["10", "15L", "15R", "28", "33L", "33R"],
    "KADW": ["01L", "01R", "19L", "19R"],
}

SURFACE_EVENT_TYPES: list[str] = [
    "TAXI_OUT", "TAXI_IN", "PUSHBACK", "RUNWAY_ENTER",
    "RUNWAY_EXIT", "GATE_DEPARTURE", "GATE_ARRIVAL", "HOLD_SHORT",
]

# Representative airline/cargo callsigns operating in the PCT area
AIRCRAFT_IDS: list[str] = [
    "UAL123", "DAL456", "AAL789", "SWA321", "FDX101",
    "UPS202", "JBU303", "SKW404", "EJA505", "N12345",
    "ASA601", "WN2271", "DL9823", "AA1547", "UA5588",
]

# Ground vehicle types at major airports
VEHICLE_IDS: list[str] = [
    "TUG-01", "TUG-02", "FUEL-A", "FUEL-B", "MAINT-1",
    "FOLLOW-ME", "DEICER-3", "RESCUE-1", "SECURITY-7", "CATERING-4",
]

FEED_STATUSES: list[str] = ["operational", "degraded", "unavailable", "disconnected"]
FEED_STATUS_WEIGHTS: list[float] = [0.75, 0.12, 0.08, 0.05]


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _event_time() -> datetime:
    """Recent event time with slight random jitter (simulates real-time stream)."""
    return datetime.now(timezone.utc) - timedelta(milliseconds=random.randint(50, 5_000))



def _received_time(event_time: datetime) -> datetime:
    """Simulate network + broker + processing latency (occasionally very high)."""
    # ~10% of messages exceed the 30 s alert threshold to trigger ALERT_HIGH_LATENCY
    if random.random() < 0.10:
        latency_ms = random.randint(30_500, 60_000)
    else:
        latency_ms = random.randint(80, 8_000)
    return event_time + timedelta(milliseconds=latency_ms)


def _base_fields(airport: str, service: str, message_type: str) -> dict:
    event_time = _event_time()
    received_time = _received_time(event_time)
    latency_ms = (received_time - event_time).total_seconds() * 1_000
    return dict(
        airport_code=airport,
        tracon=config.SUPPORTED_TRACON,
        service_name=service,
        message_type=message_type,
        event_time_utc=event_time,
        received_time_utc=received_time,
        latency_ms=latency_ms,
        data_origin="synthetic",
    )


# ── Individual service generators ─────────────────────────────────────────────

def generate_smes_event(airport: str | None = None) -> NASEvent:
    """Surface Movement Event Service — aircraft/vehicle position update."""
    airport = airport or random.choice(AIRPORTS)
    fields = _base_fields(airport, "SMES", "SURFACE_MOVEMENT")
    fields["surface_event_type"] = random.choice(SURFACE_EVENT_TYPES)

    # Each SMES event must reference exactly one of three entity types
    entity = random.choice(["aircraft", "vehicle", "track_only"])
    if entity == "aircraft":
        fields["aircraft_id"] = random.choice(AIRCRAFT_IDS)
        fields["track_id"] = f"TRK{random.randint(1000, 9999)}"
    elif entity == "vehicle":
        fields["vehicle_id"] = random.choice(VEHICLE_IDS)
    else:
        fields["track_id"] = f"TRK{random.randint(1000, 9999)}"

    return NASEvent(**fields)


def generate_rvr_event(airport: str | None = None) -> NASEvent:
    """Airport Data / Runway Visual Range — sensor reading for a specific runway."""
    airport = airport or random.choice(AIRPORTS)
    fields = _base_fields(airport, "APDS_RVR", "RVR_UPDATE")
    runway = random.choice(RUNWAYS[airport])

    # 15% probability of low-visibility condition to exercise ALERT_LOW_RVR
    if random.random() < 0.15:
        touchdown = random.randint(400, 1_199)
    else:
        touchdown = random.randint(1_200, 6_000)

    fields.update(
        runway=runway,
        rvr_touchdown=touchdown,
        rvr_midpoint=random.choice([None, random.randint(1_000, 5_000)]),
        rvr_rollout=random.choice([None, random.randint(1_200, 6_000)]),
    )
    return NASEvent(**fields)


def generate_ismc_event(airport: str | None = None) -> NASEvent:
    """Infrastructure, System Monitoring and Control — link health report."""
    airport = airport or random.choice(AIRPORTS)
    fields = _base_fields(airport, "ISMC", "LINK_STATUS")
    fields["feed_status"] = random.choices(FEED_STATUSES, weights=FEED_STATUS_WEIGHTS)[0]
    return NASEvent(**fields)


def generate_tower_departure_event(airport: str | None = None) -> NASEvent:
    """Tower Departure Event — aircraft released for takeoff."""
    airport = airport or random.choice(AIRPORTS)
    fields = _base_fields(airport, "TOWER_DEPARTURE", "DEPARTURE_EVENT")
    fields.update(
        runway=random.choice(RUNWAYS[airport]),
        aircraft_id=random.choice(AIRCRAFT_IDS),
        track_id=f"TRK{random.randint(1000, 9999)}",
        surface_event_type="TAKEOFF",
    )
    return NASEvent(**fields)


def generate_terminal_auto_event(airport: str | None = None) -> NASEvent:
    """Terminal Automation Information — STARS/ARTS system position update."""
    airport = airport or random.choice(AIRPORTS)
    fields = _base_fields(airport, "TERMINAL_AUTO", "AUTOMATION_UPDATE")
    fields.update(
        track_id=f"TRK{random.randint(1000, 9999)}",
        aircraft_id=random.choice(AIRCRAFT_IDS),
        feed_status=random.choice(["operational", "degraded"]),
    )
    return NASEvent(**fields)


# ── Batch generator ───────────────────────────────────────────────────────────

_GENERATORS: list[Callable] = [
    generate_smes_event,
    generate_rvr_event,
    generate_ismc_event,
    generate_tower_departure_event,
    generate_terminal_auto_event,
]


def generate_sample_batch(count: int = 50) -> list[NASEvent]:
    """Generate a mixed batch of realistic NASEvents across all service types."""
    return [random.choice(_GENERATORS)() for _ in range(count)]
