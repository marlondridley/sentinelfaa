"""
ContextRouter: Noise-to-signal pre-processor for agentic layers.
Asynchronously routes queries to context slices from PostgreSQL via asyncpg.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src import config, storage
from src.execution_gate import _strict_bool


def _summarise_events(events: list[dict]) -> dict[str, dict[str, Any]]:
    by_service: dict[str, dict[str, Any]] = {}
    for e in events:
        svc = e.get("service_name") or "unknown"
        if svc not in by_service:
            by_service[svc] = {
                "count": 0,
                "latencies_ms": [],
                "feed_statuses": set(),
                "sample_event_ids": [],
            }
        s = by_service[svc]
        s["count"] += 1
        if e.get("latency_ms") is not None:
            s["latencies_ms"].append(e["latency_ms"])
        if e.get("feed_status"):
            s["feed_statuses"].add(e["feed_status"])
        if len(s["sample_event_ids"]) < 5:
            event_id = e.get("event_id") or e.get("id") or "UNKNOWN"
            s["sample_event_ids"].append(event_id)

    result: dict[str, dict[str, Any]] = {}
    for svc, s in by_service.items():
        lats = s["latencies_ms"]
        result[svc] = {
            "count": s["count"],
            "feed_statuses": sorted(s["feed_statuses"]),
            "sample_event_ids": s["sample_event_ids"],
        }
        if lats:
            result[svc]["avg_latency_ms"] = round(sum(lats) / len(lats), 1)
            result[svc]["max_latency_ms"] = round(max(lats), 1)
    return result


def _worst_status(statuses: list[str]) -> str:
    rank = {"OK": 0, "STALE": 1, "DEGRADED": 2, "UNAVAILABLE": 3, "DISCONNECTED": 4}
    return max(statuses, key=lambda s: rank.get(s.upper(), 0)) if statuses else "OK"


def _service_status_from_feed_statuses(feed_statuses: list[str]) -> str:
    fs = [s.lower() for s in feed_statuses]
    if "disconnected" in fs:
        return "DISCONNECTED"
    if "unavailable" in fs:
        return "UNAVAILABLE"
    if "degraded" in fs:
        return "DEGRADED"
    return "OK"


def _index_rvr_by_airport(rvr_rows: list[dict]) -> dict[str, dict[str, Any]]:
    rvr_by_airport: dict[str, dict[str, Any]] = {}
    for row in rvr_rows:
        ap = row["airport_code"]
        rwy = row.get("runway") or "unknown"
        rvr_by_airport.setdefault(ap, {})[rwy] = {
            "touchdown_ft": row.get("rvr_touchdown"),
            "midpoint_ft": row.get("rvr_midpoint"),
            "rollout_ft": row.get("rvr_rollout"),
            "time_utc": row.get("event_time_utc"),
        }
    return rvr_by_airport


def _build_airport_snapshot(
    ap: str,
    ap_events: list[dict],
    phase_counts: dict,
    ap_aircraft: list[dict],
    ap_rvr: dict
) -> dict[str, Any]:
    svc_summary = _summarise_events(ap_events)

    svc_statuses: dict[str, str] = {}
    for svc, s in svc_summary.items():
        svc_statuses[svc] = _service_status_from_feed_statuses(
            s.get("feed_statuses", [])
        )

    overall = _worst_status(list(svc_statuses.values())) if svc_statuses else "NO DATA"

    sample_flights = []
    for ac in ap_aircraft[:8]:
        callsign = ac.get("aircraft_id") or "UNKNOWN"
        phase = ac.get("flight_phase") or "?"
        alt = ac.get("altitude_ft")
        rwy = ac.get("runway") or "-"
        sample_flights.append(f"{callsign} ({phase}, alt={alt}ft, rwy={rwy})")

    return {
        "overall_status": overall,
        "event_count_30m": len(ap_events),
        "services": {
            svc: {
                "status": svc_statuses.get(svc, "OK"),
                **{k: v for k, v in s.items() if k != "sample_event_ids"},
                "sample_event_ids": s.get("sample_event_ids", []),
            }
            for svc, s in svc_summary.items()
        },
        "rvr": ap_rvr,
        "active_aircraft_count": len(ap_aircraft),
        "flight_phases": {
            "DEPARTURE": phase_counts.get("DEPARTURE", 0),
            "ARRIVAL": phase_counts.get("ARRIVAL", 0),
            "ENROUTE": phase_counts.get("ENROUTE", 0),
            "GROUND": phase_counts.get("GROUND", 0),
            "UNKNOWN": phase_counts.get("UNKNOWN", 0),
        },
        "sample_flights": sample_flights,
    }


def _enrich_alerts_with_critic(
    active_alerts: list[dict], 
    critic_by_alert: dict[str, dict]
) -> list[dict]:
    enriched_alerts = []
    for a in active_alerts:
        entry: dict[str, Any] = {
            "alert_id": a["alert_id"],
            "alert_type": a["alert_type"],
            "severity": a["severity"],
            "airport_code": a["airport_code"],
            "service_name": a["service_name"],
            "description": a["description"],
            "triggered_at": a["triggered_at"],
        }
        cr = critic_by_alert.get(a["alert_id"])
        if cr:
            entry["critic"] = {
                "confidence": cr["confidence"],
                "pattern": cr["pattern"],
                "escalate": _strict_bool(cr.get("escalate")),
                "reasoning": cr["reasoning"],
            }
        enriched_alerts.append(entry)
    return enriched_alerts


class ContextRouter:
    async def build_alert_context(
        self,
        airport_code: str | None,
        window_minutes: int = 30,
    ) -> dict[str, Any]:
        recent_events = await storage.get_events_for_critic(
            airport_code=airport_code,
            window_minutes=window_minutes,
            limit=50,
        )
        recent_alerts = await storage.get_alerts_for_critic(
            airport_code=airport_code,
            window_minutes=60,
            limit=10,
        )
        return {
            "window_minutes": window_minutes,
            "total_recent_events": len(recent_events),
            "service_summary": _summarise_events(recent_events),
            "recent_alerts": [
                {
                    "alert_type": a["alert_type"],
                    "severity": a["severity"],
                    "service_name": a["service_name"],
                    "description": a["description"],
                    "triggered_at": a["triggered_at"],
                }
                for a in recent_alerts
            ],
        }

    async def build_status_context(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)

        # 1. Orchestrate Data Fetching
        all_events = await storage.get_events_for_critic(airport_code=None, window_minutes=30, limit=500)
        active_alerts = await storage.get_alerts_for_critic(airport_code=None, window_minutes=120, limit=50)
        critic_rows = await storage.get_recent_critic_results(limit=30)
        rvr_rows = await storage.get_latest_rvr()
        flight_phase_counts = await storage.get_flight_phase_counts(window_minutes=15)
        active_aircraft = await storage.get_active_aircraft(window_minutes=5, limit=300)

        # 2. Pre-process and Index Global State
        critic_by_alert = {r["alert_id"]: r for r in critic_rows}
        rvr_by_airport = _index_rvr_by_airport(rvr_rows)
        
        aircraft_by_airport: dict[str, list[dict]] = {}
        for ac in active_aircraft:
            if ap := ac.get("airport_code"):
                aircraft_by_airport.setdefault(ap, []).append(ac)

        # 3. Build Airport Contexts
        airports: dict[str, Any] = {}
        for ap in sorted(config.SUPPORTED_AIRPORTS):
            ap_events = [e for e in all_events if e.get("airport_code") == ap]
            airports[ap] = _build_airport_snapshot(
                ap=ap,
                ap_events=ap_events,
                phase_counts=flight_phase_counts.get(ap, {}),
                ap_aircraft=aircraft_by_airport.get(ap, []),
                ap_rvr=rvr_by_airport.get(ap, {})
            )

        # 4. Enrich Alerts
        enriched_alerts = _enrich_alerts_with_critic(active_alerts, critic_by_alert)

        # 5. Aggregate Summaries & Return
        critical_count = sum(1 for a in active_alerts if a["severity"] == "CRITICAL")
        warning_count = sum(1 for a in active_alerts if a["severity"] == "WARNING")
        escalation_needed = any(bool(r.get("escalate")) for r in critic_rows)

        return {
            "snapshot_time": now.isoformat(),
            "window_minutes": 30,
            "airports": airports,
            "active_alerts": enriched_alerts[:20],
            "summary": {
                "total_events_30m": len(all_events),
                "critical_alerts": critical_count,
                "warning_alerts": warning_count,
                "escalation_required": escalation_needed,
            },
        }