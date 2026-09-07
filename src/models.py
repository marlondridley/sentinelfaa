"""
Pydantic data models for NAS FeedOps Sentinel.

NASEvent is the normalized, canonical representation of any STDDS message
received from the FAA SWIM feed (or generated/replayed locally).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class WatchOfficerBrief(BaseModel):
    """
    Structured operational brief produced by the Watch Officer Agent.
    The brief is the NARRATIVE that becomes part of an OperationalTicket.
    """
    brief_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    posture: str = "UNKNOWN"           # GOOD | NORMAL | ATTENTION | ACTION | UNKNOWN
    trust_assessment: str = "UNKNOWN"  # TRUSTWORTHY | DEGRADED | INCOMPLETE | UNKNOWN
    headline: str = ""
    what_changed: List[str] = Field(default_factory=list)
    why_it_matters: List[str] = Field(default_factory=list)
    recommended_human_review: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    pending_actions_count: int = 0
    critical_alert_count: int = 0
    affected_airports: List[str] = Field(default_factory=list)
    confidence: float = 0.5
    llm_used: bool = False
    limitations: List[str] = Field(default_factory=list)


class OperationalTicket(BaseModel):
    """
    Internal escalation package. NOT submitted to any external system.
    The ticket carries the Watch Officer narrative + the staged-action context
    so a human operator can decide: Approve Escalation / Reject / Continue Monitoring.
    """
    ticket_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: Optional[str] = None
    alert_id: Optional[str] = None
    brief_id: Optional[str] = None
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    title: str = ""
    severity: str = "CRITICAL"
    affected_airport: Optional[str] = None
    affected_service: Optional[str] = None
    posture: str = "ACTION"
    trust_assessment: str = "DEGRADED"
    summary: str = ""
    what_changed: List[str] = Field(default_factory=list)
    why_it_matters: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    recommended_review: List[str] = Field(default_factory=list)
    critic_rationale: Optional[str] = None
    status: str = "DRAFT"  # DRAFT | APPROVED_FOR_ESCALATION | REJECTED | MONITORING
    created_by_agent: str = "watch_officer"
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_note: Optional[str] = None
    idempotency_key: str = ""


class StagedAction(BaseModel):
    """
    A safety-critical action staged for human approval.

    Critical alerts produce a StagedAction in PENDING status. The action cannot
    auto-execute. A human operator must approve or reject it via the dashboard;
    every state change is recorded in `action_audit_log`.
    """
    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_id: Optional[str] = None
    event_id: Optional[str] = None
    airport_code: Optional[str] = None
    service_name: Optional[str] = None
    severity: str = "CRITICAL"
    alert_type: Optional[str] = None
    recommended_action: str = ""
    rationale: str = ""
    critic_confidence: Optional[float] = None
    critic_pattern: Optional[str] = None
    status: str = "PENDING"                # PENDING | APPROVED | REJECTED | EXPIRED
    idempotency_key: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    decision_note: Optional[str] = None


class ActionAuditEntry(BaseModel):
    """One row in the action_audit_log table — every state transition."""
    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str
    previous_status: str
    new_status: str
    actor: str
    note: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NASEvent(BaseModel):
    """Normalized FAA SWIM/STDDS terminal event record."""

    # Core identity
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    airport_code: str                   # ICAO: KDCA, KIAD, KBWI, KADW
    tracon: str                         # Always PCT for this scenario
    service_name: str                   # SMES, APDS_RVR, ISMC, etc.
    message_type: str                   # Subtype within the service

    # Timestamps
    event_time_utc: datetime            # When the event occurred at the facility
    received_time_utc: datetime         # When this system received the message

    # Derived latency (received - event, in milliseconds)
    latency_ms: Optional[float] = None

    # RVR fields (APDS_RVR service)
    runway: Optional[str] = None
    rvr_touchdown: Optional[int] = None   # feet
    rvr_midpoint: Optional[int] = None    # feet
    rvr_rollout: Optional[int] = None     # feet

    # Aircraft / vehicle / track identity (SMES, TOWER_DEPARTURE, TERMINAL_AUTO)
    track_id: Optional[str] = None
    aircraft_id: Optional[str] = None
    vehicle_id: Optional[str] = None
    surface_event_type: Optional[str] = None

    # Spatial / track state (TAIS / TERMINAL_AUTO)
    lat: Optional[float] = None              # WGS84 latitude
    lon: Optional[float] = None              # WGS84 longitude
    altitude_ft: Optional[int] = None        # Reported altitude in feet
    ground_speed_kt: Optional[int] = None    # Ground speed in knots
    vertical_speed_fpm: Optional[int] = None # Vertical speed (fpm); + climbing, - descending
    flight_phase: Optional[str] = None       # DEPARTURE | ARRIVAL | ENROUTE | GROUND | UNKNOWN
    flight_rules: Optional[str] = None       # IFR | VFR | SVFR
    beacon_code: Optional[str] = None        # Transponder squawk

    # Feed / link status (ISMC, TERMINAL_AUTO)
    feed_status: Optional[str] = None    # operational | degraded | unavailable | disconnected

    # Validation bookkeeping (populated by validators.py)
    validation_status: str = "PENDING"   # PENDING | PASSED | FAILED
    validation_errors: List[str] = Field(default_factory=list)

    # Provenance
    raw_message_path: Optional[str] = None   # Path to source JSON file (replay mode)


class AlertEvent(BaseModel):
    """A triggered alert produced by the alert engine."""

    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alert_type: str                          # e.g. ALERT_LOW_RVR
    severity: str                            # INFO | WARNING | CRITICAL
    airport_code: Optional[str] = None
    service_name: Optional[str] = None
    description: str
    triggered_at: datetime = Field(default_factory=datetime.utcnow)
    event_id: Optional[str] = None           # Source event that caused the alert


class ValidationResult(BaseModel):
    """Result returned by validate_event()."""

    passed: bool
    errors: List[str] = Field(default_factory=list)


class GuardrailResult(BaseModel):
    """Output of the input or output guardrail check."""

    is_compliant: bool
    violation_type: Optional[str] = None   # INJECTION | SCOPE_VIOLATION | STRUCTURAL | BOUNDS
    injection_score: float = 0.0           # 0.0 = clean, 1.0 = definite injection
    sanitized_input: Optional[str] = None  # cleaned version of the raw input
    detail: str = ""


class GateDecision(BaseModel):
    """Decision produced by ExecutionGate for a structured plan."""

    allowed: bool
    risk_tier: str                          # LOW | MEDIUM | HIGH | CRITICAL
    hitl_required: bool
    idempotency_key: str
    risk_factors: List[str] = Field(default_factory=list)
    ticket_id: Optional[str] = None        # populated when hitl_required=True
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)


class EvalResult(BaseModel):
    """Result of a single evaluation case."""

    scenario_name: str
    passed: bool
    score: float                            # 0.0–1.0
    checks: dict = Field(default_factory=dict)   # check_name → pass/fail/value
    latency_ms: Optional[float] = None
    notes: str = ""


class EvalReport(BaseModel):
    """Aggregate evaluation report across all scenarios."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)
    total_cases: int = 0
    passed: int = 0
    failed: int = 0
    score_pct: float = 0.0
    llm_enabled: bool = False
    results: List[EvalResult] = Field(default_factory=list)


class PlannerResult(BaseModel):
    """Operational intelligence brief produced by PlannerAgent."""

    question: str
    answer: str
    sub_queries: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    citations: List[str] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)


class CriticResult(BaseModel):
    """Assessment produced by CriticAgent for a triggered AlertEvent."""

    alert_id: str
    confidence: Optional[float] = None  # None when critic is unavailable
    reasoning: str
    citations: List[str] = Field(default_factory=list)
    pattern: str = "UNKNOWN"  # CASCADE_FAILURE | ISOLATED_FAULT | WEATHER_CORRELATED | DATA_ANOMALY | TRANSIENT | UNKNOWN
    escalate: bool = False
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)
