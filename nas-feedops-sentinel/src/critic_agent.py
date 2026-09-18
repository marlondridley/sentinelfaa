"""
CriticAgent: Agentic reasoning layer for NAS telemetry and alert validation.
Refactored for CrewAI Flow integration and LiteLLM proxy fallback.
"""
from __future__ import annotations

import time
from typing import Any
from crewai import Agent

from src.config import settings
from src.context_router import ContextRouter
from src.llm_client import build_llm_client
from src.llm_config import get_llm
from src.logging_config import get_logger
from src.metrics import LLM_CALL_LATENCY, LLM_CALLS
from src.models import AlertEvent, CriticResult
from src.prompts_loader import system_prompt, tool_schema

_router = ContextRouter()
_log = get_logger(__name__)


def get_critic_agent(llm: Any = None) -> Agent:
    """Factory function producing a standardized CrewAI Agent instance for the Critic role."""
    return Agent(
        role="Critic Agent",
        goal="Identify operational anomaly patterns, risk markers, and false positives in aviation telemetry.",
        backstory=(
            "You are an expert aviation telemetry specialist for PCT TRACON. "
            "You analyze real-time ISMC, APDS_RVR, and SMES feed anomalies to assess operational risk "
            "and determine whether alerts warrant human escalation."
        ),
        verbose=settings.AGENT_VERBOSE,
        allow_delegation=False,
        llm=llm or get_llm(),
    )


def _format_user_message(alert: AlertEvent, context: dict[str, Any]) -> str:
    svc_lines = []
    for svc, s in context["service_summary"].items():
        lat = ""
        if "avg_latency_ms" in s:
            lat = f" | avg_lat={s['avg_latency_ms']}ms max_lat={s['max_latency_ms']}ms"
        status = f" | feed_statuses={s['feed_statuses']}" if s["feed_statuses"] else ""
        ids = f" | sample_ids={s['sample_event_ids']}"
        svc_lines.append(f"  {svc:20s} count={s['count']}{lat}{status}{ids}")

    prior_alerts = "None in window"
    if context["recent_alerts"]:
        prior_alerts = "\n".join(
            f"  [{a['severity']:8s}] {a['alert_type']} | {a['service_name']} | {a['triggered_at']}"
            for a in context["recent_alerts"]
        )

    return (
        f"ALERT TO CRITIQUE\n"
        f"-----------------\n"
        f"Alert ID   : {alert.alert_id}\n"
        f"Type       : {alert.alert_type}\n"
        f"Severity   : {alert.severity}\n"
        f"Airport    : {alert.airport_code or 'N/A'}\n"
        f"Service    : {alert.service_name or 'N/A'}\n"
        f"Source evt : {alert.event_id or 'N/A'}\n"
        f"Description: {alert.description}\n"
        f"\n"
        f"EVIDENCE CONTEXT (last {context['window_minutes']} min at "
        f"{alert.airport_code or 'all airports'}, "
        f"{context['total_recent_events']} events)\n"
        f"{'-' * 60}\n"
        f"Service activity:\n"
        + ("\n".join(svc_lines) if svc_lines else "  No events in window")
        + f"\n\nPrior alerts at this airport (last 60 min):\n{prior_alerts}\n"
        f"\nCall submit_critique to record your structured assessment."
    )


class CriticAgent:
    """Async engine wrapper for non-CrewAI direct agentic queries (Temporal / Pipeline)."""

    def __init__(self) -> None:
        self._llm = build_llm_client() if settings.CRITIC_ENABLED else None
        self._prompt = system_prompt("critic")
        self._tool = tool_schema("critic")

    async def critique(self, alert: AlertEvent) -> CriticResult:
        """Evaluate an alert asynchronously using context retrieved from async storage."""
        if not self._llm:
            LLM_CALLS.labels(agent="critic", outcome="disabled").inc()
            reason = (
                "Critic disabled (CRITIC_ENABLED=0)."
                if not settings.CRITIC_ENABLED
                else "Critic unavailable (no LLM provider configured)."
            )
            return CriticResult(alert_id=alert.alert_id, reasoning=reason)

        start = time.perf_counter()
        try:
            context = await _router.build_alert_context(alert.airport_code)
            result = await self._call_llm(alert, context)
            LLM_CALLS.labels(agent="critic", outcome="success").inc()
            _log.info(
                "critic_success",
                extra={
                    "alert_id": alert.alert_id,
                    "pattern": result.pattern,
                    "confidence": result.confidence,
                    "escalate": result.escalate,
                },
            )
            return result
        except Exception as exc:  # noqa: BLE001
            LLM_CALLS.labels(agent="critic", outcome="error").inc()
            _log.error(
                "critic_error",
                extra={"alert_id": alert.alert_id, "error": str(exc)},
            )
            return CriticResult(
                alert_id=alert.alert_id,
                reasoning=f"Critic error: {exc}",
            )
        finally:
            LLM_CALL_LATENCY.labels(agent="critic").observe(time.perf_counter() - start)

    async def _call_llm(self, alert: AlertEvent, context: dict[str, Any]) -> CriticResult:
        inp = await self._llm.acall_with_tool(
            system_prompt=self._prompt,
            tool=self._tool,
            user_message=_format_user_message(alert, context),
            max_tokens=512,
        )
        return CriticResult(
            alert_id=alert.alert_id,
            confidence=float(inp["confidence"]),
            reasoning=inp["reasoning"],
            citations=inp.get("citations", []),
            pattern=inp["pattern"],
            escalate=bool(inp["escalate"]),
        )