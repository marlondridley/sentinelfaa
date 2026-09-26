"""
The evaluation layer (src/evaluation.py) and the readiness verdict built on
it (src/readiness.py), end to end on real records.

A CRITICAL alert is staged by the rules, investigated through the real
workflow and the real model client -- with litellm.acompletion scripted to
cite real evidence ids and, once, evidence that was never collected -- and
decided by a verified person. The evaluation must then read everything back
from the durable record and judge it without asking any agent how it did.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
import tenacity

import src.llm_client as llm
from src import evaluation, governance, readiness, storage
from src.auth import Decision, Principal, PrincipalSource
from src.models import AlertEvent, CriticResult

_EVIDENCE_ID = re.compile(r'"evidence_id":\s*"([^"]+)"')


def _reply(messages: list[dict]) -> dict:
    """What a model would answer, per specialist, citing the evidence it was shown."""
    system, user = messages[0]["content"], messages[-1]["content"]
    first = _EVIDENCE_ID.findall(user)[0]
    if "FEED_ABNORMAL" in system:
        return {"conclusion": "FEED_NORMAL", "summary": "Feeds delivering.", "open_questions": [],
                "claims": [{"statement": "The feed delivered in the window.", "evidence_ids": [first]},
                           {"statement": "A claim citing evidence nobody collected.", "evidence_ids": ["invented:1"]}]}
    if "WEATHER_CORRELATED" in system:
        return {"conclusion": "ISOLATED", "summary": "One alert.", "open_questions": [],
                "claims": [{"statement": "Only this alert fired.", "evidence_ids": [first]}]}
    if "strongest_alternative" in system:
        return {"strongest_alternative": {"statement": "A sensor fault would look the same.", "evidence_ids": [first]},
                "challenges": [{"kind": "CONTRADICTING_EVIDENCE", "statement": "The trigger disagrees.",
                                "evidence_ids": [first], "target": None}]}
    return {"bottom_line": "Isolated alert; verify with the facility.", "verify_next": [], "limitations": [],
            "established": [{"statement": "The alert fired once at KDCA.", "evidence_ids": [first]}]}


async def _scripted_model(**kwargs):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_reply(kwargs["messages"]))))],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100), model="deepseek-chat",
        _hidden_params={"additional_headers": {"llm_provider-x-litellm-response-cost": "0.002"}},
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    storage.override_db_path(":memory:")
    governance.reload_policy()
    monkeypatch.setattr(llm.LiteLLMClient._acall_with_retries.retry, "wait", tenacity.wait_none())
    yield


def _alert() -> AlertEvent:
    return AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code="KDCA",
                      service_name="ISMC", description="ISMC feed disconnected at KDCA.")


async def _investigate(monkeypatch, *, use_llm: bool):
    from src.execution_gate import stage_critical_alert
    from src.investigation import specialists
    from src.investigation.workflow import run_investigation
    from src.storage.pool import get_db_pool
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    monkeypatch.setattr(llm.litellm, "acompletion", _scripted_model)
    monkeypatch.setattr(specialists, "build_llm_client",
                        lambda model=None: llm.LiteLLMClient(model="openai/primary-model"))
    alert = _alert()
    await storage.save_alerts([alert])
    action = await stage_critical_alert(alert)
    inv = await run_investigation(action, alert, use_llm=use_llm)
    await InvestigationsRepository(await get_db_pool()).save(inv)
    return alert, action, inv


def _officer() -> Principal:
    return Principal(principal_id="jsmith", role="Watch Officer", source=PrincipalSource.PASSWORD_GRANT,
                     roles=frozenset({"watch_officer"}))


async def test_an_investigation_is_evaluated_from_the_record_not_from_the_agents(monkeypatch):
    from src.execution_gate import decide_staged_action

    alert, action, inv = await _investigate(monkeypatch, use_llm=True)
    await storage.save_critic_result(CriticResult(alert_id=alert.alert_id, confidence=0.8, escalate=True,
                                                  pattern="ISOLATED_FAULT", reasoning="r"))
    await decide_staged_action(action.action_id, Decision.APPROVE, _officer(), "reviewed")

    ev = await evaluation.evaluate_action(action.action_id)
    assert ev.status == "COMPLETE" and ev.used_model
    # Every model call was attributed to this investigation through its trace's subject.
    assert ev.model_calls == 4 and ev.prompt_tokens == 4000 and ev.cost_usd == pytest.approx(0.008)
    assert ev.served_models == ["deepseek-chat"] and ev.providers == ["deepseek"]
    # The grounding gate judged the model: one invented citation rejected, the rest accepted.
    assert (ev.claims_accepted, ev.claims_rejected) == (5, 1)
    assert ev.grounding == pytest.approx(5 / 6)
    assert ev.contradictions == 1 and ev.depth == 3 and ev.termination == "COMPLETE"
    # The operator's recorded decision, not the model, decides correctness.
    assert (ev.escalate, ev.action_status, ev.operator_agreed) == (True, "APPROVED", True)
    assert ev.decision_results["correctness"]["verdict"] == "pass"
    assert ev.decision_score == pytest.approx((5 / 6 + 1.0) / 2, abs=1e-4)

    gates = {(g.group, g.name): g for g in ev.gates}
    assert gates[("Quality", "Grounded")].status == "PASS"
    assert gates[("Quality", "Model claims grounded")].status == "WARN"
    assert gates[("Safety", "Advisory only")].status == "PASS"
    assert gates[("Safety", "Human approval")].status == "PASS"
    assert gates[("Safety", "Policy enforced")].status == "PASS"
    assert gates[("Cost", "Budget")].source == "agent-os-kernel BudgetTracker"
    assert gates[("Cost", "Budget")].status == "PASS"
    assert gates[("Audit", "Sealed record")].status == "PASS"
    assert gates[("Audit", "Model calls on record")].status == "PASS"
    assert gates[("Audit", "Human decision captured")].status == "PASS"
    assert ev.gate_summary["Audit"] == "PASS"


async def test_a_rules_only_investigation_has_no_decision_score_rather_than_a_made_up_one(monkeypatch):
    _, action, _ = await _investigate(monkeypatch, use_llm=False)
    ev = await evaluation.evaluate_action(action.action_id)
    assert not ev.used_model and ev.model_calls == 0 and ev.cost_usd == 0.0
    assert ev.decision_score is None and ev.grounding is None
    gates = {(g.group, g.name): g.status for g in ev.gates}
    assert gates[("Quality", "Model claims grounded")] == "N/A"
    assert gates[("Safety", "Human approval")] == "PENDING"
    assert gates[("Audit", "Model calls on record")] == "N/A"


async def test_a_budget_the_investigation_exceeds_fails_its_cost_gate(monkeypatch, tmp_path):
    _, action, _ = await _investigate(monkeypatch, use_llm=True)
    tight = evaluation.GateConfig(budget=evaluation.agt.Budget(max_cost_usd=0.001, max_tokens=None,
                                                               max_tool_calls=None, max_duration_seconds=None),
                                  max_retries=3, max_depth=3)
    ev = await evaluation.evaluate_action(action.action_id, tight)
    [budget] = [g for g in ev.gates if g.name == "Budget"]
    assert budget.status == "FAIL" and "cost" in budget.detail


async def test_the_window_is_aggregated_by_agt_components(monkeypatch):
    from src.execution_gate import decide_staged_action

    for _ in range(2):
        alert, action, _ = await _investigate(monkeypatch, use_llm=True)
        await storage.save_critic_result(CriticResult(alert_id=alert.alert_id, confidence=0.9, escalate=False,
                                                      pattern="TRANSIENT", reasoning="r"))
        await decide_staged_action(action.action_id, Decision.APPROVE, _officer(), "escalating anyway")

    window = await evaluation.evaluate_window(24)
    m = window.metrics
    assert m["counts"]["investigations"] == 2 and m["counts"]["with_model"] == 2
    q = m["quality"]
    assert q["unsupported_claim_rate"]["value"] == pytest.approx(2 / 12)
    assert q["unsupported_claim_rate"]["source"].startswith("agent-sre HallucinationRate")
    assert q["missed_escalation_rate"]["value"] == 1.0, "approved where the Critic said not to escalate"
    assert q["recommendation_accuracy"]["value"] == 0.0
    assert q["contradiction_rate"]["value"] == 1.0
    assert m["cost"]["per_investigation"] == {"spent_usd": pytest.approx(0.016), "tasks": 2, "mean_usd": pytest.approx(0.008)}
    assert m["reliability"]["investigation_completion"]["value"] == 1.0
    assert m["reliability"]["providers"]["members"]["deepseek"]["success_rate"] == 1.0
    assert m["latency"]["investigation"]["n"] == 2 and m["latency"]["investigation"]["p95"] is not None
    assert m["orchestration"]["depth_within_limit"]["value"] == 1.0
    assert m["governance"]["policy_compliance"]["rate"] == 1.0


async def test_readiness_with_no_evidence_says_so_and_fails_the_gate():
    report = await readiness.build_report(24)
    assert report.verdict == readiness.INSUFFICIENT
    assert all(r.status == readiness.NO_DATA for r in report.objectives)
    assert json.dumps(report.to_dict(), default=str)
    assert {r.label for r in report.objectives} >= {
        "Decision quality", "Evidence grounding", "Human override", "Unsupported claims", "P95 investigation time",
        "Cost / investigation", "Agent failure", "Missed escalation", "Audit completeness"}


def test_an_unbounded_budget_use_is_json_safe():
    status, used = readiness.score_status(0.5, 10, 1.0, 0.75, 1)
    assert status == readiness.MISSED and used == float("inf")
    report = readiness.Report(verdict="NOT READY", reasons=[], window_hours=1, generated_at="", calls=0,
                              objectives=[readiness.ObjectiveResult(
                                  name="audit_completeness", label="Audit completeness", description="",
                                  status=status, value=0.5, target=1.0, limit=None, samples=10, budget_used=used,
                                  required=True, detail="", source="in-repo")], breakdown={})
    assert "Infinity" not in json.dumps(report.to_dict())


def test_every_objective_in_the_file_is_measurable():
    _, objectives = readiness.load_spec()
    assert {o.name for o in objectives} <= set(readiness.KNOWN_OBJECTIVES)
