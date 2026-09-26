"""
Known bugs in the Microsoft Agent Governance Toolkit (agent-sre and
agent-os-kernel 3.7.0), each shown by a test, and the host-side answer to
each.

Every `test_agt_*` test asserts what the toolkit *should* return and is a
strict xfail: it runs, fails against 3.7.0, and is reported as XFAIL. If an
AGT release fixes the bug, the test passes, strict mode turns that into a
suite failure, and the workaround or in-repo code named in the reason can be
retired. Each `test_sentinel_*` test next to it shows Sentinel's code
returning the right value for the same input.

These tests are the evidence for the upstream issue drafted in
docs/upstream/agent-sre-error-budget-issue.md. The full metric mapping is in
docs/agent-metrics.md.
"""
from __future__ import annotations

import time
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from agent_os.policies import PolicyDocument
    from agent_sre.evals import EvalCriterion, EvalInput, EvalResult, EvalSuite, EvaluationEngine, Verdict
    from agent_sre.slo import indicators
    from agent_sre.slo.indicators import (
        CalibrationDeltaSLI,
        CostPerTask,
        HallucinationRate,
        PolicyCompliance,
        ResponseLatency,
        TaskSuccessRate,
        ToolCallAccuracy,
    )
    from agent_sre.slo.objectives import SLO, ErrorBudget, SLOStatus

from src.agt_metrics import expected_calibration_error
from src.readiness import MET, ratio_status


def agt_bug(reason: str):
    return pytest.mark.xfail(strict=True, reason=f"AGT 3.7.0 bug: {reason}")


# One failure in a hundred calls, measured against a 95% objective: 99% is
# comfortably inside it. The allowance is 5% errors; 1% used a fifth of it.
TARGET, GOOD, TOTAL = 0.95, 99, 100


def _budget() -> ErrorBudget:
    budget = ErrorBudget(total=1.0 - TARGET)
    for i in range(TOTAL):
        budget.record_event(good=i < GOOD)
    return budget


# ── agent-sre: ErrorBudget ───────────────────────────────────────────────────

@agt_bug("ErrorBudget.record_event() adds 1.0 per failure against a fractional budget (1 - target)")
def test_agt_error_budget_one_failure_in_a_hundred_is_not_exhausted():
    budget = _budget()
    assert not budget.is_exhausted
    assert budget.remaining == pytest.approx(0.8)


@agt_bug("ErrorBudget.burn_rate() divides the budget by the window length in seconds")
def test_agt_error_budget_burn_rate_is_error_rate_over_allowed_rate():
    assert _budget().burn_rate() == pytest.approx(0.2)


@agt_bug("SLO.evaluate() reports EXHAUSTED from the ErrorBudget above")
def test_agt_slo_at_99_percent_against_95_is_not_exhausted():
    slo = SLO("availability", indicators=[TaskSuccessRate(target=TARGET)])
    for i in range(TOTAL):
        slo.record_event(good=i < GOOD)
    assert slo.evaluate() is not SLOStatus.EXHAUSTED


def test_sentinel_error_budget_for_the_same_calls():
    status, value, used = ratio_status(GOOD, TOTAL, TARGET, at_risk=0.75, min_samples=1)
    assert value == pytest.approx(0.99)
    assert used == pytest.approx(0.2), "a fifth of the budget, which is also the burn rate over the window"
    assert status == MET


# ── agent-sre: SLIs that average their own running rates ─────────────────────

def _one_bad_then_good(record) -> None:
    record(False)
    for _ in range(TOTAL - 1):
        record(True)


@agt_bug("rate SLIs record the running rate per event and current_value() averages those running rates")
@pytest.mark.parametrize("make, record", [
    (lambda: TaskSuccessRate(target=TARGET), lambda s: s.record_task),
    (lambda: ToolCallAccuracy(target=TARGET), lambda s: s.record_call),
    (lambda: PolicyCompliance(target=TARGET), lambda s: s.record_check),
], ids=["TaskSuccessRate", "ToolCallAccuracy", "PolicyCompliance"])
def test_agt_rate_sli_current_value_is_the_rate(make, record):
    sli = make()
    _one_bad_then_good(record(sli))
    assert sli.current_value() == pytest.approx(0.99)      # 3.7.0: 0.948 -- reads as missing 95%


@agt_bug("HallucinationRate.current_value() averages running rates")
def test_agt_hallucination_rate_current_value_is_the_rate():
    sli = HallucinationRate(target=0.05)
    sli.record_evaluation(hallucinated=True)
    for _ in range(TOTAL - 1):
        sli.record_evaluation(hallucinated=False)
    assert sli.current_value() == pytest.approx(0.01)      # 3.7.0: 0.052 -- reads as over a 5% ceiling


@agt_bug("CostPerTask.current_value() averages running averages")
def test_agt_cost_per_task_current_value_is_the_mean_cost():
    sli = CostPerTask(target_usd=0.50)
    sli.record_cost(1.00)
    for _ in range(TOTAL - 1):
        sli.record_cost(0.0)
    assert sli.current_value() == pytest.approx(0.01)      # 3.7.0: 0.052


def test_sentinel_reads_rate_slis_through_collect_which_uses_the_counts():
    """Sentinel's workaround: a fresh SLI per window, read through collect()."""
    sli = TaskSuccessRate(target=TARGET)
    _one_bad_then_good(sli.record_task)
    assert sli.collect().value == pytest.approx(0.99)
    rate = HallucinationRate(target=0.05)
    rate.record_evaluation(hallucinated=True)
    for _ in range(TOTAL - 1):
        rate.record_evaluation(hallucinated=False)
    assert rate.collect().value == pytest.approx(0.01)


# ── agent-sre: lower-is-better SLIs graded as higher-is-better ───────────────

@agt_bug("ResponseLatency inherits compliance() where value >= target is good")
def test_agt_latency_under_its_target_is_compliant():
    sli = ResponseLatency(target_ms=5000.0)
    sli.record_latency(100.0)
    assert sli.compliance() == 1.0                          # 3.7.0: 0.0


@agt_bug("CostPerTask inherits compliance() where value >= target is good")
def test_agt_cost_under_its_target_is_compliant():
    sli = CostPerTask(target_usd=0.50)
    sli.record_cost(0.10)
    assert sli.compliance() == 1.0                          # 3.7.0: 0.0


@agt_bug("ResponseLatency.current_value() reads every latency ever recorded, not its window")
def test_agt_latency_outside_the_window_is_not_current(monkeypatch):
    sli = ResponseLatency(target_ms=5000.0, window="1h")
    sli.record_latency(100.0)
    later = time.time() + 2 * 3600
    monkeypatch.setattr(indicators.time, "time", lambda: later)
    assert sli.values_in_window() == []
    assert sli.current_value() is None                      # 3.7.0: 100.0


# ── agent-sre: calibration ───────────────────────────────────────────────────

def _offsetting_predictions() -> list[tuple[float, bool]]:
    """Certain and wrong half the time; certain it is wrong, and right, the other half."""
    return [(1.0, False), (0.0, True)] * 50


@agt_bug("CalibrationDeltaSLI compares mean confidence with mean success over all predictions, so errors cancel")
def test_agt_calibration_delta_detects_offsetting_miscalibration():
    sli = CalibrationDeltaSLI(target_delta=0.05)
    for stated, outcome in _offsetting_predictions():
        sli.record_prediction(predicted_confidence=stated, actual_success=outcome)
    assert sli.current_value() > 0.5                        # 3.7.0: 0.0, "perfectly calibrated"


def test_sentinel_expected_calibration_error_sees_it():
    error, samples = expected_calibration_error(_offsetting_predictions())
    assert (error, samples) == (1.0, 100)
    assert expected_calibration_error([(0.8, True)] * 4 + [(0.8, False)]) == (pytest.approx(0.0), 5)
    assert expected_calibration_error([]) == (None, 0)


# ── agent-sre: EvaluationEngine ──────────────────────────────────────────────

class _Judge:
    judge_id = "test"

    def __init__(self, scores: dict[EvalCriterion, float | Exception]):
        self.scores = scores

    def evaluate(self, eval_input: EvalInput, criterion: EvalCriterion) -> EvalResult:
        score = self.scores[criterion]
        if isinstance(score, Exception):
            raise score
        return EvalResult(criterion=criterion, verdict=Verdict.PASS if score >= 0.5 else Verdict.FAIL, score=score)


_INPUT = EvalInput(query="q", response="r")


@agt_bug("EvaluationEngine.pass_rate() and average_score() return 0.0 when nothing was evaluated")
def test_agt_evaluation_engine_with_no_evaluations_has_no_score():
    engine = EvaluationEngine(_Judge({}))
    assert engine.pass_rate() is None and engine.average_score() is None     # 3.7.0: 0.0, 0.0


@agt_bug("EvaluationEngine.get_stats() leaves out every criterion whose average score is 0")
def test_agt_evaluation_stats_keep_a_criterion_that_always_fails():
    engine = EvaluationEngine(_Judge({EvalCriterion.CORRECTNESS: 1.0, EvalCriterion.HALLUCINATION: 0.0}))
    engine.run(_INPUT, EvalSuite(name="s", criteria=[EvalCriterion.CORRECTNESS, EvalCriterion.HALLUCINATION]))
    assert engine.get_stats()["by_criterion"].get("hallucination") == 0.0   # 3.7.0: missing


@agt_bug("a judge error on a required criterion becomes ABSTAIN, which does not fail the report")
def test_agt_a_required_criterion_the_judge_could_not_evaluate_fails_the_report():
    engine = EvaluationEngine(_Judge({EvalCriterion.CORRECTNESS: 1.0, EvalCriterion.SAFETY: RuntimeError("judge down")}))
    report = engine.run(_INPUT, EvalSuite(name="s", criteria=[EvalCriterion.CORRECTNESS, EvalCriterion.SAFETY],
                                          required_criteria=[EvalCriterion.SAFETY]))
    assert report.overall_pass is False                     # 3.7.0: True


# ── agent-os-kernel: PolicyDocument ──────────────────────────────────────────

@agt_bug("PolicyDocument ignores unknown keys, so a misspelt `rules:` loads as a policy with no rules")
def test_agt_policy_document_rejects_an_unknown_key():
    with pytest.raises(ValueError):
        PolicyDocument.model_validate({"name": "typo", "defaults": {"action": "deny"}, "rule": []})


@agt_bug("PolicyDocument defaults to allow when `defaults` is absent")
def test_agt_policy_document_without_defaults_does_not_allow_everything():
    document = PolicyDocument.model_validate({"name": "no-defaults"})
    assert document.defaults.action.value != "allow"


@agt_bug("PolicyDocument.tool_allowlist is documented as enforced by PolicyEvaluator; the evaluator never reads it")
def test_agt_policy_evaluator_enforces_the_tool_allowlist():
    from agent_os.policies import PolicyEvaluator

    document = PolicyDocument.model_validate({
        "name": "allowlisted", "defaults": {"action": "allow"}, "tool_allowlist": ["read_alert_context"],
    })
    decision = PolicyEvaluator(policies=[document]).evaluate({"tool_name": "delete_everything"})
    assert decision.allowed is False                        # 3.7.0: allowed


@pytest.mark.parametrize("content", [
    "name: typo\ndefaults: {action: deny}\nrule: []\n",
    "name: no-defaults\n",
    "name: allowlist\ndefaults: {action: deny}\ntool_allowlist: [read_alert_context]\nrules: []\n",
])
def test_sentinel_refuses_every_call_under_either_policy(monkeypatch, tmp_path, content):
    from src import governance
    from src.config import settings

    path = tmp_path / "model-use.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(settings, "MODEL_POLICY_PATH", path)
    governance.reload_policy()
    try:
        decision = governance.evaluate_model_call(
            role="critic", model="openai/primary-model", prompt="x", max_tokens=100, redacted=True)
        assert decision.allowed is False and decision.policy is None
    finally:
        governance.reload_policy()
