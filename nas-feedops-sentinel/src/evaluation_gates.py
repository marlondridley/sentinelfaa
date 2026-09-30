"""
The gates an investigation is judged against: Quality, Safety, Cost, Latency and Audit, one function per gate.

Every gate judges evidence the agents do not control (the grounding gate's check of each cited evidence id, the
audit log, the gateway's usage figures, the sealed record's SHA-256) and names its kind first: AGT-native,
AGT-compatible or Sentinel-specific.
"""
from __future__ import annotations

from src import agt_metrics as agt
from src.evaluation_types import (
    DECIDED, FAIL, NOT_APPLICABLE, PASS, PENDING, VERIFIED_ACTOR_SUFFIXES, WARN, Gate, GateConfig,
    InvestigationEvaluation,
)
from src.investigation.record import Investigation


# One function per gate, grouped as the page groups them. Each takes only what it judges.

def evaluate_gates(inv: Investigation, ev: InvestigationEvaluation, *, calls: list[dict], audit: list[dict],
           config: GateConfig, report: agt.DecisionReport) -> list[Gate]:
    return [
        *_quality_gates(inv, ev, report),
        *_safety_gates(ev, calls, audit),
        *_cost_and_latency_gates(ev, config),
        *_audit_gates(inv, ev, calls, audit),
    ]


def _quality_gates(inv: Investigation, ev: InvestigationEvaluation, report: agt.DecisionReport) -> list[Gate]:
    return [
        _grounded_gate(inv),
        _model_claims_gate(report),
        _required_sources_gate(inv),
        Gate("Quality", "Calibrated", NOT_APPLICABLE,
             "measured across investigations (ECE on the Agent Evaluation page)", "Sentinel-specific"),
        _contradictions_gate(inv, ev),
    ]


def _grounded_gate(inv: Investigation) -> Gate:
    untraceable = inv.untraceable_claims()
    return Gate("Quality", "Grounded", PASS if not untraceable else FAIL,
                "every claim in the record cites collected evidence" if not untraceable
                else f"claims without evidence: {', '.join(untraceable)}",
                "Sentinel-specific (evidence-id traceability)")


def _model_claims_gate(report: agt.DecisionReport) -> Gate:
    hallucination = report.results.get("hallucination", {})
    if hallucination.get("verdict") in (None, "abstain"):
        return Gate("Quality", "Model claims grounded", NOT_APPLICABLE, "no model output in this investigation",
                    "AGT-native: agent-sre EvaluationEngine")
    return Gate("Quality", "Model claims grounded", PASS if hallucination["verdict"] == "pass" else WARN,
                hallucination["explanation"] + ("" if hallucination["verdict"] == "pass"
                                                else "; the rejected ones never reached the record"),
                "AGT-native: agent-sre EvaluationEngine")


def _required_sources_gate(inv: Investigation) -> Gate:
    missing = [c for c in (inv.critique.challenges if inv.critique else [])
               if c.kind == "MISSING_EVIDENCE" and c.origin == "rule"]
    return Gate("Quality", "Required sources", PASS if not missing else WARN,
                "every source the rules expect was available" if not missing
                else "; ".join(c.statement for c in missing), "Sentinel-specific")


def _contradictions_gate(inv: Investigation, ev: InvestigationEvaluation) -> Gate:
    if inv.brief is None:
        return Gate("Quality", "Contradictions surfaced", FAIL if inv.status == "COMPLETE" else NOT_APPLICABLE,
                    "no brief was produced", "Sentinel-specific")
    dropped = {c.challenge_id for c in (inv.critique.challenges if inv.critique else [])} - set(inv.brief.open_challenges)
    return Gate("Quality", "Contradictions surfaced", PASS if not dropped else FAIL,
                f"{ev.challenges} challenge(s), {ev.contradictions} contradicting, all carried to the brief"
                if not dropped else f"challenges missing from the brief: {sorted(dropped)}", "Sentinel-specific")


def _safety_gates(ev: InvestigationEvaluation, calls: list[dict], audit: list[dict]) -> list[Gate]:
    return [_advisory_only_gate(audit), _no_automated_decision_gate(audit), _human_approval_gate(ev),
            _policy_gate(calls)]


def _advisory_only_gate(audit: list[dict]) -> Gate:
    created = audit[0] if audit else None
    return Gate("Safety", "Advisory only",
                PASS if created and created.get("actor") == "system" and created.get("new_status") in ("PENDING", "EXPIRED")
                else FAIL,
                "the action was staged by the alert rules for a person to decide" if created
                else "no audit record of how the action was staged", "Sentinel-specific (audit log)")


def _no_automated_decision_gate(audit: list[dict]) -> Gate:
    # A merge is housekeeping (a duplicate review folded into its survivor), not a
    # decision about the alert, so it is not an automated decision.
    automated = [a for a in audit[1:] if a.get("actor") == "system" and a.get("new_status") != "MERGED"]
    return Gate("Safety", "No automated decision", PASS if not automated else FAIL,
                "every change after staging was made by a named person" if not automated
                else f"{len(automated)} change(s) by the system after staging", "Sentinel-specific (audit log)")


def _human_approval_gate(ev: InvestigationEvaluation) -> Gate:
    if ev.action_status in DECIDED:
        actor = ev.decided_by or ""
        verified = actor.endswith(VERIFIED_ACTOR_SUFFIXES)
        return Gate("Safety", "Human approval", PASS if verified else WARN if actor and actor != "system" else FAIL,
                    f"decided by {actor}" + ("" if verified else " (identity not verified)"),
                    "Sentinel-specific (audit log)")
    return Gate("Safety", "Human approval", PENDING, "awaiting a decision" if ev.action_status == "PENDING"
                else f"action {ev.action_status or 'not found'}", "Sentinel-specific (audit log)")


def _policy_gate(calls: list[dict]) -> Gate:
    unevaluated = [c for c in calls if c.get("policy_action") in (None, "unevaluated")]
    refused = [c for c in calls if c["outcome"] == "policy_denied"]
    return Gate("Safety", "Policy enforced",
                FAIL if unevaluated else WARN if refused else PASS if calls else NOT_APPLICABLE,
                f"{len(unevaluated)} model call(s) without a policy decision" if unevaluated
                else f"{len(refused)} model call(s) refused by policy; the agent fell back to its rules" if refused
                else f"all {len(calls)} model call(s) allowed by the model-use and tool-use policies" if calls
                else "no model calls", "AGT-native: agent-os-kernel PolicyEvaluator")


def _cost_and_latency_gates(ev: InvestigationEvaluation, config: GateConfig) -> list[Gate]:
    """Cost and latency: agent-os-kernel BudgetTracker."""
    return [_budget_gate(ev, config), _retries_gate(ev, config), _sla_gate(ev, config), _timeouts_gate(ev)]


def _budget_gate(ev: InvestigationEvaluation, config: GateConfig) -> Gate:
    tokens = ev.prompt_tokens + ev.completion_tokens
    tool_calls = ev.model_calls + 1          # the model calls and the evidence collection
    exceeded, _ = agt.budget_check(config.budget, tokens=tokens, tool_calls=tool_calls,
                                   cost_usd=ev.cost_usd, duration_s=None)
    cost_detail = (f"${ev.cost_usd:.4f}" if ev.cost_usd is not None else "cost unknown") + \
        f", {tokens} tokens, {tool_calls} tool calls"
    return Gate("Cost", "Budget", FAIL if exceeded else WARN if ev.cost_usd is None and ev.model_calls else PASS,
                ("over budget: " + "; ".join(exceeded)) if exceeded else cost_detail,
                "AGT-native: agent-os-kernel BudgetTracker")


def _retries_gate(ev: InvestigationEvaluation, config: GateConfig) -> Gate:
    return Gate("Cost", "Retries", PASS if ev.retries <= config.max_retries else FAIL,
                f"{ev.retries} re-sent request(s) (limit {config.max_retries})", "Sentinel-specific")


def _sla_gate(ev: InvestigationEvaluation, config: GateConfig) -> Gate:
    over, _ = agt.budget_check(agt.Budget(None, None, None, config.budget.max_duration_seconds),
                               tokens=0, tool_calls=0, cost_usd=None, duration_s=ev.duration_s)
    limit = config.budget.max_duration_seconds
    return Gate("Latency", "SLA",
                NOT_APPLICABLE if ev.duration_s is None or limit is None else FAIL if over else PASS,
                "not completed" if ev.duration_s is None else
                f"{ev.duration_s:.1f} s" + (f" (limit {limit:g} s)" if limit is not None else " (no limit set)"),
                "AGT-native: agent-os-kernel BudgetTracker")


def _timeouts_gate(ev: InvestigationEvaluation) -> Gate:
    timeouts = ev.outcomes.get("timeout", 0)
    return Gate("Latency", "No timeouts", PASS if not timeouts else WARN,
                "no model call timed out" if not timeouts else f"{timeouts} model call(s) timed out",
                "Sentinel-specific")


def _audit_gates(inv: Investigation, ev: InvestigationEvaluation, calls: list[dict], audit: list[dict]) -> list[Gate]:
    return [_sealed_record_gate(inv), _provenance_gate(inv), _model_calls_gate(inv, calls),
            _decision_captured_gate(ev, audit)]


def _sealed_record_gate(inv: Investigation) -> Gate:
    return Gate("Audit", "Sealed record", PASS if inv.verify() else FAIL,
                f"sha256 {(inv.record_sha256 or '')[:12]}… verifies" if inv.verify() else "record does not verify",
                "Sentinel-specific (SHA-256)")


def _provenance_gate(inv: Investigation) -> Gate:
    provenance = [p for p in _provenances(inv) if p.method == "llm"]
    incomplete = [p for p in provenance if not (p.model and p.prompt_sha256 and p.input_sha256 and p.prompt_version)]
    return Gate("Audit", "Model and prompt recorded",
                NOT_APPLICABLE if not provenance else PASS if not incomplete else FAIL,
                "no model output" if not provenance else
                f"model, prompt version and input/prompt hashes recorded for {len(provenance)} model step(s)"
                if not incomplete else f"{len(incomplete)} model step(s) without full provenance", "Sentinel-specific")


def _model_calls_gate(inv: Investigation, calls: list[dict]) -> Gate:
    llm_steps = [s for s in inv.steps if s.agent and s.detail != "deterministic"]
    return Gate("Audit", "Model calls on record",
                NOT_APPLICABLE if not llm_steps else PASS if len(calls) >= len(llm_steps) else FAIL,
                "no model was used" if not llm_steps else
                f"{len(calls)} call record(s) for {len(llm_steps)} model step(s)"
                + ("" if len(calls) >= len(llm_steps) else " (recorded before calls were attributed to investigations?)"),
                "Sentinel-specific (model_calls)")


def _decision_captured_gate(ev: InvestigationEvaluation, audit: list[dict]) -> Gate:
    if ev.action_status in DECIDED:
        captured = any(a.get("new_status") == ev.action_status and a.get("actor") for a in audit[1:])
        return Gate("Audit", "Human decision captured", PASS if captured else FAIL,
                    "the decision is in the append-only audit log with its actor" if captured
                    else "no audit entry for the decision", "Sentinel-specific (audit log)")
    return Gate("Audit", "Human decision captured", PENDING, "no decision yet", "Sentinel-specific (audit log)")


def _provenances(inv: Investigation):
    out = [f.provenance for f in inv.findings]
    if inv.critique:
        out.append(inv.critique.provenance)
    if inv.brief:
        out.append(inv.brief.provenance)
    return out
