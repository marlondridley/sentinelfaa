"""
Deployment readiness for the parts of Sentinel that call a model.

    python -m src.readiness                   # last window_hours, readable
    python -m src.readiness --hours 72 --json
    python -m src.readiness --gate            # exit 0 only when READY

Measures real behavior against policies/model-slos.yaml and returns one verdict:

  READY              every objective met, with enough evidence to say so
  AT RISK            nothing missed, but something is close to its limit
  NOT READY          at least one objective missed
  INSUFFICIENT DATA  too few calls, or a required objective has no evidence yet

Sources -- all durable, all written by the system itself:

  model_calls       every model call: policy decision, outcome class, wall
                    time, tokens, cost (src/model_telemetry.py)
  investigations    sealed records: what models claimed, and what the grounding
                    gate rejected for citing evidence that was never collected;
                    each record is verified against its SHA-256 as it is read
  critic_results + staged_actions
                    what operators decided about alerts a model assessed

Error budgets use the standard definition, described in model-slos.yaml. The
Microsoft Agent Governance Toolkit's agent-sre package was evaluated for this
and not used: in 3.7.0, ErrorBudget.record_event() adds 1.0 per failed event
against a fractional budget, so one failure in a hundred calls reads as
EXHAUSTED.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src import storage
from src.async_bridge import run_async
from src.config import settings
from src.model_telemetry import SUCCESS_OUTCOMES

MET, AT_RISK, MISSED, NO_DATA = "MET", "AT_RISK", "MISSED", "NO_DATA"
READY, AT_RISK_VERDICT, NOT_READY, INSUFFICIENT = "READY", "AT RISK", "NOT READY", "INSUFFICIENT DATA"

# Outcomes where a completion came back, usable or not.
RESPONDED = ("ok", "repaired", "incomplete", "invalid_output")
KNOWN_OBJECTIVES = (
    "availability", "output_validity", "grounding", "latency_p95_seconds",
    "cost_per_call_usd", "policy_compliance", "operator_agreement",
)


@dataclass(frozen=True)
class Objective:
    name: str
    description: str = ""
    target: float | None = None      # ratio objectives: the minimum good share
    max: float | None = None         # limit objectives: the maximum value
    required: bool = True
    min_samples: int = 1


@dataclass
class ObjectiveResult:
    name: str
    description: str
    status: str
    value: float | None
    target: float | None
    limit: float | None
    samples: int
    budget_used: float | None
    required: bool
    detail: str


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
        return asdict(self)


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
        cleaned["description"] = " ".join(str(cleaned.get("description", "")).split())
        objectives.append(Objective(**cleaned))
    return data, objectives


# ── Status arithmetic ────────────────────────────────────────────────────────

def ratio_status(good: int, total: int, target: float, at_risk: float,
                 min_samples: int) -> tuple[str, float | None, float | None]:
    """(status, value, share of the error budget used) for a ratio objective."""
    value = good / total if total else None
    if total < max(min_samples, 1):
        return NO_DATA, value, None
    allowed = 1.0 - target
    errors = 1.0 - value
    if allowed <= 0:
        used = 0.0 if errors <= 0 else math.inf
    else:
        used = round(errors / allowed, 6)   # 1 - 0.95 is not exactly 0.05
    return _grade(used, at_risk), value, used


def limit_status(value: float | None, limit: float, samples: int, at_risk: float,
                 min_samples: int) -> tuple[str, float | None, float | None]:
    """(status, value, share of the limit used) for a limit objective."""
    if value is None or samples < max(min_samples, 1):
        return NO_DATA, value, None
    used = round(value / limit, 6) if limit > 0 else math.inf
    return _grade(used, at_risk), value, used


def _grade(used: float, at_risk: float) -> str:
    return MISSED if used > 1.0 else AT_RISK if used > at_risk else MET


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


# ── Measurements ─────────────────────────────────────────────────────────────

def grounding_by_role(investigations: list) -> dict[str, dict[str, int]]:
    """Claims models made that survived the grounding gate, and those it rejected, by role."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"accepted": 0, "rejected": 0})
    for inv in investigations:
        for finding in inv.findings:
            if finding.provenance.method == "llm":
                counts[finding.agent]["accepted"] += len(finding.claims)
                counts[finding.agent]["rejected"] += len(finding.rejected_claims)
        critique = inv.critique
        if critique is not None and critique.provenance.method == "llm":
            alt = critique.strongest_alternative
            counts[critique.agent]["accepted"] += (
                sum(1 for c in critique.challenges if c.origin == "llm")
                + (1 if alt is not None and alt.claim_id.endswith(":ALT") else 0)
            )
            counts[critique.agent]["rejected"] += len(critique.rejected_claims)
        brief = inv.brief
        if brief is not None and brief.provenance.method == "llm":
            counts[brief.agent]["accepted"] += len(brief.established)
            counts[brief.agent]["rejected"] += len(brief.rejected_claims)
    return {role: dict(c) for role, c in counts.items()}


def operator_agreement(verdicts: list[dict]) -> dict[str, int]:
    """
    Model verdicts only -- a degraded Critic result has no confidence. Approved
    and rejected actions are unambiguous; an override edits the
    recommendation and is counted apart.
    """
    out = {"agreed": 0, "disagreed": 0, "overridden": 0, "undecided": 0}
    for v in verdicts:
        if v.get("confidence") is None:
            continue
        status = v.get("action_status")
        if status == "APPROVED":
            out["agreed" if v.get("escalate") else "disagreed"] += 1
        elif status == "REJECTED":
            out["disagreed" if v.get("escalate") else "agreed"] += 1
        elif status == "OVERRIDDEN":
            out["overridden"] += 1
        else:
            out["undecided"] += 1
    return out


def summarize_calls(rows: list[dict]) -> dict[str, Any]:
    """Failure modes, latency, tokens, cost and served models from model_calls rows."""
    sent = [r for r in rows if r["outcome"] != "policy_denied"]
    responded = [r for r in sent if r["outcome"] in RESPONDED]
    latencies = [r["latency_ms"] / 1000 for r in sent]
    costs = [r["cost_usd"] for r in responded if r.get("cost_usd") is not None]

    by_role: dict[str, dict[str, Any]] = {}
    for role in sorted({r["role"] for r in rows}):
        mine = [r for r in rows if r["role"] == role]
        mine_sent = [r["latency_ms"] / 1000 for r in mine if r["outcome"] != "policy_denied"]
        by_role[role] = {
            "calls": len(mine),
            "outcomes": dict(Counter(r["outcome"] for r in mine)),
            "latency_p50_s": percentile(mine_sent, 0.50),
            "latency_p95_s": percentile(mine_sent, 0.95),
            "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in mine), 6),
        }

    served = Counter(r["served_model"] for r in responded if r.get("served_model"))
    primary = served.most_common(1)[0][0] if served else None
    return {
        "calls": len(rows),
        "sent": len(sent),
        "refused_by_policy": len(rows) - len(sent),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "policy_rules": dict(Counter(r["policy_rule"] or "(default)" for r in rows
                                     if r["outcome"] == "policy_denied")),
        "latency_p50_s": percentile(latencies, 0.50),
        "latency_p95_s": percentile(latencies, 0.95),
        "latency_max_s": max(latencies) if latencies else None,
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in sent),
        "completion_tokens": sum(r.get("completion_tokens") or 0 for r in sent),
        "cost_usd": round(sum(costs), 6),
        "cost_known_calls": len(costs),
        "cost_unknown_calls": len(responded) - len(costs),
        "served_models": dict(served),
        "served_by_other_than_most_used": (
            round(1 - served[primary] / sum(served.values()), 4) if primary else None
        ),
        "by_role": by_role,
    }


# ── The report ───────────────────────────────────────────────────────────────

async def build_report(hours: float | None = None, spec_path: Path | None = None) -> Report:
    spec, objectives = load_spec(spec_path)
    window = float(hours or spec.get("window_hours", 24))
    at_risk = float(spec.get("at_risk_budget", 0.75))
    min_calls = int(spec.get("min_calls", 20))
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window)

    rows = await storage.get_model_calls_since(since)
    verdicts = await storage.get_critic_verdicts_since(since)
    integrity_error = None
    try:
        investigations = await storage.get_investigations_since(since)
    except Exception as exc:  # noqa: BLE001 -- a record failing its hash is itself the finding
        investigations, integrity_error = [], f"{type(exc).__name__}: {exc}"

    calls = summarize_calls(rows)
    grounding = grounding_by_role(investigations)
    agreement = operator_agreement(verdicts)
    sent = [r for r in rows if r["outcome"] != "policy_denied"]
    responded = [r for r in sent if r["outcome"] in RESPONDED]

    measures = {
        "availability": ("ratio", sum(r["outcome"] in SUCCESS_OUTCOMES for r in sent), len(sent), "calls sent"),
        "output_validity": ("ratio", sum(r["outcome"] == "ok" for r in responded), len(responded), "answers"),
        "grounding": ("ratio", sum(c["accepted"] for c in grounding.values()),
                      sum(c["accepted"] + c["rejected"] for c in grounding.values()), "model claims"),
        "policy_compliance": ("ratio", len(sent), len(rows), "calls attempted"),
        "operator_agreement": ("ratio", agreement["agreed"], agreement["agreed"] + agreement["disagreed"],
                               "decided actions"),
        "latency_p95_seconds": ("limit", calls["latency_p95_s"], len(sent), "calls sent"),
        "cost_per_call_usd": ("limit", (calls["cost_usd"] / calls["cost_known_calls"])
                              if calls["cost_known_calls"] else None, calls["cost_known_calls"],
                              "calls with a known cost"),
    }

    results: list[ObjectiveResult] = []
    for obj in objectives:
        kind, a, b, unit = measures[obj.name]
        if obj.target is not None:
            if kind != "ratio":
                raise ValueError(f"objective {obj.name!r} is a limit; give it max, not target")
            status, value, used = ratio_status(a, b, obj.target, at_risk, obj.min_samples)
            samples = b
            detail = (f"{a} of {b} {unit} ({_pct(value)}; objective at least {_pct(obj.target)})"
                      if b else f"no {unit} in the window")
        else:
            if kind != "limit":
                raise ValueError(f"objective {obj.name!r} is a ratio; give it target, not max")
            status, value, used = limit_status(a, obj.max, b, at_risk, obj.min_samples)
            samples = b
            detail = (f"{_num(value)} over {b} {unit} (objective at most {_num(obj.max)})"
                      if value is not None else f"no {unit} in the window")
        if status == NO_DATA and b and b < obj.min_samples:
            detail += f"; {obj.min_samples} needed to judge"
        if used is not None:
            detail += f"; {_pct(used)} of the {'error budget' if kind == 'ratio' else 'limit'} used"
        results.append(ObjectiveResult(
            name=obj.name, description=obj.description, status=status, value=value,
            target=obj.target, limit=obj.max, samples=samples, budget_used=used,
            required=obj.required, detail=detail,
        ))

    verdict, reasons = decide(results, calls["calls"], min_calls)
    if integrity_error:
        verdict = NOT_READY
        reasons.insert(0, f"an investigation record failed its integrity check: {integrity_error}")

    return Report(
        verdict=verdict, reasons=reasons, window_hours=window,
        generated_at=now.isoformat(timespec="seconds"), calls=calls["calls"], objectives=results,
        breakdown={"calls": calls, "grounding_by_role": grounding, "operator_agreement": agreement},
    )


def decide(results: list[ObjectiveResult], calls: int, min_calls: int) -> tuple[str, list[str]]:
    missed = [r for r in results if r.status == MISSED]
    if missed:
        return NOT_READY, [f"{r.name}: {r.detail}" for r in missed]
    if calls < min_calls:
        return INSUFFICIENT, [f"{calls} model call(s) in the window; {min_calls} are needed to judge readiness."]
    lacking = [r for r in results if r.status == NO_DATA and r.required]
    if lacking:
        return INSUFFICIENT, [f"{r.name}: {r.detail}" for r in lacking]
    risky = [r for r in results if r.status == AT_RISK]
    if risky:
        return AT_RISK_VERDICT, [f"{r.name}: {r.detail}" for r in risky]
    return READY, ["Every objective met."]


# ── Output ───────────────────────────────────────────────────────────────────

def _pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return "inf" if math.isinf(x) else f"{x:.1%}"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.4g}"


def render_text(report: Report) -> str:
    c = report.breakdown["calls"]
    lines = [
        f"Model readiness -- last {report.window_hours:g} h (generated {report.generated_at})",
        f"Verdict: {report.verdict}",
        *[f"  - {reason}" for reason in report.reasons],
        "",
        f"{'objective':<22}{'status':<10}{'value':>12}  {'budget used':>11}  samples",
    ]
    for r in report.objectives:
        value = _pct(r.value) if r.target is not None else _num(r.value)
        req = "" if r.required else "  (informational)"
        lines.append(f"{r.name:<22}{r.status:<10}{value:>12}  {_pct(r.budget_used):>11}  {r.samples}{req}")
    lines += [
        "",
        f"Calls: {c['calls']} attempted, {c['sent']} sent, {c['refused_by_policy']} refused by policy",
        "Outcomes: " + (", ".join(f"{k} {v}" for k, v in sorted(c["outcomes"].items())) or "none"),
        f"Latency: p50 {_num(c['latency_p50_s'])} s, p95 {_num(c['latency_p95_s'])} s, max {_num(c['latency_max_s'])} s",
        f"Tokens: {c['prompt_tokens']} prompt, {c['completion_tokens']} completion",
        f"Cost: ${c['cost_usd']:.4f} over {c['cost_known_calls']} call(s) with a known cost"
        + (f"; {c['cost_unknown_calls']} unknown" if c["cost_unknown_calls"] else ""),
        "Served models: " + (", ".join(f"{k} {v}" for k, v in c["served_models"].items()) or "none"),
    ]
    for role, g in sorted(report.breakdown["grounding_by_role"].items()):
        total = g["accepted"] + g["rejected"]
        lines.append(f"Grounding, {role}: {g['accepted']} of {total} claims cited collected evidence")
    a = report.breakdown["operator_agreement"]
    lines.append(f"Operator agreement: {a['agreed']} agreed, {a['disagreed']} disagreed, "
                 f"{a['overridden']} overridden, {a['undecided']} undecided")
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
