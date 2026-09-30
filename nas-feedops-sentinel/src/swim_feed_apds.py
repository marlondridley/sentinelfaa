"""
APDS: runway visual range.
"""
from __future__ import annotations

from src.swim_common import _coerce_int, _common_fields, _primary_airport_from, _scale_rvr

# ── APDS -- runway visual range ───────────────────────────────────────────────

def _extract_apds_rvr(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns2:RVRDataUpdateMessage -> one event per runway.

    Shape (confirmed against tests/fixtures/swim_kit/apds_sample, a real
    captured message):

        { "airport": "KBWI",
          "runwayData": [ { "runwayID": {"numericRunwayID": 15,
                                         "runwaySubID": "L"},
                            "touchdownVisualRange": 60,
                            "midpointVisualRange": 60,
                            "rolloutVisualRange": 60, ... } ] }

    One event per runway rather than per message, because NASEvent carries a
    single runway plus its three RVR readings, and ALERT_LOW_RVR needs to
    name the affected runway.
    """
    tracon, service_name, event_ts = _common_fields(headers)
    msg_type = headers.get("msgType", "RR")

    airport = _primary_airport_from(payload.get("airport"), headers.get("airport"))
    if airport is None:
        return []

    runways = payload.get("runwayData") or []
    if isinstance(runways, dict):
        runways = [runways]

    out: list[dict] = []
    for rw in runways:
        if not isinstance(rw, dict):
            continue

        rw_id = rw.get("runwayID") or {}
        number = _coerce_int(rw_id.get("numericRunwayID"))
        if number is None:
            continue
        runway = f"{number:02d}{(rw_id.get('runwaySubID') or '').strip().upper()}"

        touchdown = _scale_rvr(rw.get("touchdownVisualRange"))
        midpoint = _scale_rvr(rw.get("midpointVisualRange"))
        rollout = _scale_rvr(rw.get("rolloutVisualRange"))

        # A runway reporting no RVR at all carries no signal -- emitting it
        # would just create events that can never trigger or clear an alert.
        if touchdown is None and midpoint is None and rollout is None:
            continue

        out.append({
            "airport_code": airport,
            "tracon": tracon,
            "service_name": service_name,
            "message_type": f"APDS_{msg_type}",
            "event_time_utc": event_ts,
            "runway": runway,
            "rvr_touchdown": touchdown,
            "rvr_midpoint": midpoint,
            "rvr_rollout": rollout,
            "feed_status": "operational",
        })

    return out
