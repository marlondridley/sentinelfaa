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
import os
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from pathlib import Path
from typing import Any, Iterator

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src import config, storage
from src.async_bridge import run_async
from src.metrics import SWIM_FOLDER_BACKLOG, SWIM_FOLDER_OLDEST_WAITING_SECONDS, SWIM_MESSAGES_WITHHELD
from src.models import NASEvent, content_key, stable_id
from src.raw_message import (
    ENVELOPE_EXTENSION,
    PROPERTY_RE,
    Delivery,
    PollBatch,
    RawMessage,
    RawMessageSource,
    message_from_envelope,
    message_from_kit_text,
)
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
SCAN_EXTENSIONS: set[str] = {".json", ".xml", ".txt", ENVELOPE_EXTENSION, ""}

# The Jumpstart kit names each file with its arrival time in epoch millis.
_ARRIVAL_MILLIS = re.compile(r"(\d{12,})$")

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


def _append_log_entries(entries: list[dict]) -> None:
    if entries:
        run_async(storage.record_ingest_log_entries(entries))


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

_JMS_PROPERTY_RE = PROPERTY_RE   # the pattern lives with the codec, src/raw_message.py


class WithheldMessage(Exception):
    """
    Raised by the parser for a message this system must not ingest. The scan
    archives the file and logs it as withheld: it is neither data nor a failure.
    """


def _honor_block_at_faa(headers: dict[str, str]) -> None:
    """
    Refuse a message the FAA flagged blockAtFAA=true.

    Every STDDS message on this subscription (TAIS, TDES, APDS, SMES) carries
    the blockAtFAA JMS property, and its value is also a level of the Solace
    routing topic (TAIS/all/false/FP/PCT/PCT) -- the FAA routes on it. Its
    exact semantics are in each service's JMS description document, which is
    not public, and every captured message says false. The name and the
    routing settle the handling anyway: a message marked true was not meant
    to leave the FAA, so if one ever reaches this consumer, nothing from it is
    stored, shown, or sent to a model.

    Absent is not flagged: the kit writes JMS properties only when run with
    -Dheaders=true, and a message without them cannot carry the flag. Any
    value other than true/false fails the file into data/failed/ rather than
    guessing which way it was meant.
    """
    raw = headers.get("blockAtFAA")
    if raw is None:
        return
    value = raw.strip().lower()
    if value == "false":
        return
    if value == "true":
        raise WithheldMessage("blockAtFAA=true")
    raise ValueError(f"Unrecognised blockAtFAA value {raw!r}; message not ingested")


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
    return records_from_raw(message_from_kit_text(text, path.name, _receipt_time(path)), path)


def records_from_raw(message: RawMessage, path: Path | None = None) -> list[dict]:
    """
    Turn one raw SWIM message into normalised record dicts: the STDDS understanding
    starts here. Every source shares this -- a file of the stock kit format, a record
    from the Java edge, or a broker delivery all arrive as a RawMessage.

    Returns one record per aircraft track in the payload (TAIS messages typically
    contain many). Headers are merged into each record so downstream knows tracon,
    msgType, timestamp, etc.
    """
    path = path or Path(message.source_name or message.message_id)
    headers = message.headers

    # Before anything is read out of the message, including the headers-only
    # fallback below.
    _honor_block_at_faa(headers)

    if message.convert_error:
        # The edge could not turn the payload into JSON and kept the text as received:
        # that is a data problem to surface, not a message to guess at.
        raise ValueError(
            f"the payload could not be converted to JSON by the kit ({message.convert_error}); "
            "the original text is kept in the record")

    body_text = message.body.strip()
    if not body_text:
        # No payload; emit one summary record from headers alone
        return [_jms_headers_only_record(headers, path)]

    try:
        body = json.loads(body_text)
    except json.JSONDecodeError:
        return [_jms_headers_only_record(headers, path)]

    records, recognised = _extract_records(headers, body, path)
    if records:
        # The message's own identity on the SWIM stream. Every record from one
        # message shares it; the scan loop adds each record's position.
        if headers.get("globalID") or headers.get("msgSeqID"):
            key = f"{headers.get('mex', '')}:{headers.get('globalID', '')}:{headers.get('msgSeqID', '')}"
            for record in records:
                record["_source_key"] = key
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
            headers.get("tracon") or headers.get("srcTracon") or config.SUPPORTED_TRACON
        ),
        "tracon": headers.get("tracon", config.SUPPORTED_TRACON),
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
        # A flight plan names the TRACON itself when the airport is not filed.
        if v_vert > 300 and (departure == this_airport or departure in config.SUPPORTED_TRACONS):
            return "DEPARTURE"
        if v_vert < -300 and (destination == this_airport or destination in config.SUPPORTED_TRACONS):
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
        return config.SUPPORTED_TRACON
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


def _primary_airport_from(*candidates: str | None) -> str | None:
    """
    Return the first candidate identifier that is a supported (in-scope) airport.

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
        if code in config.SUPPORTED_AIRPORTS:
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

    if "SurfaceMovementEventMessage" in root_name:
        return _extract_smes_event(headers, payload), True

    return [], False


def _common_fields(headers: dict[str, str]) -> tuple[str, str, str | None]:
    """tracon, service_name and the header-derived timestamp shared by all feeds."""
    return (
        headers.get("tracon") or headers.get("srcTracon") or config.SUPPORTED_TRACON,
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
    # 0 is a not-reporting code, not zero visibility. Live on 2026-09-25, KBWI
    # 10/28 and KIAD 01C/19C read 0 on both ends while every other runway at the
    # same airport read 5000-6000 ft, and flipped between 0 and 6000 from one
    # update to the next. Read as feet, it staged a CRITICAL ALERT_LOW_RVR per
    # update for an idle sensor.
    if raw is None or raw == 0:
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
        parsed = datetime.strptime(text, "%m%d%Y%H%M%S").replace(tzinfo=timezone.utc)
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


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _scalar(value: Any) -> Any:
    # A field that carries an XML attribute arrives as {"content": v, "r": 1}.
    return value.get("content") if isinstance(value, dict) else value


def _keep_position(time_text: Any) -> bool:
    """Deterministic sampling; see SMES_POSITION_SAMPLE_SECONDS in src/config.py."""
    period = config.SMES_POSITION_SAMPLE_SECONDS
    if period <= 1:
        return True
    try:
        when = _as_utc(datetime.fromisoformat(str(time_text).replace("Z", "+00:00")))
    except ValueError:
        return True  # kept, so normalisation rejects the bad time visibly
    return int(when.timestamp()) % period == 0


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
    ns2:asdexMsg -> one event per surface report.

    adsbReport shape (confirmed against tests/fixtures/swim_kit/smes_sample, a
    real captured message):

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
          "callsign": "ZXU339", "mode3ACode": 7056, "altitude": 418.75,
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
        elif suffix == ENVELOPE_EXTENSION:
            # A record written by the Java edge (IngestFolderFileOutput).
            text = path.read_text(encoding="utf-8")
            return records_from_raw(message_from_envelope(text, path.name), path), None
        elif suffix == "":
            # Extension-less = Kit's MessageFileOutput format
            return _parse_jms_message_file(path), None
        else:
            return [], f"Unsupported extension: {path.suffix}"
    except WithheldMessage:
        raise
    except Exception as exc:
        return [], str(exc)


# Record normaliser

def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _receipt_time(path: Path) -> datetime:
    # The kit writes each message the moment the broker delivers it, so the
    # file's write time is the receipt time. Stamping "now" at scan instead
    # counted every minute a file waited for the watcher (a restart, a backlog)
    # as feed latency. Never later than now, whatever the host clock says.
    now = datetime.now(timezone.utc)
    try:
        return min(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc), now)
    except OSError:
        return now


def _normalise_record(
    raw: dict, source_file: str, index: int | None = None, data_origin: str = "live",
    received_at: datetime | None = None,
) -> tuple[NASEvent | None, str | None]:
    """
    Coerce a raw dict into a NASEvent.

    received_time_utc is when the message reached this system: `received_at`
    (the scanner passes the kit's file write time) or, without one, now.
    Returns (event, error_string).
    """
    now = received_at or datetime.now(timezone.utc)
    raw = dict(raw)

    # Stable identity: the same message ingested twice is the same event.
    # Key = the SWIM message identity (mex:globalID:msgSeqID) or, lacking one,
    # the file name; plus the record's position in the message. Without a
    # position (a caller normalising one record), the record's content.
    source_key = raw.pop("_source_key", None) or Path(source_file).name
    # Set by whoever scans, never taken from the file's own content.
    raw["data_origin"] = data_origin
    if not raw.get("event_id"):
        raw["event_id"] = stable_id(
            "swim", source_key, index if index is not None else content_key(raw))

    # Every timestamp becomes UTC-aware; one without an offset is taken to be UTC.
    for ts_field in ("event_time_utc", "received_time_utc"):
        if isinstance(raw.get(ts_field), datetime):
            raw[ts_field] = _as_utc(raw[ts_field])
        elif isinstance(raw.get(ts_field), str):
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
                raw[ts_field] = _as_utc(datetime.fromisoformat(ts_text))
            except ValueError:
                # event_time_utc is load-bearing: latency_ms is derived from it and
                # drives ALERT_HIGH_LATENCY. Dropping it here would hand the record to
                # the `now` fallback below, fabricating latency_ms == 0 and making a
                # stale message look perfectly fresh. Fail the record instead.
                if ts_field == "event_time_utc":
                    return None, f"invalid event_time_utc: {raw[ts_field]!r}"
                # received_time_utc is overwritten with the receipt time below,
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
        self.files_withheld: int = 0
        self.events_parsed: int = 0
        self.latest_file_time: datetime | None = None
        self.latest_service: str | None = None
        self.scan_time: datetime = datetime.now(timezone.utc)
        # Messages parsed by a defer_archive scan, awaiting commit_scan():
        # (source, delivery, ingest_log entry).
        self.pending: list[tuple[RawMessageSource, Delivery, dict]] = []

    def as_dict(self) -> dict:
        return {
            "scan_time": self.scan_time.isoformat(),
            "files_detected": self.files_detected,
            "files_processed": self.files_processed,
            "files_failed": self.files_failed,
            "files_skipped": self.files_skipped,
            "files_withheld": self.files_withheld,
            "events_parsed": self.events_parsed,
            "latest_file_time": self.latest_file_time.isoformat() if self.latest_file_time else None,
            "latest_service": self.latest_service,
        }


class MessageDelivery:
    """
    A Delivery over a RawMessage: what a broker-backed source hands the driver, and
    what FolderSource hands over for a record file or a stock-kit file.
    """

    def __init__(self, message: RawMessage, ref: str | None = None):
        self.message = message
        self.name = message.source_name or message.message_id
        self.ref = ref or self.name
        self.received_at = message.received_at

    def records(self) -> tuple[list[dict], str | None]:
        try:
            return records_from_raw(self.message), None
        except WithheldMessage:
            raise
        except Exception as exc:  # noqa: BLE001 -- the reason is what the quarantine records
            return [], str(exc)


class FolderDelivery:
    """One claimed file of a Kit folder."""

    def __init__(self, path: Path, work: Path, received_at: datetime):
        self.path = path            # where it was
        self.work = work            # where it is now: claimed, in .inflight/
        self.name = path.name
        self.ref = str(path)
        self.received_at = received_at   # the file's write time; a record file states its own

    def records(self) -> tuple[list[dict], str | None]:
        if self.work.suffix.lower() != ENVELOPE_EXTENSION:
            return _parse_file(self.work)
        try:
            message = message_from_envelope(self.work.read_text(encoding="utf-8"), self.name)
        except (OSError, ValueError) as exc:
            return [], f"unreadable record: {exc}"
        # The edge stamped the arrival; never later than now, whatever the clocks say.
        self.received_at = min(message.received_at, datetime.now(timezone.utc))
        return MessageDelivery(message, self.ref).records()


def _arrival_order(path: Path) -> tuple[int, str]:
    """The kit names each file with its arrival time in epoch millis, so the name orders them without
    a stat per file; requeue_<millis> files sort by their original arrival, and a name with no such
    number sorts last."""
    match = _ARRIVAL_MILLIS.search(path.stem)
    return (int(match.group(1)) if match else 0, path.name)


class FolderSource:
    """
    A RawMessageSource over a folder of message files: the Kit's output folder.

    Everything that is about files lives here -- discovery, the already-handled check
    by name, claiming (one scanner per file), and the move to processed/ or failed/.
    Nothing below this class knows a message arrived as a file.
    """

    def __init__(self, source_dir: Path, processed_dir: Path, failed_dir: Path):
        self.source_dir = source_dir
        self.processed_dir = processed_dir
        self.failed_dir = failed_dir

    def poll(self, max_messages: int) -> PollBatch:
        _release_stale_claims(self.source_dir)
        all_candidates = message_files(self.source_dir)
        already_processed = _get_already_processed([f.name for f in all_candidates])

        unprocessed = [f for f in all_candidates if f.name not in already_processed]
        # Cap files per scan so a backlog doesn't pin the pipeline for minutes, and take the
        # newest first so the live picture stays current.
        by_arrival = sorted(((_arrival_order(f), f) for f in unprocessed), key=lambda kf: kf[0], reverse=True)
        candidates = [f for _, f in by_arrival[:max_messages]]
        # How far the backlog is from the janitor, which deletes by age: the oldest message still waiting.
        stamped = [key[0] for key, _ in by_arrival if key[0]]
        SWIM_FOLDER_BACKLOG.set(len(unprocessed))
        SWIM_FOLDER_OLDEST_WAITING_SECONDS.set(max(0.0, time.time() - min(stamped) / 1000) if stamped else 0.0)
        excess_backlog = len(unprocessed) - len(candidates)
        if excess_backlog > 0:
            _log.warning(
                "swim_ingest_backlog_cap_exceeded",
                extra={
                    "unprocessed_count": len(unprocessed),
                    "max_files": max_messages,
                    "deferred_count": excess_backlog,
                },
            )
        batch = PollBatch(
            planned=len(candidates),
            detected=len(all_candidates),
            skipped=(len(all_candidates) - len(unprocessed)) + excess_backlog,
        )
        batch.deliveries = self._claim_each(candidates, batch)
        return batch

    def _claim_each(self, paths: list[Path], batch: PollBatch) -> Iterator[FolderDelivery]:
        """Claim lazily, so a scan stopped part way leaves the rest in the folder, not stranded in .inflight/."""
        for path in paths:
            received_at = _receipt_time(path)  # before the claim, which re-stamps the file's mtime
            work = _claim(path)
            if work is None:
                batch.skipped += 1             # another scanner took it
                continue
            yield FolderDelivery(path, work, received_at)

    def commit(self, delivery: FolderDelivery) -> None:
        _archive_to_processed(delivery.work, delivery.path, self.processed_dir)

    def quarantine(self, delivery: FolderDelivery, reason: str) -> None:
        _move_to_failed(delivery.work, reason, self.failed_dir)


def ingest_batch(
    source: RawMessageSource,
    max_messages: int = 500,
    data_origin: str = "live",
    defer_commit: bool = False,
) -> tuple[list[NASEvent], IngestStats]:
    """
    Take what `source` has, parse it, and return NASEvents for the validation + alert
    pipeline. The one driver for every source: it commits a message to the source when
    its events are ready (or, with defer_commit, when the caller says they are stored:
    commit_scan) and quarantines one that can never succeed. The ingest_log table gets
    an entry for every message touched.
    """
    stats = IngestStats()
    all_events: list[NASEvent] = []
    batch = source.poll(max_messages)
    stats.files_detected = batch.detected

    log_entries: list[dict] = []  # written once, after the loop
    for delivery in batch.deliveries:
        if _shutdown_requested.is_set():
            _log.warning(
                "swim_ingest_shutdown_drain",
                extra={
                    "files_processed": stats.files_processed,
                    "files_remaining": batch.planned - stats.files_processed - stats.files_failed,
                },
            )
            break

        _log.info("swim_ingest_processing_file", extra={"file_name": delivery.name})
        try:
            raw_records, parse_error = delivery.records()
        except WithheldMessage as withheld:
            # Committed like any other message, so it is not delivered again; nothing
            # from it reaches the pipeline.
            source.commit(delivery)
            log_entries.append({
                "file": delivery.name,
                "status": "withheld",
                "event_count": 0,
                "error": str(withheld),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            stats.files_withheld += 1
            SWIM_MESSAGES_WITHHELD.inc()
            _log.warning(
                "swim_ingest_message_withheld",
                extra={"file_name": delivery.name, "reason": str(withheld)},
            )
            continue

        if parse_error and not raw_records:
            source.quarantine(delivery, parse_error)
            log_entries.append({
                "file": delivery.name,
                "status": "failed",
                "error": parse_error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            stats.files_failed += 1
            continue

        events: list[NASEvent] = []
        errors: list[str] = []
        for record_index, raw in enumerate(raw_records):
            # One malformed record fails its own message, never the scan: an
            # exception escaping here used to abort the scan, and in
            # watch mode that crash-looped the whole nas-sentinel container.
            try:
                event, err = _normalise_record(
                    raw, delivery.ref, index=record_index, data_origin=data_origin,
                    received_at=delivery.received_at)
            except Exception as exc:  # noqa: BLE001
                event, err = None, f"{type(exc).__name__} while normalising record: {exc}"
            if event:
                events.append(event)
                if event.service_name:
                    stats.latest_service = event.service_name
            else:
                errors.append(err or "Unknown normalisation error")

        if not events and errors:
            source.quarantine(delivery, "; ".join(errors))
            log_entries.append({
                "file": delivery.name,
                "status": "failed",
                "error": "; ".join(errors[:3]),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            stats.files_failed += 1
            continue

        services = list({e.service_name for e in events if e.service_name})
        entry = {
            "file": delivery.name,
            "status": "processed",
            "event_count": len(events),
            "services": services,
            "parse_errors": len(errors),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if defer_commit:
            stats.pending.append((source, delivery, entry))
        else:
            source.commit(delivery)
            log_entries.append(entry)

        stats.files_processed += 1
        stats.events_parsed += len(events)
        stats.latest_file_time = datetime.now(timezone.utc)
        all_events.extend(events)
        _log.info(
            "swim_ingest_file_success",
            extra={
                "file_name": delivery.name,
                "event_count": len(events),
                "parse_errors": len(errors),
            },
        )

    stats.files_skipped = batch.skipped       # final: includes claims another scanner won
    _append_log_entries(log_entries)
    if stats.files_detected == 0:
        _log.debug("swim_ingest_no_new_files")
    else:
        _log.info("swim_ingest_scan_complete", extra=stats.as_dict())

    return all_events, stats


def scan_swim_folder(
    source_dir: Path | None = None,
    max_files: int = 500,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
    data_origin: str = "live",
    defer_archive: bool = False,
) -> tuple[list[NASEvent], IngestStats]:
    """
    Scan source_dir for new STDDS message files, parse them, and return
    NASEvents ready for the validation + alert pipeline. This is ingest_batch()
    over a FolderSource.

    Files are moved to processed_dir on success or failed_dir on error.
    The ingest_log table is updated with every file touched.
    Already-processed files (tracked by name in ingest_log) are skipped.

    With defer_archive, a file that parsed is NOT archived or logged here: it
    stays claimed in .inflight/ until the caller has stored its events and calls
    commit_scan(stats). The events exist only in memory until then, so archiving
    first meant a failure in between (database down, a crash) left the file
    marked processed with nothing stored. A file left claimed is released after
    CLAIM_TIMEOUT_SECONDS and scanned again; stable event ids make that safe.

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
    return ingest_batch(
        FolderSource(source_dir, processed_dir, failed_dir),
        max_messages=max_files, data_origin=data_origin, defer_commit=defer_archive)

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
    and logs files; it doesn't run that pipeline itself). The files of a pass
    are archived and logged only after on_events returns: if it raises, they
    stay claimed, the failure is logged, the watcher keeps running, and the
    files are released and scanned again after CLAIM_TIMEOUT_SECONDS.

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

    def _run_scan(reason: str) -> int:
        """Scan once; returns how many files it consumed (0 if another scan held the lock)."""
        if not scan_lock.acquire(blocking=False):
            # A scan is already in flight; whatever prompted this call will
            # be picked up by that scan (or the next reconciliation pass) --
            # safe to skip rather than queue up overlapping scans.
            return 0
        try:
            (_log.debug if reason == "poll" else _log.info)("swim_watch_scan_triggered", extra={"reason": reason})
            events, stats = scan_swim_folder(source_dir, max_files=max_files, defer_archive=True)
            if events and on_events is not None:
                try:
                    on_events(events, stats)
                except Exception:  # noqa: BLE001 -- the events were not stored; keep watching
                    _log.exception("swim_pipeline_failed_files_left_claimed",
                                   extra={"files": len(stats.pending), "events": len(events)})
                    return 0
            commit_scan(stats)
            return stats.files_processed + stats.files_failed
        finally:
            scan_lock.release()

    # Native file events do not cross a Docker Desktop bind mount from a
    # Windows or macOS host into a Linux container: the kit writes on the
    # host, no inotify event fires, and a new message waited for the next
    # reconcile pass (up to reconcile_interval, 5 minutes). SWIM_WATCH_POLLING
    # polls instead -- by scanning. watchdog's PollingObserver was used here,
    # but it snapshots the folder by stat-ing every file every poll: ~27 s per
    # poll at a 24,000-file backlog on the host mount, running beside the scan
    # and starving it. A scan already lists the folder in one directory read.
    observer = None
    if not config.SWIM_WATCH_POLLING:
        observer = Observer()
        observer.schedule(_NewFileHandler(trigger), str(source_dir), recursive=False)
        observer.start()
    _log.info(
        "swim_watch_started",
        extra={"source_dir": str(source_dir), "reconcile_interval": reconcile_interval,
               "polling": observer is None},
    )

    # Catch up immediately on anything already sitting in the folder (or that
    # arrived in the gap before the observer thread was ready) rather than
    # waiting for the first event or the first reconcile tick.
    _run_scan("startup")
    last_reconcile = time.monotonic()

    try:
        while not _shutdown_requested.is_set():
            if observer is None:
                # Straight back in while a backlog remains; otherwise idle one poll.
                if not _run_scan("poll"):
                    _shutdown_requested.wait(config.SWIM_WATCH_POLL_SECONDS)
                continue
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
        if observer is not None:
            observer.stop()
            observer.join(timeout=5.0)
        _log.info("swim_watch_stopped")


def message_files(source_dir: Path) -> list[Path]:
    """
    The messages waiting in a Kit folder: what a scan would take.

    Dotfiles are skipped: the kit writes extensionless files, so "" is in
    SCAN_EXTENSIONS, and a dotfile such as the folder's .gitkeep has no
    extension either -- it was being ingested as a message, and its empty
    record crashed the watcher. The Kit's own messages.log is not a message.

    scandir, not iterdir + is_file: the entry type comes with the directory
    read. On the Windows host bind mount every stat is a round trip, and
    stat-ing a 22,000-file backlog took ~27 s per scan -- longer than the
    scan's own work, so the backlog could only grow.
    """
    with os.scandir(source_dir) as entries:
        return [
            Path(e.path) for e in entries
            if e.is_file(follow_symlinks=False)
            and not e.name.startswith(".")
            and Path(e.name).suffix.lower() in SCAN_EXTENSIONS
        ]


def commit_scan(stats: IngestStats) -> int:
    """
    Archive and log the files a defer_archive scan parsed, now that their events
    are stored. Returns how many were committed. Call it only after the pipeline
    has succeeded; on failure leave the files claimed and they are scanned again.
    """
    pending, stats.pending = stats.pending, []
    for source, delivery, _entry in pending:
        source.commit(delivery)
    _append_log_entries([entry for *_rest, entry in pending])
    return len(pending)


def read_ingest_log(limit: int = 100) -> list[dict]:
    """Return the most recent ingest log entries (newest first)."""
    return run_async(storage.get_recent_ingest_log(limit))


def read_failure_reasons(limit: int = 10) -> list[dict]:
    """Failed ingest attempts grouped by their recorded reason, most frequent first."""
    return run_async(storage.get_ingest_failure_reasons(limit))


# ── Single consumer per file ──────────────────────────────────────────────────
#
# More than one process can scan the same folder: the live watcher and a
# Temporal ingest run, or two watchers after a misconfiguration. Before this,
# both could parse the same file, race to move it, and ingest it twice.
#
# A scanner now claims a file before touching it: an atomic rename into the
# folder's hidden .inflight/ subdirectory, keeping its name. A rename can only
# succeed once, so every other scanner finds the file gone and skips it -- this
# works across processes and containers sharing a mount, with no lock server.
# A claim left behind by a scanner that died is released after CLAIM_TIMEOUT.

INFLIGHT_DIR = ".inflight"
CLAIM_TIMEOUT_SECONDS = 600


def _claim(path: Path) -> Path | None:
    """
    Atomically take ownership of `path`; returns the path to read from, or None
    if the file is not ours this pass.

    A folder this process cannot write (a read-only mount) cannot be consumed
    destructively by anyone, so its files are read in place, exactly as before
    claims existed; the ingest_log and stable event ids stop re-ingestion.
    """
    if not os.access(path.parent, os.W_OK):
        return path
    inflight = path.parent / INFLIGHT_DIR
    claimed = inflight / path.name
    try:
        inflight.mkdir(exist_ok=True)
        os.rename(path, claimed)
    except FileNotFoundError:
        return None              # another scanner claimed it first
    except OSError:
        return None              # e.g. the writer on a Windows host still holds it; next pass
    os.utime(claimed)            # claim time, for stale-claim release
    return claimed


def _release_stale_claims(source_dir: Path) -> int:
    """Return files claimed by a scanner that died back to the folder."""
    inflight = source_dir / INFLIGHT_DIR
    if not inflight.is_dir():
        return 0
    released = 0
    cutoff = time.time() - CLAIM_TIMEOUT_SECONDS
    for f in inflight.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff and not (source_dir / f.name).exists():
                os.rename(f, source_dir / f.name)
                released += 1
        except OSError:
            continue
    if released:
        _log.warning("swim_ingest_stale_claims_released", extra={"count": released, "source_dir": str(source_dir)})
    return released


def _archive_to_processed(work: Path, path: Path, processed_dir: Path) -> None:
    """Move a finished file (claimed as `work`, originally `path`) into processed_dir."""
    dest = processed_dir / path.name
    if dest.exists():
        dest = processed_dir / f"{path.stem}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}{path.suffix}"
    try:
        shutil.move(str(work), str(dest))
    except OSError as exc:
        # The file was handled (ingested, or withheld); only the archival move
        # failed. That is the normal case on a read-only ingest mount (a :ro
        # volume, or a network share the container cannot write to), and it
        # must not abort the scan or discard events we already extracted.
        #
        # _move_to_failed() has always caught this; the success path did
        # not, so one unwritable mount raised OSError straight out of
        # scan_swim_folder and killed the whole pass.
        #
        # The file stays where it is. The ingest_log entry the caller writes
        # is what stops it being reprocessed on the next scan, so
        # leaving it in place is safe rather than a duplicate-ingest risk.
        # Put a claimed file back where it was, so the next scan sees it
        # and skips it by name instead of it sitting in .inflight/.
        if work != path:
            try:
                os.rename(work, path)
            except OSError:
                pass
        _log.warning(
            "swim_ingest_archive_move_failed",
            extra={
                "file_name": path.name,
                "destination": str(dest),
                "error": str(exc),
            },
        )


def _move_to_failed(path: Path, reason: str, failed_dir: Path | None = None) -> None:
    failed_dir = failed_dir or FAILED_DIR
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / path.name
    if dest.exists():
        dest = failed_dir / f"{path.stem}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}{path.suffix}"
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