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

import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src import agt_metrics as agt
from src import storage
from src.config import settings
from src.investigation.record import Investigation
from src.model_telemetry import SUCCESS_OUTCOMES

PASS, WARN, FAIL, PENDING, NOT_APPLICABLE = "PASS", "WARN", "FAIL", "PENDING", "N/A"
GROUPS = ("Quality", "Safety", "Cost", "Latency", "Audit")
DECIDED = ("APPROVED", "REJECTED", "OVERRIDDEN")
AGENT_STEPS = ("feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer")
PARALLEL = ("feed_diagnostician", "correlation_analyst")
RESPONDED = ("ok", "repaired", "incomplete", "invalid_output")
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
    accepted = rejected = 0
    finding_statements = {c.statement for f in inv.findings for c in f.claims}
    for finding in inv.findings:
        if finding.provenance.method == "llm":
            accepted += len(finding.claims)
        rejected += len(finding.rejected_claims)
    critique = inv.critique
    if critique is not None:
        if critique.provenance.method == "llm":
            statements = {c.statement for c in critique.challenges}
            accepted += sum(1 for c in critique.challenges if c.origin == "llm")
            alt = critique.strongest_alternative
            accepted += 1 if alt is not None and alt.statement not in statements else 0
        rejected += len(critique.rejected_claims)
    brief = inv.brief
    if brief is not None:
        if brief.provenance.method == "llm":
            accepted += sum(1 for c in brief.established if c.statement not in finding_statements)
        rejected += sum(1 for r in brief.rejected_claims if r.reason != _VERIFY_STEP_REJECTION)
    return accepted, rejected


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


def _call_totals(rows: list[dict]) -> dict[str, Any]:
    responded = [r for r in rows if r["outcome"] in RESPONDED]
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    unknown = sum(1 for r in rows if r.get("cost_usd") is None)
    return {
        "calls": len(rows),
        "attempts": sum(r.get("attempts") or 0 for r in rows),
        "retries": sum(max((r.get("attempts") or 0) - 1 - (1 if r.get("repaired") else 0), 0) for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
        "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
        "cost_usd": round(sum(costs), 6) if rows and not unknown else (None if rows else 0.0),
        "served_models": sorted({r["served_model"] for r in responded if r.get("served_model")}),
        "providers": sorted({r.get("provider") or "_OTHER" for r in rows if r["outcome"] != "policy_denied"}),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
    }


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
    depth = sum(1 for stage in (PARALLEL, ("red_team_critic",), ("watch_officer",))
                if any(s.step in stage for s in inv.steps))
    error_step = next((s for s in inv.steps if s.outcome == "error"), None)
    termination = "COMPLETE" if inv.status == "COMPLETE" else (
        f"FAILED at {error_step.step}: {error_step.detail}" if error_step else inv.status)

    confidence = verdict.get("confidence") if verdict else None
    if confidence is None and action is not None:
        confidence = action.critic_confidence
    escalate = verdict.get("escalate") if verdict and verdict.get("confidence") is not None else None
    status = action.status if action is not None else None
    decided_at = _parse(action.decided_at) if action is not None else None
    agreed = None
    if escalate is not None and status in DECIDED:
        agreed = (status == "APPROVED" and escalate) or (status == "REJECTED" and not escalate)

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
        depth=depth, termination=termination,
        confidence=float(confidence) if confidence is not None else None, escalate=escalate,
        action_status=status, decided_by=action.decided_by if action is not None else None,
        decided_at=decided_at, time_to_decision_s=_seconds(inv.completed_at, decided_at) if status in DECIDED else None,
        operator_agreed=agreed, decision_score=report.score, decision_results=report.results,
    )
    evaluation.gates = _gates(inv, evaluation, calls=calls, audit=audit, config=config, report=report)
    return evaluation


def _gates(inv: Investigation, ev: InvestigationEvaluation, *, calls: list[dict], audit: list[dict],
           config: GateConfig, report: agt.DecisionReport) -> list[Gate]:
    gates: list[Gate] = []
    add = gates.append

    # Quality
    untraceable = inv.untraceable_claims()
    add(Gate("Quality", "Grounded", PASS if not untraceable else FAIL,
             "every claim in the record cites collected evidence" if not untraceable
             else f"claims without evidence: {', '.join(untraceable)}", "Sentinel-specific (evidence-id traceability)"))
    hallucination = report.results.get("hallucination", {})
    if hallucination.get("verdict") in (None, "abstain"):
        add(Gate("Quality", "Model claims grounded", NOT_APPLICABLE, "no model output in this investigation",
                 "AGT-native: agent-sre EvaluationEngine"))
    else:
        add(Gate("Quality", "Model claims grounded", PASS if hallucination["verdict"] == "pass" else WARN,
                 hallucination["explanation"] + ("" if hallucination["verdict"] == "pass"
                                                 else "; the rejected ones never reached the record"),
                 "AGT-native: agent-sre EvaluationEngine"))
    missing = [c for c in (inv.critique.challenges if inv.critique else [])
               if c.kind == "MISSING_EVIDENCE" and c.origin == "rule"]
    add(Gate("Quality", "Required sources", PASS if not missing else WARN,
             "every source the rules expect was available" if not missing
             else "; ".join(c.statement for c in missing), "Sentinel-specific"))
    add(Gate("Quality", "Calibrated", NOT_APPLICABLE,
             "measured across investigations (ECE on the Agent Evaluation page)", "Sentinel-specific"))
    if inv.brief is None:
        add(Gate("Quality", "Contradictions surfaced", FAIL if inv.status == "COMPLETE" else NOT_APPLICABLE,
                 "no brief was produced", "Sentinel-specific"))
    else:
        dropped = {c.challenge_id for c in (inv.critique.challenges if inv.critique else [])} - set(inv.brief.open_challenges)
        add(Gate("Quality", "Contradictions surfaced", PASS if not dropped else FAIL,
                 f"{ev.challenges} challenge(s), {ev.contradictions} contradicting, all carried to the brief"
                 if not dropped else f"challenges missing from the brief: {sorted(dropped)}", "Sentinel-specific"))

    # Safety
    created = audit[0] if audit else None
    add(Gate("Safety", "Advisory only",
             PASS if created and created.get("actor") == "system" and created.get("new_status") in ("PENDING", "EXPIRED")
             else FAIL,
             "the action was staged by the alert rules for a person to decide" if created
             else "no audit record of how the action was staged", "Sentinel-specific (audit log)"))
    later = audit[1:]
    automated = [a for a in later if a.get("actor") == "system"]
    add(Gate("Safety", "No automated decision", PASS if not automated else FAIL,
             "every change after staging was made by a named person" if not automated
             else f"{len(automated)} change(s) by the system after staging", "Sentinel-specific (audit log)"))
    if ev.action_status in DECIDED:
        actor = ev.decided_by or ""
        verified = actor.endswith(VERIFIED_ACTOR_SUFFIXES)
        add(Gate("Safety", "Human approval", PASS if verified else WARN if actor and actor != "system" else FAIL,
                 f"decided by {actor}" + ("" if verified else " (identity not verified)"), "Sentinel-specific (audit log)"))
    else:
        add(Gate("Safety", "Human approval", PENDING, "awaiting a decision" if ev.action_status == "PENDING"
                 else f"action {ev.action_status or 'not found'}", "Sentinel-specific (audit log)"))
    unevaluated = [c for c in calls if c.get("policy_action") in (None, "unevaluated")]
    refused = [c for c in calls if c["outcome"] == "policy_denied"]
    add(Gate("Safety", "Policy enforced",
             FAIL if unevaluated else WARN if refused else PASS if calls else NOT_APPLICABLE,
             f"{len(unevaluated)} model call(s) without a policy decision" if unevaluated
             else f"{len(refused)} model call(s) refused by policy; the agent fell back to its rules" if refused
             else f"all {len(calls)} model call(s) allowed by the model-use and tool-use policies" if calls
             else "no model calls", "AGT-native: agent-os-kernel PolicyEvaluator"))

    # Cost and latency: agent-os-kernel BudgetTracker
    tokens = ev.prompt_tokens + ev.completion_tokens
    tool_calls = ev.model_calls + 1          # the model calls and the evidence collection
    exceeded, _ = agt.budget_check(config.budget, tokens=tokens, tool_calls=tool_calls,
                                   cost_usd=ev.cost_usd, duration_s=None)
    cost_detail = (f"${ev.cost_usd:.4f}" if ev.cost_usd is not None else "cost unknown") + \
        f", {tokens} tokens, {tool_calls} tool calls"
    add(Gate("Cost", "Budget", FAIL if exceeded else WARN if ev.cost_usd is None and ev.model_calls else PASS,
             ("over budget: " + "; ".join(exceeded)) if exceeded else cost_detail, "AGT-native: agent-os-kernel BudgetTracker"))
    add(Gate("Cost", "Retries", PASS if ev.retries <= config.max_retries else FAIL,
             f"{ev.retries} re-sent request(s) (limit {config.max_retries})", "Sentinel-specific"))
    over, _ = agt.budget_check(agt.Budget(None, None, None, config.budget.max_duration_seconds),
                               tokens=0, tool_calls=0, cost_usd=None, duration_s=ev.duration_s)
    limit = config.budget.max_duration_seconds
    add(Gate("Latency", "SLA",
             NOT_APPLICABLE if ev.duration_s is None or limit is None else FAIL if over else PASS,
             "not completed" if ev.duration_s is None else
             f"{ev.duration_s:.1f} s" + (f" (limit {limit:g} s)" if limit is not None else " (no limit set)"),
             "AGT-native: agent-os-kernel BudgetTracker"))
    timeouts = ev.outcomes.get("timeout", 0)
    add(Gate("Latency", "No timeouts", PASS if not timeouts else WARN,
             "no model call timed out" if not timeouts else f"{timeouts} model call(s) timed out", "Sentinel-specific"))

    # Audit
    add(Gate("Audit", "Sealed record", PASS if inv.verify() else FAIL,
             f"sha256 {(inv.record_sha256 or '')[:12]}… verifies" if inv.verify() else "record does not verify",
             "Sentinel-specific (SHA-256)"))
    provenance = [p for p in _provenances(inv) if p.method == "llm"]
    incomplete = [p for p in provenance if not (p.model and p.prompt_sha256 and p.input_sha256 and p.prompt_version)]
    add(Gate("Audit", "Model and prompt recorded",
             NOT_APPLICABLE if not provenance else PASS if not incomplete else FAIL,
             "no model output" if not provenance else
             f"model, prompt version and input/prompt hashes recorded for {len(provenance)} model step(s)"
             if not incomplete else f"{len(incomplete)} model step(s) without full provenance", "Sentinel-specific"))
    llm_steps = [s for s in inv.steps if s.agent and s.detail != "deterministic"]
    add(Gate("Audit", "Model calls on record",
             NOT_APPLICABLE if not llm_steps else PASS if len(calls) >= len(llm_steps) else FAIL,
             "no model was used" if not llm_steps else
             f"{len(calls)} call record(s) for {len(llm_steps)} model step(s)"
             + ("" if len(calls) >= len(llm_steps) else " (recorded before calls were attributed to investigations?)"),
             "Sentinel-specific (model_calls)"))
    if ev.action_status in DECIDED:
        captured = any(a.get("new_status") == ev.action_status and a.get("actor") for a in later)
        add(Gate("Audit", "Human decision captured", PASS if captured else FAIL,
                 "the decision is in the append-only audit log with its actor" if captured
                 else "no audit entry for the decision", "Sentinel-specific (audit log)"))
    else:
        add(Gate("Audit", "Human decision captured", PENDING, "no decision yet", "Sentinel-specific (audit log)"))
    return gates


def _provenances(inv: Investigation):
    out = [f.provenance for f in inv.findings]
    if inv.critique:
        out.append(inv.critique.provenance)
    if inv.brief:
        out.append(inv.brief.provenance)
    return out


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


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def aggregate(evaluations: list[InvestigationEvaluation], calls: list[dict], verdicts: list[dict],
              investigations: list[Investigation], config: GateConfig, engine: Any) -> dict[str, Any]:
    """The metric groups for a window. Every value names its kind: AGT-native, AGT-compatible or Sentinel-specific."""
    from agent_sre.evals import EvalCriterion

    model_runs = [e for e in evaluations if e.used_model]
    sent = [c for c in calls if c["outcome"] != "policy_denied"]
    responded = [c for c in sent if c["outcome"] in RESPONDED]
    decided = [v for v in verdicts if v.get("confidence") is not None and v.get("action_status") in DECIDED]

    # Decision quality
    score, scored = agt.engine_average(engine)
    grounding, grounded_runs = agt.engine_average(engine, EvalCriterion.HALLUCINATION)
    accuracy, accuracy_n = agt.engine_average(engine, EvalCriterion.CORRECTNESS)
    judgments = [True] * sum(e.claims_rejected for e in evaluations) + \
        [False] * sum(e.claims_accepted for e in evaluations)
    unsupported, claims_judged = agt.hallucination_rate(judgments)
    escalations = [v for v in decided if v.get("escalate")]
    approvals = [v for v in decided if v.get("action_status") == "APPROVED"]
    false_escalation = agt.share(escalations, lambda v: v["action_status"] == "REJECTED")
    missed_escalation = agt.share(approvals, lambda v: not v.get("escalate"))
    override = agt.share(decided, lambda v: v["action_status"] == "OVERRIDDEN")
    calibration = agt.expected_calibration_error(
        (float(v["confidence"]), (v["action_status"] == "APPROVED") == bool(v.get("escalate")))
        for v in decided)
    contradiction = agt.share(evaluations, lambda e: e.contradictions > 0)
    quality = {
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
        "evidence_improvement": {"value": _mean(e.useful_items for e in model_runs), "samples": len(model_runs),
                                 "source": "Sentinel-specific"},
    }

    # Latency: agent-sre ResponseLatency for every percentile
    by_agent: dict[str, list[float]] = {}
    for inv in investigations:
        for step in inv.steps:
            if step.agent:
                by_agent.setdefault(step.agent, []).append(_seconds(step.started_at, step.finished_at))
    for c in calls:
        if c.get("role") in ("critic", "planner") and c["outcome"] != "policy_denied":
            by_agent.setdefault(c["role"], []).append(c["latency_ms"] / 1000.0)
    latency = {
        "investigation": agt.percentiles(e.duration_s for e in evaluations),
        "model_call": agt.percentiles(c["latency_ms"] / 1000.0 for c in sent),
        "evidence_collection": agt.percentiles(e.stages_s.get("collect_evidence") for e in evaluations),
        "handoff": agt.percentiles(e.handoff_s for e in evaluations),
        "queue": agt.percentiles(e.queue_s for e in evaluations),
        "time_to_decision": agt.percentiles(e.time_to_decision_s for e in evaluations),
        "by_agent": {agent: agt.percentiles(values) for agent, values in sorted(by_agent.items())},
        "by_stage": {stage: agt.percentiles(e.stages_s.get(stage) for e in evaluations)
                     for stage in ("collect_evidence", "specialists", "red_team_critic", "watch_officer", "seal")},
        "timeout_rate": dict(zip(("value", "samples"), agt.share(sent, lambda c: c["outcome"] == "timeout"))),
        "retry_rate": dict(zip(("value", "samples"),
                               agt.share(sent, lambda c: (c.get("attempts") or 0) - (1 if c.get("repaired") else 0) > 1))),
    }

    # Cost: agent-sre CostGuard
    known = [c for c in calls if c.get("cost_usd") is not None]
    runs_with_cost = [e for e in model_runs if e.cost_usd is not None]
    resolved = [e for e in evaluations if e.action_status in DECIDED]
    total_spend = sum(c["cost_usd"] for c in known)
    useful = sum(e.useful_items for e in runs_with_cost)
    alert_spend = [(e.alert_id or e.investigation_id, e.investigation_id,
                    (e.cost_usd or 0.0) + (e.critic_cost_usd or 0.0))
                   for e in runs_with_cost]
    cost = {
        "tokens": {"input": sum(c.get("prompt_tokens") or 0 for c in sent),
                   "output": sum(c.get("completion_tokens") or 0 for c in sent)},
        "total_usd": round(total_spend, 6) if known else None,
        "calls_with_cost": len(known), "calls_cost_unknown": len(responded) - len([c for c in responded
                                                                                   if c.get("cost_usd") is not None]),
        "per_invocation_by_role": agt.spend((c["role"], c["call_id"], c["cost_usd"]) for c in known),
        "per_investigation": agt.spend(("investigation", e.investigation_id, e.cost_usd) for e in runs_with_cost)
                                .get("investigation"),
        "per_alert": agt.spend(("alert", key, value) for key, _, value in alert_spend).get("alert"),
        "by_model": agt.spend((c.get("served_model") or "(unknown)", c["call_id"], c["cost_usd"]) for c in known),
        "per_resolved_investigation": (sum(e.cost_usd or 0.0 for e in runs_with_cost) / len(resolved)
                                       if resolved and runs_with_cost else None),
        "per_useful_item": total_spend / useful if useful and known else None,
        "retry_cost_usd": round(sum(c["cost_usd"] for c in known if (c.get("attempts") or 0) > 1), 6) if known else None,
        "failed_call_cost_usd": round(sum(c["cost_usd"] for c in known if c["outcome"] not in SUCCESS_OUTCOMES), 6)
                                if known else None,
        "sources": {"per_invocation, per_investigation, per_alert, by_model": "AGT-native: agent-sre CostGuard",
                    "per_resolved, per_useful_item, retry, failed": "Sentinel-specific"},
    }

    # Reliability: agent-sre FleetManager, TaskSuccessRate
    step_events = [
        (s.agent, s.outcome == "ok", _seconds(s.started_at, s.finished_at) or 0.0, 0.0)
        for inv in investigations for s in inv.steps if s.agent and s.detail != "deterministic"
    ] + [(c["role"], c["outcome"] in SUCCESS_OUTCOMES, c["latency_ms"] / 1000.0, c.get("cost_usd") or 0.0)
         for c in sent if c.get("role") in ("critic", "planner")]
    agents = agt.fleet(step_events)
    providers = agt.fleet((c.get("provider") or "_OTHER", c["outcome"] in SUCCESS_OUTCOMES,
                           c["latency_ms"] / 1000.0, c.get("cost_usd") or 0.0) for c in sent)
    completion = agt.success_rate(e.status == "COMPLETE" for e in evaluations)
    reliability = {
        "agents": agents,
        "agent_failure_rate": {"value": (1 - agents["success_rate"]) if agents["success_rate"] is not None else None,
                               "samples": agents["events"], "source": "AGT-native: agent-sre FleetManager"},
        "call_success_rate": {**dict(zip(("value", "samples"), agt.success_rate(c["outcome"] in SUCCESS_OUTCOMES
                                                                                 for c in sent))),
                              "source": "AGT-native: agent-sre TaskSuccessRate"},
        "providers": providers,
        "outcomes": dict(Counter(c["outcome"] for c in calls)),
        "fallback_rate": dict(zip(("value", "samples"), agt.share(
            [s for inv in investigations for s in inv.steps if s.agent and s.detail != "deterministic"],
            lambda s: s.outcome == "fallback"))),
        "schema_failure_rate": dict(zip(("value", "samples"), agt.share(
            responded, lambda c: c["outcome"] in ("incomplete", "invalid_output")))),
        "malformed_output_rate": dict(zip(("value", "samples"), agt.share(
            responded, lambda c: c["outcome"] == "invalid_output"))),
        "output_validity": {**dict(zip(("value", "samples"), agt.share(responded, lambda c: c["outcome"] == "ok"))),
                            "source": "Sentinel-specific"},
        "investigation_completion": {"value": completion[0], "samples": completion[1],
                                     "source": "AGT-native: agent-sre TaskSuccessRate"},
    }

    # Orchestration
    depth_ok = agt.depth_compliance((e.depth for e in evaluations), config.max_depth)
    role_counts = Counter((c.get("subject"), c["role"]) for c in calls if c.get("subject"))
    prompt_by_role: dict[str, list[int]] = {}
    for c in sent:
        if c.get("prompt_tokens"):
            prompt_by_role.setdefault(c["role"], []).append(c["prompt_tokens"])
    stage_order = ("feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer")
    orchestration = {
        "agents_invoked": {agent: m["events"] for agent, m in agents["members"].items()},
        "tool_calls_per_investigation": _mean(e.model_calls + 1 for e in evaluations),
        "handoffs_per_investigation": _mean(len(e.stages_s) - 1 for e in evaluations if e.stages_s),
        "parallel_stage": "feed_diagnostician + correlation_analyst",
        "dependency_failures": sum(1 for inv in investigations for s in inv.steps if s.outcome == "error"),
        "duplicate_model_calls": sum(n - 1 for n in role_counts.values() if n > 1),
        "dead_ends": sum(1 for e in evaluations if e.dead_end),
        "context_tokens_by_role": {role: round(sum(v) / len(v)) for role, v in sorted(prompt_by_role.items())},
        "context_growth": [(role, round(sum(prompt_by_role[role]) / len(prompt_by_role[role])))
                           for role in stage_order if role in prompt_by_role],
        "max_depth": max((e.depth for e in evaluations), default=None),
        "depth_within_limit": {"value": depth_ok[0], "samples": depth_ok[1], "limit": config.max_depth,
                               "source": "AGT-native: agent-sre DelegationChainDepth"},
        "termination": dict(Counter(e.termination.split(":")[0] for e in evaluations)),
    }

    # Governance: gates per investigation, policy compliance via agent-os-kernel GovernanceMetrics
    compliance = agt.policy_compliance((c["role"], c["outcome"] != "policy_denied") for c in calls)
    governance = {
        "gates": {group: dict(Counter(e.group_status(group) for e in evaluations)) for group in GROUPS},
        "audit_completeness": {**dict(zip(("value", "samples"), agt.share(
            evaluations, lambda e: e.group_status("Audit") in (PASS, PENDING)))), "source": "Sentinel-specific"},
        "policy_compliance": {**compliance, "source": "AGT-native: agent-os-kernel GovernanceMetrics"},
    }
    return {"quality": quality, "latency": latency, "cost": cost, "reliability": reliability,
            "orchestration": orchestration, "governance": governance,
            "counts": {"investigations": len(evaluations), "with_model": len(model_runs), "calls": len(calls),
                       "decided": len(decided)}}


def finite(value: Any) -> Any:
    """JSON-safe: infinities become None (no data), recursively."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value
