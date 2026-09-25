"""
SWIM File Ingest Adapter for NAS FeedOps Sentinel.

Architecture:
  FAA SCDS/SWIFT Jumpstart Kit  (Java, connects to STDDS JMS queue)
        |
        v writes raw messages
  data/raw/swim_live/           (local ingest folder)
        |
        v this module scans
  swim_file_ingest.py           (parse -> validate -> store -> alert)
        |
        v
  data/processed/               (successfully ingested files)
  data/failed/                  (files that could not be parsed)
  ingest_log (Postgres table)   (audit trail of every file processed)

Supported raw file formats:
  .json  -- JSON dict or list of dicts (same shape as replay files)
  .xml   -- FIXM/AIXM-style XML; fields extracted by tag name matching
  .txt   -- one JSON object per line (newline-delimited JSON)

Already-processed files are tracked in the ingest_log table (see
src/storage/repositories/ingest_log_repo.py), so repeated scans of the same
folder are idempotent. This used to be a plain append-only
data/processed/ingest_log.jsonl file that a long-running `--watch` process
grew forever and fully re-read on every scan pass just to answer "have I
seen this file name before?" -- the DB-backed version answers that with a
single indexed, bounded query per scan instead, and the table itself stays
bounded (see MAX_ROWS in ingest_log_repo.py) rather than growing without
limit over the life of a long-running deployment.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from src import config, storage
from src.async_bridge import run_async
from src.models import NASEvent
from src.logging_config import get_logger

_log = get_logger(__name__)

# Set by the entrypoint's SIGTERM/SIGINT handler (see main.py) so a scan already
# in progress can stop starting new files and let the caller exit cleanly instead
# of being killed mid-write when the orchestrator's grace period runs out.
_shutdown_requested = threading.Event()


def request_shutdown() -> None:
    """Ask any in-progress scan_swim_folder() call to stop after the current file."""
    _shutdown_requested.set()


def shutdown_requested() -> bool:
    return _shutdown_requested.is_set()


PROCESSED_DIR: Path = config.PROCESSED_DIR
FAILED_DIR: Path = config.DATA_DIR / "failed"

# Accept .json/.xml/.txt AND extension-less files (Kit's MessageFileOutput
# writes one file per JMS message named by epoch millis, no extension).
SCAN_EXTENSIONS: set[str] = {".json", ".xml", ".txt", ""}

# PCT TRACON primary airports -- used to filter TAIS records to in-scope traffic.
_PCT_PRIMARY_AIRPORTS: set[str] = {"KDCA", "KIAD", "KBWI", "KADW"}

# XML tag names that map to NASEvent fields (best-effort)
_XML_FIELD_MAP: dict[str, str] = {
    "airportCode": "airport_code",
    "airport": "airport_code",
    "facilityId": "airport_code",
    "tracon": "tracon",
    "serviceName": "service_name",
    "messageType": "message_type",
    "eventTime": "event_time_utc",
    "runway": "runway",
    "rvrTouchdown": "rvr_touchdown",
    "rvrMidpoint": "rvr_midpoint",
    "rvrRollout": "rvr_rollout",
    "aircraftId": "aircraft_id",
    "callsign": "aircraft_id",
    "trackId": "track_id",
    "vehicleId": "vehicle_id",
    "surfaceEventType": "surface_event_type",
    "feedStatus": "feed_status",
    "linkStatus": "feed_status",
}


#  Ingest log helpers   

def _get_already_processed(candidate_names: list[str]) -> set[str]:
    """
    Return the subset of candidate_names already recorded in the ingest_log table.

    Deliberately bounded to the file names currently sitting in the watched folder
    (small, and further capped by scan_swim_folder's max_files) rather than loading
    every file name ever processed -- see ingest_log_repo.py's docstring for why that
    distinction matters for a long-running --watch deployment.
    """
    return run_async(storage.get_ingested_file_names(candidate_names))


def _append_log_entry(entry: dict) -> None:
    run_async(storage.record_ingest_log_entry(entry))


#   File parsers  

def _parse_json_file(path: Path) -> list[dict]:
    # utf-8-sig: strips a leading BOM if present, identical to utf-8 if not.
    # A BOM left in place surfaces as \ufeff and breaks json.loads on a
    # perfectly valid message -- Windows tooling emits them routinely.
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"Unexpected JSON root type: {type(data).__name__}")


def _parse_txt_file(path: Path) -> list[dict]:
    """Newline-delimited JSON (one object per line)."""
    records = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _extract_text(elem: ET.Element, tag: str) -> str | None:
    """Find first child or descendant element by local tag name, return text."""
    for child in elem.iter():
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local == tag and child.text:
            return child.text.strip()
    return None


def _parse_xml_file(path: Path) -> list[dict]:
    """
    Best-effort extraction from FIXM/AIXM-style XML.
    Maps known tag names to NASEvent field names.
    Returns at least one record even if most fields are absent.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    # Collect all repeating message elements, or treat the whole doc as one
    # STDDS may wrap multiple messages; try common container tags first
    message_roots: list[ET.Element] = []
    for container_tag in ("message", "event", "record", "item"):
        hits = [
            child for child in root.iter()
            if (child.tag.split("}")[-1] if "}" in child.tag else child.tag).lower()
            == container_tag
        ]
        if hits:
            message_roots = hits
            break

    if not message_roots:
        message_roots = [root]

    records: list[dict] = []
    for msg in message_roots:
        record: dict[str, Any] = {}
        for xml_tag, nas_field in _XML_FIELD_MAP.items():
            value = _extract_text(msg, xml_tag)
            if value:
                record[nas_field] = value
        if record:
            records.append(record)

    return records if records else [{}]


#  FAA Jumpstart Kit MessageFileOutput parser (JMS headers + JSON body) 

_JMS_PROPERTY_RE = re.compile(r"^Property name = (?P<key>[^;]+);\s*value = (?P<val>.*)$")


def _parse_jms_message_file(path: Path) -> list[dict]:
    """
    Parse one file written by the FAA Jumpstart Kit's MessageFileOutput.

    Format:
        Property name = <key>; value = <val>     (zero or more lines)
        <single JSON object on its own line>      (the STDDS payload)

    Returns one normalized record dict per aircraft track in the payload
    (TAIS messages typically contain many records). Headers are merged
    into each record so downstream knows tracon, msgType, timestamp, etc.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")

    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False

    for line in text.splitlines():
        if not in_body:
            m = _JMS_PROPERTY_RE.match(line)
            if m:
                headers[m.group("key").strip()] = m.group("val").strip()
                continue
            stripped = line.strip()
            if not stripped:
                continue
            # First non-header, non-blank line begins the JSON body
            in_body = True
            body_lines.append(line)
        else:
            body_lines.append(line)

    body_text = "\n".join(body_lines).strip()
    if not body_text:
        # No payload; emit one summary record from headers alone
        return [_jms_headers_only_record(headers, path)]

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return [_jms_headers_only_record(headers, path)]

    records, recognised = _extract_records(headers, body, path)
    if records:
        return records

    # No records. Whether that is fine depends entirely on WHY.
    #
    #   recognised=True  -- we understand this feed and it simply had nothing
    #       for us: no PCT-primary traffic in this TAIS message, an RVR update
    #       for an airport outside scope, a departure from elsewhere. Normal.
    #
    #   recognised=False -- we do not understand this payload shape at all.
    #       Before this distinction existed, such a file took neither failure
    #       branch in scan_swim_folder(): it was moved to data/processed/ and
    #       logged status=processed with event_count=0. Three of the four feeds
    #       on the live subscription (TDES, APDS, SMES) hit that path, so most
    #       of the feed was discarded while the ingest counters and dashboard
    #       both showed healthy.
    #
    # Raising routes the file to data/failed/ with a diagnostic naming the feed
    # and the real payload root -- which is what tells you what to write next.
    if recognised:
        return []

    root_name, _ = _drill_payload(body)
    shape = (
        list(body.keys())[:6] if isinstance(body, dict)
        else type(body).__name__
    )
    raise ValueError(
        f"Unrecognised STDDS payload shape (mex={headers.get('mex', '?')}, "
        f"msgType={headers.get('msgType', '?')}): root={root_name or 'unknown'}, "
        f"top-level keys={shape}. No extractor handles this feed yet -- "
        f"see _extract_records() in src/swim_file_ingest.py."
    )


def _jms_headers_only_record(headers: dict[str, str], path: Path) -> dict:
    """Fallback when there's no JSON body -- synthesize a metadata-only event."""
    return {
        "airport_code": _normalise_icao(
            headers.get("tracon") or headers.get("srcTracon") or "PCT"
        ),
        "tracon": headers.get("tracon", "PCT"),
        "service_name": _service_from_mex(headers.get("mex", "")),
        "message_type": headers.get("msgType", "unknown"),
        "event_time_utc": headers.get("safaRcvTimestamp") or headers.get("timestamp"),
    }


def _service_from_mex(mex: str) -> str:
    """Map JMS 'mex' (message exchange) header to a supported service_name."""
    mex_up = (mex or "").upper()
    return {
        "TAIS": "TERMINAL_AUTO",
        "SMES": "SMES",
        "APDS": "APDS_RVR",
        "APDS_RVR": "APDS_RVR",
        "ISMC": "ISMC",
        "TFDM": "TOWER_DEPARTURE",
        # The live STDDS subscription sends TDES (Tower Data Exchange /
        # TDLS), not TFDM. Without this it fell through to the
        # TERMINAL_AUTO default, mislabelling every departure-clearance
        # message as terminal-automation data. Confirmed against real
        # captured messages: mex=TDES, root ns5:TowerDepartureEventMessage.
        "TDES": "TOWER_DEPARTURE",
    }.get(mex_up, "TERMINAL_AUTO")


def _derive_flight_phase(
    altitude_ft: int | None,
    v_vert: int | None,
    this_airport: str,
    departure: str,
    destination: str,
) -> str:
    """
    Heuristic flight-phase classifier from track data.

    GROUND     -- altitude < 500 ft (taxiing / on surface)
    DEPARTURE  -- climbing AND this airport is the departure
    ARRIVAL    -- descending AND this airport is the destination
    ENROUTE    -- level flight at cruise altitude
    UNKNOWN    -- insufficient signal
    """
    if altitude_ft is None:
        return "UNKNOWN"
    if altitude_ft < 500:
        return "GROUND"
    if v_vert is not None:
        if v_vert > 300 and (departure == this_airport or departure == "PCT"):
            return "DEPARTURE"
        if v_vert < -300 and (destination == this_airport or destination == "PCT"):
            return "ARRIVAL"
        if v_vert > 500:
            return "DEPARTURE"
        if v_vert < -500:
            return "ARRIVAL"
    if altitude_ft >= 10000:
        return "ENROUTE"
    if destination == this_airport:
        return "ARRIVAL"
    if departure == this_airport:
        return "DEPARTURE"
    return "ENROUTE"


def _normalise_icao(code: str | None) -> str:
    """Ensure an airport identifier is a K-prefixed ICAO (US convention)."""
    if not code:
        return "PCT"
    c = code.strip().upper()
    if len(c) == 3 and c.isalpha():
        return f"K{c}"
    return c


def _drill_payload(body: dict) -> tuple[str, dict]:
    """
    Strip the namespace-prefixed root element and return (root_name, payload).

    Every STDDS message arrives as a single-key object whose key names the
    message type, e.g. "ns2:RVRDataUpdateMessage" -> the RVR payload. The root
    name is the most reliable discriminator between feeds, so unlike the
    previous drill-and-discard it is kept.
    """
    if isinstance(body, dict) and len(body) == 1:
        root_name = next(iter(body.keys()))
        inner = body[root_name]
        if isinstance(inner, dict):
            return root_name, inner
    return "", body if isinstance(body, dict) else {}


def _pct_primary_from(*candidates: str | None) -> str | None:
    """
    Return the first candidate identifier that is a PCT primary airport.

    Deliberately checks EVERY candidate rather than normalising the first
    non-empty one and then testing it. A flight from HGR into KDCA carries
    flightPlan.airport="HGR" (its origin) alongside
    enhancedData.destinationAirport="KDCA"; first-wins logic normalised "HGR"
    to "KHGR", failed the primary-airport test, and dropped an arrival into
    National.
    """
    for candidate in candidates:
        if not candidate:
            continue
        code = _normalise_icao(candidate)
        if code in _PCT_PRIMARY_AIRPORTS:
            return code
    return None


def _coerce_int(value: Any) -> int | None:
    """STDDS uses "" for an unavailable numeric field; treat that as absent."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_records(
    headers: dict[str, str],
    body: dict,
    path: Path,
) -> tuple[list[dict], bool]:
    """
    Turn one STDDS message into NASEvent dicts.

    Returns (records, recognised). The second element matters: an empty list
    from a feed we understand (no PCT-primary traffic in this message) is a
    normal outcome, while an empty list from a shape we do not understand is
    silent data loss and must be surfaced by the caller.
    """
    root_name, payload = _drill_payload(body)

    if "TATrackAndFlightPlan" in root_name or "record" in payload:
        return _extract_tais(headers, payload), True

    if "RVRDataUpdateMessage" in root_name or "runwayData" in payload:
        return _extract_apds_rvr(headers, payload), True

    if "TowerDepartureEventMessage" in root_name or "departureAirport" in payload:
        return _extract_tdes(headers, payload), True

    if "TDLSCSPMessage" in root_name:
        return _extract_tdls_dcl(headers, payload), True

    if "asdexMsg" in root_name or "adsbReport" in payload:
        return _extract_smes(headers, payload), True

    if "SafetyLogicHoldBar" in root_name:
        return _extract_smes_holdbar(headers, payload), True

    return [], False


def _common_fields(headers: dict[str, str]) -> tuple[str, str, str | None]:
    """tracon, service_name and the header-derived timestamp shared by all feeds."""
    return (
        headers.get("tracon") or headers.get("srcTracon") or "PCT",
        _service_from_mex(headers.get("mex", "")),
        headers.get("safaRcvTimestamp") or headers.get("timestamp"),
    )


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

    airport = _pct_primary_from(payload.get("airport"), headers.get("airport"))
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


def _scale_rvr(value: Any) -> int | None:
    """
    Convert a wire RVR reading to feet via config.APDS_RVR_SCALE_FT.

    The scale factor is corroborated but not confirmed against the APDS ICD;
    see APDS_RVR_SCALE_FT in src/config.py for the evidence. A result above
    config.APDS_RVR_MAX_PLAUSIBLE_FT is physically impossible for RVR, so it
    is treated as proof the scale is wrong and the message is refused, rather
    than stored as a reading far above every alert threshold.
    """
    raw = _coerce_int(value)
    if raw is None:
        return None
    feet = raw * config.APDS_RVR_SCALE_FT
    if feet > config.APDS_RVR_MAX_PLAUSIBLE_FT:
        raise ValueError(
            f"APDS RVR wire value {raw} scales to {feet} ft, above the "
            f"{config.APDS_RVR_MAX_PLAUSIBLE_FT} ft maximum any RVR system "
            f"reports. APDS_RVR_SCALE_FT={config.APDS_RVR_SCALE_FT} is probably "
            f"wrong for this feed; see src/config.py."
        )
    return feet


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

    airport = _pct_primary_from(
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


def _parse_tdls_time(value: Any) -> str | None:
    """
    TDLS "time" is MMDDYYYYHHMMSS in UTC with no separators, e.g.
    "08272026204225" -> "2026-08-27T20:42:25Z" (cross-checked against the
    capture's own JMS timestamp header, 2026-08-27T20:42:25.503Z). Anything
    that does not parse returns None so the caller falls back to the header
    time rather than inventing one.
    """
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%m%d%Y%H%M%S")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


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

    airport = _pct_primary_from(
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

    airport = _pct_primary_from(payload.get("airport"), headers.get("airport"))
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
# The ASDE-X schema defines others (positionReport among them), but none has
# appeared in captured traffic, so their field layout is unconfirmed.
_SMES_VERIFIED_REPORTS = frozenset({"adsbReport"})
_SMES_NON_REPORT_KEYS = frozenset({"airport"})


def _coerce_float(value: Any) -> float | None:
    """STDDS numbers can arrive as JSON numbers or strings; "" means absent."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_smes(headers: dict[str, str], payload: dict) -> list[dict]:
    """
    ns2:asdexMsg -> one event per ADS-B surface report.

    Shape (confirmed against tests/fixtures/swim_kit/smes_sample, a real
    captured message):

        { "airport": "KIAD",
          "adsbReport": [
            { "enhancedData": { "eramGufi": "DA981441pZ" },
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

    A message carrying any report type other than adsbReport is refused
    rather than partly ingested. Guessing at an unseen layout could store
    wrong positions, and silently extracting only the ADS-B half would drop
    the rest while reporting success. Refused files land in data/failed/
    with the unverified keys named, and can be re-ingested once supported.
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

    airport = _pct_primary_from(payload.get("airport"), headers.get("airport"))
    if airport is None:
        return []

    reports = payload.get("adsbReport") or []
    if isinstance(reports, dict):
        reports = [reports]

    records = []
    for report in reports:
        basic = ((report or {}).get("report") or {}).get("basicReport") or {}
        position = basic.get("position") or {}
        track = basic.get("track")
        records.append({
            "airport_code": airport,
            "tracon": tracon,
            "service_name": service_name,
            "message_type": f"SMES_{msg_type}",
            "event_time_utc": basic.get("time") or header_ts,
            "track_id": str(track) if track not in (None, "") else None,
            "lat": _coerce_float(position.get("lat")),
            "lon": _coerce_float(position.get("lon")),
            "surface_event_type": "ADSB_REPORT",
            "feed_status": "operational",
        })
    return records


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
    origin was discarded. _pct_primary_from() now tests every candidate.
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

        airport = _pct_primary_from(
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
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "altitude_ft": altitude_ft,
            "ground_speed_kt": ground_speed_kt,
            "vertical_speed_fpm": v_vert,
            "flight_phase": flight_phase,
            "flight_rules": fp.get("flightRules"),
            "beacon_code": str(beacon) if beacon not in (None, "") else None,
            "feed_status": "operational",
        })

    return out


def _parse_file(path: Path) -> tuple[list[dict], str | None]:
    """Route to the right parser by extension. Returns (records, error)."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".json":
            return _parse_json_file(path), None
        elif suffix == ".txt":
            return _parse_txt_file(path), None
        elif suffix == ".xml":
            return _parse_xml_file(path), None
        elif suffix == "":
            # Extension-less = Kit's MessageFileOutput format
            return _parse_jms_message_file(path), None
        else:
            return [], f"Unsupported extension: {path.suffix}"
    except Exception as exc:
        return [], str(exc)


# Record normaliser 

def _normalise_record(raw: dict, source_file: str) -> tuple[NASEvent | None, str | None]:
    """
    Coerce a raw dict into a NASEvent.
    Injects received_time_utc = now so every live-ingested event has a receipt stamp.
    Returns (event, error_string).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    raw = dict(raw)

    # Coerce ISO timestamp strings; strip timezone info to keep all datetimes naive UTC
    for ts_field in ("event_time_utc", "received_time_utc"):
        if ts_field in raw and isinstance(raw[ts_field], str):
            try:
                # STDDS timestamps are Zulu-suffixed (2026-08-27T20:42:16.000Z).
                # datetime.fromisoformat() only learned to accept a trailing Z in
                # Python 3.11; on 3.10 it raises ValueError, and because
                # event_time_utc is load-bearing that rejected EVERY timestamped
                # STDDS message. The container runs 3.12 so it happened to work
                # there, but a local run on 3.10 silently dropped the entire feed.
                # Normalising the suffix makes the parse version-independent.
                ts_text = raw[ts_field].strip()
                if ts_text.endswith(("Z", "z")):
                    ts_text = ts_text[:-1] + "+00:00"
                parsed = datetime.fromisoformat(ts_text)
                # Replace timezone-aware datetime with its naive UTC equivalent
                if parsed.tzinfo is not None:
                    parsed = parsed.utctimetuple()
                    parsed = datetime(*parsed[:6])
                raw[ts_field] = parsed
            except ValueError:
                # event_time_utc is load-bearing: latency_ms is derived from it and
                # drives ALERT_HIGH_LATENCY. Dropping it here would hand the record to
                # the `now` fallback below, fabricating latency_ms == 0 and making a
                # stale message look perfectly fresh. Fail the record instead.
                if ts_field == "event_time_utc":
                    return None, f"invalid event_time_utc: {raw[ts_field]!r}"
                # received_time_utc is overwritten with `now` on the live-ingest path,
                # so a malformed inbound value is safe to discard.
                raw.pop(ts_field, None)

    # Live-ingest always stamps the received time
    raw["received_time_utc"] = now

    # Derive event time if missing. A present-but-None value (a headers-only
    # record with no timestamp header) counts as missing; checking only for the
    # key let None through to the subtraction below and raised TypeError.
    if raw.get("event_time_utc") is None:
        raw["event_time_utc"] = now

    # Calculate latency
    delta = raw["received_time_utc"] - raw["event_time_utc"]
    raw["latency_ms"] = delta.total_seconds() * 1_000

    # Fill in tracon default for PCT if not present
    if "tracon" not in raw:
        raw["tracon"] = config.SUPPORTED_TRACON

    raw["raw_message_path"] = source_file

    # Coerce integer RVR fields if they arrived as strings
    for rvr_field in ("rvr_touchdown", "rvr_midpoint", "rvr_rollout"):
        if rvr_field in raw and isinstance(raw[rvr_field], str):
            try:
                raw[rvr_field] = int(raw[rvr_field])
            except ValueError:
                raw.pop(rvr_field, None)

    try:
        return NASEvent(**raw), None
    except Exception as exc:
        return None, str(exc)


# ── Public API ────────────────────────────────────────────────────────────────

class IngestStats:
    """Mutable stats object accumulated during a scan pass."""
    def __init__(self):
        self.files_detected: int = 0
        self.files_processed: int = 0
        self.files_failed: int = 0
        self.files_skipped: int = 0
        self.events_parsed: int = 0
        self.latest_file_time: datetime | None = None
        self.latest_service: str | None = None
        self.scan_time: datetime = datetime.now(timezone.utc).replace(tzinfo=None)

    def as_dict(self) -> dict:
        return {
            "scan_time": self.scan_time.isoformat(),
            "files_detected": self.files_detected,
            "files_processed": self.files_processed,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "events_parsed": self.events_parsed,
            "latest_file_time": self.latest_file_time.isoformat() if self.latest_file_time else None,
            "latest_service": self.latest_service,
        }


def scan_swim_folder(
    source_dir: Path | None = None,
    max_files: int = 500,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
) -> tuple[list[NASEvent], IngestStats]:
    """
    Scan source_dir for new STDDS message files, parse them, and return
    NASEvents ready for the validation + alert pipeline.

    Files are moved to processed_dir on success or failed_dir on error.
    The ingest_log table is updated with every file touched.
    Already-processed files (tracked by name in ingest_log) are skipped.

    processed_dir/failed_dir default to the module-level PROCESSED_DIR and
    FAILED_DIR (i.e. data/processed and data/failed). They are parameters
    rather than hardcoded globals so a caller -- in practice a test -- can
    point them at a tmp_path and get real isolation. Reading them from
    globals meant every test shared data/processed/, so runs polluted the
    repo and interfered with each other; the tests in
    tests/test_swim_file_ingest.py were written against this signature
    before it existed.
    """
    source_dir = source_dir or (config.DATA_DIR / "raw" / "swim_live")
    processed_dir = processed_dir or PROCESSED_DIR
    failed_dir = failed_dir or FAILED_DIR
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    stats = IngestStats()
    all_events: list[NASEvent] = []

    # Dotfiles are skipped: the kit writes extensionless files, so "" is in
    # SCAN_EXTENSIONS, and a dotfile such as the folder's .gitkeep has no
    # extension either -- it was being ingested as a message, and its empty
    # record crashed the watcher.
    all_candidates = [
        f for f in sorted(source_dir.iterdir())
        if f.is_file()
        and not f.name.startswith(".")
        and f.suffix.lower() in SCAN_EXTENSIONS
    ]
    stats.files_detected = len(all_candidates)

    already_processed = _get_already_processed([f.name for f in all_candidates])

    # Separate already-processed files and account for them in skipped stats
    unprocessed = [f for f in all_candidates if f.name not in already_processed]
    stats.files_skipped = len(all_candidates) - len(unprocessed)

    # Cap files per scan so a backlog doesn't pin the pipeline for minutes.
    candidate_files = sorted(unprocessed, key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    
    # Account for files deferred due to the max_files backlog cap
    excess_backlog = len(unprocessed) - len(candidate_files)
    stats.files_skipped += excess_backlog

    if excess_backlog > 0:
        _log.warning(
            "swim_ingest_backlog_cap_exceeded",
            extra={
                "unprocessed_count": len(unprocessed),
                "max_files": max_files,
                "deferred_count": excess_backlog,
            },
        )

    for path in candidate_files:
        if _shutdown_requested.is_set():
            _log.warning(
                "swim_ingest_shutdown_drain",
                extra={
                    "files_processed": stats.files_processed,
                    "files_remaining": len(candidate_files) - stats.files_processed - stats.files_failed,
                },
            )
            break

        _log.info("swim_ingest_processing_file", extra={"file_name": path.name})
        raw_records, parse_error = _parse_file(path)

        if parse_error and not raw_records:
            _move_to_failed(path, parse_error, failed_dir)
            _append_log_entry({
                "file": path.name,
                "status": "failed",
                "error": parse_error,
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            })
            stats.files_failed += 1
            continue

        events: list[NASEvent] = []
        errors: list[str] = []
        for raw in raw_records:
            # One malformed record fails its own file, never the scan: an
            # exception escaping here used to abort scan_swim_folder(), and in
            # watch mode that crash-looped the whole nas-sentinel container.
            try:
                event, err = _normalise_record(raw, str(path))
            except Exception as exc:  # noqa: BLE001
                event, err = None, f"{type(exc).__name__} while normalising record: {exc}"
            if event:
                events.append(event)
                if event.service_name:
                    stats.latest_service = event.service_name
            else:
                errors.append(err or "Unknown normalisation error")

        if not events and errors:
            _move_to_failed(path, "; ".join(errors), failed_dir)
            _append_log_entry({
                "file": path.name,
                "status": "failed",
                "error": "; ".join(errors[:3]),
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            })
            stats.files_failed += 1
            continue

        # Move to processed
        dest = processed_dir / path.name
        if dest.exists():
            dest = processed_dir / f"{path.stem}_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%H%M%S%f')}{path.suffix}"
        try:
            shutil.move(str(path), str(dest))
        except OSError as exc:
            # The file parsed fine; only the archival move failed. That is the
            # normal case on a read-only ingest mount (a :ro volume, or a
            # network share the container cannot write to), and it must not
            # abort the scan or discard events we already extracted.
            #
            # _move_to_failed() has always caught this; the success path did
            # not, so one unwritable mount raised OSError straight out of
            # scan_swim_folder and killed the whole pass.
            #
            # The file stays where it is. The ingest_log entry written below
            # is what stops it being reprocessed on the next scan, so
            # leaving it in place is safe rather than a duplicate-ingest risk.
            _log.warning(
                "swim_ingest_archive_move_failed",
                extra={
                    "file_name": path.name,
                    "destination": str(dest),
                    "error": str(exc),
                },
            )

        services = list({e.service_name for e in events if e.service_name})
        _append_log_entry({
            "file": path.name,
            "status": "processed",
            "event_count": len(events),
            "services": services,
            "parse_errors": len(errors),
            "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        })

        stats.files_processed += 1
        stats.events_parsed += len(events)
        stats.latest_file_time = datetime.now(timezone.utc).replace(tzinfo=None)
        all_events.extend(events)
        _log.info(
        "swim_ingest_file_success",
        extra={
            "file_name": path.name,
            "event_count": len(events),
            "parse_errors": len(errors),
            },
        )
        if stats.files_detected == 0:
            _log.info("swim_ingest_no_new_files", extra={"source_dir": str(source_dir)})
        else:
            _log.info(
                "swim_ingest_scan_complete",
                extra=stats.as_dict(),
            )

    return all_events, stats


class _NewFileHandler(FileSystemEventHandler):
    """Sets a threading.Event on any file arriving in the watched folder.

    Deliberately dumb: it doesn't care what the file is or try to parse it
    inline (the watcher thread firing this callback shouldn't be blocked doing
    file I/O). It just flags "something changed" and lets the watch loop
    debounce and hand off to the existing scan_swim_folder() pass, which
    already knows how to parse/validate/move/log a file correctly.
    """

    def __init__(self, trigger: threading.Event) -> None:
        self._trigger = trigger

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._trigger.set()

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._trigger.set()


def watch_swim_folder(
    source_dir: Path | None = None,
    max_files: int = 500,
    reconcile_interval: float = 300.0,
    debounce_seconds: float = 2.0,
    on_events=None,
) -> None:
    """
    Long-running watcher for source_dir: reacts to new files as they land
    (fast path, via filesystem events) and re-scans on a fixed interval
    regardless (slow safety-net path) to catch anything the fast path missed
    -- events dropped by the OS, a network-mounted folder where inotify-style
    watching isn't reliable, or a backlog that existed before this process
    started.

    on_events, if given, is called as on_events(events, stats) after every
    scan pass that ingested at least one event -- this is how the caller
    hooks the downstream validate/alert/critic/HITL pipeline in without this
    module needing to import main.py (scan_swim_folder() only parses, moves,
    and logs files; it doesn't run that pipeline itself).

    Runs until request_shutdown() is called (installed as the SIGTERM/SIGINT
    handler in main.py) or the process is interrupted. A single in-process
    lock keeps the fast-path and safety-net scans from ever overlapping --
    scan_swim_folder() moves files and appends to a shared audit log, so two
    concurrent passes over the same folder would race each other.
    """
    source_dir = source_dir or (config.DATA_DIR / "raw" / "swim_live")
    source_dir.mkdir(parents=True, exist_ok=True)

    scan_lock = threading.Lock()
    trigger = threading.Event()

    def _run_scan(reason: str) -> None:
        if not scan_lock.acquire(blocking=False):
            # A scan is already in flight; whatever prompted this call will
            # be picked up by that scan (or the next reconciliation pass) --
            # safe to skip rather than queue up overlapping scans.
            return
        try:
            _log.info("swim_watch_scan_triggered", extra={"reason": reason})
            events, stats = scan_swim_folder(source_dir, max_files=max_files)
            if events and on_events is not None:
                on_events(events, stats)
        finally:
            scan_lock.release()

    # Native file events do not cross a Docker Desktop bind mount from a
    # Windows or macOS host into a Linux container: the kit writes on the
    # host, no inotify event fires, and a new message waited for the next
    # reconcile pass (up to reconcile_interval, 5 minutes). Polling the
    # folder works everywhere; SWIM_WATCH_POLLING turns it on.
    observer = (
        PollingObserver(timeout=config.SWIM_WATCH_POLL_SECONDS)
        if config.SWIM_WATCH_POLLING else Observer()
    )
    observer.schedule(_NewFileHandler(trigger), str(source_dir), recursive=False)
    observer.start()
    _log.info(
        "swim_watch_started",
        extra={"source_dir": str(source_dir), "reconcile_interval": reconcile_interval},
    )

    # Catch up immediately on anything already sitting in the folder (or that
    # arrived in the gap before the observer thread was ready) rather than
    # waiting for the first event or the first reconcile tick.
    _run_scan("startup")
    last_reconcile = time.monotonic()

    try:
        while not _shutdown_requested.is_set():
            fired = trigger.wait(timeout=1.0)
            if _shutdown_requested.is_set():
                break
            if fired:
                trigger.clear()
                # Coalesce a burst of near-simultaneous events (a batch of
                # files landing together) into a single scan instead of one
                # per file.
                time.sleep(debounce_seconds)
                trigger.clear()
                _run_scan("fs_event")
                last_reconcile = time.monotonic()
            elif time.monotonic() - last_reconcile >= reconcile_interval:
                _run_scan("reconcile")
                last_reconcile = time.monotonic()
    finally:
        observer.stop()
        observer.join(timeout=5.0)
        _log.info("swim_watch_stopped")


def read_ingest_log(limit: int = 100) -> list[dict]:
    """Return the most recent ingest log entries (newest first)."""
    return run_async(storage.get_recent_ingest_log(limit))


def _move_to_failed(path: Path, reason: str, failed_dir: Path | None = None) -> None:
    failed_dir = failed_dir or FAILED_DIR
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / path.name
    if dest.exists():
        dest = failed_dir / f"{path.stem}_{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%H%M%S%f')}{path.suffix}"
    try:
        shutil.move(str(path), str(dest))
        _log.warning(
            "swim_ingest_file_failed",
            extra={"file_name": path.name, "reason": reason},
        )
    except OSError as exc:
        _log.error(
            "swim_ingest_move_failed",
            extra={"file_name": path.name, "error": str(exc)},
        )