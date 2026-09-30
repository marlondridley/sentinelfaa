"""
The metric groups for a window of investigations and model calls: quality, latency, cost, reliability,
orchestration and governance. Every value names its kind (AGT-native, AGT-compatible or Sentinel-specific), and a
value that cannot be computed honestly is None and is shown as "no data".
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from src import agt_metrics as agt
from src.evaluation_types import (
    DECIDED, GROUPS, PASS, PENDING, RESPONDED, GateConfig, InvestigationEvaluation, _seconds,
)
from src.investigation.record import Investigation
from src.model_telemetry import SUCCESS_OUTCOMES


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class _Window:
    """What the metric groups are computed from, and the subsets of it they share."""

    evaluations: list[InvestigationEvaluation]
    calls: list[dict]
    verdicts: list[dict]
    investigations: list[Investigation]
    config: GateConfig
    engine: Any
    model_runs: list[InvestigationEvaluation]     # the evaluations that used a model
    sent: list[dict]                              # calls that reached a model (not refused by policy)
    responded: list[dict]                         # ... and got a reply back
    decided: list[dict]                           # Critic verdicts with a confidence that a person has decided


def _value_samples(pair) -> dict[str, Any]:
    """A (value, samples) pair as the dict the page reads."""
    return dict(zip(("value", "samples"), pair))


def aggregate(evaluations: list[InvestigationEvaluation], calls: list[dict], verdicts: list[dict],
              investigations: list[Investigation], config: GateConfig, engine: Any) -> dict[str, Any]:
    """The metric groups for a window. Every value names its kind: AGT-native, AGT-compatible or Sentinel-specific."""
    sent = [c for c in calls if c["outcome"] != "policy_denied"]
    window = _Window(
        evaluations=evaluations, calls=calls, verdicts=verdicts, investigations=investigations, config=config,
        engine=engine, model_runs=[e for e in evaluations if e.used_model], sent=sent,
        responded=[c for c in sent if c["outcome"] in RESPONDED],
        decided=[v for v in verdicts if v.get("confidence") is not None and v.get("action_status") in DECIDED])
    agents = _agent_fleet(window)
    return {"quality": _quality_metrics(window), "latency": _latency_metrics(window),
            "cost": _cost_metrics(window), "reliability": _reliability_metrics(window, agents),
            "orchestration": _orchestration_metrics(window, agents), "governance": _governance_metrics(window),
            "counts": {"investigations": len(evaluations), "with_model": len(window.model_runs),
                       "calls": len(calls), "decided": len(window.decided)}}


def _quality_metrics(w: _Window) -> dict[str, Any]:
    """Decision quality."""
    from agent_sre.evals import EvalCriterion

    score, scored = agt.engine_average(w.engine)
    grounding, grounded_runs = agt.engine_average(w.engine, EvalCriterion.HALLUCINATION)
    accuracy, accuracy_n = agt.engine_average(w.engine, EvalCriterion.CORRECTNESS)
    judgments = [True] * sum(e.claims_rejected for e in w.evaluations) + \
        [False] * sum(e.claims_accepted for e in w.evaluations)
    unsupported, claims_judged = agt.hallucination_rate(judgments)
    escalations = [v for v in w.decided if v.get("escalate")]
    approvals = [v for v in w.decided if v.get("action_status") == "APPROVED"]
    false_escalation = agt.share(escalations, lambda v: v["action_status"] == "REJECTED")
    missed_escalation = agt.share(approvals, lambda v: not v.get("escalate"))
    override = agt.share(w.decided, lambda v: v["action_status"] == "OVERRIDDEN")
    calibration = agt.expected_calibration_error(
        (float(v["confidence"]), (v["action_status"] == "APPROVED") == bool(v.get("escalate")))
        for v in w.decided)
    contradiction = agt.share(w.evaluations, lambda e: e.contradictions > 0)
    return {
        "decision_quality": {"value": score, "samples": scored, "source": "AGT-native: agent-sre EvaluationEngine"},
        "evidence_grounding": {"value": grounding, "samples": grounded_runs, "source": "AGT-native: agent-sre EvaluationEngine"},
        "recommendation_accuracy": {"value": accuracy, "samples": accuracy_n, "source": "AGT-native: agent-sre EvaluationEngine"},
        "unsupported_claim_rate": {"value": unsupported, "samples": claims_judged,
                                   "source": "AGT-native: agent-sre HallucinationRate (judged by the grounding gate)"},
        "false_escalation_rate": {"value": false_escalation[0], "samples": false_escalation[1], "source": "Sentinel-specific"},
        "missed_escalation_rate": {"value": missed_escalation[0], "samples": missed_escalation[1], "source": "Sentinel-specific"},
        "human_override_rate": {"value": override[0], "samples": override[1], "source": "Sentinel-specific"},
        "calibration_error": {"value": calibration[0], "samples": calibration[1], "source": "AGT-compatible: ECE (AGT bug G)"},
        "contradiction_rate": {"value": contradiction[0], "samples": contradiction[1], "source": "Sentinel-specific"},
        "evidence_improvement": {"value": _mean(e.useful_items for e in w.model_runs), "samples": len(w.model_runs),
                                 "source": "Sentinel-specific"},
    }


def _latency_metrics(w: _Window) -> dict[str, Any]:
    """Latency: agent-sre ResponseLatency for every percentile."""
    by_agent = _seconds_by_agent(w)
    return {
        "investigation": agt.percentiles(e.duration_s for e in w.evaluations),
        "model_call": agt.percentiles(c["latency_ms"] / 1000.0 for c in w.sent),
        "evidence_collection": agt.percentiles(e.stages_s.get("collect_evidence") for e in w.evaluations),
        "handoff": agt.percentiles(e.handoff_s for e in w.evaluations),
        "queue": agt.percentiles(e.queue_s for e in w.evaluations),
        "time_to_decision": agt.percentiles(e.time_to_decision_s for e in w.evaluations),
        "by_agent": {agent: agt.percentiles(values) for agent, values in sorted(by_agent.items())},
        "by_stage": {stage: agt.percentiles(e.stages_s.get(stage) for e in w.evaluations)
                     for stage in ("collect_evidence", "specialists", "red_team_critic", "watch_officer", "seal")},
        "timeout_rate": _value_samples(agt.share(w.sent, lambda c: c["outcome"] == "timeout")),
        "retry_rate": _value_samples(
            agt.share(w.sent, lambda c: (c.get("attempts") or 0) - (1 if c.get("repaired") else 0) > 1)),
    }


def _seconds_by_agent(w: _Window) -> dict[str, list[float]]:
    """How long each agent took: its investigation steps, and the Critic's and Planner's own calls."""
    by_agent: dict[str, list[float]] = {}
    for inv in w.investigations:
        for step in inv.steps:
            if step.agent:
                by_agent.setdefault(step.agent, []).append(_seconds(step.started_at, step.finished_at))
    for c in w.calls:
        if c.get("role") in ("critic", "planner") and c["outcome"] != "policy_denied":
            by_agent.setdefault(c["role"], []).append(c["latency_ms"] / 1000.0)
    return by_agent


def _cost_metrics(w: _Window) -> dict[str, Any]:
    """Cost: agent-sre CostGuard."""
    known = [c for c in w.calls if c.get("cost_usd") is not None]
    runs_with_cost = [e for e in w.model_runs if e.cost_usd is not None]
    total_spend = sum(c["cost_usd"] for c in known)
    return {
        "tokens": {"input": sum(c.get("prompt_tokens") or 0 for c in w.sent),
                   "output": sum(c.get("completion_tokens") or 0 for c in w.sent)},
        "total_usd": round(total_spend, 6) if known else None,
        "calls_with_cost": len(known), "calls_cost_unknown": len(w.responded) - len([c for c in w.responded
                                                                                     if c.get("cost_usd") is not None]),
        **_spend_breakdowns(known, runs_with_cost),
        **_spend_ratios(w, runs_with_cost, total_spend, bool(known)),
        **_retry_and_failed_cost(known),
        "sources": {"per_invocation, per_investigation, per_alert, by_model": "AGT-native: agent-sre CostGuard",
                    "per_resolved, per_useful_item, retry, failed": "Sentinel-specific"},
    }


def _spend_breakdowns(known: list[dict], runs_with_cost: list[InvestigationEvaluation]) -> dict[str, Any]:
    """Spend per role, investigation, alert and served model (agent-sre CostGuard)."""
    alert_spend = [(e.alert_id or e.investigation_id, (e.cost_usd or 0.0) + (e.critic_cost_usd or 0.0))
                   for e in runs_with_cost]
    return {
        "per_invocation_by_role": agt.spend((c["role"], c["call_id"], c["cost_usd"]) for c in known),
        "per_investigation": agt.spend(("investigation", e.investigation_id, e.cost_usd) for e in runs_with_cost)
                                .get("investigation"),
        "per_alert": agt.spend(("alert", key, value) for key, value in alert_spend).get("alert"),
        "by_model": agt.spend((c.get("served_model") or "(unknown)", c["call_id"], c["cost_usd"]) for c in known),
    }


def _spend_ratios(w: _Window, runs_with_cost: list[InvestigationEvaluation], total_spend: float,
                  any_cost_known: bool) -> dict[str, Any]:
    resolved = [e for e in w.evaluations if e.action_status in DECIDED]
    useful = sum(e.useful_items for e in runs_with_cost)
    return {
        "per_resolved_investigation": (sum(e.cost_usd or 0.0 for e in runs_with_cost) / len(resolved)
                                       if resolved and runs_with_cost else None),
        "per_useful_item": total_spend / useful if useful and any_cost_known else None,
    }


def _retry_and_failed_cost(known: list[dict]) -> dict[str, Any]:
    return {
        "retry_cost_usd": round(sum(c["cost_usd"] for c in known if (c.get("attempts") or 0) > 1), 6) if known else None,
        "failed_call_cost_usd": round(sum(c["cost_usd"] for c in known if c["outcome"] not in SUCCESS_OUTCOMES), 6)
                                if known else None,
    }


def _agent_fleet(w: _Window) -> dict[str, Any]:
    """Every agent step and every Critic/Planner call as one fleet (agent-sre FleetManager)."""
    step_events = [
        (s.agent, s.outcome == "ok", _seconds(s.started_at, s.finished_at) or 0.0, 0.0)
        for inv in w.investigations for s in inv.steps if s.agent and s.detail != "deterministic"
    ] + [(c["role"], c["outcome"] in SUCCESS_OUTCOMES, c["latency_ms"] / 1000.0, c.get("cost_usd") or 0.0)
         for c in w.sent if c.get("role") in ("critic", "planner")]
    return agt.fleet(step_events)


def _reliability_metrics(w: _Window, agents: dict[str, Any]) -> dict[str, Any]:
    """Reliability: agent-sre FleetManager, TaskSuccessRate."""
    providers = agt.fleet((c.get("provider") or "_OTHER", c["outcome"] in SUCCESS_OUTCOMES,
                           c["latency_ms"] / 1000.0, c.get("cost_usd") or 0.0) for c in w.sent)
    completion = agt.success_rate(e.status == "COMPLETE" for e in w.evaluations)
    return {
        "agents": agents,
        "agent_failure_rate": {"value": (1 - agents["success_rate"]) if agents["success_rate"] is not None else None,
                               "samples": agents["events"], "source": "AGT-native: agent-sre FleetManager"},
        "call_success_rate": {**_value_samples(agt.success_rate(c["outcome"] in SUCCESS_OUTCOMES for c in w.sent)),
                              "source": "AGT-native: agent-sre TaskSuccessRate"},
        "providers": providers,
        "outcomes": dict(Counter(c["outcome"] for c in w.calls)),
        "fallback_rate": _value_samples(agt.share(
            [s for inv in w.investigations for s in inv.steps if s.agent and s.detail != "deterministic"],
            lambda s: s.outcome == "fallback")),
        "schema_failure_rate": _value_samples(agt.share(
            w.responded, lambda c: c["outcome"] in ("incomplete", "invalid_output"))),
        "malformed_output_rate": _value_samples(agt.share(w.responded, lambda c: c["outcome"] == "invalid_output")),
        "output_validity": {**_value_samples(agt.share(w.responded, lambda c: c["outcome"] == "ok")),
                            "source": "Sentinel-specific"},
        "investigation_completion": {"value": completion[0], "samples": completion[1],
                                     "source": "AGT-native: agent-sre TaskSuccessRate"},
    }


def _prompt_tokens_by_role(w: _Window) -> dict[str, list[int]]:
    prompt_by_role: dict[str, list[int]] = {}
    for c in w.sent:
        if c.get("prompt_tokens"):
            prompt_by_role.setdefault(c["role"], []).append(c["prompt_tokens"])
    return prompt_by_role


def _orchestration_metrics(w: _Window, agents: dict[str, Any]) -> dict[str, Any]:
    depth_ok = agt.depth_compliance((e.depth for e in w.evaluations), w.config.max_depth)
    role_counts = Counter((c.get("subject"), c["role"]) for c in w.calls if c.get("subject"))
    prompt_by_role = _prompt_tokens_by_role(w)
    stage_order = ("feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer")
    return {
        "agents_invoked": {agent: m["events"] for agent, m in agents["members"].items()},
        "tool_calls_per_investigation": _mean(e.model_calls + 1 for e in w.evaluations),
        "handoffs_per_investigation": _mean(len(e.stages_s) - 1 for e in w.evaluations if e.stages_s),
        "parallel_stage": "feed_diagnostician + correlation_analyst",
        "dependency_failures": sum(1 for inv in w.investigations for s in inv.steps if s.outcome == "error"),
        "duplicate_model_calls": sum(n - 1 for n in role_counts.values() if n > 1),
        "dead_ends": sum(1 for e in w.evaluations if e.dead_end),
        "context_tokens_by_role": {role: round(sum(v) / len(v)) for role, v in sorted(prompt_by_role.items())},
        "context_growth": [(role, round(sum(prompt_by_role[role]) / len(prompt_by_role[role])))
                           for role in stage_order if role in prompt_by_role],
        "max_depth": max((e.depth for e in w.evaluations), default=None),
        "depth_within_limit": {"value": depth_ok[0], "samples": depth_ok[1], "limit": w.config.max_depth,
                               "source": "AGT-native: agent-sre DelegationChainDepth"},
        "termination": dict(Counter(e.termination.split(":")[0] for e in w.evaluations)),
    }


def _governance_metrics(w: _Window) -> dict[str, Any]:
    """Governance: gates per investigation, policy compliance via agent-os-kernel GovernanceMetrics."""
    compliance = agt.policy_compliance((c["role"], c["outcome"] != "policy_denied") for c in w.calls)
    return {
        "gates": {group: dict(Counter(e.group_status(group) for e in w.evaluations)) for group in GROUPS},
        "audit_completeness": {**_value_samples(agt.share(
            w.evaluations, lambda e: e.group_status("Audit") in (PASS, PENDING))), "source": "Sentinel-specific"},
        "policy_compliance": {**compliance, "source": "AGT-native: agent-os-kernel GovernanceMetrics"},
    }


def finite(value: Any) -> Any:
    """JSON-safe: infinities become None (no data), recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value
