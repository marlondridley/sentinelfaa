"""
Planner Agent (Layer 2) — Operational Advisory & Query Synthesis.
Refactored for CrewAI Flow capability & unified LiteLLM client execution.
"""
from __future__ import annotations

import json
from typing import Any
from crewai import Agent

from src import storage
from src.config import settings
from src.guardrails import OutputGuardrail
from src.llm_client import build_llm_client
from src.llm_config import get_llm
from src.models import PlannerResult

_PLANNER_SYSTEM_PROMPT = """You are the NAS FeedOps Sentinel Planner Agent for PCT TRACON.
Your task is to analyze operational questions, inspect current feed and alert data,
and return a JSON response containing an advisory synthesis for the duty manager.

STRICT REQUIREMENTS:
1. Return ONLY a valid JSON object with these keys:
   - "answer": Clear, concise string summarizing operational status or answering the question.
   - "confidence": Float between 0.0 and 1.0 reflecting certainty based on available data.
   - "recommended_actions": List of strings detailing immediate operational steps.
   - "sub_queries": List of strings representing secondary diagnostic queries or checks.

2. Do NOT invent alerts or events not present in the provided context.
3. If critical/high alerts are active, always include explicit escalation or notification steps.
4. Keep answers focused strictly on PCT TRACON aviation operations, feed health, and RVR conditions.
"""


def get_planner_agent(llm: Any = None) -> Agent:
    """Factory function producing a standardized CrewAI Agent instance for the Planner role."""
    return Agent(
        role="Planner Agent",
        goal="Synthesize operational mitigation actions and sub-queries based on telemetry evaluations.",
        backstory=(
            "You are a Senior Advisory Planner for PCT TRACON operations. "
            "You evaluate telemetry analysis from the Critic Agent and construct actionable operational "
            "mitigation steps and diagnostic sub-queries for duty managers."
        ),
        verbose=True,
        allow_delegation=False,
        llm=llm or get_llm(),
    )


class PlannerAgent:
    """Standalone Planner execution engine using LiteLLM and guardrail validation."""

    def __init__(self) -> None:
        self._llm = build_llm_client() if settings.CRITIC_ENABLED else None
        self._guardrail = OutputGuardrail()

    async def query(self, question: str) -> PlannerResult:
        """Processes an operator question against current persisted NAS state asynchronously."""
        recent_events = await storage.get_recent_events(limit=20)
        recent_alerts = await storage.get_recent_alerts(limit=10)
        critic_reviews = await storage.get_recent_critic_results(limit=5)

        context_summary = {
            "question": question,
            "active_alert_count": len(recent_alerts),
            "alerts": [
                {
                    "alert_id": a.get("alert_id"),
                    "type": a.get("alert_type"),
                    "severity": a.get("severity"),
                    "airport": a.get("airport_code"),
                    "service": a.get("service_name"),
                }
                for a in recent_alerts
            ],
            "recent_events_count": len(recent_events),
            "critic_reviews": critic_reviews,
        }

        if self._llm:
            result = await self._query_llm(question, context_summary)
        else:
            result = self._query_deterministic(question, context_summary)

        raw_dict = {
            "answer": result.answer,
            "confidence": result.confidence,
            "recommended_actions": result.recommended_actions,
            "sub_queries": result.sub_queries,
        }
        guard_res = self._guardrail.verify_planner_output(raw_dict)

        if not guard_res.is_compliant:
            return PlannerResult(
                question=question,
                answer=f"Guardrail intercepted response: {guard_res.detail}",
                confidence=0.0,
                recommended_actions=["Manual duty manager review required due to output compliance failure."],
                sub_queries=["Check system logs for output guardrail violations."],
            )

        return result

    async def _query_llm(self, question: str, context: dict[str, Any]) -> PlannerResult:
        user_content = f"Question: {question}\n\nCurrent System Context:\n{json.dumps(context, indent=2, default=str)}"

        try:
            # Route call through the LiteLLM client proxy
            raw_response = await self._llm.acall(
                system_prompt=_PLANNER_SYSTEM_PROMPT,
                user_message=user_content,
                max_tokens=1000,
                temperature=0.1,
            )
            parsed = json.loads(raw_response.strip())

            return PlannerResult(
                question=question,
                answer=str(parsed.get("answer", "")),
                confidence=float(parsed.get("confidence", 0.0)),
                recommended_actions=list(parsed.get("recommended_actions", [])),
                sub_queries=list(parsed.get("sub_queries", [])),
            )
        except Exception as err:
            return PlannerResult(
                question=question,
                answer=f"LLM execution error: {err}",
                confidence=0.0,
                recommended_actions=["Fallback to manual operator review."],
                sub_queries=[],
            )

    def _query_deterministic(self, question: str, context: dict[str, Any]) -> PlannerResult:
        alerts = context.get("alerts", [])
        has_critical = any(a.get("severity") == "CRITICAL" for a in alerts)
        has_warnings = any(a.get("severity") in ("WARNING", "HIGH") for a in alerts)

        if not alerts:
            return PlannerResult(
                question=question,
                answer="PCT TRACON operations are currently nominal. All monitored feeds report healthy status with no active alerts.",
                confidence=0.95,
                recommended_actions=["Continue routine system monitoring."],
                sub_queries=["Verify telemetry heartbeats across KDCA, KIAD, KBWI, and KADW."],
            )

        if has_critical:
            airports = sorted({a.get("airport") for a in alerts if a.get("airport")})
            return PlannerResult(
                question=question,
                answer=f"CRITICAL operational impact detected across airports: {', '.join(airports)}. Multiple high-severity feed anomalies active.",
                confidence=0.88,
                recommended_actions=[
                    "Escalate immediately to Duty Manager and TRACON Technical Supervisor.",
                    "Verify backup data feeds and switch feed sources if secondary links are operational.",
                    "Notify affected tower controllers of potential telemetry latency or outage.",
                ],
                sub_queries=[
                    "Check ISMC gateway transport logs for connection drops.",
                    "Inspect APDS_RVR sensor line metrics.",
                ],
            )

        if has_warnings:
            return PlannerResult(
                question=question,
                answer="Elevated risk detected. Degradation or transient anomalies present on TRACON feeds.",
                confidence=0.75,
                recommended_actions=[
                    "Monitor affected feed latency closely.",
                    "Notify system administrator if latency exceeds 30-second window.",
                ],
                sub_queries=["Query recent latency trends for degraded services."],
            )

        return PlannerResult(
            question=question,
            answer="Active low-priority events recorded. No immediate threat to TRACON operations.",
            confidence=0.60,
            recommended_actions=["Review event logs during standard shift transition."],
            sub_queries=[],
        )