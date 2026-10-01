"""
The evaluation layer: agent execution telemetry in, quality metrics and
governance gates out.

    execution telemetry  ->  evaluation  ->  quality metrics  ->  dashboard
    (model_calls, sealed     (this module)   (per investigation,   (three layers)
     investigations,                           and per window)
     staged actions,
     the audit log)

Agents never grade themselves. Every judgment here comes from evidence the
agents do not control: the grounding gate's check of every cited evidence id,
the operator's recorded decision, the gateway's usage and cost figures, the
sealed record's SHA-256, and the append-only audit log. A value that cannot
be computed honestly is None and is shown as "no data".

The Agent Governance Toolkit computes wherever it computes correctly
(src/agt_metrics.py): decision quality through agent-sre's EvaluationEngine
with Sentinel's evidence judge, percentiles through ResponseLatency, spend
through CostGuard, reliability through FleetManager, completion through
TaskSuccessRate, unsupported claims through HallucinationRate, depth through
DelegationChainDepth, policy compliance through agent-os-kernel's
GovernanceMetrics, and the cost and latency gates through its BudgetTracker.
Every value and gate carries a source that names its kind first
(src/agt_metrics.py CATEGORIES): AGT-native, AGT-compatible (Sentinel's
calculation while the installed AGT has a known defect) or Sentinel-specific
(AGT defines no such metric), with the reason in docs/agent-metrics.md.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src import agt_metrics as agt
from src import storage
from src.evaluation_gates import evaluate_gates
from src.evaluation_metrics import aggregate, finite
from src.evaluation_types import (
    AGENT_STEPS, DECIDED, FAIL, GROUPS, NOT_APPLICABLE, PARALLEL, PASS, PENDING, RESPONDED, VERIFIED_ACTOR_SUFFIXES,
    WARN, Gate, GateConfig, InvestigationEvaluation, _VERIFY_STEP_REJECTION, _parse, _seconds, load_gate_config,
)
from src.investigation.record import Investigation
from src.model_telemetry import NOT_SENT_OUTCOMES

__all__ = [
    "AGENT_STEPS", "DECIDED", "FAIL", "GROUPS", "NOT_APPLICABLE", "PARALLEL", "PASS", "PENDING", "RESPONDED",
    "VERIFIED_ACTOR_SUFFIXES", "WARN", "Gate", "GateConfig", "InvestigationEvaluation", "WindowEvaluation",
    "aggregate", "evaluate_action", "evaluate_investigation", "evaluate_latest", "evaluate_window", "finite",
    "grounding_counts", "load_gate_config",
]


def grounding_counts(inv: Investigation) -> tuple[int, int]:
    """
    (model claims the grounding gate accepted, claims it rejected).

    Only what a model contributed counts as accepted: a finding's claims when
    it came from the model; the Red-Team Critic's model challenges, and its
    alternative when the model's (not one derived from the rule challenges);
    the brief's established claims that are not verbatim copies of a
    finding's. Every claim the gate rejected counts, whichever step it came
    from -- a specialist that produced only ungrounded claims falls back to
    its rules but its rejections are kept -- except rejected verification
    steps, which are recommendations, not claims.
    """
    finding_statements = {c.statement for f in inv.findings for c in f.claims}
    parts = (_findings_grounding(inv.findings), _critique_grounding(inv.critique),
             _brief_grounding(inv.brief, finding_statements))
    return sum(accepted for accepted, _ in parts), sum(rejected for _, rejected in parts)


def _findings_grounding(findings) -> tuple[int, int]:
    accepted = sum(len(f.claims) for f in findings if f.provenance.method == "llm")
    return accepted, sum(len(f.rejected_claims) for f in findings)


def _critique_grounding(critique) -> tuple[int, int]:
    if critique is None:
        return 0, 0
    accepted = 0
    if critique.provenance.method == "llm":
        statements = {c.statement for c in critique.challenges}
        accepted = sum(1 for c in critique.challenges if c.origin == "llm")
        alt = critique.strongest_alternative
        accepted += 1 if alt is not None and alt.statement not in statements else 0
    return accepted, len(critique.rejected_claims)


def _brief_grounding(brief, finding_statements: set[str]) -> tuple[int, int]:
    if brief is None:
        return 0, 0
    accepted = 0
    if brief.provenance.method == "llm":
        accepted = sum(1 for c in brief.established if c.statement not in finding_statements)
    return accepted, sum(1 for r in brief.rejected_claims if r.reason != _VERIFY_STEP_REJECTION)


def _stages(inv: Investigation) -> tuple[dict[str, float], float | None]:
    """Seconds per stage (the two parallel specialists as one wall-clock stage) and the rest: handoff."""
    steps = {s.step: s for s in inv.steps}
    stages: dict[str, float] = {}
    for name in ("collect_evidence", "red_team_critic", "watch_officer", "seal"):
        if name in steps:
            stages[name] = _seconds(steps[name].started_at, steps[name].finished_at) or 0.0
    parallel = [steps[n] for n in PARALLEL if n in steps]
    if parallel:
        stages["specialists"] = _seconds(min(s.started_at for s in parallel), max(s.finished_at for s in parallel)) or 0.0
    total = _seconds(inv.opened_at, inv.completed_at)
    handoff = max(total - sum(stages.values()), 0.0) if total is not None else None
    return stages, handoff


def _total_cost(rows: list[dict]) -> float | None:
    """What these calls cost; None when any one's cost is unknown (a partial sum would read as the whole), 0 for none."""
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    unknown = sum(1 for r in rows if r.get("cost_usd") is None)
    return round(sum(costs), 6) if rows and not unknown else (None if rows else 0.0)


def _call_totals(rows: list[dict]) -> dict[str, Any]:
    responded = [r for r in rows if r["outcome"] in RESPONDED]
    return {
        "calls": len(rows),
        "attempts": sum(r.get("attempts") or 0 for r in rows),
        "retries": sum(max((r.get("attempts") or 0) - 1 - (1 if r.get("repaired") else 0), 0) for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
        "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
        "cost_usd": _total_cost(rows),
        "served_models": sorted({r["served_model"] for r in responded if r.get("served_model")}),
        "providers": sorted({r.get("provider") or "_OTHER" for r in rows if r["outcome"] not in NOT_SENT_OUTCOMES}),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
    }


def _termination(inv: Investigation) -> str:
    if inv.status == "COMPLETE":
        return "COMPLETE"
    error_step = next((s for s in inv.steps if s.outcome == "error"), None)
    return f"FAILED at {error_step.step}: {error_step.detail}" if error_step else inv.status


def _depth(inv: Investigation) -> int:
    """How many of the three sequential stages (the parallel specialists, the critic, the officer) ran."""
    return sum(1 for stage in (PARALLEL, ("red_team_critic",), ("watch_officer",))
               if any(s.step in stage for s in inv.steps))


def _operator_view(verdict: dict | None, action: Any | None) -> tuple:
    """
    (confidence, escalate, action status, decided_at, operator agreed): what the Critic recommended and what a
    person did about it. `agreed` is None until there is both a recommendation and a decision.
    """
    confidence = verdict.get("confidence") if verdict else None
    if confidence is None and action is not None:
        confidence = action.critic_confidence
    escalate = verdict.get("escalate") if verdict and verdict.get("confidence") is not None else None
    status = action.status if action is not None else None
    decided_at = _parse(action.decided_at) if action is not None else None
    agreed = None
    if escalate is not None and status in DECIDED:
        agreed = (status == "APPROVED" and escalate) or (status == "REJECTED" and not escalate)
    return confidence, escalate, status, decided_at, agreed


def evaluate_investigation(
    inv: Investigation,
    *,
    calls: list[dict],
    critic_calls: list[dict],
    action: Any | None,
    audit: list[dict],
    verdict: dict | None,
    config: GateConfig,
    engine: Any | None = None,
) -> InvestigationEvaluation:
    """Evaluate one investigation from its sealed record and the evidence around it. Pure."""
    trigger = inv.trigger or {}
    used_model = any(s.agent and s.detail != "deterministic" for s in inv.steps)
    stages, handoff = _stages(inv)
    totals = _call_totals(calls)
    critic_totals = _call_totals(critic_calls)
    accepted, rejected = grounding_counts(inv)
    challenges = inv.critique.challenges if inv.critique else []
    contradictions = sum(1 for c in challenges if c.kind == "CONTRADICTING_EVIDENCE")
    disagreements = len(inv.brief.disagreements) if inv.brief else 0
    confidence, escalate, status, decided_at, agreed = _operator_view(verdict, action)

    report = agt.evaluate_decision(engine or agt.new_engine(), {
        "claims_judged": accepted + rejected, "claims_accepted": accepted, "operator_agreed": agreed,
    })

    evaluation = InvestigationEvaluation(
        investigation_id=inv.investigation_id, action_id=inv.action_id, alert_id=inv.alert_id,
        status=inv.status, opened_at=inv.opened_at, completed_at=inv.completed_at,
        alert_type=trigger.get("alert_type"), airport=trigger.get("airport_code"), used_model=used_model,
        duration_s=_seconds(inv.opened_at, inv.completed_at),
        queue_s=_seconds(_parse(trigger.get("triggered_at")), inv.opened_at),
        stages_s=stages, handoff_s=handoff,
        model_calls=totals["calls"], attempts=totals["attempts"], retries=totals["retries"],
        prompt_tokens=totals["prompt_tokens"], completion_tokens=totals["completion_tokens"],
        # A model run with no call on record has an unknown cost, not a zero one.
        cost_usd=None if used_model and not calls else totals["cost_usd"],
        critic_cost_usd=critic_totals["cost_usd"] if critic_calls else None,
        served_models=totals["served_models"], providers=totals["providers"], outcomes=totals["outcomes"],
        claims_accepted=accepted, claims_rejected=rejected,
        grounding=accepted / (accepted + rejected) if accepted + rejected else None,
        useful_items=accepted, challenges=len(challenges), contradictions=contradictions,
        disagreements=disagreements,
        dead_end=inv.status != "COMPLETE" or (used_model and accepted == 0),
        depth=_depth(inv), termination=_termination(inv),
        confidence=float(confidence) if confidence is not None else None, escalate=escalate,
        action_status=status, decided_by=action.decided_by if action is not None else None,
        decided_at=decided_at, time_to_decision_s=_seconds(inv.completed_at, decided_at) if status in DECIDED else None,
        operator_agreed=agreed, decision_score=report.score, decision_results=report.results,
    )
    evaluation.gates = evaluate_gates(inv, evaluation, calls=calls, audit=audit, config=config, report=report)
    return evaluation


# ── Loading ──────────────────────────────────────────────────────────────────

async def _context(investigations: list[Investigation]) -> dict[str, Any]:
    ids = [i.investigation_id for i in investigations]
    alert_ids = [i.alert_id for i in investigations if i.alert_id]
    action_ids = [i.action_id for i in investigations]
    calls = await storage.get_model_calls_for_subjects(ids + alert_ids)
    actions = {a.action_id: a for a in await storage.get_staged_actions_by_ids(action_ids)}
    audit: dict[str, list[dict]] = {}
    for row in await storage.get_audit_log_for_actions(action_ids):
        audit.setdefault(row["action_id"], []).append(row)
    verdicts: dict[str, dict] = {}
    for row in await storage.get_critic_verdicts_for_alerts(alert_ids):
        verdicts.setdefault(row["alert_id"], row)            # newest first
    by_subject: dict[str, list[dict]] = {}
    for row in calls:
        by_subject.setdefault(row.get("subject"), []).append(row)
    return {"actions": actions, "audit": audit, "verdicts": verdicts, "by_subject": by_subject}


def _evaluate_all(investigations: list[Investigation], ctx: dict[str, Any], config: GateConfig,
                  engine: Any) -> list[InvestigationEvaluation]:
    return [
        evaluate_investigation(
            inv, calls=ctx["by_subject"].get(inv.investigation_id, []),
            critic_calls=ctx["by_subject"].get(inv.alert_id, []) if inv.alert_id else [],
            action=ctx["actions"].get(inv.action_id), audit=ctx["audit"].get(inv.action_id, []),
            verdict=ctx["verdicts"].get(inv.alert_id) if inv.alert_id else None, config=config, engine=engine,
        )
        for inv in investigations
    ]


async def evaluate_action(action_id: str, config: GateConfig | None = None) -> InvestigationEvaluation | None:
    """The evaluation of one staged action's investigation (Layer 2), or None."""
    inv = await storage.get_investigation_for_action(action_id)
    if inv is None:
        return None
    [evaluation] = _evaluate_all([inv], await _context([inv]), config or load_gate_config(), agt.new_engine())
    return evaluation


async def evaluate_latest(config: GateConfig | None = None) -> InvestigationEvaluation | None:
    """The most recent completed investigation's evaluation (Layer 1), or None."""
    inv = await storage.get_latest_investigation()
    if inv is None:
        return None
    [evaluation] = _evaluate_all([inv], await _context([inv]), config or load_gate_config(), agt.new_engine())
    return evaluation


# ── Per window ───────────────────────────────────────────────────────────────

@dataclass
class WindowEvaluation:
    since: datetime
    until: datetime
    investigations: list[InvestigationEvaluation]
    calls: list[dict]
    verdicts: list[dict]
    metrics: dict[str, Any]


async def evaluate_window(hours: float, config: GateConfig | None = None) -> WindowEvaluation:
    """Every investigation and model call in the last `hours`, evaluated and aggregated (Layer 3, readiness)."""
    config = config or load_gate_config()
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    investigations = await storage.get_investigations_since(since)
    engine = agt.new_engine()
    evaluations = _evaluate_all(investigations, await _context(investigations), config, engine)
    calls = await storage.get_model_calls_since(since)
    verdicts = await storage.get_critic_verdicts_since(since)
    return WindowEvaluation(since=since, until=until, investigations=evaluations, calls=calls, verdicts=verdicts,
                            metrics=aggregate(evaluations, calls, verdicts, investigations, config, engine))
