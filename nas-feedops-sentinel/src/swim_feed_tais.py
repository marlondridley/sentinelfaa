"""
TAIS: terminal automation tracks and their flight plans.
"""
from __future__ import annotations

from src.swim_common import (
    _coerce_float,
    _coerce_int,
    _common_fields,
    _derive_flight_phase,
    _normalise_icao,
    _primary_airport_from,
)

# ── TAIS -- terminal automation tracks ────────────────────────────────────────

def _extract_tais(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns2:TATrackAndFlightPlan -> one event per track associated with a PCT
    primary airport.

    Most records in a TAIS message are simply aircraft inside PCT airspace
    with no airport association at all, and NASEvent.airport_code is a
    Literal of the four PCT primaries -- so those genuinely cannot become
    events and are dropped. That is correct, not a gap.

    What was a gap: airport selection took the first non-empty identifier and
    then tested it, so an arrival into KDCA whose flightPlan.airport named its
    origin was discarded. _primary_airport_from() now tests every candidate.
    """
    tracon, service_name, event_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "track")

    records = payload.get("record") or []
    if isinstance(records, dict):
        records = [records]

    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue

        fp = rec.get("flightPlan") or {}
        enhanced = rec.get("enhancedData") or {}
        track = rec.get("track") or {}

        airport = _primary_airport_from(
            enhanced.get("destinationAirport"),
            enhanced.get("departureAirport"),
            fp.get("airport"),
        )
        if airport is None:
            continue

        lat = track.get("lat")
        lon = track.get("lon")
        altitude_ft = _coerce_int(track.get("reportedAltitude"))
        vx = track.get("vx")
        vy = track.get("vy")
        v_vert = _coerce_int(track.get("vVert"))

        ground_speed_kt = None
        if vx is not None and vy is not None:
            try:
                ground_speed_kt = int(round((float(vx) ** 2 + float(vy) ** 2) ** 0.5))
            except (TypeError, ValueError):
                pass

        flight_phase = _derive_flight_phase(
            altitude_ft=altitude_ft,
            v_vert=v_vert,
            this_airport=airport,
            departure=_normalise_icao(enhanced.get("departureAirport")),
            destination=_normalise_icao(enhanced.get("destinationAirport")),
        )

        # STARS uses "NNNN" as its unassigned-runway sentinel; it is not a runway.
        runway_raw = (fp.get("runway") or "").strip().upper()
        runway = None if runway_raw in ("", "NNNN") else runway_raw

        # FAA STDDS beacon code notice (2017-10-11): a TAIS flight plan's beacon
        # code must not be propagated -- stored, printed, republished or shown
        # in any interface -- until the plan is active; Pending and Passive
        # plans belong to flights not yet cleared. Only an explicitly active
        # plan releases its codes, so a missing or unknown status withholds
        # them too. A track with no flight plan keeps the code its own
        # transponder is broadcasting.
        if fp and str(fp.get("status") or "").strip().lower() != "active":
            beacon = None
        else:
            beacon = fp.get("assignedBeaconCode") or track.get("reportedBeaconCode")

        out.append({
            "airport_code": airport,
            "tracon": tracon,
            "service_name": service_name,
            "message_type": f"TAIS_{msg_type}",
            # The per-record receipt time beats the message-level header:
            # one TAIS message carries many tracks sampled at different times.
            "event_time_utc": rec.get("recSAFAReceiptTime") or event_ts,
            "aircraft_id": fp.get("acid") or track.get("acAddress"),
            "track_id": str(track.get("trackNum")) if track.get("trackNum") is not None else None,
            "runway": runway,
            # One track's unreadable coordinate is that track's problem: float() raised out of the whole
            # message and quarantined every other track in it.
            "lat": _coerce_float(lat),
            "lon": _coerce_float(lon),
            "altitude_ft": altitude_ft,
            "ground_speed_kt": ground_speed_kt,
            "vertical_speed_fpm": v_vert,
            "flight_phase": flight_phase,
            "flight_rules": fp.get("flightRules"),
            "beacon_code": str(beacon) if beacon not in (None, "") else None,
            "feed_status": "operational",
        })

    return out
