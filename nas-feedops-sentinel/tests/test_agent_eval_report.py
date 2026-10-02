"""The printed evaluation report (src/agent_evaluator.py print_eval_report)."""
from __future__ import annotations

from src.agent_evaluator import print_eval_report
from src.models import EvalReport, EvalResult


def test_the_report_prints_each_case_with_its_status_latency_and_a_note_for_a_failure(capsys):
    report = EvalReport(
        total_cases=3, passed=1, failed=2, score_pct=33.3, llm_enabled=False,
        results=[
            EvalResult(scenario_name="guardrails::ok", passed=True, score=1.0, latency_ms=12.34),
            EvalResult(scenario_name="guardrails::broken", passed=False, score=0.0, latency_ms=None,
                       notes="the guardrail let it through"),
            EvalResult(scenario_name="guardrails::silent", passed=False, score=0.0, latency_ms=1.0),
        ],
    )

    print_eval_report(report)
    out = capsys.readouterr().out

    assert f"Run: {report.run_id}" in out and "Overall Score: 33.3%" in out and "LLM Evaluated: False" in out
    assert "guardrails::ok" in out and "PASS" in out and "12.3ms" in out
    assert "N/A" in out and "-> Note: the guardrail let it through" in out
    assert out.count("-> Note:") == 1, "a failure with no note prints no note line"
