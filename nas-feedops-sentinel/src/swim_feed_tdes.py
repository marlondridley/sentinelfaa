"""
TDES: tower departure events, and TDLS departure clearance.
"""
from __future__ import annotations

from src.swim_common import _common_fields, _parse_tdls_time, _primary_airport_from

# ── TDES -- tower departure / TDLS clearance events ───────────────────────────

def _extract_tdes(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns5:TowerDepartureEventMessage -> one departure event.

    Shape (confirmed against tests/fixtures/swim_kit/tdes_sample):

        { "departureAirport": "KDCA", "aircraftID": "JIA5567",
          "aircraftType": "CRJ7", "beaconCode": 3755, "parkingGate": "G",
          "flightID": "...", "eventTime": "2026-08-27T20:42:16.000Z",
          "clearanceDeliveryTime": "...",
          "enhancedData": { "destinationAirport": "KCRW", ... } }

    eventTime is the facility's own timestamp for the event and is preferred
    over the JMS receipt header, because latency_ms is derived from it and
    drives ALERT_HIGH_LATENCY -- using the receipt time would measure this
    system's own clock against itself and report zero latency always.
    """
    tracon, service_name, header_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "DE")
    enhanced = payload.get("enhancedData") or {}

    airport = _primary_airport_from(
        payload.get("departureAirport"),
        headers.get("airport"),
        enhanced.get("destinationAirport"),
    )
    if airport is None:
        return []

    beacon = payload.get("beaconCode")

    return [{
        "airport_code": airport,
        "tracon": tracon,
        "service_name": service_name,
        "message_type": f"TDES_{msg_type}",
        "event_time_utc": payload.get("eventTime") or header_ts,
        "aircraft_id": payload.get("aircraftID") or payload.get("flightID"),
        "beacon_code": str(beacon) if beacon not in (None, "") else None,
        # A tower departure event is by definition a departure, and the
        # aircraft is still on the surface at clearance delivery.
        "flight_phase": "DEPARTURE",
        "surface_event_type": f"DEPARTURE_{msg_type}",
        "feed_status": "operational",
    }]

def _extract_tdls_dcl(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns4:TDLSCSPMessage -> one departure-clearance event.

    A TDLS CPDLC departure clearance (DCL) sent to a flight at a PCT airport.
    Shape (confirmed against tests/fixtures/swim_kit/tdls_sample, a real
    captured message):

        { "airportID": "KBWI", "aircraftID": "SWA1085", "aircraftType": "B737",
          "beaconCode": 2172, "computerID": "03P", "time": "08272026204225",
          "dataHeader": "...", "dataBody": "<free-text clearance>",
          "enhancedData": { "destinationAirport": "KMCI", ... } }

    It maps onto the departure model already used for TowerDepartureEvent:
    the aircraft is on the surface awaiting departure. dataBody is NOT
    stored -- it is free text carrying the full route and the aircraft's
    registration, none of which NASEvent models or any rule needs, and
    keeping it would widen what can leak into a prompt.
    """
    tracon, service_name, header_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "RD")
    enhanced = payload.get("enhancedData") or {}

    airport = _primary_airport_from(
        payload.get("airportID"),
        headers.get("airport"),
        enhanced.get("destinationAirport"),
    )
    if airport is None:
        return []

    beacon = payload.get("beaconCode")

    return [{
        "airport_code": airport,
        "tracon": tracon,
        "service_name": service_name,
        "message_type": f"TDLS_{msg_type}",
        "event_time_utc": _parse_tdls_time(payload.get("time")) or header_ts,
        "aircraft_id": payload.get("aircraftID"),
        "beacon_code": str(beacon) if beacon not in (None, "") else None,
        "flight_phase": "DEPARTURE",
        "surface_event_type": "DEPARTURE_CLEARANCE_DCL",
        "feed_status": "operational",
    }]
