"""
From a file, or a RawMessage, to the raw records in it: the .json / .txt / .xml / Kit-format readers, the
blockAtFAA withholding rule, and the dispatch to each feed's extractor.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as SafeET

from src import config
from src.raw_message import ENVELOPE_EXTENSION, RawMessage, message_from_envelope, message_from_kit_text
from src.swim_common import _drill_payload, _service_from_mex
from src.swim_feed_apds import _extract_apds_rvr
from src.swim_feed_smes import _extract_smes, _extract_smes_event, _extract_smes_holdbar
from src.swim_feed_tais import _extract_tais
from src.swim_feed_tdes import _extract_tdes, _extract_tdls_dcl
from src.swim_normalise import _receipt_time

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

    Parsed with defusedxml: these files come from an external broker, so a
    DTD, entity declaration or external reference is refused outright (the
    file is quarantined with that reason) rather than left to the stdlib
    parser's defaults to bound.
    """
    tree = SafeET.parse(path, forbid_dtd=True)
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
    """
    Fallback when there's no JSON body: a record of the headers alone.

    It names no airport (the TRACON is not one) and is marked so that _normalise_record reports what it is,
    instead of failing validation on an airport it was never given.
    """
    return {
        "_headers_only": True,
        "tracon": headers.get("tracon", config.SUPPORTED_TRACON),
        "service_name": _service_from_mex(headers.get("mex", "")),
        "message_type": headers.get("msgType", "unknown"),
        "event_time_utc": headers.get("safaRcvTimestamp") or headers.get("timestamp"),
    }

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
