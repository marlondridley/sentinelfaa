"""
Inputs for the ingest parsers: every real STDDS message in tests/fixtures, messages built here for the shapes the
captures lack, every file format, and seeded mutations of the Kit-format messages.

Not a test module. tests/test_swim_parse_golden.py runs the parsers and the record normaliser over these and
compares a fingerprint of each result with one taken before src/swim_file_ingest.py was split into modules, so the
split is proved to change nothing, including on the malformed and surprising inputs an external feed produces:
keys missing, values of the wrong type, numbers out of range, lists where an object was expected.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"

_JUNK = (None, "", "abc", "  ", 0, -5, 3.75, 10**10, True, [], {}, ["KDCA"], {"a": 1}, "KXXX", "999999",
         "2026-13-45T99:99:99Z", "NNNN", "0", "-1", "1e9", "é中")

_KIT_HEADERS = (
    "Property name = mex; value = {mex}\n"
    "Property name = tracon; value = PCT\n"
    "Property name = msgType; value = {msg_type}\n"
    "Property name = blockAtFAA; value = false\n"
)


def _tais_tracks() -> list:
    """Tracks of every shape the TAIS extractor branches on: airports named three ways, every plan status."""
    def track(**kw):
        return {"lat": "38.85", "lon": -77.04, "reportedAltitude": 2400, "vx": 120, "vy": -90, "vVert": -700,
                "reportedBeaconCode": "1200", "trackNum": 4411, "acAddress": "A1B2C3", **kw}

    return [
        {"flightPlan": {"airport": "KDCA", "acid": "AAL123", "runway": "01", "status": "active",
                        "assignedBeaconCode": "4521", "flightRules": "IFR"},
         "enhancedData": {"departureAirport": "KORD", "destinationAirport": "KDCA"}, "track": track(),
         "recSAFAReceiptTime": "2026-09-01T12:00:05Z"},
        {"flightPlan": {"airport": "KIAD", "acid": "UAL9", "runway": "NNNN", "status": "pending",
                        "assignedBeaconCode": "7777"},
         "enhancedData": {"departureAirport": "KIAD", "destinationAirport": "KJFK"},
         "track": track(reportedAltitude=300, vVert=900, trackNum=7)},
        {"flightPlan": {"airport": "BWI", "acid": "SWA4", "status": "Active"},
         "enhancedData": {"destinationAirport": "BWI"}, "track": track(reportedAltitude=15000, vVert=0, vx="x")},
        {"enhancedData": {"departureAirport": "KADW"}, "track": track(reportedAltitude=None, vVert=-800, vy=None)},
        {"flightPlan": {"airport": "KDCA"}, "track": track(lat="bad", lon="", reportedAltitude="3000", vVert=400)},
        {"flightPlan": {"airport": "KXXX", "acid": "NOPE"}, "track": track()},
        {"enhancedData": {"destinationAirport": "KBWI"}, "flightPlan": {}, "track": {}},
        "not a record",
    ]


def _synthetic() -> dict:
    """Messages built here because the captured ones hold no PCT-primary TAIS track and few shapes of the rest."""
    tracks = _tais_tracks()
    return {
        "synthetic/tais_rich": _KIT_HEADERS.format(mex="TAIS", msg_type="track")
        + json.dumps({"ns2:TATrackAndFlightPlan": {"record": tracks}}),
        "synthetic/tais_single_dict": _KIT_HEADERS.format(mex="TAIS", msg_type="FP")
        + json.dumps({"ns2:TATrackAndFlightPlan": {"record": tracks[0]}}),
        "synthetic/tais_no_records": _KIT_HEADERS.format(mex="TAIS", msg_type="track")
        + json.dumps({"ns2:TATrackAndFlightPlan": {}}),
        # RVR: 0 is the sensor's "not reporting" code, a value above what any system reports is a wrong scale,
        # a string is a number the feed quoted, and a runway with no id or no reading is skipped.
        "synthetic/apds_rvr": _KIT_HEADERS.format(mex="APDS", msg_type="RR") + json.dumps({"ns2:RVRDataUpdateMessage": {
            "airport": "KDCA", "runwayData": [
                {"runwayID": {"numericRunwayID": 1, "runwaySubID": "l"}, "touchdownVisualRange": 0,
                 "midpointVisualRange": 0, "rolloutVisualRange": 0},
                {"runwayID": {"numericRunwayID": 19, "runwaySubID": ""}, "touchdownVisualRange": 60,
                 "midpointVisualRange": 0, "rolloutVisualRange": None},
                {"runwayID": {"numericRunwayID": 10, "runwaySubID": "R"}, "touchdownVisualRange": "40",
                 "midpointVisualRange": 9999999, "rolloutVisualRange": "x"},
                {"runwayID": {}, "touchdownVisualRange": 60},
                {"runwayID": {"numericRunwayID": "7", "runwaySubID": "C"}, "rolloutVisualRange": 30},
                "junk"]}}),
        "synthetic/apds_single_runway": _KIT_HEADERS.format(mex="APDS", msg_type="RR") + json.dumps({
            "ns2:RVRDataUpdateMessage": {"airport": "KBWI", "runwayData": {
                "runwayID": {"numericRunwayID": 15, "runwaySubID": "L"}, "touchdownVisualRange": 25}}}),
    }


def corpus() -> dict:
    """name -> Kit-format text: every real message in the fixtures, and the synthetic ones."""
    out = {}
    for path in sorted((FIXTURES / "swim_kit").rglob("*")):
        if path.is_file():
            out[str(path.relative_to(FIXTURES)).replace("\\", "/")] = path.read_text(encoding="utf-8")
    return {**out, **_synthetic()}


def formats() -> dict:
    """name -> (file suffix, text) for the non-Kit formats: .json, .txt, .xml and the Java edge's envelope."""
    sample = corpus()["swim_kit/tdes_sample"]
    headers = {}
    for line in sample.splitlines():
        if line.startswith("Property name = "):
            key, _, val = line[len("Property name = "):].partition("; value = ")
            headers[key] = val
    body = "\n".join(line for line in sample.splitlines() if not line.startswith("Property name = "))
    record = {"airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC", "message_type": "STATUS",
              "feed_status": "operational", "event_time_utc": "2026-09-01T12:00:00Z", "rvr_touchdown": "1800"}
    envelope = {"format": "swim-raw/1", "message_id": "mex:1:2", "received_at": "2026-09-01T12:00:00+00:00",
                "headers": headers, "body": body}
    xml = ('<messages xmlns:x="urn:x"><message><airportCode>KDCA</airportCode><x:serviceName>ISMC</x:serviceName>'
           "<messageType>STATUS</messageType><linkStatus>degraded</linkStatus>"
           "<eventTime>2026-09-01T12:00:00Z</eventTime><runway>10L</runway><rvrTouchdown>600</rvrTouchdown></message>"
           "<message><facilityId>KIAD</facilityId><callsign>AAL1</callsign></message></messages>")
    return {
        "json/dict": (".json", json.dumps(record)),
        "json/list": (".json", json.dumps([record, {**record, "airport_code": "KBWI"}, {}])),
        "json/bom": (".json", "﻿" + json.dumps(record)),
        "json/scalar": (".json", "42"),
        "json/invalid": (".json", "{not json"),
        "txt/ndjson": (".txt", json.dumps(record) + "\n\n" + json.dumps({**record, "service_name": "SMES"}) + "\n"),
        "txt/bad_line": (".txt", json.dumps(record) + "\nnope\n"),
        "xml/messages": (".xml", xml),
        "xml/root_only": (".xml", "<root><airportCode>KDCA</airportCode><feedStatus>ok</feedStatus></root>"),
        "xml/empty": (".xml", "<root/>"),
        "xml/dtd": (".xml", '<!DOCTYPE x [<!ENTITY a "b">]><x><airportCode>&a;</airportCode></x>'),
        "xml/malformed": (".xml", "<a><b></a>"),
        "envelope/valid": (".swimmsg", json.dumps(envelope)),
        "envelope/bad_format": (".swimmsg", json.dumps({**envelope, "format": "swim-raw/9"})),
        "envelope/no_body": (".swimmsg", json.dumps({k: v for k, v in envelope.items() if k != "body"})),
        "envelope/not_json": (".swimmsg", "nope"),
        "other/unsupported": (".csv", "a,b"),
    }


def split_kit_text(text: str):
    """(header lines, parsed JSON body) for a kit file, or None when there is no JSON body."""
    header_lines, body_lines, in_body = [], [], False
    for line in text.splitlines():
        if not in_body and line.startswith("Property name = "):
            header_lines.append(line)
        else:
            in_body = True
            body_lines.append(line)
    try:
        return "\n".join(header_lines) + "\n", json.loads("\n".join(body_lines))
    except json.JSONDecodeError:
        return None


def _paths(node: Any, prefix: tuple = ()) -> list:
    found = [prefix] if prefix else []
    if isinstance(node, dict):
        for key in node:
            found += _paths(node[key], (*prefix, key))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += _paths(item, (*prefix, i))
    return found


def _set(node: Any, path: tuple, value: Any, delete: bool) -> None:
    for key in path[:-1]:
        node = node[key]
    if delete:
        if isinstance(node, dict):
            node.pop(path[-1], None)
        elif isinstance(node, list) and path[-1] < len(node):
            node.pop(path[-1])
    else:
        node[path[-1]] = value


def mutate(body: Any, rng: random.Random, n: int) -> Any:
    """`body` with up to `n` nodes deleted or replaced by something the feed should not have sent."""
    body = json.loads(json.dumps(body))
    for _ in range(n):
        paths = _paths(body)
        if not paths:
            break
        path = rng.choice(paths)
        try:
            _set(body, path, rng.choice(_JUNK), delete=rng.random() < 0.35)
        except (KeyError, IndexError, TypeError):
            continue
    return body


def variants(per_file: int = 30) -> dict:
    """name -> Kit-format text, for seeded mutations of every corpus message that has a JSON body."""
    out = {}
    for name, text in corpus().items():
        parts = split_kit_text(text)
        if parts is None:
            continue
        headers, body = parts
        for k in range(per_file):
            rng = random.Random(f"{name}:{k}")
            out[f"{name}#{k}"] = headers + json.dumps(mutate(body, rng, rng.randint(1, 4)))
    return out


def files() -> dict:
    """name -> (file suffix, text) for everything the golden test parses: Kit files have no suffix."""
    return {**{n: ("", t) for n, t in corpus().items()}, **formats(), **{n: ("", t) for n, t in variants().items()}}


def record_variants(record: dict, seed: str, count: int = 12) -> list:
    """Seeded mutations of one parsed record, for the normaliser."""
    out = []
    for k in range(count):
        rng = random.Random(f"{seed}:{k}")
        copy = json.loads(json.dumps(record, default=str))
        for key in rng.sample(sorted(copy), min(len(copy), rng.randint(1, 3))):
            if rng.random() < 0.3:
                copy.pop(key)
            else:
                copy[key] = rng.choice(_JUNK)
        out.append(copy)
    return out
