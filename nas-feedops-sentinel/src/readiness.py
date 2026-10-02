"""
Deployment readiness for the parts of Sentinel that use a model.

    python -m src.readiness                   # last window_hours, readable
    python -m src.readiness --hours 72 --json
    python -m src.readiness --gate            # exit 0 only when READY

Measures real behaviour against policies/model-slos.yaml and returns one
verdict:

  READY              every objective met, with enough evidence to say so
  AT RISK            nothing missed, but something is close to its limit
  NOT READY          at least one objective missed, or a sealed record
                     failed its integrity check
  INSUFFICIENT DATA  too few calls, or a required objective has no evidence yet

The measurements come from the evaluation layer (src/evaluation.py), which
computes them with the Agent Governance Toolkit wherever AGT computes them
correctly (docs/agent-metrics.md). The verdict is AGT-compatible: AGT
defines error budgets and SLOs, but the error-budget arithmetic and the
grading are Sentinel's until the installed AGT's are fixed. agent-sre's ErrorBudget adds
1.0 per failed event against a fractional budget, so one failure in a
hundred calls against a 95% objective reads as EXHAUSTED, and its burn rate
divides by the window in seconds (tests/test_agt_known_bugs.py).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.async_bridge import run_async
from src.config import settings

MET, AT_RISK, MISSED, NO_DATA = "MET", "AT_RISK", "MISSED", "NO_DATA"
READY, AT_RISK_VERDICT, NOT_READY, INSUFFICIENT = "READY", "AT RISK", "NOT READY", "INSUFFICIENT DATA"

# Objective name -> how to read it from the evaluation layer's window metrics:
# (group, key, sub-key for the value, sub-key for the sample count, unit).
_MEASURES: dict[str, tuple[str, str, str, str, str]] = {
    "decision_quality": ("quality", "decision_quality", "value", "samples", "scored criteria"),
    "evidence_grounding": ("quality", "evidence_grounding", "value", "samples", "investigations with model output"),
    "human_override_rate": ("quality", "human_override_rate", "value", "samples", "decided actions"),
    "unsupported_claim_rate": ("quality", "unsupported_claim_rate", "value", "samples", "model claims"),
    "missed_escalation_rate": ("quality", "missed_escalation_rate", "value", "samples", "approved escalations"),
    "false_escalation_rate": ("quality", "false_escalation_rate", "value", "samples", "decided escalations"),
    "calibration_error": ("quality", "calibration_error", "value", "samples", "decided recommendations"),
    "investigation_latency_p95_seconds": ("latency", "investigation", "p95", "n", "investigations"),
    "model_call_latency_p95_seconds": ("latency", "model_call", "p95", "n", "model calls sent"),
    "cost_per_investigation_usd": ("cost", "per_investigation", "mean_usd", "tasks", "investigations with a known cost"),
    "agent_failure_rate": ("reliability", "agent_failure_rate", "value", "samples", "agent invocations"),
    "availability": ("reliability", "call_success_rate", "value", "samples", "model calls sent"),
    "output_validity": ("reliability", "output_validity", "value", "samples", "answers"),
    "audit_completeness": ("governance", "audit_completeness", "value", "samples", "investigations"),
    "policy_compliance": ("governance", "policy_compliance", "rate", "checks", "policy checks"),
}
KNOWN_OBJECTIVES = tuple(_MEASURES)


@dataclass(frozen=True)
class Objective:
    name: str
    label: str = ""
    description: str = ""
    target: float | None = None      # higher is better: the minimum good share
    max: float | None = None         # lower is better: the maximum value
    required: bool = True
    min_samples: int = 1
    data: str = ""                   # what it is measured from, in words
    watch_officer: str = ""          # what a watch officer does when it is off target
    system_engineer: str = ""        # ... and a system engineer


@dataclass
class ObjectiveResult:
    name: str
    label: str
    description: str
    status: str
    value: float | None
    target: float | None
    limit: float | None
    samples: int
    budget_used: float | None
    required: bool
    detail: str
    source: str                      # which component calculated it (docs/agent-metrics.md), not the data
    min_samples: int = 1
    unit: str = ""                   # what `samples` counts: "investigations", "model calls sent", ...
    data: str = ""
    watch_officer: str = ""
    system_engineer: str = ""


@dataclass
class Report:
    verdict: str
    reasons: list[str]
    window_hours: float
    generated_at: str
    calls: int
    objectives: list[ObjectiveResult]
    breakdown: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _finite(asdict(self))


# ── Objectives file ──────────────────────────────────────────────────────────

def load_spec(path: Path | None = None) -> tuple[dict[str, Any], list[Objective]]:
    """Read model-slos.yaml. An objective this module cannot measure is an error, not a skip."""
    data = yaml.safe_load(Path(path or settings.MODEL_SLO_PATH).read_text(encoding="utf-8"))
    names = {f.name for f in fields(Objective)}
    objectives = []
    for raw in data.get("objectives") or []:
        if raw.get("name") not in KNOWN_OBJECTIVES:
            raise ValueError(f"unknown objective {raw.get('name')!r} in model-slos.yaml")
        if (raw.get("target") is None) == (raw.get("max") is None):
            raise ValueError(f"objective {raw['name']!r} needs exactly one of target or max")
        cleaned = {k: v for k, v in raw.items() if k in names}
        for text in ("description", "data", "watch_officer", "system_engineer"):
            cleaned[text] = " ".join(str(cleaned.get(text, "")).split())
        cleaned.setdefault("label", raw["name"].replace("_", " ").capitalize())
        objectives.append(Objective(**cleaned))
    return data, objectives


# ── Status arithmetic (AGT-compatible: ErrorBudget bugs A, B) ───────────────────

def score_status(value: float | None, samples: int, target: float, at_risk: float,
                 min_samples: int) -> tuple[str, float | None]:
    """(status, share of the error budget used) for a higher-is-better share with target T."""
    if value is None or samples < max(min_samples, 1):
        return NO_DATA, None
    allowed = 1.0 - target
    errors = max(1.0 - value, 0.0)
    if allowed <= 0:
        used = 0.0 if errors <= 1e-12 else math.inf
    else:
        used = round(errors / allowed, 6)   # 1 - 0.95 is not exactly 0.05
    return _grade(used, at_risk), used


def ratio_status(good: int, total: int, target: float, at_risk: float,
                 min_samples: int) -> tuple[str, float | None, float | None]:
    """(status, value, share of the error budget used) for good events out of total."""
    value = good / total if total else None
    status, used = score_status(value, total, target, at_risk, min_samples)
    return status, value, used


def limit_status(value: float | None, limit: float, samples: int, at_risk: float,
                 min_samples: int) -> tuple[str, float | None, float | None]:
    """(status, value, share of the limit used) for a lower-is-better value."""
    if value is None or samples < max(min_samples, 1):
        return NO_DATA, value, None
    used = round(value / limit, 6) if limit > 0 else (0.0 if value <= 0 else math.inf)
    return _grade(used, at_risk), value, used


def _grade(used: float, at_risk: float) -> str:
    return MISSED if used > 1.0 else AT_RISK if used > at_risk else MET


# ── The report ───────────────────────────────────────────────────────────────

def _read(metrics: dict[str, Any], name: str) -> tuple[float | None, int, str, str]:
    group, key, value_key, count_key, unit = _MEASURES[name]
    entry = (metrics.get(group) or {}).get(key) or {}
    value = entry.get(value_key)
    samples = int(entry.get(count_key) or 0)
    source = entry.get("source") or {
        "latency": "AGT-native: agent-sre ResponseLatency", "cost": "AGT-native: agent-sre CostGuard",
    }.get(group, "Sentinel-specific")
    return (float(value) if value is not None else None), samples, unit, source


def grade(objectives: list[Objective], metrics: dict[str, Any], at_risk: float) -> list[ObjectiveResult]:
    results = []
    for obj in objectives:
        value, samples, unit, source = _read(metrics, obj.name)
        if obj.target is not None:
            status, used = score_status(value, samples, obj.target, at_risk, obj.min_samples)
            shown = f"{_pct(value)} over {samples} {unit} (objective at least {_pct(obj.target)})"
        else:
            status, value, used = limit_status(value, obj.max, samples, at_risk, obj.min_samples)
            shown = (f"{_in_unit(obj.name, value)} over {samples} {unit} "
                     f"(objective at most {_in_unit(obj.name, obj.max)})")
        detail = shown if value is not None and samples else f"no {unit} in the window"
        if status == NO_DATA and samples and samples < obj.min_samples:
            detail += f"; {obj.min_samples} needed to judge"
        if used is not None:
            detail += f"; {_pct(used)} of the {'error budget' if obj.target is not None else 'limit'} used"
        results.append(ObjectiveResult(
            name=obj.name, label=obj.label, description=obj.description, status=status, value=value,
            target=obj.target, limit=obj.max, samples=samples, budget_used=used, required=obj.required,
            detail=detail, source=source, min_samples=obj.min_samples, unit=unit, data=obj.data,
            watch_officer=obj.watch_officer, system_engineer=obj.system_engineer,
        ))
    return results


async def build_report(hours: float | None = None, spec_path: Path | None = None) -> Report:
    return (await build_report_with_window(hours, spec_path))[0]


async def build_report_with_window(hours: float | None = None, spec_path: Path | None = None):
    """
    (the report, the evaluated window it was graded from). The window is what src/evaluation_evidence.py lists
    row by row behind each objective; it is None when a record failed its integrity check.
    """
    from src.evaluation import evaluate_window, load_gate_config

    spec, objectives = load_spec(spec_path)
    window = float(hours or spec.get("window_hours", 24))
    at_risk = float(spec.get("at_risk_budget", 0.75))
    min_calls = int(spec.get("min_calls", 20))
    now = datetime.now(timezone.utc)

    integrity_error, evaluated = None, None
    try:
        evaluated = await evaluate_window(window, load_gate_config(spec_path))
        metrics, calls = evaluated.metrics, len(evaluated.calls)
    except Exception as exc:  # noqa: BLE001 -- a record failing its hash is itself the finding
        if type(exc).__name__ != "IntegrityError":
            raise
        integrity_error, metrics, calls = f"{type(exc).__name__}: {exc}", {}, 0

    results = grade(objectives, metrics, at_risk)
    verdict, reasons = decide(results, calls, min_calls)
    if integrity_error:
        verdict = NOT_READY
        reasons.insert(0, f"an investigation record failed its integrity check: {integrity_error}")
    return Report(verdict=verdict, reasons=reasons, window_hours=window,
                  generated_at=now.isoformat(timespec="seconds"), calls=calls, objectives=results,
                  breakdown=metrics), evaluated


def decide(results: list[ObjectiveResult], calls: int, min_calls: int) -> tuple[str, list[str]]:
    missed = [r for r in results if r.status == MISSED]
    if missed:
        return NOT_READY, [f"{r.label}: {r.detail}" for r in missed]
    if calls < min_calls:
        return INSUFFICIENT, [f"{calls} model call(s) in the window; {min_calls} are needed to judge readiness."]
    lacking = [r for r in results if r.status == NO_DATA and r.required]
    if lacking:
        return INSUFFICIENT, [f"{r.label}: {r.detail}" for r in lacking]
    risky = [r for r in results if r.status == AT_RISK]
    if risky:
        return AT_RISK_VERDICT, [f"{r.label}: {r.detail}" for r in risky]
    return READY, ["Every objective met."]


# ── Output ───────────────────────────────────────────────────────────────────

def _finite(value: Any) -> Any:
    """JSON has no infinity: an unbounded budget use is reported as null with the status saying why."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return "inf" if math.isinf(x) else f"{x:.1%}"


def _in_unit(name: str, x: float | None) -> str:
    """A lower-is-better value in its own unit, as the page shows it: seconds, dollars, or a rate as a percentage."""
    if x is None:
        return "n/a"
    if name.endswith("_seconds"):
        return f"{x:.1f} s"
    if name.endswith("_usd"):
        return f"${x:.4f}"
    return f"{x:.1%}"


def render_text(report: Report) -> str:
    lines = [
        f"Model readiness -- last {report.window_hours:g} h (generated {report.generated_at})",
        f"Verdict: {report.verdict}",
        *[f"  - {reason}" for reason in report.reasons],
        "",
        f"{'objective':<34}{'status':<10}{'value':>12}  {'budget used':>11}  samples",
    ]
    for r in report.objectives:
        value = _pct(r.value) if r.target is not None else _in_unit(r.name, r.value)
        req = "" if r.required else "  (informational)"
        lines.append(f"{r.label:<34}{r.status:<10}{value:>12}  {_pct(r.budget_used):>11}  {r.samples}{req}")
    counts = report.breakdown.get("counts") or {}
    if counts:
        lines += ["", f"Window: {counts.get('investigations', 0)} investigation(s), "
                      f"{counts.get('with_model', 0)} with a model; {counts.get('calls', 0)} model call(s); "
                      f"{counts.get('decided', 0)} decided recommendation(s)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.readiness", description=__doc__.split("\n\n")[0])
    parser.add_argument("--hours", type=float, default=None, help="window to measure (default: model-slos.yaml)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--gate", action="store_true", help="exit 1 unless the verdict is READY")
    args = parser.parse_args(argv)
    try:
        report = run_async(build_report(args.hours))
    except Exception as exc:  # noqa: BLE001
        print(f"readiness: could not build the report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), indent=2, default=str) if args.json else render_text(report))
    return 0 if (report.verdict == READY or not args.gate) else 1


if __name__ == "__main__":
    sys.exit(main())
