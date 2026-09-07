"""
Watch Officer — Advisory Operational Narrative Layer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from crewai import Agent, Crew, Task
from pydantic import BaseModel, Field, field_validator

from src import config, storage
from src.async_bridge import run_async
from src.feed_health import compute_feed_health
from src.llm_config import get_configured_llm
from src.logging_config import get_logger
from src.models import NASEvent, WatchOfficerBrief

_log = get_logger(__name__)

EVENT_LIMIT = 200


def get_watch_officer_agent(llm: Any = None, tools: list | None = None) -> Agent:
    return Agent(
        role="Watch Officer",
        goal="Synthesize complex feed telemetry into executive operational briefs for duty management.",
        backstory=(
            "You are the PCT TRACON Watch Officer. You summarize operational state, feed health, "
            "and active alerts into concise briefs. You strictly adhere to control-plane truth and never "
            "approve or stage operational actions directly."
        ),
        tools=tools or [],
        verbose=True,
        allow_delegation=False,
        llm=llm or get_configured_llm(),
    )


class RawLLMNarrative(BaseModel):
    headline: str
    what_changed: list[str] = Field(default_factory=list)
    why_it_matters: list[str] = Field(default_factory=list)
    recommended_human_review: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("headline", mode="before")
    @classmethod
    def normalize_headline(cls, value: Any) -> str:
        if value is None:
            return "Operational watch summary unavailable."
        return str(value).strip()

    @field_validator(
        "what_changed",
        "why_it_matters",
        "recommended_human_review",
        "limitations",
        mode="before",
    )
    @classmethod
    def normalize_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]


def _rebuild_events(rows: list[dict]) -> tuple[list[NASEvent], int]:
    events: list[NASEvent] = []
    failed_count = 0

    for row in rows:
        try:
            data = dict(row)
            for field_name in ("event_time_utc", "received_time_utc"):
                value = data.get(field_name)
                if isinstance(value, str):
                    data[field_name] = datetime.fromisoformat(value)

            if isinstance(data.get("validation_errors"), str):
                data["validation_errors"] = json.loads(data["validation_errors"])

            events.append(NASEvent(**data))

        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            _log.warning(
                "event_reconstruction_failed",
                extra={
                    "error": str(exc),
                    "row_id": row.get("id"),
                    "event_id": row.get("event_id"),
                },
            )

    return events, failed_count


async def build_watch_officer_context(
    *,
    alert: Any | None = None,
    event_limit: int = EVENT_LIMIT,
) -> dict[str, Any]:
    events_raw = await storage.get_recent_events(limit=event_limit)
    alerts_raw = await storage.get_recent_alerts(limit=200)
    phase_counts = await storage.get_flight_phase_counts(window_minutes=15)
    active = await storage.get_active_aircraft(window_minutes=5, limit=300)
    latency_stats = await storage.get_latency_stats()
    pending = await storage.get_pending_staged_actions()
    draft_tickets = await storage.get_draft_operational_tickets()
    critic_rows = await storage.get_recent_critic_results(limit=20)

    live_events, failed_reconstructions = _rebuild_events(events_raw)

    health = (
        compute_feed_health(live_events)
        if live_events
        else {
            "airport_status": {},
            "service_status": {},
            "latency_stats": {},
            "smes_counts": {},
        }
    )

    airport_status = health.get("airport_status", {})
    per_airport: dict[str, dict[str, Any]] = {}

    for airport in config.SUPPORTED_AIRPORTS:
        per_airport[airport] = {
            "status": airport_status.get(airport, "NO DATA"),
            "phase_counts": phase_counts.get(airport, {}),
            "active_aircraft": sum(
                1 for aircraft in active if aircraft.get("airport_code") == airport
            ),
        }

    critical_alerts = [a for a in alerts_raw if a.get("severity") == "CRITICAL"]
    provenance_ids = [
        event.event_id for event in live_events if getattr(event, "event_id", None)
    ][:10]

    context: dict[str, Any] = {
        "snapshot_time": datetime.now(timezone.utc).isoformat(),
        "per_airport": per_airport,
        "airport_status": airport_status,
        "phase_counts": phase_counts,
        "active_aircraft": len(active),
        "events_in_window": len(events_raw),
        "analyzed_event_count": len(live_events),
        "failed_reconstruction_count": failed_reconstructions,
        "provenance_ids": provenance_ids,
        "avg_latency_ms": latency_stats.get("avg_ms"),
        "alerts": alerts_raw[:50],
        "critical_alerts": critical_alerts,
        "critical_alert_count": len(critical_alerts),
        "pending_actions": [
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
            for item in pending
        ],
        "pending_actions_count": len(pending),
        "draft_tickets": [
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
            for item in draft_tickets
        ],
        "draft_tickets_count": len(draft_tickets),
        "recent_critic_results": [dict(row) for row in critic_rows[:10]],
    }

    if alert is not None:
        if hasattr(alert, "model_dump"):
            context["triggering_alert"] = alert.model_dump()
        elif isinstance(alert, dict):
            context["triggering_alert"] = dict(alert)

    return context


def _detect_signals(context: dict[str, Any]) -> dict[str, Any]:
    signals: dict[str, Any] = {
        "stale_airports": [],
        "missing_airports": [],
        "degraded_airports": [],
        "multi_airport_degradation": False,
        "pending_actions_exist": context.get("pending_actions_count", 0) > 0,
        "draft_tickets_exist": context.get("draft_tickets_count", 0) > 0,
        "critical_alerts_exist": context.get("critical_alert_count", 0) > 0,
        "no_active_aircraft": context.get("active_aircraft", 0) == 0,
        "no_recent_events": context.get("events_in_window", 0) == 0,
        "latency_severe": False,
        "position_event_mismatch": False,
    }

    airport_status = context.get("airport_status", {})
    for airport, status in airport_status.items():
        if status == "STALE":
            signals["stale_airports"].append(airport)
        elif status in ("UNAVAILABLE", "DISCONNECTED"):
            signals["missing_airports"].append(airport)
        elif status == "DEGRADED":
            signals["degraded_airports"].append(airport)

    degraded_set = (
        set(signals["stale_airports"])
        | set(signals["missing_airports"])
        | set(signals["degraded_airports"])
    )
    signals["multi_airport_degradation"] = len(degraded_set) >= 2

    latency = context.get("avg_latency_ms")
    if latency is not None and latency >= 300_000:
        signals["latency_severe"] = True

    return signals


def _derive_posture(signals: dict[str, Any], context: dict[str, Any]) -> str:
    if signals["pending_actions_exist"] or signals["draft_tickets_exist"]:
        return "ACTION"
    if (
        signals["stale_airports"]
        or signals["missing_airports"]
        or signals["degraded_airports"]
        or signals["latency_severe"]
    ):
        return "ATTENTION"
    if context.get("active_aircraft", 0) > 0 or context.get("events_in_window", 0) > 50:
        return "NORMAL"
    if context.get("events_in_window", 0) > 0:
        return "GOOD"
    return "UNKNOWN"


def _derive_trust(signals: dict[str, Any], context: dict[str, Any]) -> str:
    if signals["no_recent_events"]:
        return "UNKNOWN"
    if signals["missing_airports"] or signals["no_active_aircraft"]:
        return "INCOMPLETE"
    if signals["stale_airports"] or signals["degraded_airports"] or signals["latency_severe"]:
        return "DEGRADED"
    if context.get("events_in_window", 0) > 0:
        return "TRUSTWORTHY"
    return "UNKNOWN"


def _affected_airports(signals: dict[str, Any], context: dict[str, Any]) -> list[str]:
    airports = (
        set(signals["stale_airports"])
        | set(signals["missing_airports"])
        | set(signals["degraded_airports"])
    )
    for alert in context.get("critical_alerts", []):
        if alert.get("airport_code"):
            airports.add(alert["airport_code"])
    return sorted(airports)


class OutputGuardrail:
    @staticmethod
    def guard(
        raw_narrative: RawLLMNarrative,
        context: dict[str, Any],
        signals: dict[str, Any],
        *,
        llm_used: bool,
    ) -> WatchOfficerBrief:
        posture = _derive_posture(signals, context)
        trust = _derive_trust(signals, context)
        affected = _affected_airports(signals, context)

        return WatchOfficerBrief(
            posture=posture,
            trust_assessment=trust,
            headline=raw_narrative.headline,
            what_changed=raw_narrative.what_changed,
            why_it_matters=raw_narrative.why_it_matters,
            recommended_human_review=raw_narrative.recommended_human_review,
            pending_actions_count=context.get("pending_actions_count", 0),
            critical_alert_count=context.get("critical_alert_count", 0),
            affected_airports=affected,
            confidence=raw_narrative.confidence,
            llm_used=llm_used,
            limitations=list(raw_narrative.limitations),
        )


def generate_watch_officer_brief(
    context: dict[str, Any] | None = None,
    use_llm: bool = True,
    mcp_tools: list | None = None,
) -> WatchOfficerBrief:
    if context is None:
        # build_watch_officer_context is async; run it on the same shared,
        # persistent event loop the rest of the app uses (see
        # src/async_bridge.py) rather than asyncio.run(), which would spin
        # up a throwaway loop that clashes with storage.py's cached asyncpg
        # pool the next time anything calls get_db_pool().
        context = run_async(build_watch_officer_context())

    signals = _detect_signals(context)

    if use_llm:
        try:
            agent = get_watch_officer_agent(llm=get_configured_llm(), tools=mcp_tools)
            
            task = Task(
                description=f"Synthesize operational watch summary for context: {json.dumps({'context': context, 'signals': signals}, default=str)}",
                expected_output="A valid JSON object adhering to the RawLLMNarrative schema.",
                agent=agent,
                output_json=RawLLMNarrative,
            )
            
            crew = Crew(agents=[agent], tasks=[task], verbose=True)
            result = crew.kickoff()
            
            narrative = result.pydantic if hasattr(result, "pydantic") and result.pydantic else RawLLMNarrative.model_validate_json(result.raw)
            return OutputGuardrail.guard(narrative, context, signals, llm_used=True)
            
        except Exception as exc:  # noqa: BLE001
            _log.warning("watch_officer_crew_execution_failed", extra={"error": str(exc)})

    raw = RawLLMNarrative(
        headline=f"Operational Posture: {_derive_posture(signals, context)}.",
        confidence=0.5,
        limitations=["Deterministic fallback brief generated; no LLM narrative used."],
    )
    return OutputGuardrail.guard(raw, context, signals, llm_used=False)