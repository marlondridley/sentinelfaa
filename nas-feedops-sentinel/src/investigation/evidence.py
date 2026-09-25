"""
Evidence collection. No model runs here.

gather_snapshot() does all the I/O once, at the start of an investigation.
The builders below are pure functions of that snapshot: the same snapshot
always yields the same evidence with the same evidence_ids, which is what
makes an investigation reproducible and its tests deterministic.

Each specialist only ever sees the evidence kinds it is scoped to
(FEED_KINDS, CORRELATION_KINDS); the orchestrator enforces that.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src import config, storage
from src.feed_health import compute_feed_health
from src.investigation.record import Evidence
from src.models import AlertEvent, NASEvent, StagedAction

TRIGGER_ID = "trigger"

FEED_KINDS = frozenset({
    "trigger", "feed_heartbeat", "last_message", "ingest_latency",
    "failed_files", "parser_failures", "service_health",
})
CORRELATION_KINDS = frozenset({
    "trigger", "alerts_by_airport", "alerts_by_service", "alert_rate",
    "related_alert", "rvr_reading", "prior_incident",
})

EVENT_LIMIT = 2000
ALERT_LIMIT = 500
INGEST_LOG_LIMIT = 200
STAGED_LIMIT = 200
RELATED_ALERT_LIMIT = 10


@dataclass(frozen=True)
class Snapshot:
    """Everything an investigation looks at, read once."""

    taken_at: datetime
    alert: AlertEvent
    action_id: str
    events: list[NASEvent]
    alerts: list[dict]
    ingest_log: list[dict]
    failed_files: list[tuple[str, datetime]] | None   # None: directory unreadable
    rvr: list[dict]
    staged_actions: list[StagedAction]
    window: timedelta = field(default=timedelta(minutes=60))


async def gather_snapshot(alert: AlertEvent, action_id: str, failed_dir: Path | None = None) -> Snapshot:
    from src.watch_officer_agent import rebuild_events

    events, _ = rebuild_events(await storage.get_recent_events(limit=EVENT_LIMIT))
    return Snapshot(
        taken_at=datetime.now(timezone.utc),
        alert=alert,
        action_id=action_id,
        events=events,
        alerts=await storage.get_recent_alerts(limit=ALERT_LIMIT),
        ingest_log=await storage.get_recent_ingest_log(limit=INGEST_LOG_LIMIT),
        failed_files=_list_failed_files(failed_dir or (config.DATA_DIR / "failed")),
        rvr=await storage.get_latest_rvr(),
        staged_actions=await storage.get_recent_staged_actions(limit=STAGED_LIMIT),
        window=timedelta(minutes=config.INVESTIGATION_WINDOW_MINUTES),
    )


def _list_failed_files(failed_dir: Path) -> list[tuple[str, datetime]] | None:
    try:
        return [
            (p.name, datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc))
            for p in failed_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        ]
    except OSError:
        return None


# ── helpers ───────────────────────────────────────────────────────────────────

def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def _ev(snapshot: Snapshot, evidence_id: str, kind: str, subject: str, source: str, data: dict) -> Evidence:
    return Evidence(
        evidence_id=evidence_id, kind=kind, subject=subject, source=source,
        observed_at=snapshot.taken_at, data=data,
    )


def trigger_evidence(s: Snapshot) -> Evidence:
    a = s.alert
    return _ev(s, TRIGGER_ID, "trigger", f"{a.alert_type} at {a.airport_code or 'PCT'}",
               "alert_events (the alert that staged this action)", {
                   "alert_id": a.alert_id, "alert_type": a.alert_type, "severity": a.severity,
                   "airport": a.airport_code, "service": a.service_name,
                   "description": a.description, "triggered_at": _iso(_aware(a.triggered_at)),
                   "action_id": s.action_id, "data_origin": a.data_origin,
               })


# ── Feed Diagnostician evidence ──────────────────────────────────────────────

def feed_evidence(s: Snapshot) -> list[Evidence]:
    """feed heartbeat, last-message timestamps, ingest latency, failed files,
    parser failures, service health."""
    now = s.taken_at
    events_src = f"nas_events (latest {EVENT_LIMIT} by received_time_utc)"
    out = [trigger_evidence(s)]

    # Feed heartbeat: per service, across airports. Every supported service is
    # listed, including silent ones -- zero messages is itself the evidence.
    by_service: dict[str, list[datetime]] = defaultdict(list)
    for e in s.events:
        by_service[e.service_name].append(e.received_time_utc)
    for service in sorted(config.SUPPORTED_SERVICES):
        times = sorted(by_service.get(service, []))
        in_window = [t for t in times if t >= now - s.window]
        gaps = [(b - a).total_seconds() for a, b in zip(in_window, in_window[1:], strict=False)]
        out.append(_ev(s, f"heartbeat:{service}", "feed_heartbeat", service, events_src, {
            "messages_last_5m": sum(t >= now - timedelta(minutes=5) for t in times),
            "messages_last_15m": sum(t >= now - timedelta(minutes=15) for t in times),
            "messages_in_window": len(in_window),
            "window_minutes": int(s.window.total_seconds() // 60),
            "max_gap_seconds_in_window": max(gaps) if gaps else None,
            "last_received": _iso(times[-1]) if times else None,
            "seconds_since_last": round((now - times[-1]).total_seconds(), 1) if times else None,
        }))

    # Last-message timestamps: per airport/service pair that has ever reported.
    latest: dict[tuple[str, str], NASEvent] = {}
    for e in s.events:
        key = (e.airport_code, e.service_name)
        if key not in latest or e.received_time_utc > latest[key].received_time_utc:
            latest[key] = e
    for (airport, service), e in sorted(latest.items()):
        since = (now - e.received_time_utc).total_seconds()
        out.append(_ev(s, f"last_message:{airport}:{service}", "last_message", f"{airport} / {service}", events_src, {
            "last_received": _iso(e.received_time_utc),
            "last_event_time": _iso(e.event_time_utc),
            "seconds_since_last": round(since, 1),
            "stale_threshold_seconds": config.STALE_FEED_SECONDS,
            "stale": since > config.STALE_FEED_SECONDS,
        }))

    # Ingest latency: per service, over the window.
    lat: dict[str, list[float]] = defaultdict(list)
    for e in s.events:
        if e.latency_ms is not None and e.received_time_utc >= now - s.window:
            lat[e.service_name].append(e.latency_ms)
    for service, values in sorted(lat.items()):
        out.append(_ev(s, f"latency:{service}", "ingest_latency", service, events_src, {
            "count": len(values),
            "avg_ms": round(sum(values) / len(values), 1),
            "p95_ms": round(_p95(values), 1),
            "max_ms": round(max(values), 1),
            "threshold_ms": config.LATENCY_ALERT_MS,
            "over_threshold": sum(v > config.LATENCY_ALERT_MS for v in values),
        }))

    # Failed files: what sits in data/failed/ right now.
    if s.failed_files is None:
        failed = {"readable": False}
    else:
        newest = sorted(s.failed_files, key=lambda f: f[1], reverse=True)
        failed = {
            "readable": True,
            "total": len(newest),
            "last_hour": sum(m >= now - timedelta(hours=1) for _, m in newest),
            "newest": [{"file": n, "mtime": _iso(m)} for n, m in newest[:5]],
        }
    out.append(_ev(s, "failed_files", "failed_files", "data/failed/", "filesystem listing of data/failed/", failed))

    # Parser failures: ingest_log rows in the window.
    entries = [r for r in s.ingest_log if (_aware(r.get("timestamp")) or now) >= now - s.window]
    failures = [r for r in entries if r.get("status") == "failed"]
    out.append(_ev(s, "parser_failures", "parser_failures", "SWIM file ingest",
                   f"ingest_log (latest {INGEST_LOG_LIMIT} rows, within window)", {
        "files_in_window": len(entries),
        "failed_in_window": len(failures),
        "recent_errors": [
            {"file": r.get("file"), "error": (r.get("error") or "")[:200], "at": r.get("timestamp")}
            for r in failures[:5]
        ],
    }))

    # Service health: the same computation the dashboard and pipeline use,
    # plus the feed_status values the messages reported about themselves.
    recent = [e for e in s.events if e.received_time_utc >= now - s.window]
    health = compute_feed_health(recent, now=now) if recent else {"service_status": {}}
    reported: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in recent:
        if e.feed_status:
            reported[(e.airport_code, e.service_name)].add(e.feed_status)
    for service, per_airport in sorted(health["service_status"].items()):
        for airport, status in sorted(per_airport.items()):
            out.append(_ev(s, f"health:{airport}:{service}", "service_health", f"{airport} / {service}",
                           "feed_health.compute_feed_health over the window", {
                "status": status,
                "reported_feed_status": sorted(reported.get((airport, service), set())),
            }))
    return out


# ── Correlation Analyst evidence ─────────────────────────────────────────────

def correlation_evidence(s: Snapshot) -> list[Evidence]:
    """airports, services, time windows, RVR, related alerts, prior incidents."""
    a = s.alert
    t0 = _aware(a.triggered_at) or s.taken_at
    alerts_src = f"alert_events (latest {ALERT_LIMIT})"
    out = [trigger_evidence(s)]

    others = [r for r in s.alerts if r.get("alert_id") != a.alert_id]
    in_window = [r for r in others if abs((_aware(r["triggered_at"]) - t0).total_seconds()) <= s.window.total_seconds()]

    by_airport: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_service: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in in_window:
        by_airport[r.get("airport_code") or "PCT"][r["severity"]] += 1
        by_service[r.get("service_name") or "unknown"][r["severity"]] += 1
    window_note = f"within ±{int(s.window.total_seconds() // 60)} min of the trigger, excluding it"
    out.append(_ev(s, "alerts_by_airport", "alerts_by_airport", window_note, alerts_src,
                   {k: dict(v) for k, v in sorted(by_airport.items())}))
    out.append(_ev(s, "alerts_by_service", "alerts_by_service", window_note, alerts_src,
                   {k: dict(v) for k, v in sorted(by_service.items())}))

    # Time windows: is the rate before the trigger unusual?
    def _count(scope_airport: str | None, minutes: int) -> int:
        return sum(
            1 for r in others
            if timedelta(0) <= t0 - _aware(r["triggered_at"]) <= timedelta(minutes=minutes)
            and (scope_airport is None or r.get("airport_code") == scope_airport)
        )
    out.append(_ev(s, "alert_rate", "alert_rate", "alerts before the trigger", alerts_src, {
        "same_airport": {m: _count(a.airport_code, m) for m in (15, 60, 1440)},
        "all_airports": {m: _count(None, m) for m in (15, 60, 1440)},
        "same_type_same_airport_24h": sum(
            1 for r in others
            if r.get("alert_type") == a.alert_type and r.get("airport_code") == a.airport_code
            and timedelta(0) <= t0 - _aware(r["triggered_at"]) <= timedelta(hours=24)
        ),
    }))

    # Related alerts: the nearest in time, preferring other airports/services.
    def _rank(r: dict) -> tuple:
        cross = (r.get("airport_code") != a.airport_code) + (r.get("service_name") != a.service_name)
        return (r["severity"] != "CRITICAL", -cross, abs((_aware(r["triggered_at"]) - t0).total_seconds()))
    for r in sorted(in_window, key=_rank)[:RELATED_ALERT_LIMIT]:
        out.append(_ev(s, f"alert:{r['alert_id']}", "related_alert",
                       f"{r['alert_type']} at {r.get('airport_code') or 'PCT'}", alerts_src, {
            "alert_type": r["alert_type"], "severity": r["severity"],
            "airport": r.get("airport_code"), "service": r.get("service_name"),
            "triggered_at": _iso(_aware(r["triggered_at"])),
            "offset_seconds": round((_aware(r["triggered_at"]) - t0).total_seconds(), 1),
            "description": (r.get("description") or "")[:200],
        }))

    # RVR: latest reading per runway, with the low-visibility screen applied.
    for r in s.rvr:
        readings = [v for v in (r.get("rvr_touchdown"), r.get("rvr_midpoint"), r.get("rvr_rollout")) if v is not None]
        at = _aware(r.get("event_time_utc"))
        out.append(_ev(s, f"rvr:{r.get('airport_code')}:{r.get('runway')}", "rvr_reading",
                       f"{r.get('airport_code')} runway {r.get('runway')}", "nas_events APDS_RVR (latest per runway)", {
            "touchdown_ft": r.get("rvr_touchdown"), "midpoint_ft": r.get("rvr_midpoint"),
            "rollout_ft": r.get("rvr_rollout"),
            "min_ft": min(readings) if readings else None,
            "low_threshold_ft": config.RVR_LOW_THRESHOLD_FT,
            "below_threshold": bool(readings) and min(readings) < config.RVR_LOW_THRESHOLD_FT,
            "event_time": _iso(at),
            "age_minutes": round((s.taken_at - at).total_seconds() / 60, 1) if at else None,
        }))

    # Prior incidents: staged actions at the same airport in the last 24 h.
    # Operator names and free-text notes are deliberately left out.
    for p in s.staged_actions:
        created = _aware(p.created_at)
        if (p.action_id != s.action_id and p.airport_code == a.airport_code
                and created and timedelta(0) <= t0 - created <= timedelta(hours=24)):
            out.append(_ev(s, f"prior:{p.action_id}", "prior_incident",
                           f"{p.alert_type} at {p.airport_code}", "staged_actions (last 24 h, same airport)", {
                "alert_type": p.alert_type, "status": p.status,
                "created_at": _iso(created),
                "hours_before_trigger": round((t0 - created).total_seconds() / 3600, 2),
            }))
    return out


def collect(s: Snapshot) -> list[Evidence]:
    """All evidence for the record, de-duplicated by id (the trigger is shared)."""
    seen: dict[str, Evidence] = {}
    for ev in feed_evidence(s) + correlation_evidence(s):
        seen.setdefault(ev.evidence_id, ev)
    return list(seen.values())
