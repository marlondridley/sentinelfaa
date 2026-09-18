"""
Pydantic data models for NAS FeedOps Sentinel.

NASEvent is the normalized, canonical representation of any STDDS message
received from the FAA SWIM feed (or generated/replayed locally).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

AirportCode = Literal["KDCA", "KIAD", "KBWI", "KADW"]
TraconCode = Literal["PCT"]
ServiceName = Literal["SMES", "APDS_RVR", "ISMC", "TOWER_DEPARTURE", "TERMINAL_AUTO"]


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
    status: str = "DRAFT"  # DRAFT | APPROVED_FOR_ESCALATION | REJECTED | MONITORING
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
    status: str = "PENDING"                # PENDING | APPROVED | REJECTED | EXPIRED
    idempotency_key: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None


class ActionAuditEntry(BaseModel):
    """One row in the action_audit_log table — every state transition."""
    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str
    previous_status: str
    new_status: str
    actor: str
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NASEvent(BaseModel):
    """Normalized FAA SWIM/STDDS terminal event record."""

    # Core identity
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    airport_code: AirportCode           # ICAO: KDCA, KIAD, KBWI, KADW
    tracon: TraconCode                  # Always PCT for this scenario
    service_name: ServiceName           # SMES, APDS_RVR, ISMC, etc.
    message_type: str                   # Subtype within the service

    # Timestamps
    event_time_utc: datetime            # When the event occurred at the facility
    received_time_utc: datetime         # When this system received the message

    # Derived latency (received - event, in milliseconds)
    latency_ms: float | None = None

    # RVR fields (APDS_RVR service)
    runway: str | None = None
    rvr_touchdown: int | None = None   # feet
    rvr_midpoint: int | None = None    # feet
    rvr_rollout: int | None = None     # feet

    # Aircraft / vehicle / track identity (SMES, TOWER_DEPARTURE, TERMINAL_AUTO)
    track_id: str | None = None
    aircraft_id: str | None = None
    vehicle_id: str | None = None
    surface_event_type: str | None = None

    # Spatial / track state (TAIS / TERMINAL_AUTO)
    lat: float | None = None              # WGS84 latitude
    lon: float | None = None              # WGS84 longitude
    altitude_ft: int | None = None        # Reported altitude in feet
    ground_speed_kt: int | None = None    # Ground speed in knots
    vertical_speed_fpm: int | None = None # Vertical speed (fpm); + climbing, - descending
    flight_phase: str | None = None       # DEPARTURE | ARRIVAL | ENROUTE | GROUND | UNKNOWN
    flight_rules: str | None = None       # IFR | VFR | SVFR
    beacon_code: str | None = None        # Transponder squawk

    # Feed / link status (ISMC, TERMINAL_AUTO)
    feed_status: str | None = None    # operational | degraded | unavailable | disconnected

    # Validation bookkeeping (populated by validators.py)
    validation_status: str = "PENDING"   # PENDING | PASSED | FAILED
    validation_errors: list[str] = Field(default_factory=list)

    # Provenance
    raw_message_path: str | None = None   # Path to source JSON file (replay mode)

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


class GateDecision(BaseModel):
    """Decision produced by ExecutionGate for a structured plan."""

    allowed: bool
    risk_tier: str                          # LOW | MEDIUM | HIGH | CRITICAL
    hitl_required: bool
    idempotency_key: str
    risk_factors: list[str] = Field(default_factory=list)
    ticket_id: str | None = None        # populated when hitl_required=True
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
