"""
Agent Evaluation & Governance -- Layer 3 of the agent evaluation.

The operator's screens show at most five AI indicators (the Overview card)
and one investigation's evaluation (the Review page). This page is the rest,
for engineering, program management and governance: is the AI layer
measured, governed and ready to deploy?

Everything here is computed by the evaluation layer (src/evaluation.py) from
the durable record -- model_calls, sealed investigations, staged actions and
the append-only audit log -- never from an agent's account of itself, and
with the Microsoft Agent Governance Toolkit wherever AGT computes the metric
correctly (docs/agent-metrics.md; the Source column says which). The verdict
and its targets are policies/model-slos.yaml, the same ones
`python -m src.readiness --gate` applies. Anything that cannot be measured
yet says "no data".
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from lib import safe_text, theme  # noqa: E402
from lib.agent_eval import NO_DATA, pct, seconds, usd  # noqa: E402
from lib.page_setup import bootstrap_page  # noqa: E402
from src import readiness  # noqa: E402
from src.async_bridge import run_async as run  # noqa: E402

bootstrap_page(page_title="Agent Evaluation — PCT FeedOps Sentinel", page_icon="🧪")

theme.page_header(
    "Agent Evaluation & Governance",
    "Is the AI layer measured, governed and ready to deploy?",
    detail=(
        "Agents never grade themselves: execution telemetry → evaluation → quality metrics → this page. "
        "The Microsoft Agent Governance Toolkit computes and governs; OpenTelemetry carries the data. "
        "Targets are policies/model-slos.yaml, starting points rather than measured results."
    ),
)

_WINDOWS = {"Last 24 hours": 24.0, "Last 72 hours": 72.0, "Last 7 days": 168.0, "Last 30 days": 720.0}
_choice = st.segmented_control("Window", list(_WINDOWS), default="Last 24 hours") or "Last 24 hours"

try:
    report = run(readiness.build_report(_WINDOWS[_choice]))
except Exception as exc:  # noqa: BLE001
    st.error(safe_text.failure("The evaluation could not be built", exc))
    st.stop()

m = report.breakdown or {}
counts = m.get("counts", {})

# ── Verdict ──────────────────────────────────────────────────────────────────
_tone = {"READY": "ok", "AT RISK": "warn", "NOT READY": "bad"}.get(report.verdict, "neutral")
theme.posture_banner(
    report.verdict,
    " ".join(report.reasons[:2]) or "—",
    tone=_tone,
    subtitle="deployment readiness",
    meta=[f"{counts.get('investigations', 0)} investigations ({counts.get('with_model', 0)} with a model)",
          f"{counts.get('calls', 0)} model calls", f"{counts.get('decided', 0)} decided recommendations",
          f"window {report.window_hours:g} h"],
)


def _value(r: readiness.ObjectiveResult) -> str:
    if r.value is None:
        return NO_DATA
    if r.name.endswith("_seconds"):
        return seconds(r.value)
    if r.name.endswith("_usd"):
        return usd(r.value)
    return pct(r.value, 1)


def _target(r: readiness.ObjectiveResult) -> str:
    if r.target is not None:
        return f"≥ {pct(r.target, 0)}"
    if r.name.endswith("_seconds"):
        return f"≤ {seconds(r.limit)}"
    if r.name.endswith("_usd"):
        return f"≤ {usd(r.limit)}"
    return f"≤ {pct(r.limit, 0)}"


_ICON = {"MET": "🟢", "AT_RISK": "🟡", "MISSED": "🔴", "NO_DATA": "⚪"}

theme.section("Health", "Each dimension against its target, measured over the window.")
st.dataframe(pd.DataFrame([
    {"": _ICON.get(r.status, ""), "Dimension": r.label, "Current": _value(r), "Target": _target(r),
     "Status": r.status.replace("_", " ") if r.status != "NO_DATA"
     else (f"too little evidence ({r.samples} of {r.min_samples})" if r.samples else NO_DATA),
     "Evidence": f"{r.samples}" + ("" if r.required else " (informational)"), "Source": r.source}
    for r in report.objectives
]), hide_index=True, width="stretch")
with st.expander("What each dimension measures"):
    for r in report.objectives:
        st.markdown(f"**{r.label}.** {r.description} _{r.detail}._")

# ── Latency ──────────────────────────────────────────────────────────────────
lat = m.get("latency", {})
theme.section("Latency", "Alert → investigation → evidence → human review: where the time goes.",
              right="percentiles AGT-native: agent-sre ResponseLatency")


def _p_row(name: str, p: dict) -> dict:
    return {"": name, "P50": seconds(p.get("p50")), "P95": seconds(p.get("p95")), "P99": seconds(p.get("p99")),
            "n": p.get("n", 0)}


_path = [("alert → investigation (queue)", lat.get("queue", {})),
         *[(f"  {k.replace('_', ' ')}", v) for k, v in (lat.get("by_stage") or {}).items()],
         ("  handoffs between stages", lat.get("handoff", {})),
         ("investigation, end to end", lat.get("investigation", {})),
         ("sealed → human decision", lat.get("time_to_decision", {}))]
c1, c2 = st.columns(2)
with c1:
    st.markdown("**The path**")
    st.dataframe(pd.DataFrame([_p_row(n, p) for n, p in _path]), hide_index=True, width="stretch")
with c2:
    st.markdown("**By agent, and per model call**")
    rows = [_p_row(a.replace("_", " "), p) for a, p in (lat.get("by_agent") or {}).items()]
    rows.append(_p_row("one model call", lat.get("model_call", {})))
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(f"Timeouts {pct((lat.get('timeout_rate') or {}).get('value'), 1)} · "
               f"retried calls {pct((lat.get('retry_rate') or {}).get('value'), 1)} of calls sent (Sentinel-specific)")

# ── Cost ─────────────────────────────────────────────────────────────────────
cost = m.get("cost", {})
theme.section("Cost", "What the models cost, per unit of work.", right="AGT-native: agent-sre CostGuard; ratios Sentinel-specific")
per_inv, per_alert = cost.get("per_investigation") or {}, cost.get("per_alert") or {}
tokens = cost.get("tokens") or {}
theme.kpi_row([
    theme.kpi("Spend in window", usd(cost.get("total_usd")), tone="info",
              foot=f"{cost.get('calls_with_cost', 0)} calls priced · {cost.get('calls_cost_unknown', 0)} unknown"),
    theme.kpi("Per investigation", usd(per_inv.get("mean_usd")), tone="neutral",
              foot=f"{per_inv.get('tasks', 0)} investigations with a known cost"),
    theme.kpi("Per alert", usd(per_alert.get("mean_usd")), tone="neutral", foot="Critic + investigation"),
    theme.kpi("Per resolved investigation", usd(cost.get("per_resolved_investigation")), tone="neutral",
              foot="spend ÷ investigations a person decided"),
    theme.kpi("Per useful evidence item", usd(cost.get("per_useful_item")), tone="neutral",
              foot="spend ÷ grounded model contributions"),
    theme.kpi("Tokens", f"{tokens.get('input', 0) + tokens.get('output', 0):,}", tone="neutral",
              foot=f"{tokens.get('input', 0):,} in · {tokens.get('output', 0):,} out"),
], columns=6)
c1, c2 = st.columns(2)
with c1:
    st.markdown("**Per invocation, by agent**")
    st.dataframe(pd.DataFrame([{"agent": k, "calls": v["tasks"], "mean": usd(v["mean_usd"]), "spend": usd(v["spent_usd"])}
                               for k, v in (cost.get("per_invocation_by_role") or {}).items()])
                 if cost.get("per_invocation_by_role") else pd.DataFrame([{"": NO_DATA}]),
                 hide_index=True, width="stretch")
with c2:
    st.markdown("**By served model**")
    st.dataframe(pd.DataFrame([{"model": k, "calls": v["tasks"], "mean": usd(v["mean_usd"]), "spend": usd(v["spent_usd"])}
                               for k, v in (cost.get("by_model") or {}).items()])
                 if cost.get("by_model") else pd.DataFrame([{"": NO_DATA}]),
                 hide_index=True, width="stretch")
st.caption(f"Spent on retried calls {usd(cost.get('retry_cost_usd'))} · on failed calls "
           f"{usd(cost.get('failed_call_cost_usd'))}")

# ── Reliability ──────────────────────────────────────────────────────────────
rel = m.get("reliability", {})
theme.section("Reliability", "Do the agents and the providers behind the gateway hold up?",
              right="AGT-native: agent-sre FleetManager, TaskSuccessRate")
agents = (rel.get("agents") or {}).get("members") or {}
providers = (rel.get("providers") or {}).get("members") or {}
c1, c2 = st.columns(2)
with c1:
    st.markdown("**Agents**")
    st.dataframe(pd.DataFrame([{"agent": a.replace("_", " "), "success": pct(v["success_rate"], 1),
                                "invocations": v["events"], "mean time": seconds(v["mean_latency_s"])}
                               for a, v in agents.items()]) if agents else pd.DataFrame([{"": NO_DATA}]),
                 hide_index=True, width="stretch")
with c2:
    st.markdown("**Providers** (availability of calls each served)")
    st.dataframe(pd.DataFrame([{"provider": p, "availability": pct(v["success_rate"], 1), "calls": v["events"],
                                "mean time": seconds(v["mean_latency_s"])}
                               for p, v in providers.items()]) if providers else pd.DataFrame([{"": NO_DATA}]),
                 hide_index=True, width="stretch")
    st.caption("A call that fails before any response cannot be attributed to a provider behind the gateway; "
               "it is counted under _OTHER.")
theme.kpi_row([
    theme.kpi("Investigations completed", pct((rel.get("investigation_completion") or {}).get("value"), 1),
              tone="neutral", foot="COMPLETE of all opened"),
    theme.kpi("Fell back to rules", pct((rel.get("fallback_rate") or {}).get("value"), 1), tone="neutral",
              foot="of agent steps that tried a model"),
    theme.kpi("Schema failures", pct((rel.get("schema_failure_rate") or {}).get("value"), 1), tone="neutral",
              foot="incomplete or invalid answers"),
    theme.kpi("Malformed output", pct((rel.get("malformed_output_rate") or {}).get("value"), 1), tone="neutral",
              foot="not JSON, or not an object"),
], columns=4)
st.caption("Outcome classes: " + (", ".join(f"{k} {v}" for k, v in sorted((rel.get("outcomes") or {}).items()))
                                  or "none"))

# ── Decision quality ─────────────────────────────────────────────────────────
q = m.get("quality", {})
theme.section("Decision quality", "Is what the agents put in front of a person grounded, and borne out?",
              right="judged by the grounding gate and the operator's decision")
st.dataframe(pd.DataFrame([
    {"measure": label, "value": pct((q.get(key) or {}).get("value"), 1) if key != "evidence_improvement"
     else (f"{(q.get(key) or {}).get('value'):.1f} items" if (q.get(key) or {}).get("value") is not None else NO_DATA),
     "evidence": (q.get(key) or {}).get("samples", 0), "source": (q.get(key) or {}).get("source", "")}
    for key, label in (
        ("decision_quality", "Decision quality score"), ("evidence_grounding", "Evidence grounding"),
        ("recommendation_accuracy", "Recommendation accuracy"), ("unsupported_claim_rate", "Unsupported claims"),
        ("false_escalation_rate", "False escalations"), ("missed_escalation_rate", "Missed escalations"),
        ("human_override_rate", "Human overrides"), ("calibration_error", "Confidence calibration error (ECE)"),
        ("contradiction_rate", "Investigations with contradicting evidence"),
        ("evidence_improvement", "Evidence the models added per investigation"),
    )
]), hide_index=True, width="stretch")

# ── Orchestration ────────────────────────────────────────────────────────────
orch = m.get("orchestration", {})
theme.section("Orchestration", "How the investigation workflow ran.")
depth = orch.get("depth_within_limit") or {}
c1, c2 = st.columns(2)
with c1:
    st.dataframe(pd.DataFrame([
        {"measure": "tool calls per investigation", "value": f"{orch['tool_calls_per_investigation']:.1f}"
         if orch.get("tool_calls_per_investigation") is not None else NO_DATA},
        {"measure": "handoffs per investigation", "value": f"{orch['handoffs_per_investigation']:.1f}"
         if orch.get("handoffs_per_investigation") is not None else NO_DATA},
        {"measure": "parallel stage", "value": orch.get("parallel_stage", "")},
        {"measure": "dependency failures", "value": str(orch.get("dependency_failures", 0))},
        {"measure": "duplicate model calls", "value": str(orch.get("duplicate_model_calls", 0))},
        {"measure": "dead-end investigations", "value": str(orch.get("dead_ends", 0))},
        {"measure": "max depth", "value": str(orch["max_depth"]) if orch.get("max_depth") is not None else NO_DATA},
        {"measure": f"within depth limit ({depth.get('limit')})", "value": pct(depth.get("value"), 0)},
    ]), hide_index=True, width="stretch")
with c2:
    st.markdown("**Context size by agent** (mean prompt tokens, in workflow order)")
    growth = orch.get("context_growth") or []
    st.dataframe(pd.DataFrame([{"agent": a.replace("_", " "), "prompt tokens": t} for a, t in growth])
                 if growth else pd.DataFrame([{"": NO_DATA}]), hide_index=True, width="stretch")
    st.caption("Termination: " + (", ".join(f"{k} {v}" for k, v in (orch.get("termination") or {}).items())
                                  or "none"))

# ── Governance gates ─────────────────────────────────────────────────────────
gov = m.get("governance", {})
theme.section("Governance gates", "Did every investigation pass its quality, safety, cost, latency and audit gates?",
              right="cost and latency AGT-native: agent-os-kernel BudgetTracker")
gates = gov.get("gates") or {}
st.dataframe(pd.DataFrame([
    {"gate": group, **{status: counts_.get(status, 0) for status in ("PASS", "WARN", "FAIL", "PENDING", "N/A")},
     "pass rate": pct((counts_.get("PASS", 0) / sum(counts_.values())) if sum(counts_.values()) else None, 0)}
    for group, counts_ in gates.items()
]) if gates else pd.DataFrame([{"": NO_DATA}]), hide_index=True, width="stretch")
pc = gov.get("policy_compliance") or {}
st.caption(f"Policy compliance (AGT-native: agent-os-kernel GovernanceMetrics): {pc.get('allowed', 0)} of {pc.get('checks', 0)} "
           f"model calls allowed, {pc.get('refused', 0)} refused. Every decision, including tool calls, is in AGT's "
           "audit log (OpenTelemetry log records, agt.*).")

theme.back_link()
