"""
Agent metrics: the Agent Governance Toolkit computes them, OpenTelemetry
carries them.

Every agent metric Sentinel reports comes from an AGT component (agent-sre or
agent-os-kernel) unless this module says otherwise, next to the code that
replaces it, with one of two reasons:

  AGT bug       a test in tests/test_agt_known_bugs.py shows AGT returning the
                wrong value. Those tests are strict xfails: when an AGT
                release fixes the bug they start passing, the suite fails,
                and the in-repo code here should be removed in favor of AGT.
  no equivalent AGT has no metric that measures this.

How AGT is used safely: every window builds fresh AGT objects from the
durable record (model_calls, sealed investigations, the audit log) and reads
them only through the paths those tests show to be correct -- an SLI's
collect(), never its current_value() or compliance(); CostGuard for means,
never CostPerTask; an EvaluationEngine whose empty history is "no data" and
whose judge errors fail a required criterion. A value that cannot be
computed is None ("no data"), never a placeholder.

The mapping from every metric in the evaluation design to its source is in
docs/agent-metrics.md.
"""
from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from agent_os.metrics import GovernanceMetrics
    from agent_os.policies.budget import BudgetPolicy, BudgetTracker
    from agent_sre.cost.guard import CostGuard
    from agent_sre.evals import EvalCriterion, EvalInput, EvalResult, EvalSuite, EvaluationEngine, Verdict
    from agent_sre.fleet import FleetManager
    from agent_sre.slo.indicators import DelegationChainDepth, HallucinationRate, ResponseLatency, TaskSuccessRate

PERCENTILES = (0.50, 0.95, 0.99)


# ── Latency: agent-sre ResponseLatency ───────────────────────────────────────

def percentiles(values_s: Iterable[float | None], ps: tuple[float, ...] = PERCENTILES) -> dict[str, float | None]:
    """
    {"p50": s, "p95": s, "p99": s, "n": count} from agent-sre ResponseLatency,
    one fresh instance per percentile (its current_value() ignores its
    window, bug E, so an instance never outlives the values it is given).
    """
    values = [float(v) for v in values_s if v is not None]
    out: dict[str, float | None] = {"n": len(values)}
    for p in ps:
        sli = ResponseLatency(target_ms=1.0, percentile=p, window="30d")
        for v in values:
            sli.record_latency(v * 1000.0)
        value = sli.current_value()
        out[f"p{int(p * 100)}"] = round(value / 1000.0, 3) if value is not None else None
    return out


# ── Rates: agent-sre TaskSuccessRate / HallucinationRate via collect() ───────

def success_rate(outcomes: Iterable[bool]) -> tuple[float | None, int]:
    """(share of True, count) from agent-sre TaskSuccessRate.collect() (bug C avoided)."""
    items = list(outcomes)
    if not items:
        return None, 0
    sli = TaskSuccessRate(target=0.0, window="30d")
    for ok in items:
        sli.record_task(bool(ok))
    return sli.collect().value, len(items)


def hallucination_rate(judgments: Iterable[bool]) -> tuple[float | None, int]:
    """
    (share of model claims the grounding gate rejected, claims judged) from
    agent-sre HallucinationRate.collect(). The judge is Sentinel's grounding
    gate: a claim is hallucinated when it cites nothing, or evidence that was
    never collected (src/investigation/specialists.py ground()).
    """
    items = list(judgments)
    if not items:
        return None, 0
    sli = HallucinationRate(target=0.0, window="30d")
    for hallucinated in items:
        sli.record_evaluation(hallucinated=bool(hallucinated))
    return sli.collect().value, len(items)


# ── Cost: agent-sre CostGuard ────────────────────────────────────────────────

def spend(entries: Iterable[tuple[str, str, float]]) -> dict[str, dict[str, float | int]]:
    """
    {key: {"spent_usd", "tasks", "mean_usd"}} from agent-sre CostGuard, one
    entry per (key, task_id, cost). Budgets are set out of reach: this is
    accounting over a closed window, not enforcement.
    """
    guard = CostGuard(per_task_limit=1e12, per_agent_daily_limit=1e12, org_monthly_budget=0.0,
                      anomaly_detection=False, auto_throttle=False)
    keys: set[str] = set()
    for key, task_id, cost in entries:
        guard.record_cost(agent_id=key, task_id=task_id, cost_usd=float(cost))
        keys.add(key)
    out = {}
    for key in sorted(keys):
        budget = guard.get_budget(key)
        out[key] = {"spent_usd": round(budget.spent_today_usd, 6), "tasks": budget.task_count_today,
                    "mean_usd": round(budget.avg_cost_per_task, 6)}
    return out


# ── Reliability and invocations: agent-sre FleetManager ──────────────────────

def fleet(events: Iterable[tuple[str, bool, float, float]]) -> dict[str, Any]:
    """
    Per member and fleet-wide success rate, mean latency, spend and count
    from agent-sre FleetManager; one event per (member, success, latency_s,
    cost_usd). A member is an agent, or a provider for provider availability.
    """
    manager = FleetManager()
    for member, success, latency_s, cost in events:
        if manager.get_agent(member) is None:
            manager.register(member)
        manager.record_event(member, success=bool(success), latency_ms=float(latency_s) * 1000.0,
                             cost_usd=float(cost or 0.0))
    members = {}
    for member in sorted(manager.agent_ids):
        health = manager.agent_health(member)
        members[member] = {
            "success_rate": health.success_rate, "events": health.event_count,
            "mean_latency_s": round(health.avg_latency_ms / 1000.0, 3) if health.avg_latency_ms is not None else None,
            "spent_usd": round(health.total_cost_usd, 6),
        }
    status = manager.status()
    return {"members": members, "success_rate": status.fleet_success_rate, "events": status.total_events}


# ── Governance: agent-os-kernel GovernanceMetrics and BudgetTracker ──────────

def policy_compliance(decisions: Iterable[tuple[str, bool]]) -> dict[str, Any]:
    """Allowed share of policy checks, per role and overall, from agent-os-kernel GovernanceMetrics."""
    governance = GovernanceMetrics()
    for role, allowed in decisions:
        governance.record_check(role, latency_ms=0.0, approved=bool(allowed))
    snap = governance.snapshot()
    total = snap["total_checks"]
    return {"checks": total, "allowed": snap["approvals"], "refused": snap["violations"],
            "rate": snap["approvals"] / total if total else None, "by_role": snap["adapters"]}


@dataclass(frozen=True)
class Budget:
    """The per-investigation limits (policies/model-slos.yaml `gates`)."""

    max_cost_usd: float | None
    max_tokens: int | None
    max_tool_calls: int | None
    max_duration_seconds: float | None


def budget_check(budget: Budget, *, tokens: int, tool_calls: int, cost_usd: float | None,
                 duration_s: float | None) -> tuple[list[str], dict[str, float | None]]:
    """(exceeded reasons, utilization) from agent-os-kernel BudgetTracker. Unknown cost or duration is not counted."""
    tracker = BudgetTracker(BudgetPolicy(
        max_tokens=budget.max_tokens, max_tool_calls=budget.max_tool_calls,
        max_cost_usd=budget.max_cost_usd, max_duration_seconds=budget.max_duration_seconds,
    ))
    tracker.record_tokens(int(tokens))
    for _ in range(int(tool_calls)):
        tracker.record_tool_call()
    if cost_usd is not None:
        tracker.record_cost(float(cost_usd))
    if duration_s is not None:
        tracker.record_duration(float(duration_s))
    return tracker.exceeded_reasons(), tracker.utilization()


def depth_compliance(depths: Iterable[int], max_depth: int) -> tuple[float | None, int]:
    """Share of runs within max_depth, from agent-sre DelegationChainDepth (its compliance() is lower-is-better, correctly)."""
    items = list(depths)
    if not items:
        return None, 0
    sli = DelegationChainDepth(max_depth=max_depth, window="30d")
    for depth in items:
        sli.record_depth(int(depth))
    return sli.compliance(), len(items)


# ── Decision quality: agent-sre EvaluationEngine with Sentinel's judge ───────

class EvidenceJudge:
    """
    A JudgeProtocol for agent-sre's EvaluationEngine that judges only from
    evidence the agents do not control, carried in EvalInput.metadata:

      hallucination  grounding: share of the model's claims the grounding
                     gate accepted (ABSTAIN when no model output was judged)
      correctness    the operator's decision against the recommendation
                     (ABSTAIN until an operator decides)
    """

    judge_id = "sentinel-evidence-judge"

    def evaluate(self, eval_input: EvalInput, criterion: EvalCriterion) -> EvalResult:
        meta = eval_input.metadata
        if criterion == EvalCriterion.HALLUCINATION:
            judged = meta.get("claims_judged") or 0
            if not judged:
                return EvalResult(criterion=criterion, verdict=Verdict.ABSTAIN, score=0.0,
                                  explanation="no model output was judged by the grounding gate")
            score = meta["claims_accepted"] / judged
            return EvalResult(criterion=criterion, verdict=Verdict.PASS if meta["claims_accepted"] == judged
                              else Verdict.FAIL, score=score,
                              explanation=f"{meta['claims_accepted']} of {judged} model claims cited collected evidence")
        if criterion == EvalCriterion.CORRECTNESS:
            agreed = meta.get("operator_agreed")
            if agreed is None:
                return EvalResult(criterion=criterion, verdict=Verdict.ABSTAIN, score=0.0,
                                  explanation="no operator decision yet")
            return EvalResult(criterion=criterion, verdict=Verdict.PASS if agreed else Verdict.FAIL,
                              score=1.0 if agreed else 0.0,
                              explanation="the operator's decision matched the recommendation" if agreed
                              else "the operator decided against the recommendation")
        return EvalResult(criterion=criterion, verdict=Verdict.ABSTAIN, score=0.0, explanation="not judged")


DECISION_SUITE = EvalSuite(
    name="sentinel-decision-quality",
    criteria=[EvalCriterion.HALLUCINATION, EvalCriterion.CORRECTNESS],
    required_criteria=[EvalCriterion.HALLUCINATION],
    min_score=0.9,
)


@dataclass
class DecisionReport:
    score: float | None          # EvalReport.overall_score; None when every criterion abstained
    passed: bool | None
    results: dict[str, dict[str, Any]]


def evaluate_decision(engine: EvaluationEngine, metadata: dict[str, Any]) -> DecisionReport:
    """
    One investigation through agent-sre's EvaluationEngine. Two host-side
    guards (bugs H, J): a report where every criterion abstained has no
    score, and a required criterion the judge could not evaluate fails.
    """
    report = engine.run(EvalInput(query="", response="", metadata=metadata), DECISION_SUITE)
    results = {r.criterion.value: {"verdict": r.verdict.value, "score": r.score, "explanation": r.explanation}
               for r in report.results}
    scored = [r for r in report.results if r.verdict != Verdict.ABSTAIN]
    judge_failed = any(r.criterion in DECISION_SUITE.required_criteria and r.verdict == Verdict.ABSTAIN
                       and r.explanation.startswith("Judge error") for r in report.results)
    if not scored:
        return DecisionReport(score=None, passed=None, results=results)
    return DecisionReport(score=round(report.overall_score, 4), passed=report.overall_pass and not judge_failed,
                          results=results)


def engine_average(engine: EvaluationEngine, criterion: EvalCriterion | None = None) -> tuple[float | None, int]:
    """
    (mean score, results scored) from EvaluationEngine.average_score(); None
    with nothing scored (bug H: the engine itself returns 0.0).
    get_stats() is never used (bug I).
    """
    scored = [r for report in engine.history for r in report.results
              if r.verdict != Verdict.ABSTAIN and (criterion is None or r.criterion == criterion)]
    if not scored:
        return None, 0
    return round(engine.average_score(criterion), 4), len(scored)


def new_engine() -> EvaluationEngine:
    return EvaluationEngine(EvidenceJudge())


# ── In-repo: AGT has a bug here, or no equivalent ────────────────────────────

def expected_calibration_error(pairs: Iterable[tuple[float, bool]], bins: int = 10) -> tuple[float | None, int]:
    """
    Expected calibration error of stated confidences against outcomes: the
    size-weighted mean, over equal-width confidence bins, of
    |mean stated confidence - observed success rate|. (value, samples);
    value is None with no samples.

    In-repo -- AGT bug: agent-sre's CalibrationDeltaSLI compares the mean
    confidence with the mean success rate over every prediction at once, so
    over- and under-confidence cancel: a model that is certain and always
    wrong half the time, and certain it is wrong and always right the other
    half, reads as perfectly calibrated
    (test_agt_calibration_delta_detects_offsetting_miscalibration).
    """
    counts = [0] * bins
    confidence = [0.0] * bins
    correct = [0.0] * bins
    n = 0
    for stated, outcome in pairs:
        stated = min(max(float(stated), 0.0), 1.0)
        index = min(int(stated * bins), bins - 1)
        counts[index] += 1
        confidence[index] += stated
        correct[index] += 1.0 if outcome else 0.0
        n += 1
    if n == 0:
        return None, 0
    error = sum(
        (counts[i] / n) * abs(confidence[i] / counts[i] - correct[i] / counts[i])
        for i in range(bins) if counts[i]
    )
    return round(error, 6), n


def share(items: Iterable[Any], predicate: Callable[[Any], bool]) -> tuple[float | None, int]:
    """
    (share matching, count) for a rate AGT has no metric for -- false and
    missed escalations, overrides, contradictions, timeouts, retries,
    fallbacks, schema failures. In-repo: no equivalent.
    """
    items = list(items)
    if not items:
        return None, 0
    return sum(1 for i in items if predicate(i)) / len(items), len(items)
