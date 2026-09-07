"""
PlannerAgent: Natural language operational intelligence for NAS.

Accepts a plain-English question from a duty manager or operator,
builds a compressed full-status context via ContextRouter, and
returns a structured PlannerResult with an answer, sub-queries
analyzed, confidence score, event citations, and recommended actions.

Pipeline (Layer 3 alignment guardrails on input AND output):
  1. InputGuardrail   — block injection / scope violations
  2. ContextRouter    — compress 30-min status snapshot
  3. LLMClient        — untrusted reasoning (Anthropic or NIM)
  4. OutputGuardrail  — structural verification of model output
  5. Return verified PlannerResult

Degrades gracefully when no LLM provider is configured.
"""
from __future__ import annotations

import time
from typing import Any

from src import config
from src.context_router import ContextRouter
from src.guardrails import InputGuardrail, OutputGuardrail
from src.llm_client import build_llm_client
from src.logging_config import get_logger
from src.metrics import LLM_CALL_LATENCY, LLM_CALLS
from src.models import PlannerResult
from src.prompts_loader import system_prompt, tool_schema


_log = get_logger(__name__)


def _format_planner_message(question: str, context: dict[str, Any]) -> str:
    airport_lines = []
    for ap, data in context.get("airports", {}).items():
        status = data["overall_status"]
        evt_count = data["event_count_30m"]
        svc_statuses = {
            svc: s["status"] for svc, s in data.get("services", {}).items()
        }
        rvr_info = ""
        for rwy, rvr in data.get("rvr", {}).items():
            td = rvr.get("touchdown_ft")
            if td is not None:
                flag = " [LOW RVR]" if td < 1200 else ""
                rvr_info += f" | runway {rwy} touchdown={td}ft{flag}"

        # Live flight ops summary
        phases = data.get("flight_phases", {})
        ac_count = data.get("active_aircraft_count", 0)
        phase_str = (
            f"dep={phases.get('DEPARTURE',0)} "
            f"arr={phases.get('ARRIVAL',0)} "
            f"enr={phases.get('ENROUTE',0)} "
            f"gnd={phases.get('GROUND',0)}"
        )
        flights = data.get("sample_flights", [])
        flights_str = ""
        if flights:
            flights_str = "\n    Active: " + "; ".join(flights[:5])

        airport_lines.append(
            f"  {ap}: overall={status} events_30m={evt_count} "
            f"active_aircraft={ac_count} ({phase_str})\n"
            f"    services={svc_statuses}{rvr_info}"
            f"{flights_str}"
        )

    alert_lines = []
    for a in context.get("active_alerts", []):
        cr = a.get("critic", {})
        conf = f" conf={cr['confidence']:.2f}" if cr.get("confidence") is not None else ""
        pat = f" pattern={cr['pattern']}" if cr.get("pattern") else ""
        esc = " [ESCALATE]" if cr.get("escalate") else ""
        alert_lines.append(
            f"  [{a['severity']:8s}] {a['alert_type']} | {a.get('airport_code','?')} | "
            f"{a.get('service_name','?')}{conf}{pat}{esc}\n"
            f"             {a['description']}"
        )

    s = context.get("summary", {})

    return (
        f"OPERATOR QUESTION: {question}\n\n"
        f"NAS STATUS SNAPSHOT — {context.get('snapshot_time', 'unknown')} UTC\n"
        f"(last {context.get('window_minutes', 30)} min | "
        f"{s.get('total_events_30m', 0)} total events)\n"
        f"{'=' * 60}\n\n"
        f"AIRPORT STATUS:\n"
        + ("\n".join(airport_lines) if airport_lines else "  No data in window")
        + f"\n\nACTIVE ALERTS ({s.get('critical_alerts', 0)} CRITICAL, "
        f"{s.get('warning_alerts', 0)} WARNING):\n"
        + ("\n".join(alert_lines) if alert_lines else "  None")
        + f"\n\nESCALATION REQUIRED: {s.get('escalation_required', False)}\n\n"
        f"Call submit_operational_brief with your structured answer."
    )


class PlannerAgent:
    """Answers natural language operational queries over NAS feed data."""

    def __init__(self) -> None:
        self._llm = build_llm_client() if config.CRITIC_ENABLED else None
        self._router = ContextRouter()
        self._input_guard = InputGuardrail()
        self._output_guard = OutputGuardrail()
        self._prompt = system_prompt("planner")
        self._tool = tool_schema("planner")

    def query(self, question: str) -> PlannerResult:
        if not self._llm:
            reason = (
                "Planner disabled (CRITIC_ENABLED=0)."
                if not config.CRITIC_ENABLED
                else "Planner unavailable (no LLM provider configured)."
            )
            LLM_CALLS.labels(agent="planner", outcome="disabled").inc()
            return PlannerResult(
                question=question,
                answer=reason,
                recommended_actions=["Set ANTHROPIC_API_KEY (or NIM_ENDPOINT) to enable the Planner."],
            )

        # 1. Input interception
        guard = self._input_guard.verify(question)
        if not guard.is_compliant:
            _log.warning(
                "planner_input_blocked",
                extra={"violation_type": guard.violation_type, "detail": guard.detail},
            )
            return PlannerResult(
                question=question,
                answer=f"Input rejected by guardrail: {guard.detail}",
                recommended_actions=[],
            )
        safe_question = guard.sanitized_input or question

        start = time.perf_counter()
        try:
            context = self._router.build_status_context()
            result = self._call_llm(safe_question, context)
            LLM_CALLS.labels(agent="planner", outcome="success").inc()
        except Exception as exc:
            LLM_CALLS.labels(agent="planner", outcome="error").inc()
            _log.error("planner_error", extra={"error": str(exc)})
            return PlannerResult(
                question=question,
                answer=f"Planner error: {exc}",
            )
        finally:
            LLM_CALL_LATENCY.labels(agent="planner").observe(time.perf_counter() - start)

        # 4. Output verification
        out_check = self._output_guard.verify_planner_output({
            "answer": result.answer,
            "confidence": result.confidence,
            "recommended_actions": result.recommended_actions,
            "sub_queries": result.sub_queries,
        })
        if not out_check.is_compliant:
            _log.warning(
                "planner_output_blocked",
                extra={"violation_type": out_check.violation_type, "detail": out_check.detail},
            )
            return PlannerResult(
                question=question,
                answer=f"Output failed guardrail: {out_check.detail}",
                recommended_actions=[],
            )

        return result

    def _call_llm(self, question: str, context: dict[str, Any]) -> PlannerResult:
        inp = self._llm.call_with_tool(
            system_prompt=self._prompt,
            tool=self._tool,
            user_message=_format_planner_message(question, context),
            max_tokens=768,
        )
        return PlannerResult(
            question=question,
            answer=inp["answer"],
            sub_queries=inp.get("sub_queries", []),
            confidence=float(inp["confidence"]),
            citations=inp.get("citations", []),
            recommended_actions=inp.get("recommended_actions", []),
        )
