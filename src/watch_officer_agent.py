"""
PCT Watch Officer Agent.

Produces structured operational briefs from current system state.

Architecture principle:
    The LLM EXPLAINS evidence. It does NOT become the control plane.
    The model drafts brief language; only humans approve or reject anything.

Pipeline:
    build_watch_officer_context()  → snapshot of current state from SQLite
        ↓
    _detect_signals(context)       → deterministic operational signals
        ↓
    generate_watch_officer_brief() → strict-JSON brief, LLM-or-fallback
        ↓
    save_watch_officer_brief()     → persisted in SQLite
        ↓
    OperationalTicket carries the brief into the Human Review Queue
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from src import config, storage
from src.feed_health import compute_feed_health
from src.llm_client import build_llm_client
from src.logging_config import get_logger
from src.models import NASEvent, WatchOfficerBrief


_log = get_logger(__name__)


# ── Context builder ───────────────────────────────────────────────────────────

def _rebuild_events(rows: list[dict]) -> list[NASEvent]:
    """Reconstitute NASEvent objects from DB rows for feed_health computation."""
    out: list[NASEvent] = []
    for r in rows:
        try:
            rr = dict(r)
            rr["event_time_utc"]    = datetime.fromisoformat(rr["event_time_utc"])
            val_errs = rr.get("validation_errors")
            if isinstance(val_errs, str):
                try:
                    parsed_errs = json.loads(val_errs)
                    rr["validation_errors"] = [str(x) for x in parsed_errs] if isinstance(parsed_errs, list) else []
                except Exception:
                    rr["validation_errors"] = []
            elif isinstance(val_errs, list):
                rr["validation_errors"] = [str(x) for x in val_errs]
            out.append(NASEvent(**rr))
        except Exception:
            pass
    return out


def build_watch_officer_context() -> dict[str, Any]:
    """
    Pull current system state from storage and assemble the structured
    snapshot the Watch Officer reasons over. All values come from SQLite.
    """
    events_raw   = storage.get_recent_events(limit=500)
    alerts_raw   = storage.get_recent_alerts(limit=200)
    rvr_rows     = storage.get_latest_rvr()
    phase_counts = storage.get_flight_phase_counts(window_minutes=15)
    active       = storage.get_active_aircraft(window_minutes=5, limit=300)
    lat_stats    = storage.get_latency_stats()
    pending      = storage.get_pending_staged_actions()
    draft_tix    = storage.get_draft_operational_tickets()
    critic_rows  = storage.get_recent_critic_results(limit=20)

    live_events = _rebuild_events(events_raw[:200])
    health = compute_feed_health(live_events) if live_events else {
        "airport_status": {}, "service_status": {}, "latency_stats": {}, "smes_counts": {},
    }
    airport_status = health.get("airport_status", {})

    per_airport: dict[str, dict[str, Any]] = {}
    for ap in config.SUPPORTED_AIRPORTS:
        per_airport[ap] = {
            "status":          airport_status.get(ap, "NO DATA"),
            "phase_counts":    phase_counts.get(ap, {}),
            "active_aircraft": sum(1 for a in active if a.get("airport_code") == ap),
        }

    critical_alerts = [a for a in alerts_raw if a.get("severity") == "CRITICAL"]

    return {
        "snapshot_time":       datetime.now(timezone.utc).isoformat(),
        "per_airport":         per_airport,
        "airport_status":      airport_status,
        "phase_counts":        phase_counts,
        "active_aircraft":     len(active),
        "events_in_window":    len(events_raw),
        "avg_latency_ms":      lat_stats.get("avg_ms"),
        "alerts":              alerts_raw[:50],
        "critical_alerts":     critical_alerts,
        "critical_alert_count": len(critical_alerts),
        "rvr_rows":            rvr_rows,
        "pending_actions":     [a.model_dump() if hasattr(a, "model_dump") else dict(a) for a in pending],
        "pending_actions_count": len(pending),
        "draft_tickets":       [t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in draft_tix],
        "draft_tickets_count": len(draft_tix),
        "recent_critic_results": [dict(r) for r in critic_rows[:10]],
    }


# ── Deterministic signal detection ────────────────────────────────────────────

def _detect_signals(context: dict) -> dict[str, Any]:
    """
    Run rule-based detectors over the context. These are the EVIDENCE primitives
    the Watch Officer brief (LLM or fallback) reasons over.
    """
    signals: dict[str, Any] = {
        "stale_airports": [],
        "missing_airports": [],
        "degraded_airports": [],
        "multi_airport_degradation": False,
        "pending_actions_exist": context.get("pending_actions_count", 0) > 0,
        "draft_tickets_exist":   context.get("draft_tickets_count", 0) > 0,
        "critical_alerts_exist": context.get("critical_alert_count", 0) > 0,
        "critic_escalation_exists": False,
        "no_active_aircraft":    context.get("active_aircraft", 0) == 0,
        "no_recent_events":      context.get("events_in_window", 0) == 0,
        "latency_severe":        False,
        "position_event_mismatch": False,
    }

    airport_status = context.get("airport_status", {})
    for ap, st in airport_status.items():
        if st in ("STALE",):
            signals["stale_airports"].append(ap)
        elif st in ("UNAVAILABLE", "DISCONNECTED"):
            signals["missing_airports"].append(ap)
        elif st == "DEGRADED":
            signals["degraded_airports"].append(ap)

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

    lat = context.get("avg_latency_ms")
    if lat is not None and lat >= 300_000:
        signals["latency_severe"] = True

    events_in_window = context.get("events_in_window", 0)
    active_aircraft  = context.get("active_aircraft", 0)
    if events_in_window > 50 and active_aircraft == 0:
        signals["position_event_mismatch"] = True

    return signals


# ── Posture / trust derivation ────────────────────────────────────────────────

def _derive_posture(signals: dict, context: dict) -> str:
    if (signals["pending_actions_exist"] or signals["draft_tickets_exist"]
            or signals["critical_alerts_exist"]):
        return "ACTION"
    if (signals["stale_airports"] or signals["missing_airports"]
            or signals["degraded_airports"] or signals["latency_severe"]
            or signals["position_event_mismatch"]):
        return "ATTENTION"
    if context.get("active_aircraft", 0) > 0 or context.get("events_in_window", 0) > 50:
        return "NORMAL"
    if context.get("events_in_window", 0) > 0:
        return "GOOD"
    return "UNKNOWN"


def _derive_trust(signals: dict, context: dict) -> str:
    if signals["no_recent_events"]:
        return "UNKNOWN"
    if signals["missing_airports"] or signals["no_active_aircraft"]:
        return "INCOMPLETE"
    if signals["stale_airports"] or signals["degraded_airports"]:
        return "DEGRADED"
    if context.get("events_in_window", 0) > 0 and not (
        signals["stale_airports"] or signals["missing_airports"] or signals["degraded_airports"]
    ):
        return "TRUSTWORTHY"
    return "UNKNOWN"


# ── Deterministic fallback brief ──────────────────────────────────────────────

def _affected_airports(signals: dict, context: dict) -> list[str]:
    aps = set(signals["stale_airports"]) | set(signals["missing_airports"]) | set(signals["degraded_airports"])
    for a in context.get("critical_alerts", []):
        if a.get("airport_code"):
            aps.add(a["airport_code"])
    return sorted(aps)


def _deterministic_brief(context: dict, signals: dict) -> WatchOfficerBrief:
    posture = _derive_posture(signals, context)
    trust = _derive_trust(signals, context)
    aps = _affected_airports(signals, context)

    what_changed: list[str] = []
    why_it_matters: list[str] = []
    recommended: list[str] = []
    evidence: list[str] = []

    if signals["critical_alerts_exist"]:
        n = context["critical_alert_count"]
        what_changed.append(f"{n} CRITICAL alert(s) active in the last window.")
        why_it_matters.append("CRITICAL alerts indicate a safety- or feed-significant condition; deterministic rules already classified them as worthy of supervisor attention.")
        recommended.append("Open the Human Review Queue and triage each pending escalation package.")

    if signals["pending_actions_exist"]:
        what_changed.append(f"{context['pending_actions_count']} action(s) staged PENDING human review.")
        recommended.append("Decide Approve Escalation, Reject, or Continue Monitoring on each staged action.")

    if signals["draft_tickets_exist"]:
        what_changed.append(f"{context['draft_tickets_count']} escalation package(s) drafted but not yet decided.")
        evidence.append("operational_tickets table has DRAFT rows linked to staged actions.")

    for ap in signals["missing_airports"]:
        what_changed.append(f"{ap} TAIS feed is UNAVAILABLE/DISCONNECTED.")
        why_it_matters.append(f"Aircraft movement picture at {ap} cannot be trusted while the feed is missing.")
        recommended.append(f"Verify {ap} feed status against SWIM service desk and STARS console.")

    for ap in signals["stale_airports"]:
        what_changed.append(f"{ap} TAIS feed is STALE (no recent messages in window).")
        why_it_matters.append(f"Stale data at {ap} means the displayed picture may be out of date.")
        recommended.append(f"Confirm {ap} Jumpstart Kit is still publishing and broker is reachable.")

    for ap in signals["degraded_airports"]:
        what_changed.append(f"{ap} TAIS feed is DEGRADED.")
        recommended.append(f"Investigate ISMC link and message latency at {ap}.")

    if signals["multi_airport_degradation"]:
        why_it_matters.append("Multiple PCT airports degraded simultaneously suggests infrastructure rather than facility-local issue.")

    if signals["latency_severe"]:
        avg = context.get("avg_latency_ms", 0) / 60_000.0
        what_changed.append(f"Average event-to-ingest lag is {avg:.1f} min — system is processing a backlog, not live data.")
        why_it_matters.append("Backlog processing means current dashboard reflects historical state, not real-time.")

    if signals["position_event_mismatch"]:
        what_changed.append("Recent messages received but no aircraft positions available in last 5 minutes.")
        why_it_matters.append("Could indicate TAIS parser drift or non-track message types dominating the feed.")

    if signals["critic_escalation_exists"]:
        evidence.append("AI Critic recommends escalation on at least one recent CRITICAL alert.")

    # Evidence: numerical snapshot
    evidence.append(
        f"events_in_window={context.get('events_in_window', 0)} · "
        f"active_aircraft={context.get('active_aircraft', 0)} · "
        f"critical_alerts={context.get('critical_alert_count', 0)} · "
        f"pending_actions={context.get('pending_actions_count', 0)}"
    )

    # Healthy default narrative
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


# ── LLM-assisted brief ────────────────────────────────────────────────────────

_WATCH_OFFICER_SYSTEM_PROMPT = """\
You are the PCT Watch Officer Agent for an FAA SWIM/STDDS TAIS operational \
intelligence dashboard. Your job is to produce a concise operational brief \
from structured system evidence. You are not an air traffic controller and \
you do not issue operational commands. You explain data freshness, aircraft \
movement picture trust, alert posture, and what a human operator should \
review next. Do not invent facts. Use only the supplied context. Distinguish \
between data-feed degradation and real-world aircraft safety conditions. \
Say "the data picture is incomplete" rather than implying unsafe airspace \
unless explicitly supported by evidence. Recommend human review, not \
autonomous execution. Return strict JSON only."""


_WATCH_OFFICER_TOOL = {
    "name": "submit_watch_officer_brief",
    "description": "Submit the structured PCT Watch Officer brief.",
    "input_schema": {
        "type": "object",
        "properties": {
            "posture": {
                "type": "string",
                "enum": ["GOOD", "NORMAL", "ATTENTION", "ACTION", "UNKNOWN"],
            },
            "trust_assessment": {
                "type": "string",
                "enum": ["TRUSTWORTHY", "DEGRADED", "INCOMPLETE", "UNKNOWN"],
            },
            "headline": {"type": "string"},
            "what_changed":             {"type": "array", "items": {"type": "string"}},
            "why_it_matters":           {"type": "array", "items": {"type": "string"}},
            "recommended_human_review": {"type": "array", "items": {"type": "string"}},
            "evidence":                 {"type": "array", "items": {"type": "string"}},
            "affected_airports":        {"type": "array", "items": {"type": "string"}},
            "confidence":               {"type": "number"},
            "limitations":              {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "posture", "trust_assessment", "headline", "what_changed",
            "why_it_matters", "recommended_human_review", "evidence",
            "affected_airports", "confidence",
        ],
    },
}


def _format_user_message(context: dict, signals: dict) -> str:
    return (
        "PCT TRACON Watch Officer context snapshot (real SQLite state):\n"
        "============================================================\n"
        f"snapshot_time       : {context.get('snapshot_time')}\n"
        f"events_in_window    : {context.get('events_in_window')}\n"
        f"active_aircraft     : {context.get('active_aircraft')}\n"
        f"avg_latency_ms      : {context.get('avg_latency_ms')}\n"
        f"critical_alert_count: {context.get('critical_alert_count')}\n"
        f"pending_actions     : {context.get('pending_actions_count')}\n"
        f"draft_tickets       : {context.get('draft_tickets_count')}\n"
        "\nper_airport: " + json.dumps(context.get("per_airport", {}), default=str) + "\n"
        "\ndeterministic_signals: " + json.dumps(signals, default=str) + "\n"
        "\nCRITICAL alerts (sample): "
        + json.dumps(context.get("critical_alerts", [])[:5], default=str)
        + "\n\nProduce the structured brief by calling submit_watch_officer_brief. "
        "Be concise. 3–6 items per bulleted list. Limit confidence to honest values "
        "(0.4–0.7 deterministic, 0.7–0.9 when LLM adds nuance, never claim > 0.95)."
    )


def _llm_brief(context: dict, signals: dict) -> WatchOfficerBrief | None:
    """Try the LLM path. Returns None if anything goes wrong."""
    try:
        client = build_llm_client()
        if client is None:
            return None
        raw = client.call_with_tool(
            system_prompt=_WATCH_OFFICER_SYSTEM_PROMPT,
            tool=_WATCH_OFFICER_TOOL,
            user_message=_format_user_message(context, signals),
            max_tokens=900,
        )
        return WatchOfficerBrief(
            posture=raw["posture"],
            trust_assessment=raw["trust_assessment"],
            headline=raw["headline"],
            what_changed=list(raw.get("what_changed", [])),
            why_it_matters=list(raw.get("why_it_matters", [])),
            recommended_human_review=list(raw.get("recommended_human_review", [])),
            evidence=list(raw.get("evidence", [])),
            affected_airports=list(raw.get("affected_airports", [])),
            pending_actions_count=context.get("pending_actions_count", 0),
            critical_alert_count=context.get("critical_alert_count", 0),
            confidence=float(raw.get("confidence", 0.5)),
            llm_used=True,
            limitations=list(raw.get("limitations", [])) + [
                "Brief drafted by LLM; verify against deterministic signals before acting.",
                "Data-feed degradation does NOT imply unsafe airspace.",
            ],
        )
    except Exception as exc:
        _log.warning("watch_officer_llm_failed", extra={"error": str(exc)})
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def generate_watch_officer_brief(
    context: dict | None = None,
    use_llm: bool = True,
) -> WatchOfficerBrief:
    """
    Build the brief. LLM path is best-effort; fallback is deterministic.
    NEVER raises. Always returns a valid WatchOfficerBrief.
    """
    if context is None:
        context = build_watch_officer_context()
    signals = _detect_signals(context)

    if use_llm:
        llm_brief = _llm_brief(context, signals)
        if llm_brief is not None:
            return llm_brief
    return _deterministic_brief(context, signals)
