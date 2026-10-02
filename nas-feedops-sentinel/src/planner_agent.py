"""
Planner Agent (Layer 2) — Operational Advisory & Query Synthesis.
Runs through the unified LiteLLM client.
"""
from __future__ import annotations

import json
from typing import Any

from src import agent_tools, storage, surface_tools
from src.config import settings
from src.guardrails import InputGuardrail, OutputGuardrail
from src.llm_client import build_llm_client
from src.logging_config import get_logger
from src.models import PlannerResult
from src.prompts_loader import prompt_version, system_prompt, tool_schema
from src.redaction import Pseudonymizer, collect_sensitive_values, pseudonymize_structured
from src.untrusted import fence, fence_fields

_log = get_logger(__name__)


def _airport_list() -> str:
    """The supported airports as prose: 'KDCA', 'KDCA and KIAD', 'KDCA, KIAD, and KBWI'."""
    codes = sorted(settings.SUPPORTED_AIRPORTS)
    if len(codes) < 3:
        return " and ".join(codes)
    return ", ".join(codes[:-1]) + ", and " + codes[-1]


async def _read_recent_activity() -> tuple[list[dict], list[dict], list[dict]]:
    return (
        await storage.get_recent_events(limit=20),
        await storage.get_recent_alerts(limit=10),
        await storage.get_recent_critic_results(limit=5),
    )


_DETERMINISTIC_PREFIX = "LLM planner unavailable; deterministic summary of stored records. "


def _with_records(answer: str, surface: dict[str, Any]) -> str:
    """
    A model's answer, then the surface figures that name flights. Flight identifiers are redacted
    before a prompt leaves this system, so the model can give the numbers but not say which flight;
    the records can, and are appended here, locally.
    """
    if not surface_tools.has_figures(surface):
        return answer
    return (answer + "\n\nFrom the records (flight identifiers are withheld from the model and listed here): "
            + " ".join(surface_tools.describe(surface)))


class PlannerAgent:
    """Standalone Planner execution engine using LiteLLM and guardrail validation."""

    def __init__(self) -> None:
        may_ask = settings.CRITIC_ENABLED and settings.PLANNER_ENABLED      # CRITIC_ENABLED=0 stops every live agent
        self._llm = build_llm_client(model=settings.PLANNER_MODEL) if may_ask else None
        self._guardrail = OutputGuardrail()
        self._input_guard = InputGuardrail()
        self._prompt = system_prompt("planner")
        self._tool = tool_schema("planner")

    async def query(self, question: str) -> PlannerResult:
        """Processes an operator question against current persisted NAS state asynchronously."""
        with agent_tools.acting_as("planner"):
            return await self._query(question)

    async def _query(self, question: str) -> PlannerResult:
        # Before anything is read or sent: a question that tries to override the instructions, reaches for
        # credentials or commands, or is longer than the dashboard allows is refused here, unanswered.
        if len(question) > settings.PLANNER_QUESTION_MAX_CHARS:
            return self._refused(question, f"The question is longer than {settings.PLANNER_QUESTION_MAX_CHARS} characters.")
        checked = self._input_guard.verify(question, require_nas_scope=False)
        if not checked.is_compliant:
            _log.warning("planner_question_refused", extra={"violation_type": checked.violation_type})
            return self._refused(question, checked.detail)
        try:
            recent_events, recent_alerts, critic_reviews = await agent_tools.invoke(
                "read_recent_activity", _read_recent_activity)
        except agent_tools.ToolCallDenied as denied:
            return PlannerResult(
                question=question,
                answer=f"The planner could not read the current picture: {denied}",
                confidence=None,
                recommended_actions=["Review alerts and feed health on the dashboard directly."],
            )

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

        # Taxi-time questions get their numbers from the surface tools (src/surface_tools.py),
        # each a governed read; a question about anything else adds nothing here.
        surface = await surface_tools.gather(question)
        if surface:
            context_summary["surface_metrics"] = surface

        # PlannerResult.confidence is None when no model made an assessment
        # (see src/models.py), and the rest of this method keeps to that:
        # a number in that field always means a model produced it.
        if not self._llm:
            return self._query_deterministic(question, context_summary)

        pseudonyms = Pseudonymizer()
        try:
            parsed = await self._call_llm(question, context_summary, pseudonyms)
        except Exception as err:  # noqa: BLE001 -- any failure degrades, never raises to the operator
            return PlannerResult(
                question=question,
                answer=(
                    f"LLM planner unavailable ({err}). No model assessment was made; "
                    f"review alerts and feed health on the dashboard directly."
                ),
                confidence=None,
                recommended_actions=["Fallback to manual operator review."],
                sub_queries=[],
            )

        # Guard what the model actually returned, before any defaults are
        # applied -- otherwise a response that omitted `confidence` would be
        # filled in with a value and sail through the missing-field check.
        # Deterministic output is not routed through here: this guardrail
        # exists to catch malformed or out-of-bounds model output, and code
        # this module writes itself has neither failure mode.
        guard_res = self._guardrail.verify_planner_output(parsed)
        if not guard_res.is_compliant:
            return PlannerResult(
                question=question,
                answer=f"Guardrail intercepted response: {guard_res.detail}",
                confidence=None,
                recommended_actions=["Manual duty manager review required due to output compliance failure."],
                sub_queries=["Check system logs for output guardrail violations."],
            )

        return PlannerResult(
            question=question,
            # The model wrote about AC-1 and AC-2; the operator reads this on this system, so the tokens go back
            # to the flights they stood for. (Nothing here is sent anywhere.)
            answer=_with_records(pseudonyms.reveal(str(parsed["answer"])), surface),
            confidence=float(parsed["confidence"]),
            citations=list(parsed.get("citations") or []),
            recommended_actions=[pseudonyms.reveal(str(a)) for a in parsed["recommended_actions"]],
            sub_queries=[pseudonyms.reveal(str(q)) for q in parsed["sub_queries"]],
        )

    async def _call_llm(
        self, question: str, context: dict[str, Any], pseudonyms: Pseudonymizer | None = None,
    ) -> dict[str, Any]:
        """
        Return the model's structured output, unmodified. Raises on any LLM failure.

        A flight in the context goes to the model as a token (AC-1), so it can say which flight took longest
        without being told which. `pseudonyms` is the caller's, so the same tokens can be put back in the answer.
        """
        pseudonyms = pseudonyms or Pseudonymizer()
        # The question is typed by a person and the critic reviews in the context were written by a model
        # reading feed text: both are data (src/untrusted.py).
        user_content = (
            f"Question: {fence(question, 'question')}\n\n"
            f"Current System Context:\n{json.dumps(fence_fields(pseudonymize_structured(context, pseudonyms)), indent=2, default=str)}"
        )

        # Route call through the LiteLLM client proxy, forced to structured
        # output via the planner tool schema (matches CriticAgent's pattern).
        return await self._llm.acall_with_tool(
            system_prompt=self._prompt,
            tool=self._tool,
            user_message=user_content,
            max_tokens=1000,
            temperature=0.1,
            role="planner",
            prompt_version=prompt_version("planner"),
            protected_values=collect_sensitive_values(context),
        )

    @staticmethod
    def _refused(question: str, detail: str) -> PlannerResult:
        return PlannerResult(
            question=question[:200],
            answer=f"The planner did not answer this question. {detail}",
            confidence=None,
            recommended_actions=["Ask about operational status, feed health, alerts or RVR conditions."],
        )

    def _query_deterministic(self, question: str, context: dict[str, Any]) -> PlannerResult:
        """
        No model: the alert summary, and for a taxi-time question the surface figures ahead of it.

        The figures are facts the records show, so they lead. The alert summary follows, so an
        active incident never disappears behind a taxi answer.
        """
        summary = self._alert_summary(question, context)
        surface = context.get("surface_metrics")
        if not surface:
            return summary
        lines = " ".join(surface_tools.describe(surface))
        if not surface_tools.has_figures(surface):                 # a note that nothing was fetched, or a refusal
            return summary.model_copy(update={"answer": f"{summary.answer} {lines}"})
        actions = (["Confirm the SWIM surface feed is current before relying on these figures."]
                   if surface_tools.is_stale(surface) else [])
        return summary.model_copy(update={
            "answer": _DETERMINISTIC_PREFIX + lines + " Alerts: " + summary.answer.removeprefix(_DETERMINISTIC_PREFIX),
            "recommended_actions": actions + [a for a in summary.recommended_actions if a not in actions],
        })

    def _alert_summary(self, question: str, context: dict[str, Any]) -> PlannerResult:
        """
        Rule-based summary for when no LLM is configured.

        This is a deterministic control, not a model, so it states only what
        the persisted records directly show and carries no confidence score.
        It used to report fixed confidences (0.95 / 0.88 / 0.75 / 0.60) in the
        same field a model's confidence goes into, and with no alerts on
        record it told the operator "All monitored feeds report healthy
        status" -- including when nothing had been ingested at all. Absence of
        alerts is not evidence of feed health; the Watch Officer reports
        UNKNOWN in that situation, and this now agrees with it.
        """
        alerts = context.get("alerts", [])
        event_count = context.get("recent_events_count", 0)
        has_critical = any(a.get("severity") == "CRITICAL" for a in alerts)
        has_warnings = any(a.get("severity") in ("WARNING", "HIGH") for a in alerts)
        prefix = _DETERMINISTIC_PREFIX

        if not alerts:
            if event_count == 0:
                answer = (
                    prefix + "No NAS events are on record, so feed health cannot be "
                    "confirmed. Absence of alerts here reflects absence of data."
                )
                actions = ["Confirm the SWIM feeds are connected and ingesting before relying on this view."]
            else:
                answer = (
                    prefix + f"No active alerts. The {event_count} most recent NAS events on "
                    "record raised none; this does not by itself confirm every feed is current."
                )
                actions = ["Continue routine system monitoring."]
            return PlannerResult(
                question=question,
                answer=answer,
                confidence=None,
                recommended_actions=actions,
                sub_queries=[f"Verify telemetry heartbeats across {_airport_list()}."],
            )

        if has_critical:
            airports = sorted({a.get("airport") for a in alerts if a.get("airport")})
            return PlannerResult(
                question=question,
                answer=prefix + f"CRITICAL alerts are active at: {', '.join(airports)}.",
                confidence=None,
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
                answer=prefix + "WARNING-level alerts are active on TRACON feeds; no CRITICAL alerts.",
                confidence=None,
                recommended_actions=[
                    "Monitor affected feed latency closely.",
                    "Notify system administrator if latency exceeds 30-second window.",
                ],
                sub_queries=["Query recent latency trends for degraded services."],
            )

        return PlannerResult(
            question=question,
            answer=prefix + "Only low-priority alerts are active; none are WARNING or CRITICAL.",
            confidence=None,
            recommended_actions=["Review event logs during standard shift transition."],
            sub_queries=[],
        )
