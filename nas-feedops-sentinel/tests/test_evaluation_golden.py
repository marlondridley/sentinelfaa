"""
The evaluation's output, pinned.

evaluate_investigation() and aggregate() are where every gate and every metric on the Agent Evaluation page and
in the readiness verdict are decided. They were two very large functions (cyclomatic complexity 115 and 86 and a
maintainability index of 0), which is a reason to split them and, for the same reason, a reason not to trust an
edit to them on the strength of a few end-to-end tests.

tests/fixtures/evaluation_golden.json is what the functions returned, before they were split, for 80 seeded
investigations of every shape (tests/evaluation_cases.py) and for four windows over them. This test runs the same
inputs and requires the same output: every gate's status, wording and source, and every metric's value and source.
If a change to the evaluation is meant to change an output, regenerate the file in the same commit:

    REGENERATE_GOLDEN=1 python -m pytest tests/test_evaluation_golden.py
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest

from src import agt_metrics as agt
from src import evaluation
from tests.evaluation_cases import make_calls, make_case, make_verdict

GOLDEN = Path(__file__).parent / "fixtures" / "evaluation_golden.json"
SEEDS = range(80)
WINDOWS = {"empty": range(0), "one": range(1), "ten": range(10), "all": SEEDS}


def _evaluate(case, engine):
    return evaluation.evaluate_investigation(
        case["inv"], calls=case["calls"], critic_calls=case["critic_calls"], action=case["action"],
        audit=case["audit"], verdict=case["verdict"], config=case["config"], engine=engine)


def _plain(value):
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _snapshot() -> dict:
    cases = {seed: make_case(seed) for seed in SEEDS}
    investigations = {str(seed): _plain(_evaluate(case, agt.new_engine()).to_dict()) for seed, case in cases.items()}

    windows = {}
    for name, seeds in WINDOWS.items():
        engine = agt.new_engine()
        members = [cases[s] for s in seeds]
        evaluations = [_evaluate(c, engine) for c in members]
        rng = random.Random(1000 + len(members))
        calls = [row for c in members for row in (*c["calls"], *c["critic_calls"])] + make_calls(rng, None, n=len(members))
        verdicts = [v for v in (make_verdict(rng) for _ in range(len(members) * 2)) if v]
        config = members[0]["config"] if members else cases[0]["config"]
        windows[name] = _plain(evaluation.finite(evaluation.aggregate(
            evaluations, calls, verdicts, [c["inv"] for c in members], config, engine)))
    return {"investigations": investigations, "windows": windows}


def test_the_inputs_are_varied_enough_to_mean_something():
    cases = [make_case(s) for s in SEEDS]
    statuses = {c["inv"].status for c in cases}
    assert statuses == {"COMPLETE", "FAILED", "OPEN"}
    assert {c["action"].status for c in cases if c["action"]} >= {"PENDING", "APPROVED", "REJECTED", "OVERRIDDEN"}
    assert any(c["inv"].brief for c in cases) and any(c["inv"].critique for c in cases)
    assert any(not c["inv"].verify() for c in cases), "some records fail their own hash"


def test_the_evaluation_output_has_not_changed():
    snapshot = _snapshot()
    if os.environ.get("REGENERATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        pytest.skip("golden file regenerated")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    assert snapshot["windows"] == golden["windows"]
    changed = [seed for seed, ev in snapshot["investigations"].items() if ev != golden["investigations"][seed]]
    assert not changed, f"evaluate_investigation changed for seeds {changed[:10]}"


def test_the_snapshot_is_the_same_twice_so_the_comparison_is_not_noise():
    assert _snapshot() == _snapshot()
