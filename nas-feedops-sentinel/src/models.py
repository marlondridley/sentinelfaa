"""
Pydantic data models for NAS FeedOps Sentinel.

NASEvent is the normalized, canonical representation of any STDDS message
received from the FAA SWIM feed (or generated/replayed locally).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from src import config

# Namespace for identifiers derived from content (uuid5). Fixed forever: changing
# it would give every re-ingested record a new identity.
_ID_NAMESPACE = uuid.UUID("5b3f6f0e-9a41-4c44-9f7c-2e2f7c9d1a10")


def stable_id(*parts: object) -> str:
    """
    A deterministic identifier for something identified by `parts`.

    Records that arrive twice -- a re-dropped SWIM file, a Temporal activity
    retry, a double scan -- must land on the same row, not a new one. Random
    uuid4 identities made every repeat a duplicate event, a duplicate alert and,
    through the alert, a duplicate staged action and model call.
    """
    return str(uuid.uuid5(_ID_NAMESPACE, "|".join("" if p is None else str(p) for p in parts)))


def content_key(record: dict) -> str:
    """A stable digest of a record's content, for records with no better identity."""
    canonical = json.dumps(record, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Where a record came from. The trust cockpit must never present test or
# replayed data as the live FAA picture, so every event carries this and every
# alert inherits it. `unknown` is for rows ingested before the tag existed --
# their origin cannot be reconstructed honestly.
DataOrigin = Literal["live", "replay", "synthetic", "api", "unknown"]
AlertOrigin = Literal["live", "replay", "synthetic", "api", "unknown", "mixed"]

def _in_scope_airport(code: str) -> str:
    """An event's airport must be one this deployment supports (config.SUPPORTED_AIRPORTS)."""
    if code not in config.SUPPORTED_AIRPORTS:
        raise ValueError(f"airport {code!r} is not one of the supported airports {sorted(config.SUPPORTED_AIRPORTS)}")
    return code


def _in_scope_tracon(code: str) -> str:
    """An event's TRACON must be one of those the supported airports belong to (config.SUPPORTED_TRACONS)."""
    if code not in config.SUPPORTED_TRACONS:
        raise ValueError(f"TRACON {code!r} is not one of the supported TRACONs {sorted(config.SUPPORTED_TRACONS)}")
    return code


AirportCode = Annotated[str, AfterValidator(_in_scope_airport)]
TraconCode = Annotated[str, AfterValidator(_in_scope_tracon)]
ServiceName = Literal["SMES", "APDS_RVR", "ISMC", "TOWER_DEPARTURE", "TERMINAL_AUTO"]


class BriefAction(BaseModel):
    """One thing to do, who does it, and how anyone can tell it is finished."""
    action: str
    owner: str
    done_when: str


class DataHealth(BaseModel):
    """Whether our own pipeline is delivering data: checked before anything about the airspace is read."""
    state: str = "UNKNOWN"             # OK | DEGRADED | DOWN | UNKNOWN
    scope: str = "none"                # pipeline (every airport at once) | airport | backlog | none
    minutes_since_last_event: float | None = None
    stale_airports: list[str] = Field(default_factory=list)
    reason: str = ""


class BriefIncident(BaseModel):
    """Alerts that began together, across airports, reported as one thing."""
    started_at: datetime
    ended_at: datetime
    airports: list[str] = Field(default_factory=list)
    alert_types: list[str] = Field(default_factory=list)
    alert_count: int = 0
    reviews: int = 0
    historical: bool = False           # older than the alert-age limit, with no newer evidence
    artifact: bool = False             # every alert is a sensor not-reporting code (RVR 0 ft), not a reading


class BriefAssessment(BaseModel):
    """
    What the Watch Officer's brief says, worked out by rules before any model writes a word
    (src/watch_officer_assessment.py): a status, a bottom line, at most three owned actions, and the
    facts the narrative must agree with.
    """
    status: str = "GREY"               # RED | AMBER | GREEN | GREY
    status_line: str = ""
    bluf: str = ""
    actions: list[BriefAction] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)
    confidence_label: str = "LOW"      # HIGH | MEDIUM | LOW: in the diagnosis, not in the airspace
    confidence_reason: str = ""
    data_health: DataHealth = Field(default_factory=DataHealth)
    incidents: list[BriefIncident] = Field(default_factory=list)
    current_alert_count: int = 0
    current_critical_count: int = 0
    historical_alert_count: int = 0
    artifact_alert_count: int = 0
    current_review_count: int = 0
    historical_review_count: int = 0
    consistency_warnings: list[str] = Field(default_factory=list)
    above_fold_words: int = 0
    affected_airports: list[str] = Field(default_factory=list)     # stale or degraded now, or with a current CRITICAL alert


class WatchOfficerBrief(BaseModel):
    """
    Structured operational brief produced by the Watch Officer Agent.
    The brief is the NARRATIVE that becomes part of an OperationalTicket.
    """
    brief_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    posture: str = "UNKNOWN"           # GOOD | NORMAL | ATTENTION | ACTION | UNKNOWN
    trust_assessment: str = "UNKNOWN"  # TRUSTWORTHY | DEGRADED | INCOMPLETE | UNKNOWN
    headline: str = ""
    what_changed: list[str] = Field(default_factory=list)
    why_it_matters: list[str] = Field(default_factory=list)
    recommended_human_review: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    pending_actions_count: int = 0
    critical_alert_count: int = 0
    affected_airports: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    llm_used: bool = False
    limitations: list[str] = Field(default_factory=list)
    # The status, bottom line and owned actions (None on briefs stored before they existed).
    assessment: BriefAssessment | None = None


class OperationalTicket(BaseModel):
    """
    Internal escalation package. NOT submitted to any external system.
    The ticket carries the Watch Officer narrative + the staged-action context
    so a human operator can decide: Approve Escalation / Reject / Continue Monitoring.
    """
    ticket_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str | None = None
    alert_id: str | None = None
    brief_id: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    title: str = ""
    severity: str = "CRITICAL"
    affected_airport: str | None = None
    affected_service: str | None = None
    posture: str = "ACTION"
    trust_assessment: str = "DEGRADED"
    summary: str = ""
    what_changed: list[str] = Field(default_factory=list)
    why_it_matters: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    recommended_review: list[str] = Field(default_factory=list)
    critic_rationale: str | None = None
    status: str = "DRAFT"  # DRAFT | APPROVED_FOR_ESCALATION | REJECTED | MONITORING | OVERRIDDEN
    created_by_agent: str = "watch_officer"
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    idempotency_key: str = ""


class StagedAction(BaseModel):
    """
    A safety-critical action staged for human approval.

    Critical alerts produce a StagedAction in PENDING status. The action cannot
    auto-execute. A human operator must approve or reject it via the dashboard;
    every state change is recorded in `action_audit_log`.
    """
    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_id: str | None = None
    event_id: str | None = None
    airport_code: str | None = None
    service_name: str | None = None
    severity: str = "CRITICAL"
    alert_type: str | None = None
    recommended_action: str = ""
    rationale: str = ""
    critic_confidence: float | None = None
    critic_pattern: str | None = None
    status: str = "PENDING"                # PENDING | APPROVED | REJECTED | OVERRIDDEN | EXPIRED | MERGED
    idempotency_key: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    # Only set when status == OVERRIDDEN: the operator-edited recommendation/severity
    # actually signed off on. recommended_action/severity above stay untouched as the
    # AI's original suggestion, so the audit trail can show both what the AI proposed
    # and what a human actually approved in its place -- see execution_gate.override_staged_action().
    overridden_recommendation: str | None = None
    overridden_severity: str | None = None
    # The condition this action is the review for (alert type|airport|service),
    # and how many CRITICAL alerts it covers, first and last (staged_action_alerts).
    condition_key: str | None = None
    occurrences: int = 1
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class ActionAuditEntry(BaseModel):
    """One row in the action_audit_log table — every state transition."""
    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str
    previous_status: str
    new_status: str
    actor: str
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# surface_event_type values that describe the airport's surface-safety
# equipment rather than a moving aircraft or vehicle. They are exempt from the
# moving-entity validation rule and are excluded from surface-movement counts
# (feed_health smes_counts, ALERT_SURFACE_EVENT_SPIKE), where a burst of
# equipment status messages would otherwise read as a traffic spike.
SMES_FACILITY_STATUS_TYPES = frozenset({"SAFETY_LOGIC_HOLD_BAR", "ASDEX_SYSTEM_STATUS"})

# Periodic position updates of a known target. They are movement (so they must
# name the track) but not surface events: ASDE-X sends one per target per
# second, and counting them made ALERT_SURFACE_EVENT_SPIKE fire on routine
# traffic.
SMES_TRACK_UPDATE_TYPES = frozenset({"ADSB_REPORT", "POSITION_REPORT"})


def is_surface_movement(event: "NASEvent") -> bool:
    """True for SMES events that describe a moving entity (not equipment status)."""
    return (
        event.service_name == "SMES"
        and event.surface_event_type not in SMES_FACILITY_STATUS_TYPES
    )


def is_surface_event(event: "NASEvent") -> bool:
    """True for discrete SMES surface events: movement that is not a periodic position update."""
    return is_surface_movement(event) and event.surface_event_type not in SMES_TRACK_UPDATE_TYPES



# nas_events stores these as INTEGER. A value outside 32 bits made asyncpg reject the whole batch it was in, and
# the scan left every file of the batch claimed and tried them all again, for ever.
Int32 = Annotated[int, Field(ge=-2_147_483_648, le=2_147_483_647)]


def _without_nul(value: Any) -> Any:
    """PostgreSQL text and JSONB cannot hold a NUL; one in a feed field rejected the whole batch."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_without_nul(v) for v in value]
    return value


class NASEvent(BaseModel):
    """Normalized FAA SWIM/STDDS terminal event record."""

    # Unknown fields are an error, not silently dropped. Pydantic's default
    # (extra="ignore") let src/agent_evaluator.py build its "clean state" RVR
    # events with rvr_feet= and runway_id= -- neither is a field -- so they were
    # discarded, the events carried no runway and no RVR value, and the
    # evaluator's baseline raised four ALERT_SCHEMA_VALIDATION_FAILED alerts
    # against data it believed was nominal. The same default meant a client
    # POSTing a misspelled field to /ingest got past api._validate_events(),
    # whose whole job is to report malformed payloads in the 422 response.
    # nas_events' columns match these fields one-for-one, and every in-repo
    # construction site (SWIM extractors, replay, storage reads) passes the
    # suite with this on, as do the real captured FAA messages in
    # tests/fixtures/swim_kit/.
    model_config = ConfigDict(extra="forbid")

    # Core identity
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    airport_code: AirportCode           # ICAO; must be in config.SUPPORTED_AIRPORTS (default KDCA, KIAD, KBWI, KADW)
    tracon: TraconCode                  # must be in config.SUPPORTED_TRACONS (default PCT)
    service_name: ServiceName           # SMES, APDS_RVR, ISMC, etc.
    message_type: str                   # Subtype within the service

    # Timestamps
    event_time_utc: datetime            # When the event occurred at the facility
    received_time_utc: datetime         # When this system received the message

    # Derived latency (received - event, in milliseconds)
    latency_ms: float | None = None

    # RVR fields (APDS_RVR service)
    runway: str | None = None
    rvr_touchdown: Int32 | None = None   # feet
    rvr_midpoint: Int32 | None = None    # feet
    rvr_rollout: Int32 | None = None     # feet

    # Aircraft / vehicle / track identity (SMES, TOWER_DEPARTURE, TERMINAL_AUTO)
    track_id: str | None = None
    aircraft_id: str | None = None
    vehicle_id: str | None = None
    surface_event_type: str | None = None

    # Spatial / track state (TAIS / TERMINAL_AUTO)
    lat: float | None = None              # WGS84 latitude
    lon: float | None = None              # WGS84 longitude
    altitude_ft: Int32 | None = None        # Reported altitude in feet
    ground_speed_kt: Int32 | None = None    # Ground speed in knots
    vertical_speed_fpm: Int32 | None = None # Vertical speed (fpm); + climbing, - descending
    flight_phase: str | None = None       # DEPARTURE | ARRIVAL | ENROUTE | GROUND | UNKNOWN
    flight_rules: str | None = None       # IFR | VFR | SVFR
    beacon_code: str | None = None        # Transponder squawk

    # Feed / link status (ISMC, TERMINAL_AUTO)
    feed_status: str | None = None    # operational | degraded | unavailable | disconnected

    # Raw facility status word, kept verbatim where the ICD that decodes it is
    # not public (SMES SafetyLogicHoldBar's hold-bar bitmask). Stored, not
    # interpreted: nothing downstream alerts on it.
    status_code: str | None = None

    # Provenance: live (the Jumpstart kit's folder), replay (recorded files),
    # synthetic (sample generator, demo scenario), api (posted to /v1/ingest).
    data_origin: DataOrigin = "unknown"

    # Validation bookkeeping (populated by validators.py)
    validation_status: str = "PENDING"   # PENDING | PASSED | FAILED
    validation_errors: list[str] = Field(default_factory=list)

    # Provenance
    raw_message_path: str | None = None   # Path to source JSON file (replay mode)
    
    @model_validator(mode="before")
    @classmethod
    def strip_nul_characters(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: _without_nul(value) for key, value in data.items()}
        return data

    @field_validator("event_time_utc", "received_time_utc", mode="after")
    @classmethod
    def ensure_utc_awareness(cls, dt: datetime) -> datetime:
        """Enforce timezone awareness across event timestamps to prevent comparison errors."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

class AlertEvent(BaseModel):
    """A triggered alert produced by the alert engine."""

    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_type: str                          # e.g. ALERT_LOW_RVR
    severity: str                            # INFO | WARNING | CRITICAL
    airport_code: str | None = None
    service_name: str | None = None
    description: str
    triggered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str | None = None           # Source event that caused the alert
    data_origin: AlertOrigin = "unknown"  # inherited from the event(s) behind it


class ValidationResult(BaseModel):
    """Result returned by validate_event()."""

    passed: bool
    errors: list[str] = Field(default_factory=list)


class GuardrailResult(BaseModel):
    """Output of the input or output guardrail check."""

    is_compliant: bool
    violation_type: str | None = None   # INJECTION | SCOPE_VIOLATION | STRUCTURAL | BOUNDS
    injection_score: float = 0.0           # 0.0 = clean, 1.0 = definite injection
    sanitized_input: str | None = None  # cleaned version of the raw input
    detail: str = ""


class EvalResult(BaseModel):
    """Result of a single evaluation case."""

    scenario_name: str
    passed: bool
    score: float                            # 0.0–1.0
    checks: dict = Field(default_factory=dict)   # check_name → pass/fail/value
    latency_ms: float | None = None
    notes: str = ""


class EvalReport(BaseModel):
    """Aggregate evaluation report across all scenarios."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    score_pct: float = 0.0
    llm_enabled: bool = False
    results: list[EvalResult] = Field(default_factory=list)


class PlannerResult(BaseModel):
    """Operational intelligence brief produced by PlannerAgent."""

    question: str
    answer: str
    sub_queries: list[str] = Field(default_factory=list)
    confidence: float | None = None
    citations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CriticResult(BaseModel):
    """Assessment produced by CriticAgent for a triggered AlertEvent."""

    alert_id: str
    confidence: float | None = None  # None when critic is unavailable
    reasoning: str
    citations: list[str] = Field(default_factory=list)
    pattern: str = "UNKNOWN"  # CASCADE_FAILURE | ISOLATED_FAULT | WEATHER_CORRELATED | DATA_ANOMALY | TRANSIENT | UNKNOWN
    escalate: bool = False
    # One sentence, bottom line up front, for the review queue (prompts/critic.yaml).
    bluf: str | None = None
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
