"""
What the evaluation is made of: the gate and evaluation records, the gate configuration, the status names and
the few helpers every part of it shares. No evaluation logic lives here (see evaluation.py, evaluation_gates.py
and evaluation_metrics.py).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src import agt_metrics as agt
from src.config import settings

PASS, WARN, FAIL, PENDING, NOT_APPLICABLE = "PASS", "WARN", "FAIL", "PENDING", "N/A"
GROUPS = ("Quality", "Safety", "Cost", "Latency", "Audit")
DECIDED = ("APPROVED", "REJECTED", "OVERRIDDEN")
AGENT_STEPS = ("feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer")
PARALLEL = ("feed_diagnostician", "correlation_analyst")
RESPONDED = ("ok", "repaired", "incomplete", "invalid_output")
# "[keycloak-password]": decisions recorded while the dashboard used the password grant (to 2026-09-26).
VERIFIED_ACTOR_SUFFIXES = ("[oidc]", "[keycloak-password]", "[api_key]")
_VERIFY_STEP_REJECTION = "verification step without valid evidence"


# ── Configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateConfig:
    budget: agt.Budget
    max_retries: int
    max_depth: int


def load_gate_config(path: Path | None = None) -> GateConfig:
    data = yaml.safe_load(Path(path or settings.MODEL_SLO_PATH).read_text(encoding="utf-8")) or {}
    gates = data.get("investigation_gates") or {}
    return GateConfig(
        budget=agt.Budget(
            max_cost_usd=gates.get("max_cost_usd"), max_tokens=gates.get("max_tokens"),
            max_tool_calls=gates.get("max_tool_calls"), max_duration_seconds=gates.get("max_duration_seconds"),
        ),
        max_retries=int(gates.get("max_retries", 3)),
        max_depth=int(gates.get("max_depth", 3)),
    )


# ── Per investigation ────────────────────────────────────────────────────────

@dataclass
class Gate:
    group: str            # Quality | Safety | Cost | Latency | Audit
    name: str
    status: str           # PASS | WARN | FAIL | PENDING | N/A
    detail: str
    source: str           # its kind (AGT-native / AGT-compatible / Sentinel-specific), then what decided


@dataclass
class InvestigationEvaluation:
    investigation_id: str
    action_id: str
    alert_id: str | None
    status: str
    opened_at: datetime
    completed_at: datetime | None
    alert_type: str | None
    airport: str | None
    used_model: bool
    # Latency (seconds)
    duration_s: float | None
    queue_s: float | None
    stages_s: dict[str, float]
    handoff_s: float | None
    # Model calls attributed to this investigation (subject = investigation_id)
    model_calls: int
    attempts: int
    retries: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None               # None when any call's cost is unknown
    critic_cost_usd: float | None        # the per-alert Critic (subject = alert_id)
    served_models: list[str]
    providers: list[str]
    outcomes: dict[str, int]
    # Quality, judged by the grounding gate
    claims_accepted: int
    claims_rejected: int
    grounding: float | None
    useful_items: int
    challenges: int
    contradictions: int
    disagreements: int
    dead_end: bool
    depth: int
    termination: str
    # The Critic's recommendation, and what a person did with it
    confidence: float | None
    escalate: bool | None
    action_status: str | None
    decided_by: str | None
    decided_at: datetime | None
    time_to_decision_s: float | None
    operator_agreed: bool | None
    # Evaluation
    decision_score: float | None
    decision_results: dict[str, dict[str, Any]]
    gates: list[Gate] = field(default_factory=list)

    def group_status(self, group: str) -> str:
        return _combine(g.status for g in self.gates if g.group == group)

    @property
    def gate_summary(self) -> dict[str, str]:
        return {group: self.group_status(group) for group in GROUPS}

    @property
    def cost_per_useful_item(self) -> float | None:
        if self.cost_usd is None or not self.useful_items:
            return None
        return self.cost_usd / self.useful_items

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _combine(statuses) -> str:
    statuses = list(statuses)
    for status in (FAIL, WARN, PENDING, PASS):
        if status in statuses:
            return status
    return NOT_APPLICABLE


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max((end - start).total_seconds(), 0.0)


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None
