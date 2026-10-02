"""
What is behind a number on the Agent Evaluation page: for each readiness
objective, the things it was computed from, row by row.

A Health row says "Evidence grounding 91.7% over 12 investigations". That is
an aggregate with nothing to act on. This module lists the twelve: which
investigation, of which review (its staged action and ticket), by which
agent, and what its own reading was -- with the ones that count against the
objective first. So a number leads to the work it describes: a person opens
the review it came from, or goes to the agent that produced it.

Every objective's population is the one the metric itself uses
(src/evaluation_metrics.py): the samples of the items listed here add up to
the metric's sample count, and tests/test_evaluation_evidence.py holds the
two together for every objective. Nothing here is a new measurement.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.evaluation import WindowEvaluation
from src.evaluation_metrics import subsets
from src.evaluation_types import PASS, PENDING, InvestigationEvaluation, _VERIFY_STEP_REJECTION
from src.model_telemetry import SUCCESS_OUTCOMES

INVESTIGATION, MODEL_CALL, AGENT_STEP, RECOMMENDATION = "investigation", "model call", "agent step", "recommendation"
_ABSTAIN = "abstain"
_CRITIC = "critic"


@dataclass(frozen=True)
class EvidenceItem:
    """One thing a Health number was computed from."""

    kind: str                       # investigation | model call | agent step | recommendation
    reading: str                    # its own reading, in words
    samples: int                    # how many of the objective's samples it is
    against: int                    # how many of those count against the objective
    at: datetime | None = None
    agents: tuple[str, ...] = ()    # the agent(s) it is about
    action_id: str | None = None    # the review (staged action, and its ticket) it belongs to
    airport: str | None = None
    alert_type: str | None = None
    review_status: str | None = None


@dataclass(frozen=True)
class _Scope:
    """A window, and the lookups that tie a call or a verdict back to its review."""

    window: WindowEvaluation
    target: float | None            # the objective's target (higher is better) ...
    limit: float | None             # ... or its limit (lower is better)
    sent: list[dict]
    responded: list[dict]
    decided: list[dict]
    by_subject: dict[str, InvestigationEvaluation]   # investigation id, or the alert the Critic assessed

    def of_investigation(self, e: InvestigationEvaluation, **item: Any) -> EvidenceItem:
        return EvidenceItem(kind=INVESTIGATION, at=e.opened_at, action_id=e.action_id, airport=e.airport,
                            alert_type=e.alert_type, review_status=e.action_status, **item)

    def of_call(self, call: dict, reading: str, against: bool) -> EvidenceItem:
        e = self.by_subject.get(call.get("subject") or "")
        return EvidenceItem(
            kind=MODEL_CALL, reading=reading, samples=1, against=int(against), at=call.get("created_at"),
            agents=(call["role"],) if call.get("role") else (), action_id=e.action_id if e else None,
            airport=e.airport if e else None, alert_type=e.alert_type if e else None,
            review_status=e.action_status if e else None)

    def of_verdict(self, verdict: dict, reading: str, against: bool) -> EvidenceItem:
        e = self.by_subject.get(verdict.get("alert_id") or "")
        return EvidenceItem(
            kind=RECOMMENDATION, reading=reading, samples=1, against=int(against), at=verdict.get("evaluated_at"),
            agents=(_CRITIC,), action_id=verdict.get("action_id") or (e.action_id if e else None),
            airport=verdict.get("airport_code") or (e.airport if e else None),
            alert_type=verdict.get("alert_type") or (e.alert_type if e else None),
            review_status=verdict.get("action_status"))


def _seconds(value: float) -> str:
    return f"{value:.1f} s" if value < 120 else f"{value / 60:.1f} min"


def _call_reading(call: dict) -> str:
    """A model call in words: how it ended, and how long it took."""
    outcome = str(call["outcome"]).replace("_", " ")
    error = f" ({call['error_type']})" if call.get("error_type") else ""
    return f"{outcome}{error}, {_seconds(call['latency_ms'] / 1000.0)}"


def _said(verdict: dict) -> str:
    confidence = f" at {verdict['confidence']:.0%}" if verdict.get("confidence") is not None else ""
    return f"Critic said {'escalate' if verdict.get('escalate') else 'do not escalate'}{confidence}"


def _decision(verdict: dict) -> str:
    by = f" by {verdict['decided_by']}" if verdict.get("decided_by") else ""
    return f"{str(verdict.get('action_status', '')).lower()}{by}"


# ── Decision quality: judged by the grounding gate and the operator's decision ──

def _decision_quality(s: _Scope) -> list[EvidenceItem]:
    items = []
    for e in s.window.investigations:
        scored = {name: r for name, r in e.decision_results.items() if r.get("verdict") != _ABSTAIN}
        if scored:
            failed = sorted(name for name, r in scored.items() if r.get("verdict") == "fail")
            items.append(s.of_investigation(
                e, samples=len(scored), against=len(failed),
                reading=(f"score {e.decision_score:.2f}" if e.decision_score is not None else "scored")
                + (f"; failed {', '.join(failed)}: " + "; ".join(str(scored[n].get("explanation", "")) for n in failed)
                   if failed else f" over {len(scored)} criteria")))
    return items


def _rejecting_agents(s: _Scope, e: InvestigationEvaluation) -> tuple[str, ...]:
    """The agents whose claims the grounding gate rejected in this investigation."""
    record = next((r for r in s.window.records if r.investigation_id == e.investigation_id), None)
    if record is None:
        return ()
    agents = [f.agent for f in record.findings if f.rejected_claims]
    if record.critique is not None and record.critique.rejected_claims:
        agents.append(record.critique.agent)
    # A rejected verification step is a recommendation, not a claim (src/evaluation.py grounding_counts).
    if record.brief is not None and any(r.reason != _VERIFY_STEP_REJECTION for r in record.brief.rejected_claims):
        agents.append(record.brief.agent)
    return tuple(dict.fromkeys(agents))


def _evidence_grounding(s: _Scope) -> list[EvidenceItem]:
    items = []
    for e in s.window.investigations:
        judged = e.decision_results.get("hallucination") or {}
        if judged and judged.get("verdict") != _ABSTAIN:
            total = e.claims_accepted + e.claims_rejected
            items.append(s.of_investigation(
                e, samples=1, against=int(e.grounding is not None and s.target is not None and e.grounding < s.target),
                agents=_rejecting_agents(s, e),
                reading=f"{e.claims_accepted} of {total} model claims cite evidence that was collected"))
    return items


def _unsupported_claims(s: _Scope) -> list[EvidenceItem]:
    return [s.of_investigation(
        e, samples=e.claims_accepted + e.claims_rejected, against=e.claims_rejected, agents=_rejecting_agents(s, e),
        reading=f"{e.claims_rejected} of {e.claims_accepted + e.claims_rejected} model claims rejected by the grounding gate")
        for e in s.window.investigations if e.claims_accepted + e.claims_rejected]


def _human_override(s: _Scope) -> list[EvidenceItem]:
    return [s.of_verdict(v, f"{_said(v)}; {_decision(v)}", v["action_status"] == "OVERRIDDEN") for v in s.decided]


def _missed_escalation(s: _Scope) -> list[EvidenceItem]:
    return [s.of_verdict(v, f"{_said(v)}; {_decision(v)}", not v.get("escalate"))
            for v in s.decided if v.get("action_status") == "APPROVED"]


def _false_escalation(s: _Scope) -> list[EvidenceItem]:
    return [s.of_verdict(v, f"{_said(v)}; {_decision(v)}", v["action_status"] == "REJECTED")
            for v in s.decided if v.get("escalate")]


def _calibration(s: _Scope) -> list[EvidenceItem]:
    """Each decided recommendation: against, when a person disagreed with a verdict the Critic was confident of."""
    items = []
    for v in s.decided:
        agreed = (v["action_status"] == "APPROVED") == bool(v.get("escalate"))
        items.append(s.of_verdict(
            v, f"{_said(v)}; {_decision(v)}: the operator {'agreed' if agreed else 'disagreed'}",
            not agreed and float(v["confidence"]) >= 0.5))
    return items


# ── Latency and cost ─────────────────────────────────────────────────────────

def _investigation_latency(s: _Scope) -> list[EvidenceItem]:
    items = []
    for e in s.window.investigations:
        if e.duration_s is not None:
            slowest = max(e.stages_s.items(), key=lambda kv: kv[1], default=None)
            items.append(s.of_investigation(
                e, samples=1, against=int(s.limit is not None and e.duration_s > s.limit),
                reading=f"{_seconds(e.duration_s)} opened to sealed"
                + (f"; slowest stage {slowest[0].replace('_', ' ')} {_seconds(slowest[1])}" if slowest else "")))
    return items


def _model_call_latency(s: _Scope) -> list[EvidenceItem]:
    return [s.of_call(c, _call_reading(c), s.limit is not None and c["latency_ms"] / 1000.0 > s.limit) for c in s.sent]


def _cost(s: _Scope) -> list[EvidenceItem]:
    return [s.of_investigation(
        e, samples=1, against=int(s.limit is not None and e.cost_usd > s.limit),
        reading=f"${e.cost_usd:.4f} over {e.model_calls} model call(s), {e.prompt_tokens + e.completion_tokens:,} tokens")
        for e in s.window.investigations if e.used_model and e.cost_usd is not None]


# ── Reliability and governance ───────────────────────────────────────────────

def _agent_failure(s: _Scope) -> list[EvidenceItem]:
    """Every agent step that tried a model, and every Critic or Planner call: against, when it did not succeed."""
    evaluations = {e.investigation_id: e for e in s.window.investigations}
    items = []
    for record in s.window.records:
        e = evaluations.get(record.investigation_id)
        for step in record.steps:
            if step.agent and step.detail != "deterministic":
                items.append(EvidenceItem(
                    kind=AGENT_STEP, samples=1, against=int(step.outcome != "ok"), at=step.started_at,
                    agents=(step.agent,), action_id=record.action_id, airport=e.airport if e else None,
                    alert_type=e.alert_type if e else None, review_status=e.action_status if e else None,
                    reading=("answered from the model" if step.outcome == "ok" else
                             f"{'fell back to its rules' if step.outcome == 'fallback' else 'errored'}"
                             + (f": {step.detail}" if step.detail else ""))))
    items += [s.of_call(c, _call_reading(c), c["outcome"] not in SUCCESS_OUTCOMES)
              for c in s.sent if c.get("role") in (_CRITIC, "planner")]
    return items


def _availability(s: _Scope) -> list[EvidenceItem]:
    return [s.of_call(c, _call_reading(c), c["outcome"] not in SUCCESS_OUTCOMES) for c in s.sent]


def _output_validity(s: _Scope) -> list[EvidenceItem]:
    return [s.of_call(c, _call_reading(c) + ("; needed the repair request" if c.get("repaired") else ""),
                      c["outcome"] != "ok") for c in s.responded]


def _audit(s: _Scope) -> list[EvidenceItem]:
    items = []
    for e in s.window.investigations:
        failing = [g for g in e.gates if g.group == "Audit" and g.status not in (PASS, PENDING, "N/A")]
        complete = e.group_status("Audit") in (PASS, PENDING)
        items.append(s.of_investigation(
            e, samples=1, against=int(not complete),
            reading="audit gate passed" if complete else
            "; ".join(f"{g.name}: {g.detail}" for g in failing) or f"audit gate {e.group_status('Audit')}"))
    return items


def _policy(s: _Scope) -> list[EvidenceItem]:
    return [s.of_call(c, "refused by policy" + (f" ({c['policy_rule']})" if c.get("policy_rule") else "")
                      if c["outcome"] == "policy_denied" else "allowed", c["outcome"] == "policy_denied")
            for c in s.window.calls]


_EVIDENCE: dict[str, Callable[[_Scope], list[EvidenceItem]]] = {
    "decision_quality": _decision_quality,
    "evidence_grounding": _evidence_grounding,
    "unsupported_claim_rate": _unsupported_claims,
    "human_override_rate": _human_override,
    "missed_escalation_rate": _missed_escalation,
    "false_escalation_rate": _false_escalation,
    "calibration_error": _calibration,
    "investigation_latency_p95_seconds": _investigation_latency,
    "model_call_latency_p95_seconds": _model_call_latency,
    "cost_per_investigation_usd": _cost,
    "agent_failure_rate": _agent_failure,
    "availability": _availability,
    "output_validity": _output_validity,
    "audit_completeness": _audit,
    "policy_compliance": _policy,
}
OBJECTIVES = tuple(_EVIDENCE)


def evidence_for(name: str, window: WindowEvaluation, *, target: float | None = None,
                 limit: float | None = None) -> list[EvidenceItem]:
    """
    What objective `name` was computed from in this window: what counts against it first, then the newest.
    `target` / `limit` are the objective's own (policies/model-slos.yaml), for the measures judged per item.
    """
    sent, responded, decided = subsets(window.calls, window.verdicts)
    by_subject: dict[str, InvestigationEvaluation] = {}
    for e in window.investigations:
        by_subject[e.investigation_id] = e
        if e.alert_id:
            by_subject.setdefault(e.alert_id, e)
    items = _EVIDENCE[name](_Scope(window, target, limit, sent, responded, decided, by_subject))
    return sorted(items, key=lambda i: (-i.against, -(i.at.timestamp() if isinstance(i.at, datetime) else 0.0)))
