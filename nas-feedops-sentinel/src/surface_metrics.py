"""
Taxi-time arithmetic over surface events (pure functions: no database, no model).

src/storage/repositories/surface_repo.py returns raw event pairs; this turns
them into the numbers an operator asks for. Every result states its sample
size and how fresh the surface data is, because "average taxi-out is 4
minutes" from three flights, or from a feed that stopped an hour ago, is not
the same statement as one from forty flights a minute ago.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

# Surface data older than this, at the moment of the question, is called stale.
STALE_AFTER_MINUTES = 10
# Fewer completed taxis than this, and the average is said to rest on a small sample.
SMALL_SAMPLE = 5


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _minutes(start: datetime, end: datetime) -> float:
    return (_utc(end) - _utc(start)).total_seconds() / 60.0


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def freshness(latest: datetime | None, as_of: datetime) -> dict[str, Any]:
    """How recent the newest surface event is, at the moment being asked about."""
    if latest is None:
        return {"latest_surface_event_at": None, "minutes_since": None, "stale": True,
                "note": "No surface events are on record."}
    since = max(_minutes(latest, as_of), 0.0)
    stale = since > STALE_AFTER_MINUTES
    return {
        "latest_surface_event_at": _utc(latest).isoformat(),
        "minutes_since": _r(since),
        "stale": stale,
        "note": (f"The newest surface event is {since:.0f} minutes old, so this may not reflect the "
                 "airport now; the feed may be stopped." if stale else "Surface data is current."),
    }


def taxi_stats(
    pairs: list[dict], start_key: str, end_key: str, *, airport: str | None, window_minutes: int,
    as_of: datetime, latest_event: datetime | None,
) -> dict[str, Any]:
    """
    Taxi time for the pairs completed in the last `window_minutes` (by end_key),
    plus an hourly trend over the pairs given. Taxi time = end - start.
    """
    as_of = _utc(as_of)
    rows = [
        {"aircraft_id": p["aircraft_id"], "airport": p["airport_code"], "minutes": _minutes(p[start_key], p[end_key]),
         "ended_at": _utc(p[end_key])}
        for p in pairs
    ]
    in_window = [r for r in rows if _minutes(r["ended_at"], as_of) <= window_minutes]

    longest = max(in_window, key=lambda r: r["minutes"], default=None)
    by_hour: dict[datetime, list[float]] = {}
    for r in rows:
        by_hour.setdefault(r["ended_at"].replace(minute=0, second=0, microsecond=0), []).append(r["minutes"])

    result: dict[str, Any] = {
        "airport": airport or "all",
        "window_minutes": window_minutes,
        "as_of": as_of.isoformat(),
        "completed": len(in_window),
        "avg_minutes": _r(sum(r["minutes"] for r in in_window) / len(in_window)) if in_window else None,
        "median_minutes": _r(median(r["minutes"] for r in in_window)) if in_window else None,
        "max_minutes": _r(longest["minutes"]) if longest else None,
        "longest": ({"aircraft_id": longest["aircraft_id"], "airport": longest["airport"],
                     "minutes": _r(longest["minutes"]), "ended_at": longest["ended_at"].isoformat()}
                    if longest else None),
        "hourly_trend": [
            {"hour_utc": hour.isoformat(), "completed": len(values), "avg_minutes": _r(sum(values) / len(values))}
            for hour, values in sorted(by_hour.items())
        ],
        "freshness": freshness(latest_event, as_of),
    }
    if not in_window:
        result["note"] = f"No taxi completed in the last {window_minutes} minutes, so there is no average to report."
    elif len(in_window) < SMALL_SAMPLE:
        result["note"] = f"Based on only {len(in_window)} completed taxi(s); treat the average as indicative."
    return result


def queue_summary(
    rows: list[dict], *, airport: str | None, as_of: datetime, latest_event: datetime | None,
    min_minutes: float = 0.0,
) -> dict[str, Any]:
    """Aircraft that left the gate and have not taken off or returned: how many, and for how long each."""
    as_of = _utc(as_of)
    waiting = sorted(
        ({"aircraft_id": r["aircraft_id"], "airport": r["airport_code"],
          "taxiing_minutes": _r(max(_minutes(r["spot_out_at"], as_of), 0.0)),
          "spot_out_at": _utc(r["spot_out_at"]).isoformat()} for r in rows),
        key=lambda r: r["taxiing_minutes"], reverse=True,
    )
    shown = [w for w in waiting if w["taxiing_minutes"] >= min_minutes]
    return {
        "airport": airport or "all",
        "as_of": as_of.isoformat(),
        "in_departure_queue": len(waiting),
        "min_taxiing_minutes": min_minutes,
        "matching": len(shown),
        "flights": shown[:20],
        "freshness": freshness(latest_event, as_of),
    }


def gate_return_summary(
    rows: list[dict], *, airport: str | None, window_minutes: int, as_of: datetime, latest_event: datetime | None,
) -> dict[str, Any]:
    as_of = _utc(as_of)
    flights = [
        {"aircraft_id": r["aircraft_id"], "airport": r["airport_code"], "spot_out_at": _utc(r["spot_out_at"]).isoformat(),
         "back_at_gate_at": _utc(r["spot_in_at"]).isoformat(),
         "minutes_out": _r(_minutes(r["spot_out_at"], r["spot_in_at"]))}
        for r in rows
    ]
    return {"airport": airport or "all", "window_minutes": window_minutes, "as_of": as_of.isoformat(),
            "returned_to_gate": len(flights), "flights": flights[:20], "freshness": freshness(latest_event, as_of)}


def flight_timeline(aircraft_id: str, events: list[dict], *, as_of: datetime) -> dict[str, Any]:
    """One flight's surface events in order, with its taxi-out and taxi-in worked out."""
    ordered = sorted(events, key=lambda e: e["event_time_utc"])
    taxi_out = taxi_in = None
    returned = False
    spot_out = wheels_on = None
    for e in ordered:
        kind, at = e["surface_event_type"], e["event_time_utc"]
        if kind == "SPOT_OUT":
            spot_out = at
        elif kind == "WHEELS_OFF" and spot_out is not None:
            taxi_out = {"spot_out_at": _utc(spot_out).isoformat(), "wheels_off_at": _utc(at).isoformat(),
                        "minutes": _r(_minutes(spot_out, at))}
            spot_out = None
        elif kind == "WHEELS_ON":
            wheels_on = at
        elif kind == "SPOT_IN":
            if wheels_on is not None:
                taxi_in = {"wheels_on_at": _utc(wheels_on).isoformat(), "spot_in_at": _utc(at).isoformat(),
                           "minutes": _r(_minutes(wheels_on, at))}
                wheels_on = None
            elif spot_out is not None:
                returned = True
                spot_out = None
    return {
        "aircraft_id": aircraft_id,
        "as_of": _utc(as_of).isoformat(),
        "events": [{"at": _utc(e["event_time_utc"]).isoformat(), "airport": e["airport_code"],
                    "event": e["surface_event_type"], "runway": e.get("runway")} for e in ordered],
        "taxi_out": taxi_out,
        "taxi_in": taxi_in,
        "returned_to_gate": returned,
        **({} if ordered else {"note": f"No surface events for {aircraft_id} in the period searched."}),
    }
