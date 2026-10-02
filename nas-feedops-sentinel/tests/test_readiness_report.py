"""
The readiness report (src/readiness.py) beyond the status arithmetic: reading the objectives file, grading each
objective against the window's metrics, the verdict, the text and JSON output, and the command line.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from src import readiness as r


def _write(tmp_path, body: str):
    path = tmp_path / "model-slos.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# ── Reading the objectives file ──────────────────────────────────────────────────────────────────────

def test_an_objective_this_module_cannot_measure_is_an_error_not_a_skip(tmp_path):
    path = _write(tmp_path, "objectives:\n  - {name: vibes, target: 0.9}\n")
    with pytest.raises(ValueError, match="unknown objective 'vibes'"):
        r.load_spec(path)


@pytest.mark.parametrize("line", [
    "{name: availability}",                          # neither
    "{name: availability, target: 0.9, max: 3}",     # both
])
def test_an_objective_needs_exactly_one_of_target_or_max(tmp_path, line):
    with pytest.raises(ValueError, match="exactly one of target or max"):
        r.load_spec(_write(tmp_path, f"objectives:\n  - {line}\n"))


def test_a_valid_file_gives_labelled_objectives_and_ignores_unknown_keys(tmp_path):
    path = _write(tmp_path, """
window_hours: 12
objectives:
  - name: availability
    target: 0.99
    description: >
      several
      lines
    note: not a field
  - {name: calibration_error, max: 0.2, label: Calibration, required: false, min_samples: 5}
""")
    spec, objectives = r.load_spec(path)

    assert spec["window_hours"] == 12
    first, second = objectives
    assert (first.label, first.description, first.target) == ("Availability", "several lines", 0.99)
    assert (second.label, second.max, second.required, second.min_samples) == ("Calibration", 0.2, False, 5)


def test_the_shipped_objectives_file_loads():
    assert r.load_spec(None)[1], "the shipped file has objectives"


# ── Status arithmetic not covered elsewhere ──────────────────────────────────────────────────────────

def test_a_lower_is_better_value_with_no_samples_has_no_data():
    assert r.limit_status(None, 10.0, 0, 0.75, 1) == (r.NO_DATA, None, None)
    assert r.limit_status(3.0, 10.0, 0, 0.75, 1) == (r.NO_DATA, 3.0, None)


@pytest.mark.parametrize("value, limit, expected_status, expected_used", [
    (5.0, 10.0, r.MET, 0.5),
    (8.0, 10.0, r.AT_RISK, 0.8),
    (12.0, 10.0, r.MISSED, 1.2),
    (0.0, 0.0, r.MET, 0.0),             # a limit of zero is met by zero
    (1.0, 0.0, r.MISSED, math.inf),     # and missed by anything more
])
def test_a_lower_is_better_value_is_graded_against_its_limit(value, limit, expected_status, expected_used):
    status, shown, used = r.limit_status(value, limit, 10, 0.75, 1)
    assert (status, shown, used) == (expected_status, value, expected_used)


def test_a_target_of_one_allows_no_errors_at_all():
    assert r.score_status(1.0, 10, 1.0, 0.75, 1) == (r.MET, 0.0)
    assert r.score_status(0.99, 10, 1.0, 0.75, 1) == (r.MISSED, math.inf)


# ── Grading ──────────────────────────────────────────────────────────────────────────────────────────

def _metrics():
    return {
        "quality": {"decision_quality": {"value": 0.90, "samples": 40}},
        "latency": {"investigation": {"p95": 9.0, "n": 12}},
        "cost": {},
    }


def test_each_objective_is_read_from_its_place_in_the_window_metrics():
    objectives = [
        r.Objective(name="decision_quality", target=0.85, min_samples=10),
        r.Objective(name="investigation_latency_p95_seconds", max=10.0),
    ]
    quality, latency = r.grade(objectives, _metrics(), at_risk=0.75)

    assert quality.status == r.MET and quality.samples == 40
    assert "40 scored criteria" in quality.detail and "of the error budget used" in quality.detail
    assert latency.status == r.AT_RISK and "of the limit used" in latency.detail
    assert quality.source == "Sentinel-specific"
    assert latency.source == "AGT-native: agent-sre ResponseLatency"


def test_an_objective_with_no_evidence_says_so():
    [result] = r.grade([r.Objective(name="cost_per_investigation_usd", max=1.0)], _metrics(), 0.75)

    assert result.status == r.NO_DATA and result.value is None
    assert result.detail == "no investigations with a known cost in the window"
    assert result.source == "AGT-native: agent-sre CostGuard"


def test_an_objective_short_of_its_minimum_evidence_says_how_much_it_needs():
    metrics = {"quality": {"decision_quality": {"value": 0.9, "samples": 3}}}
    [result] = r.grade([r.Objective(name="decision_quality", target=0.85, min_samples=10)], metrics, 0.75)

    assert result.status == r.NO_DATA
    assert "10 needed to judge" in result.detail


# ── The verdict ──────────────────────────────────────────────────────────────────────────────────────

def _result(status, label="Thing", required=True):
    return r.ObjectiveResult(name="x", label=label, description="", status=status, value=None, target=None,
                             limit=None, samples=0, budget_used=None, required=required, detail=f"{status}!",
                             source="s")


def test_a_missed_objective_is_not_ready_whatever_else_is_true():
    verdict, reasons = r.decide([_result(r.MET), _result(r.MISSED, "Bad")], calls=1000, min_calls=20)
    assert (verdict, reasons) == (r.NOT_READY, ["Bad: MISSED!"])


def test_too_few_calls_is_insufficient_data():
    verdict, reasons = r.decide([_result(r.MET)], calls=3, min_calls=20)
    assert verdict == r.INSUFFICIENT and "3 model call(s)" in reasons[0] and "20 are needed" in reasons[0]


def test_a_required_objective_without_evidence_is_insufficient_but_an_optional_one_is_not():
    verdict, reasons = r.decide([_result(r.NO_DATA, "Needed")], calls=50, min_calls=20)
    assert (verdict, reasons) == (r.INSUFFICIENT, ["Needed: NO_DATA!"])
    assert r.decide([_result(r.NO_DATA, required=False), _result(r.MET)], 50, 20)[0] == r.READY


def test_an_objective_close_to_its_limit_makes_the_verdict_at_risk():
    verdict, reasons = r.decide([_result(r.AT_RISK, "Close"), _result(r.MET)], calls=50, min_calls=20)
    assert (verdict, reasons) == (r.AT_RISK_VERDICT, ["Close: AT_RISK!"])


def test_everything_met_is_ready():
    assert r.decide([_result(r.MET)], calls=50, min_calls=20) == (r.READY, ["Every objective met."])


# ── Building the report ──────────────────────────────────────────────────────────────────────────────

class IntegrityError(Exception):
    """Named as the evaluation layer's is: the report keys on the class name."""


def _patch_evaluation(monkeypatch, *, metrics=None, calls=(), error=None):
    import src.evaluation as evaluation

    async def _evaluate_window(window, config):
        if error:
            raise error
        return SimpleNamespace(metrics=metrics or {}, calls=list(calls))

    monkeypatch.setattr(evaluation, "evaluate_window", _evaluate_window)
    monkeypatch.setattr(evaluation, "load_gate_config", lambda path: None)


async def test_a_report_grades_the_window_and_carries_its_breakdown(monkeypatch, tmp_path):
    _patch_evaluation(monkeypatch, metrics=_metrics(), calls=range(30))
    path = _write(tmp_path, """
window_hours: 6
min_calls: 20
objectives:
  - {name: decision_quality, target: 0.85, min_samples: 10}
""")

    report = await r.build_report(spec_path=path)

    assert (report.verdict, report.window_hours, report.calls) == (r.READY, 6.0, 30)
    assert report.breakdown == _metrics()
    assert report.to_dict()["objectives"][0]["status"] == r.MET


async def test_an_explicit_window_overrides_the_files(monkeypatch, tmp_path):
    _patch_evaluation(monkeypatch, calls=range(30))
    path = _write(tmp_path, "window_hours: 6\nobjectives: []\n")

    assert (await r.build_report(hours=72, spec_path=path)).window_hours == 72.0


async def test_a_record_that_fails_its_integrity_check_is_not_ready(monkeypatch, tmp_path):
    _patch_evaluation(monkeypatch, error=IntegrityError("hash mismatch"))
    path = _write(tmp_path, "objectives: []\n")

    report = await r.build_report(spec_path=path)

    assert report.verdict == r.NOT_READY
    assert report.reasons[0] == "an investigation record failed its integrity check: IntegrityError: hash mismatch"
    assert report.calls == 0


async def test_any_other_failure_is_not_swallowed(monkeypatch, tmp_path):
    _patch_evaluation(monkeypatch, error=RuntimeError("database down"))

    with pytest.raises(RuntimeError, match="database down"):
        await r.build_report(spec_path=_write(tmp_path, "objectives: []\n"))


# ── Output ───────────────────────────────────────────────────────────────────────────────────────────

def _report(**overrides):
    base = dict(verdict=r.READY, reasons=["Every objective met."], window_hours=24.0,
                generated_at="2026-10-01T00:00:00+00:00", calls=5, objectives=[], breakdown={})
    base.update(overrides)
    return r.Report(**base)


def test_an_unbounded_budget_is_reported_as_null_in_json():
    assert r._finite({"used": math.inf, "list": [math.nan, 1.0], "nested": {"x": -math.inf}}) == {
        "used": None, "list": [None, 1.0], "nested": {"x": None}}
    assert r._finite((1, 2)) == [1, 2]


def test_percentages_and_numbers_say_when_there_is_nothing_to_show():
    assert (r._pct(None), r._pct(math.inf), r._pct(0.5)) == ("n/a", "inf", "50.0%")
    assert (r._num(None), r._num(0.123456)) == ("n/a", "0.1235")


def test_the_text_report_lists_each_objective_and_the_window_counts():
    ratio = _result(r.MET, "Ratio")
    ratio.target, ratio.value, ratio.budget_used, ratio.samples = 0.9, 0.95, 0.5, 40
    limit = _result(r.AT_RISK, "Limit", required=False)
    limit.limit, limit.value, limit.samples = 10.0, 8.0, 3
    report = _report(objectives=[ratio, limit], breakdown={"counts": {
        "investigations": 4, "with_model": 3, "calls": 30, "decided": 2}})

    text = r.render_text(report)

    assert "Verdict: READY" in text and "  - Every objective met." in text
    assert "Ratio" in text and "95.0%" in text and "(informational)" in text
    assert "Window: 4 investigation(s), 3 with a model; 30 model call(s); 2 decided recommendation(s)" in text


def test_the_text_report_omits_the_window_line_when_there_are_no_counts():
    assert "Window:" not in r.render_text(_report())


# ── The command line ─────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def cli(monkeypatch):
    def _with(report=None, error=None):
        async def _build(hours):
            if error:
                raise error
            return report

        monkeypatch.setattr(r, "build_report", _build)

    return _with


def test_the_command_prints_the_readable_report_and_succeeds(cli, capsys):
    cli(_report())
    assert r.main([]) == 0
    assert "Verdict: READY" in capsys.readouterr().out


def test_the_command_can_print_json(cli, capsys):
    cli(_report())
    assert r.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == r.READY


@pytest.mark.parametrize("verdict, gate, code", [
    (r.READY, True, 0), (r.NOT_READY, True, 1), (r.NOT_READY, False, 0), (r.AT_RISK_VERDICT, True, 1),
])
def test_the_gate_flag_turns_the_verdict_into_an_exit_code(cli, verdict, gate, code):
    cli(_report(verdict=verdict))
    assert r.main(["--gate"] if gate else []) == code


def test_a_report_that_cannot_be_built_is_exit_code_two_not_a_pass(cli, capsys):
    cli(error=ConnectionError("database down"))

    assert r.main(["--gate"]) == 2
    assert "could not build the report: ConnectionError: database down" in capsys.readouterr().err
