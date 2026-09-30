"""
SMES: ASDE-X surface movement (positions, events, status and hold-bar messages).
"""
from __future__ import annotations

from src.swim_common import _as_list, _coerce_float, _common_fields, _keep_position, _primary_airport_from, _scalar

# ── SMES -- ASDE-X surface movement ───────────────────────────────────────────

def _extract_smes_holdbar(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns2:SafetyLogicHoldBar -> one airport-level surface-safety status event.

    Shape (confirmed against tests/fixtures/swim_kit/holdbar_sample, a real
    captured message):

        { "airport": "KIAD", "control": 1,
          "status": "0000...0007c000" }

    ASDE-X Safety Logic hold-bar state for the airport. It names no aircraft,
    vehicle or track, so it is not a movement event. `status` is a bitmask
    whose bit-to-hold-bar mapping is defined in the non-public SMES ICD; it is
    kept verbatim in status_code and deliberately not decoded or alerted on.
    What the message does establish, and why it is ingested rather than
    dropped, is that the airport's SMES feed is alive.
    """
    tracon, service_name, header_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "SH")

    airport = _primary_airport_from(payload.get("airport"), headers.get("airport"))
    if airport is None:
        return []

    status = payload.get("status")
    control = payload.get("control")
    return [{
        "airport_code": airport,
        "tracon": tracon,
        "service_name": service_name,
        "message_type": f"SMES_{msg_type}",
        "event_time_utc": header_ts,
        "surface_event_type": "SAFETY_LOGIC_HOLD_BAR",
        "status_code": (
            f"control={control};status={status}" if status not in (None, "") else None
        ),
        "feed_status": "operational",
    }]

# Report arrays in an asdexMsg that this extractor has been verified against.
# positionReport and systemStatus were verified on 2026-09-25 against 160,399
# live reports quarantined from KIAD, KDCA and KBWI: every report carried time,
# track and position; decoded positions sat a median 0.5 NM from the named
# airport's reference point (99% within 11.4 NM, i.e. surface plus final
# approach); and report times were UTC, within 0.36 s of the JMS timestamp.
_SMES_VERIFIED_REPORTS = frozenset({"adsbReport", "positionReport", "systemStatus"})

_SMES_NON_REPORT_KEYS = frozenset({"airport"})

def _extract_smes(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns2:asdexMsg -> one event per surface report.

    adsbReport shape (confirmed against tests/fixtures/swim_kit/smes_sample, a
    real captured message):

        { "airport": "KIAD",
          "adsbReport": [
            { "enhancedData": { "eramGufi": "KW677325sW" },
              "report": { "basicReport": {
                  "time": "2026-08-27T20:42:24.563Z",
                  "position": { "x": 225, "y": 87,
                                "lat": 38.93986, "lon": -77.44608 },
                  "track": 417 } },
              "full": false }, ... ] }

    `track` is the ASDE-X surface track number and becomes track_id, which
    is what the SMES validation rule requires (a moving entity must be
    identified). Each report's own basicReport.time is its event time: one
    message batches reports taken at different instants, and latency_ms is
    derived from it. eramGufi identifies the ERAM flight, not the aircraft,
    so it is not put in aircraft_id; NASEvent has no field for it.

    positionReport (msgType AT; tests/fixtures/swim_kit/smes_position_sample)
    is the ASDE-X fused track: {"time", "track", "position": {"latitude",
    "longitude", ...}, "flightId": {"aircraftId", ...}, ...}. aircraftId is
    usually absent or "UNKN"; only a real callsign becomes aircraft_id.
    Reports are sampled per SMES_POSITION_SAMPLE_SECONDS.

    systemStatus (msgType AY; tests/fixtures/swim_kit/smes_status_sample) is
    {"mode", "state"} for the airport's ASDE-X. It is kept verbatim in
    status_code and not mapped to feed_status: state read "degraded" in every
    captured message, all day, so mapping it would mark SMES degraded forever.

    A message carrying any other report type is refused rather than partly
    ingested. Guessing at an unseen layout could store wrong positions, and
    silently extracting only part of a message would drop the rest while
    reporting success. Refused files land in data/failed/ with the unverified
    keys named.
    """
    unverified = sorted(
        k for k in payload
        if k not in _SMES_VERIFIED_REPORTS
        and k not in _SMES_NON_REPORT_KEYS
        and not k.startswith("xmlns")
    )
    if unverified:
        raise ValueError(
            f"SMES asdexMsg contains report types this extractor has not been "
            f"verified against: {unverified}. Only {sorted(_SMES_VERIFIED_REPORTS)} "
            f"is supported; see _extract_smes() in src/swim_file_ingest.py."
        )

    tracon, service_name, header_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "AD")

    airport = _primary_airport_from(payload.get("airport"), headers.get("airport"))
    if airport is None:
        return []

    base = {
        "airport_code": airport,
        "tracon": tracon,
        "service_name": service_name,
        "message_type": f"SMES_{msg_type}",
        "feed_status": "operational",
    }
    records = []
    for report in _as_list(payload.get("adsbReport")):
        basic = ((report or {}).get("report") or {}).get("basicReport") or {}
        position = basic.get("position") or {}
        track = basic.get("track")
        records.append({
            **base,
            "event_time_utc": basic.get("time") or header_ts,
            "track_id": str(track) if track not in (None, "") else None,
            "lat": _coerce_float(position.get("lat")),
            "lon": _coerce_float(position.get("lon")),
            "surface_event_type": "ADSB_REPORT",
        })
    for report in _as_list(payload.get("positionReport")):
        report = report or {}
        if not _keep_position(_scalar(report.get("time"))):
            continue
        position = report.get("position") or {}
        track = _scalar(report.get("track"))
        callsign = _scalar((report.get("flightId") or {}).get("aircraftId"))
        records.append({
            **base,
            "event_time_utc": _scalar(report.get("time")) or header_ts,
            "track_id": str(track) if track not in (None, "") else None,
            "aircraft_id": callsign if callsign not in (None, "", "UNKN") else None,
            "lat": _coerce_float(_scalar(position.get("latitude"))),
            "lon": _coerce_float(_scalar(position.get("longitude"))),
            "surface_event_type": "POSITION_REPORT",
        })
    for status in _as_list(payload.get("systemStatus")):
        status = status or {}
        records.append({
            **base,
            "event_time_utc": header_ts,
            "surface_event_type": "ASDEX_SYSTEM_STATUS",
            "status_code": f"mode={_scalar(status.get('mode'))};state={_scalar(status.get('state'))}",
        })
    return records

# ASDE-X surface events, named for what the event/status pairs in live traffic
# showed: "off" always came with status airborne, "on" with onrunway or
# onsurface, "spotin" with onramp.
_SURFACE_EVENTS = {
    "on": "WHEELS_ON",
    "off": "WHEELS_OFF",
    "runwayin": "RUNWAY_ENTRY",
    "runwayout": "RUNWAY_EXIT",
    "spotin": "SPOT_IN",
    "spotout": "SPOT_OUT",
}

def _extract_smes_event(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns2:SurfaceMovementEventMessage (msgType SE) -> one discrete surface event.

    Shape (tests/fixtures/swim_kit/smes_event_sample, a real captured message):

        { "airport": "KIAD", "time": "2026-09-25T23:09:11.665Z", "track": 3553,
          "callsign": "SVA038", "mode3ACode": 7056, "altitude": 418.75,
          "position": { "latitude": ..., "longitude": ... },
          "event": "off", "status": "airborne", "runway": "12/30",
          "events": { "eventRecord": [ <this flight's earlier events> ] }, ... }

    Verified on 2026-09-25 against 1,299 live messages: every one carried time,
    track, event, status and position; positions lay within 1.8 NM of the named
    airport; time equalled the JMS timestamp; every runway named exists at that
    airport; and surface altitudes matched field elevation in feet. eventRecord
    is the flight's history (a median 45 s older), so it is not re-ingested.
    """
    tracon, service_name, header_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "SE")

    airport = _primary_airport_from(payload.get("airport"), headers.get("airport"))
    if airport is None:
        return []

    event = str(_scalar(payload.get("event")) or "").strip().lower()
    position = payload.get("position") or {}
    track = _scalar(payload.get("track"))
    callsign = _scalar(payload.get("callsign"))
    beacon = _scalar(payload.get("mode3ACode"))
    altitude = _coerce_float(_scalar(payload.get("altitude")))
    status = _scalar(payload.get("status"))
    return [{
        "airport_code": airport,
        "tracon": tracon,
        "service_name": service_name,
        "message_type": f"SMES_{msg_type}",
        "event_time_utc": _scalar(payload.get("time")) or header_ts,
        "track_id": str(track) if track not in (None, "") else None,
        "aircraft_id": callsign if callsign not in (None, "", "UNKN") else None,
        # Octal digits; a numeric JSON value has lost its leading zeros.
        "beacon_code": str(beacon).zfill(4) if beacon not in (None, "") else None,
        "lat": _coerce_float(_scalar(position.get("latitude"))),
        "lon": _coerce_float(_scalar(position.get("longitude"))),
        "altitude_ft": round(altitude) if altitude is not None else None,
        "runway": _scalar(payload.get("runway")) or None,
        "surface_event_type": _SURFACE_EVENTS.get(event, f"SURFACE_{event.upper()}"),
        "status_code": f"status={status}" if status else None,
        "feed_status": "operational",
    }]
