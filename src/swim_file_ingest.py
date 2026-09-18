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
  data/processed/ingest_log.jsonl  (audit trail of every file processed)

Supported raw file formats:
  .json  -- JSON dict or list of dicts (same shape as replay files)
  .xml   -- FIXM/AIXM-style XML; fields extracted by tag name matching
  .txt   -- one JSON object per line (newline-delimited JSON)

Already-processed files are tracked via ingest_log.jsonl, so repeated
scans of the same folder are idempotent.
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
from typing import Any, Dict, List, Optional, Tuple

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src import config
from src.logging_config import get_logger
from src.models import NASEvent

# ── Ingest paths ──────────────────────────────────────────────────────────────

PROCESSED_DIR: Path = config.PROCESSED_DIR
FAILED_DIR: Path = config.DATA_DIR / "failed"
INGEST_LOG: Path = PROCESSED_DIR / "ingest_log.jsonl"

# Accept .json/.xml/.txt AND extension-less files (Kit's MessageFileOutput
# writes one file per JMS message named by epoch millis, no extension).
SCAN_EXTENSIONS: set[str] = {".json", ".xml", ".txt", ""}

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

# PCT TRACON primary airports — used to filter TAIS records to in-scope traffic.
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


# ── Ingest log helpers ────────────────────────────────────────────────────────

def _load_processed_filenames() -> set[str]:
    """Return the set of file names already recorded in ingest_log.jsonl."""
    if not INGEST_LOG.exists():
        return set()
    seen: set[str] = set()
    for line in INGEST_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            seen.add(entry.get("file", ""))
        except json.JSONDecodeError:
            pass
    return seen


def _append_log_entry(entry: dict) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with INGEST_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── File parsers ──────────────────────────────────────────────────────────────

def _parse_json_file(path: Path) -> List[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"Unexpected JSON root type: {type(data).__name__}")


def _parse_txt_file(path: Path) -> List[dict]:
    """Newline-delimited JSON (one object per line)."""
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _extract_text(elem: ET.Element, tag: str) -> Optional[str]:
    """Find first child or descendant element by local tag name, return text."""
    for child in elem.iter():
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local == tag and child.text:
            return child.text.strip()
    return None


def _parse_xml_file(path: Path) -> List[dict]:
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


# ── FAA Jumpstart Kit MessageFileOutput parser (JMS headers + JSON body) ────

_JMS_PROPERTY_RE = re.compile(r"^Property name = (?P<key>[^;]+);\s*value = (?P<val>.*)$")


def _parse_jms_message_file(path: Path) -> List[dict]:
    """
    Parse one file written by the FAA Jumpstart Kit's MessageFileOutput.

    Format:
        Property name = <key>; value = <val>     (zero or more lines)
        <single JSON object on its own line>      (the STDDS payload)

    Returns one normalized record dict per aircraft track in the payload
    (TAIS messages typically contain many records). Headers are merged
    into each record so downstream knows tracon, msgType, timestamp, etc.
    """
    text = path.read_text(encoding="utf-8", errors="replace")

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

    return _extract_records(headers, body, path)


def _jms_headers_only_record(headers: dict[str, str], path: Path) -> dict:
    """Fallback when there's no JSON body — synthesize a metadata-only event."""
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
    }.get(mex_up, "TERMINAL_AUTO")


def _derive_flight_phase(
    altitude_ft: Optional[int],
    v_vert: Optional[int],
    this_airport: str,
    departure: str,
    destination: str,
) -> str:
    """
    Heuristic flight-phase classifier from track data.

    GROUND     — altitude < 500 ft (taxiing / on surface)
    DEPARTURE  — climbing AND this airport is the departure
    ARRIVAL    — descending AND this airport is the destination
    ENROUTE    — level flight at cruise altitude
    UNKNOWN    — insufficient signal
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


def _normalise_icao(code: Optional[str]) -> str:
    """Ensure an airport identifier is a K-prefixed ICAO (US convention)."""
    if not code:
        return "PCT"
    c = code.strip().upper()
    if len(c) == 3 and c.isalpha():
        return f"K{c}"
    return c


def _extract_records(headers: dict[str, str], body: dict, path: Path) -> List[dict]:
    """
    Extract per-aircraft NASEvent dicts from a TAIS payload.

    TAIS shape: { "ns2:TATrackAndFlightPlan": { "record": [ {...}, {...} ] } }
    Each record carries a flightPlan (with airport, runway, acid) and a track
    (lat/lon, altitude). We emit one dict per record whose airport falls
    within PCT TRACON primary scope.
    """
    # Drill past the namespace-prefixed root if present (TAIS / TFDM convention)
    payload = body
    if isinstance(payload, dict) and len(payload) == 1:
        inner = next(iter(payload.values()))
        if isinstance(inner, dict):
            payload = inner

    records = payload.get("record") or []
    if isinstance(records, dict):
        records = [records]

    tracon = headers.get("tracon", "PCT")
    service_name = _service_from_mex(headers.get("mex", ""))
    msg_type = headers.get("msgType", "track")
    event_ts = headers.get("safaRcvTimestamp") or headers.get("timestamp")

    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        fp = rec.get("flightPlan") or {}
        enhanced = rec.get("enhancedData") or {}
        track = rec.get("track") or {}

        # Pick the most specific airport identifier we can find
        airport_raw = (
            fp.get("airport")
            or enhanced.get("destinationAirport")
            or enhanced.get("departureAirport")
        )
        airport = _normalise_icao(airport_raw)

        # Filter to PCT primary airports — keep volume sane and stay in demo scope
        if airport not in _PCT_PRIMARY_AIRPORTS:
            continue

        # Spatial fields from track
        lat = track.get("lat")
        lon = track.get("lon")
        altitude_ft = track.get("reportedAltitude")
        vx = track.get("vx")
        vy = track.get("vy")
        v_vert = track.get("vVert")
        ground_speed_kt = None
        if vx is not None and vy is not None:
            try:
                ground_speed_kt = int(round((float(vx) ** 2 + float(vy) ** 2) ** 0.5))
            except (TypeError, ValueError):
                pass

        # Derive flight phase from altitude + vertical speed + airport context
        dep_airport = _normalise_icao(enhanced.get("departureAirport"))
        dst_airport = _normalise_icao(enhanced.get("destinationAirport"))
        flight_phase = _derive_flight_phase(
            altitude_ft=altitude_ft,
            v_vert=v_vert,
            this_airport=airport,
            departure=dep_airport,
            destination=dst_airport,
        )

        out.append({
            "airport_code": airport,
            "tracon": tracon,
            "service_name": service_name,
            "message_type": f"TAIS_{msg_type}",
            "event_time_utc": event_ts,
            "aircraft_id": fp.get("acid") or track.get("acAddress"),
            "track_id": str(track.get("trackNum")) if track.get("trackNum") is not None else None,
            "runway": (fp.get("runway") or "").upper() or None,
            "lat": float(lat) if lat is not None else None,
            "lon": float(lon) if lon is not None else None,
            "altitude_ft": int(altitude_ft) if altitude_ft is not None else None,
            "ground_speed_kt": ground_speed_kt,
            "vertical_speed_fpm": int(v_vert) if v_vert is not None else None,
            "flight_phase": flight_phase,
            "flight_rules": fp.get("flightRules"),
            "beacon_code": str(fp.get("assignedBeaconCode") or track.get("reportedBeaconCode") or "") or None,
        })

    # If no records fall within PCT primary scope, return empty list rather
    # than emitting a noisy "PCT" placeholder that would fail ICAO validation.
    return out


def _parse_file(path: Path) -> Tuple[List[dict], Optional[str]]:
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


# ── Record normaliser ─────────────────────────────────────────────────────────

def _normalise_record(raw: dict, source_file: str) -> Tuple[Optional[NASEvent], Optional[str]]:
    """
    Coerce a raw dict into a NASEvent.
    Injects received_time_utc = now so every live-ingested event has a receipt stamp.
    Returns (event, error_string).
    """
    now = datetime.now(timezone.utc)
    raw = dict(raw)

    # Coerce ISO timestamp strings into UTC-aware datetimes
    for ts_field in ("event_time_utc", "received_time_utc"):
        if ts_field in raw and isinstance(raw[ts_field], str):
            try:
                parsed = datetime.fromisoformat(raw[ts_field])
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone(timezone.utc)
                raw[ts_field] = parsed
            except ValueError:
                raw.pop(ts_field, None)

    # Live-ingest always stamps the received time
    raw["received_time_utc"] = now

    # Derive event time if missing
    if "event_time_utc" not in raw:
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
        self.events_parsed: int = 0
        self.latest_file_time: Optional[datetime] = None
        self.latest_service: Optional[str] = None
        self.scan_time: datetime = datetime.now(timezone.utc)

    def as_dict(self) -> dict:
        return {
            "scan_time": self.scan_time.isoformat(),
            "files_detected": self.files_detected,
            "files_processed": self.files_processed,
            "files_failed": self.files_failed,
            "events_parsed": self.events_parsed,
            "latest_file_time": self.latest_file_time.isoformat() if self.latest_file_time else None,
            "latest_service": self.latest_service,
        }


def scan_swim_folder(
    source_dir: Optional[Path] = None,
    max_files: int = 500,
) -> Tuple[List[NASEvent], IngestStats]:
    """
    Scan source_dir for new STDDS message files, parse them, and return
    NASEvents ready for the validation + alert pipeline.

    Files are moved to data/processed/ on success or data/failed/ on error.
    The ingest_log.jsonl is updated with every file touched.
    Already-processed files (tracked by name in ingest_log.jsonl) are skipped.
    """
    source_dir = source_dir or (config.DATA_DIR / "raw" / "swim_live")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)

    already_processed = _load_processed_filenames()
    stats = IngestStats()
    all_events: List[NASEvent] = []

    all_candidates = [
        f for f in sorted(source_dir.iterdir())
        if f.is_file() and f.suffix.lower() in SCAN_EXTENSIONS
    ]
    stats.files_detected = len(all_candidates)

    # Cap files per scan so a backlog doesn't pin the pipeline for minutes.
    # Newest-first so live demos see the freshest data even when backlogged.
    candidate_files = sorted(all_candidates, key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    if len(all_candidates) > max_files:
        _log.warning(
            "swim_ingest_backlog",
            extra={
                "files_detected": len(all_candidates),
                "files_this_pass": max_files,
                "source_dir": str(source_dir),
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

        if path.name in already_processed:
            continue

        _log.info("swim_ingest_file_processing", extra={"file": path.name})
        raw_records, parse_error = _parse_file(path)

        if parse_error and not raw_records:
            _move_to_failed(path, parse_error)
            _append_log_entry({
                "file": path.name,
                "status": "failed",
                "error": parse_error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            stats.files_failed += 1
            continue

        events: List[NASEvent] = []
        errors: List[str] = []
        for raw in raw_records:
            event, err = _normalise_record(raw, str(path))
            if event:
                events.append(event)
                if event.service_name:
                    stats.latest_service = event.service_name
            else:
                errors.append(err or "Unknown normalisation error")

        if not events and errors:
            _move_to_failed(path, "; ".join(errors))
            _append_log_entry({
                "file": path.name,
                "status": "failed",
                "error": "; ".join(errors[:3]),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            stats.files_failed += 1
            continue

        # Move to processed
        dest = PROCESSED_DIR / path.name
        # Avoid overwrite if file already exists in processed
        if dest.exists():
            dest = PROCESSED_DIR / f"{path.stem}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}{path.suffix}"
        shutil.move(str(path), str(dest))

        services = list({e.service_name for e in events if e.service_name})
        _append_log_entry({
            "file": path.name,
            "status": "processed",
            "event_count": len(events),
            "services": services,
            "parse_errors": len(errors),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        stats.files_processed += 1
        stats.events_parsed += len(events)
        stats.latest_file_time = datetime.now(timezone.utc)
        all_events.extend(events)
        _log.info(
            "swim_ingest_file_processed",
            extra={"file": path.name, "event_count": len(events)},
        )

    if stats.files_detected == 0:
        _log.info("swim_ingest_no_new_files", extra={"source_dir": str(source_dir)})
    else:
        _log.info(
            "swim_ingest_scan_complete",
            extra={
                "files_processed": stats.files_processed,
                "files_failed": stats.files_failed,
                "events_parsed": stats.events_parsed,
            },
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
    source_dir: Optional[Path] = None,
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

    observer = Observer()
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


def read_ingest_log(limit: int = 100) -> List[dict]:
    """Return the most recent ingest log entries (newest first)."""
    if not INGEST_LOG.exists():
        return []
    lines = INGEST_LOG.read_text(encoding="utf-8").splitlines()
    entries = []
    for line in reversed(lines):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
        if len(entries) >= limit:
            break
    return entries


def _move_to_failed(path: Path, reason: str) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    dest = FAILED_DIR / path.name
    if dest.exists():
        dest = FAILED_DIR / f"{path.stem}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}{path.suffix}"
    try:
        shutil.move(str(path), str(dest))
        _log.warning(
            "swim_ingest_file_failed",
            extra={"file": path.name, "reason": reason[:200]},
        )
    except OSError as exc:
        _log.error(
            "swim_ingest_move_error",
            extra={"file": path.name, "error": str(exc)},
            exc_info=True,
        )
