"""
What is behind each number on the Agent Evaluation page (src/evaluation_evidence.py).

The Health table said "Evidence: 12" and "Source: AGT-native": a count of
nothing in particular, and who computed it. Nothing led from a number to the
investigations, the agents or the reviews it described, so nobody could act
on it. evidence_for() lists what each objective was computed from.

It must list exactly that, or the page would show a number over rows that are
not its own: for every objective, over the same 80 seeded investigations the
golden test uses, the samples of the rows add up to the metric's sample
count, and the rows counted against it give the metric's value.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from src import agt_metrics as agt
from src import evaluation, readiness
from src.evaluation_evidence import AGENT_STEP, INVESTIGATION, MODEL_CALL, OBJECTIVES, RECOMMENDATION, evidence_for
from src.model_telemetry import NOT_SENT_OUTCOMES
from tests.evaluation_cases import make_calls, make_case, make_verdict

NOW = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
_, _OBJECTIVES = readiness.load_spec()
SPEC = {o.name: o for o in _OBJECTIVES}


def _window(seeds) -> evaluation.WindowEvaluation:
    """A window over seeded cases, built the way tests/test_evaluation_golden.py builds its own."""
    engine = agt.new_engine()
    cases = [make_case(seed) for seed in seeds]
    evaluations = [evaluation.evaluate_investigation(
        c["inv"], calls=c["calls"], critic_calls=c["critic_calls"], action=c["action"], audit=c["audit"],
        verdict=c["verdict"], config=c["config"], engine=engine) for c in cases]
    rng = random.Random(1000 + len(cases))
    calls = [row for c in cases for row in (*c["calls"], *c["critic_calls"])] + make_calls(rng, None, n=len(cases))
    verdicts = [v for v in (make_verdict(rng) for _ in range(len(cases) * 2)) if v]
    records = [c["inv"] for c in cases]
    metrics = evaluation.aggregate(evaluations, calls, verdicts, records, cases[0]["config"], engine)
    return evaluation.WindowEvaluation(since=NOW, until=NOW, investigations=evaluations, calls=calls,
                                       verdicts=verdicts, metrics=metrics, records=records)


def _evidence(name: str, window):
    return evidence_for(name, window, target=SPEC[name].target, limit=SPEC[name].max)


@pytest.fixture(scope="module")
def window():
    return _window(range(80))


def test_there_is_evidence_for_every_objective_the_report_grades():
    assert set(OBJECTIVES) == set(readiness.KNOWN_OBJECTIVES)


@pytest.mark.parametrize("name", readiness.KNOWN_OBJECTIVES)
@pytest.mark.parametrize("seeds", [range(1), range(10), range(80)], ids=["one", "ten", "all"])
def test_the_rows_behind_a_number_add_up_to_its_samples(name, seeds):
    window = _window(seeds)
    _value, samples, _unit, _source = readiness._read(window.metrics, name)

    items = _evidence(name, window)

    assert sum(i.samples for i in items) == samples, f"{name}: the rows are not the metric's population"
    assert all(0 <= i.against <= i.samples for i in items)


# The share of an objective's samples that count against it is the metric's value: as a rate of the bad
# (unsupported claims, overrides, ...) or as what is left of the good (availability, validity, ...).
_RATE_OF_BAD = ("unsupported_claim_rate", "human_override_rate", "missed_escalation_rate", "false_escalation_rate",
                "agent_failure_rate")
_SHARE_OF_GOOD = ("availability", "output_validity", "audit_completeness", "policy_compliance")


@pytest.mark.parametrize("name", _RATE_OF_BAD + _SHARE_OF_GOOD)
def test_what_counts_against_an_objective_is_what_its_value_says(window, name):
    value, samples, _unit, _source = readiness._read(window.metrics, name)
    assert samples, "the seeded window exercises every objective"

    against = sum(i.against for i in _evidence(name, window)) / samples

    assert against == pytest.approx(value if name in _RATE_OF_BAD else 1 - value, abs=1e-3), name


@pytest.mark.parametrize("name", ["investigation_latency_p95_seconds", "model_call_latency_p95_seconds",
                                  "cost_per_investigation_usd"])
def test_a_limit_is_judged_item_by_item(window, name):
    """Against a limit set at the middle of the seeded readings, the rows over it are exactly the readings over it."""
    readings = {
        "investigation_latency_p95_seconds": [e.duration_s for e in window.investigations if e.duration_s is not None],
        "model_call_latency_p95_seconds": [c["latency_ms"] / 1000.0 for c in window.calls
                                           if c["outcome"] not in NOT_SENT_OUTCOMES],
        "cost_per_investigation_usd": [e.cost_usd for e in window.investigations
                                       if e.used_model and e.cost_usd is not None],
    }[name]
    limit = sorted(readings)[len(readings) // 2]

    over = sum(i.against for i in evidence_for(name, window, limit=limit))

    assert over == sum(1 for value in readings if value > limit) and 0 < over < len(readings)
    assert not any(i.against for i in evidence_for(name, window, limit=None)), "no limit, nothing is over it"


def test_what_counts_against_comes_first(window):
    for name in readiness.KNOWN_OBJECTIVES:
        flags = [bool(i.against) for i in _evidence(name, window)]
        assert flags == sorted(flags, reverse=True), name


def test_every_row_says_what_it_is_and_reads_as_a_sentence(window):
    kinds = {name: {i.kind for i in _evidence(name, window)} for name in readiness.KNOWN_OBJECTIVES}
    assert kinds["evidence_grounding"] == {INVESTIGATION} and kinds["availability"] == {MODEL_CALL}
    assert kinds["false_escalation_rate"] == {RECOMMENDATION}
    assert kinds["agent_failure_rate"] == {AGENT_STEP, MODEL_CALL}, "agent steps, and the Critic's and Planner's calls"
    assert all(i.reading and not i.reading.isdigit() for name in readiness.KNOWN_OBJECTIVES for i in _evidence(name, window))


def test_an_investigation_row_leads_to_its_review_and_names_the_agent_at_fault(window):
    by_action = {e.action_id: e for e in window.investigations}
    rejected = [i for i in _evidence("unsupported_claim_rate", window) if i.against]
    assert rejected, "the seeded window has rejected claims"
    for item in rejected:
        e = by_action[item.action_id]
        assert (item.airport, item.alert_type, item.review_status) == (e.airport, e.alert_type, e.action_status)
        assert f"{e.claims_rejected} of {e.claims_accepted + e.claims_rejected} model claims rejected" in item.reading
        assert item.agents, "a rejected claim is some agent's"
        assert set(item.agents) <= {"feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer"}


def test_a_model_call_row_names_its_agent_and_the_review_of_its_investigation(window):
    by_investigation = {e.investigation_id: e for e in window.investigations}
    calls = {c["call_id"]: c for c in window.calls}
    items = _evidence("availability", window)
    linked = [i for i in items if i.action_id]
    assert linked and len(linked) < len(items), "calls of an investigation are linked; the Planner's own are not"
    failing = [c for c in calls.values() if c["subject"] in by_investigation and c["outcome"] == "timeout"]
    assert failing, "the seeded window has a timed-out call inside an investigation"
    expected = {(c["role"], by_investigation[c["subject"]].action_id) for c in failing}
    assert expected <= {(i.agents[0], i.action_id) for i in items if i.against and i.reading.startswith("timeout")}


def test_an_agent_step_row_says_why_the_agent_fell_back(window):
    fallbacks = [i for i in _evidence("agent_failure_rate", window) if i.kind == AGENT_STEP and i.against]
    assert fallbacks and all(i.agents and i.action_id for i in fallbacks)
    assert all(i.reading.startswith(("fell back to its rules", "errored")) for i in fallbacks)


def test_a_decided_recommendation_row_says_what_the_critic_said_and_what_the_person_did(window):
    items = _evidence("false_escalation_rate", window)
    assert all(i.agents == ("critic",) and i.reading.startswith("Critic said escalate") for i in items)
    assert {i.review_status for i in items if i.against} == {"REJECTED"}
    disagreed = [i for i in _evidence("calibration_error", window) if i.against]
    assert disagreed and all("the operator disagreed" in i.reading for i in disagreed)


def test_an_empty_window_has_no_rows_and_does_not_fail():
    empty = evaluation.WindowEvaluation(since=NOW, until=NOW, investigations=[], calls=[], verdicts=[], metrics={})
    assert all(evidence_for(name, empty) == [] for name in readiness.KNOWN_OBJECTIVES)
