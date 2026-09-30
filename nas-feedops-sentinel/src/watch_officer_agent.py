"""
Watch Officer — Advisory Operational Narrative Layer.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field, field_validator

from src import agent_tools, config, storage
from src.async_bridge import run_async
from src.config import settings
from src.feed_health import compute_feed_health
from src.llm_client import build_llm_client
from src.logging_config import get_logger
from src.metrics import LLM_CALL_LATENCY, LLM_CALLS
from src.models import NASEvent, WatchOfficerBrief
from src.prompts_loader import system_prompt, tool_schema
from src.redaction import redact_structured
from src.watch_officer_projection import project_for_model

_log = get_logger(__name__)

EVENT_LIMIT = 200


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


def rebuild_events(rows: list[dict]) -> tuple[list[NASEvent], int]:
    """Reconstruct NASEvents from storage rows; returns (events, rows_that_failed)."""
    events: list[NASEvent] = []
    failed_count = 0

    for row in rows:
        try:
            data = dict(row)
            for field_name in ("event_time_utc", "received_time_utc"):
                value = data.get(field_name)
                if isinstance(value, str):
                    data[field_name] = datetime.fromisoformat(value)

            val_errs = data.get("validation_errors")
            if isinstance(val_errs, str):
                try:
                    parsed_errs = json.loads(val_errs)
                    data["validation_errors"] = [str(x) for x in parsed_errs] if isinstance(parsed_errs, list) else []
                except Exception:
                    data["validation_errors"] = []
            elif isinstance(val_errs, list):
                data["validation_errors"] = [str(x) for x in val_errs]

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


async def _read_operating_picture(event_limit: int) -> tuple:
    return (
        await storage.get_recent_events(limit=event_limit),
        await storage.get_recent_alerts(limit=200),
        await storage.get_flight_phase_counts(window_minutes=15),
        await storage.get_active_aircraft(window_minutes=5, limit=300),
        await storage.get_latency_stats(),
        await storage.get_pending_staged_actions(),
        await storage.get_draft_operational_tickets(),
        await storage.get_recent_critic_results(limit=20),
    )


async def build_watch_officer_context(
    *,
    alert: Any | None = None,
    event_limit: int = EVENT_LIMIT,
) -> dict[str, Any]:
    # A governed read on the Watch Officer's behalf (src/agent_tools.py).
    with agent_tools.acting_as("watch_officer"):
        (events_raw, alerts_raw, phase_counts, active, latency_stats, pending, draft_tickets,
         critic_rows) = await agent_tools.invoke("read_operating_picture", _read_operating_picture, event_limit)

    live_events, failed_reconstructions = rebuild_events(events_raw)

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
        "critic_escalation_exists": False,
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

    for cr in context.get("recent_critic_results", []):
        if cr.get("escalate"):
            signals["critic_escalation_exists"] = True
            break

    latency = context.get("avg_latency_ms")
    if latency is not None and latency >= 300_000:
        signals["latency_severe"] = True

    events_in_window = context.get("events_in_window", 0)
    active_aircraft = context.get("active_aircraft", 0)
    if events_in_window > 50 and active_aircraft == 0:
        signals["position_event_mismatch"] = True

    return signals


def _derive_posture(signals: dict[str, Any], context: dict[str, Any]) -> str:
    if signals["pending_actions_exist"] or signals["draft_tickets_exist"]:
        return "ACTION"
    if (
        signals["stale_airports"]
        or signals["missing_airports"]
        or signals["degraded_airports"]
        or signals["latency_severe"]
        or signals["position_event_mismatch"]
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


def _deterministic_brief(context: dict[str, Any], signals: dict[str, Any]) -> WatchOfficerBrief:
    posture = _derive_posture(signals, context)
    trust = _derive_trust(signals, context)
    aps = _affected_airports(signals, context)

    what_changed: list[str] = []
    why_it_matters: list[str] = []
    recommended: list[str] = []
    evidence: list[str] = []

    if signals.get("critical_alerts_exist"):
        n = context.get("critical_alert_count", 0)
        what_changed.append(f"{n} CRITICAL alert(s) active in the last window.")
        why_it_matters.append("CRITICAL alerts indicate a safety- or feed-significant condition; deterministic rules already classified them as worthy of supervisor attention.")
        recommended.append("Open the Human Review Queue and triage each pending escalation package.")

    if signals.get("pending_actions_exist"):
        what_changed.append(f"{context.get('pending_actions_count', 0)} action(s) staged PENDING human review.")
        recommended.append("Decide Approve Escalation, Reject, or Continue Monitoring on each staged action.")

    if signals.get("draft_tickets_exist"):
        what_changed.append(f"{context.get('draft_tickets_count', 0)} escalation package(s) drafted but not yet decided.")
        evidence.append("operational_tickets table has DRAFT rows linked to staged actions.")

    for ap in signals.get("missing_airports", []):
        what_changed.append(f"{ap} TAIS feed is UNAVAILABLE/DISCONNECTED.")
        why_it_matters.append(f"Aircraft movement picture at {ap} cannot be trusted while the feed is missing.")
        recommended.append(f"Verify {ap} feed status against SWIM service desk and STARS console.")

    for ap in signals.get("stale_airports", []):
        what_changed.append(f"{ap} TAIS feed is STALE (no recent messages in window).")
        why_it_matters.append(f"Stale data at {ap} means the displayed picture may be out of date.")
        recommended.append(f"Confirm {ap} Jumpstart Kit is still publishing and broker is reachable.")

    for ap in signals.get("degraded_airports", []):
        what_changed.append(f"{ap} TAIS feed is DEGRADED.")
        recommended.append(f"Investigate ISMC link and message latency at {ap}.")

    if signals.get("multi_airport_degradation"):
        why_it_matters.append("Multiple PCT airports degraded simultaneously suggests infrastructure rather than facility-local issue.")

    if signals.get("latency_severe"):
        avg = (context.get("avg_latency_ms") or 0) / 60_000.0
        what_changed.append(f"Average event-to-ingest lag is {avg:.1f} min — system is processing a backlog, not live data.")
        why_it_matters.append("Backlog processing means current dashboard reflects historical state, not real-time.")

    if signals.get("position_event_mismatch"):
        what_changed.append("Recent messages received but no aircraft positions available in last 5 minutes.")
        why_it_matters.append("Could indicate TAIS parser drift or non-track message types dominating the feed.")

    if signals.get("critic_escalation_exists"):
        evidence.append("AI Critic recommends escalation on at least one recent CRITICAL alert.")

    evidence.append(
        f"events_in_window={context.get('events_in_window', 0)} · "
        f"active_aircraft={context.get('active_aircraft', 0)} · "
        f"critical_alerts={context.get('critical_alert_count', 0)} · "
        f"pending_actions={context.get('pending_actions_count', 0)}"
    )

    if posture in ("GOOD", "NORMAL") and not what_changed:
        what_changed.append("No concerning signals detected in the current window.")
        why_it_matters.append("Live PCT picture appears trustworthy across all tracked airports.")
        recommended.append("Continue passive monitoring; no human action required.")

    if posture == "UNKNOWN":
        what_changed.append("No recent events ingested.")
        why_it_matters.append("Cannot assess the aircraft movement picture without data.")
        recommended.append("Verify Jumpstart Kit is running and broker connection is healthy.")

    if posture == "ACTION":
        headline = (
            f"ACTION required — {context.get('pending_actions_count', 0)} staged action(s) "
            f"awaiting human review across PCT TRACON."
        )
    elif posture == "ATTENTION":
        headline = (
            f"ATTENTION — feed integrity concerns at {', '.join(aps) if aps else 'one or more PCT airports'}. "
            "No safety claim, data picture may be stale."
        )
    elif posture == "GOOD":
        headline = "All tracked PCT airports current; no pending human review."
    elif posture == "NORMAL":
        headline = "PCT aircraft movement picture nominal; routine monitoring."
    else:
        headline = "Insufficient data to assess the PCT picture."

    return WatchOfficerBrief(
        posture=posture,
        trust_assessment=trust,
        headline=headline,
        what_changed=what_changed,
        why_it_matters=why_it_matters,
        recommended_human_review=recommended,
        evidence=evidence,
        pending_actions_count=context.get("pending_actions_count", 0),
        critical_alert_count=context.get("critical_alert_count", 0),
        affected_airports=aps,
        confidence=0.6 if posture != "UNKNOWN" else 0.2,
        llm_used=False,
        limitations=[
            "Deterministic rule-based brief; no LLM nuance.",
            "Data-feed degradation does NOT imply unsafe airspace.",
        ],
    )


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

        evidence: list[str] = []
        if signals.get("critical_alerts_exist"):
            evidence.append(f"{context.get('critical_alert_count', 0)} critical alerts active")
        if signals.get("pending_actions_exist"):
            evidence.append(f"{context.get('pending_actions_count', 0)} pending staged actions")
        if signals.get("draft_tickets_exist"):
            evidence.append("operational_tickets table has DRAFT rows linked to staged actions.")
        if signals.get("critic_escalation_exists"):
            evidence.append("AI Critic recommends escalation on at least one recent CRITICAL alert.")

        evidence.append(
            f"events_in_window={context.get('events_in_window', 0)} · "
            f"active_aircraft={context.get('active_aircraft', 0)} · "
            f"critical_alerts={context.get('critical_alert_count', 0)} · "
            f"pending_actions={context.get('pending_actions_count', 0)}"
        )

        return WatchOfficerBrief(
            posture=posture,
            trust_assessment=trust,
            headline=raw_narrative.headline,
            what_changed=raw_narrative.what_changed,
            why_it_matters=raw_narrative.why_it_matters,
            recommended_human_review=raw_narrative.recommended_human_review,
            evidence=evidence,
            pending_actions_count=context.get("pending_actions_count", 0),
            critical_alert_count=context.get("critical_alert_count", 0),
            affected_airports=affected,
            confidence=raw_narrative.confidence,
            llm_used=llm_used,
            limitations=list(raw_narrative.limitations),
        )

async def _llm_narrative(context: dict[str, Any], signals: dict[str, Any]) -> RawLLMNarrative:
    """
    One structured completion through the shared LiteLLM client -- the same
    redacted, error-classified path the Critic and Planner use. Raises on any
    failure; the caller falls back to the deterministic brief.
    """
    client = build_llm_client(model=settings.WATCH_OFFICER_MODEL)
    if client is None:
        raise RuntimeError("no LLM client configured")

    # A projection built for the four questions the brief answers, bounded by
    # construction (src/watch_officer_projection.py), not the raw snapshot.
    payload = redact_structured(project_for_model(context, signals))
    raw = await client.acall_with_tool(
        system_prompt=system_prompt("watch_officer"),
        tool=tool_schema("watch_officer"),
        user_message=(
            "Write the operational watch summary for this operating picture:\n"
            + json.dumps(payload, default=str)
        ),
        # 900 cut Claude Haiku's narrative off mid-object (finish_reason=length);
        # its briefs run about 1,200 tokens. The model-use policy's ceiling is 2,000.
        max_tokens=1800,
        role="watch_officer",
    )
    return RawLLMNarrative.model_validate(raw)


async def agenerate_watch_officer_brief(
    context: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> WatchOfficerBrief:
    """
    Build the Watch Officer brief. The model writes only the narrative;
    posture, trust and counts come from OutputGuardrail.guard(). Any LLM
    failure -- unconfigured, unreachable, malformed output -- returns the
    deterministic brief, which says it is rule-based in its limitations.
    """
    if context is None:
        context = await build_watch_officer_context()
    signals = _detect_signals(context)

    if use_llm and settings.CRITIC_ENABLED:
        start = time.perf_counter()
        try:
            narrative = await _llm_narrative(context, signals)
            LLM_CALLS.labels(agent="watch_officer", outcome="success").inc()
            return OutputGuardrail.guard(narrative, context, signals, llm_used=True)
        except Exception as exc:  # noqa: BLE001 -- advisory layer; fall back
            LLM_CALLS.labels(agent="watch_officer", outcome="error").inc()
            _log.warning("watch_officer_llm_failed", extra={"error": str(exc)})
        finally:
            LLM_CALL_LATENCY.labels(agent="watch_officer").observe(time.perf_counter() - start)

    return _deterministic_brief(context, signals)


def generate_watch_officer_brief(
    context: dict[str, Any] | None = None,
    use_llm: bool = True,
) -> WatchOfficerBrief:
    """
    Synchronous entry point for callers with no running event loop (the
    Streamlit dashboard). The deterministic path needs no loop at all, so it
    is served directly; anything that touches storage or the model runs on the
    shared bridge loop.
    """
    if not use_llm and context is not None:
        return _deterministic_brief(context, _detect_signals(context))
    return run_async(agenerate_watch_officer_brief(context, use_llm))
