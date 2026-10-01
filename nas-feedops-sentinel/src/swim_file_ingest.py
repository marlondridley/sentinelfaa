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

Layout. This module scans, claims, archives, logs and watches. What it reads is in the modules below, and every
name they define is re-exported here so callers (and tests) keep importing from swim_file_ingest:

  swim_parse.py       a file or RawMessage to raw records (formats, blockAtFAA, dispatch to the feed)
  swim_feed_*.py      one extractor per feed: APDS (RVR), TDES (departures), SMES (surface), TAIS (tracks)
  swim_common.py      what the extractors share
  swim_normalise.py   a raw record to a validated NASEvent
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src import config, retention, storage
from src.async_bridge import run_async
from src.logging_config import get_logger
from src.metrics import (
    SWIM_FOLDER_BACKLOG,
    SWIM_FOLDER_OLDEST_WAITING_SECONDS,
    SWIM_MESSAGES_WITHHELD,
    SWIM_WATCH_SCAN_FAILURES,
)
from src.models import NASEvent
from src.raw_message import (
    ENVELOPE_EXTENSION,
    PROPERTY_RE,
    Delivery,
    PollBatch,
    RawMessage,
    RawMessageSource,
    message_from_envelope,
)
from src.swim_common import (
    _as_list,
    _coerce_float,
    _coerce_int,
    _common_fields,
    _derive_flight_phase,
    _drill_payload,
    _keep_position,
    _normalise_icao,
    _parse_tdls_time,
    _primary_airport_from,
    _scalar,
    _scale_rvr,
    _service_from_mex,
)
from src.swim_feed_apds import _extract_apds_rvr
from src.swim_feed_smes import (
    _SMES_NON_REPORT_KEYS,
    _SMES_VERIFIED_REPORTS,
    _SURFACE_EVENTS,
    _extract_smes,
    _extract_smes_event,
    _extract_smes_holdbar,
)
from src.swim_feed_tais import _extract_tais
from src.swim_feed_tdes import _extract_tdes, _extract_tdls_dcl
from src.swim_normalise import _as_utc, _normalise_record, _receipt_time
from src.swim_parse import (
    _XML_FIELD_MAP,
    WithheldMessage,
    _extract_records,
    _extract_text,
    _honor_block_at_faa,
    _jms_headers_only_record,
    _parse_file,
    _parse_jms_message_file,
    _parse_json_file,
    _parse_txt_file,
    _parse_xml_file,
    records_from_raw,
)

_log = get_logger(__name__)

__all__ = [
    'CLAIM_MARKER_SUFFIX',
    'CLAIM_TIMEOUT_SECONDS',
    'FAILED_DIR',
    'FolderDelivery',
    'FolderSource',
    'INFLIGHT_DIR',
    'IngestStats',
    'MessageDelivery',
    'PROCESSED_DIR',
    'SCAN_EXTENSIONS',
    'WATCH_BACKOFF_BASE_SECONDS',
    'WATCH_BACKOFF_CAP_SECONDS',
    'WithheldMessage',
    '_SMES_NON_REPORT_KEYS',
    '_SMES_VERIFIED_REPORTS',
    '_SURFACE_EVENTS',
    '_XML_FIELD_MAP',
    '_as_list',
    '_as_utc',
    '_claim',
    '_coerce_float',
    '_coerce_int',
    '_common_fields',
    '_derive_flight_phase',
    '_drill_payload',
    '_extract_apds_rvr',
    '_extract_records',
    '_extract_smes',
    '_extract_smes_event',
    '_extract_smes_holdbar',
    '_extract_tais',
    '_extract_tdes',
    '_extract_tdls_dcl',
    '_extract_text',
    '_honor_block_at_faa',
    '_jms_headers_only_record',
    '_keep_position',
    '_normalise_icao',
    '_normalise_record',
    '_parse_file',
    '_parse_jms_message_file',
    '_parse_json_file',
    '_parse_tdls_time',
    '_parse_txt_file',
    '_parse_xml_file',
    '_primary_airport_from',
    '_receipt_time',
    '_scalar',
    '_scale_rvr',
    '_service_from_mex',
    '_shutdown_requested',
    'commit_scan',
    'ingest_batch',
    'is_poison',
    'isolate_poison',
    'release_scan',
    'message_files',
    'read_failure_reasons',
    'read_ingest_log',
    'records_from_raw',
    'request_shutdown',
    'run_retention',
    'scan_swim_folder',
    'shutdown_requested',
    'watch_swim_folder',
]

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

# While scans keep failing the watcher waits 2 s, 4 s, 8 s ... up to a minute between attempts.
WATCH_BACKOFF_BASE_SECONDS = 2.0

WATCH_BACKOFF_CAP_SECONDS = 60.0

# Accept .json/.xml/.txt AND extension-less files (Kit's MessageFileOutput
# writes one file per JMS message named by epoch millis, no extension).
SCAN_EXTENSIONS: set[str] = {".json", ".xml", ".txt", ENVELOPE_EXTENSION, ""}

# The Jumpstart kit names each file with its arrival time in epoch millis.
_ARRIVAL_MILLIS = re.compile(r"(\d{12,})$")

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

#  FAA Jumpstart Kit MessageFileOutput parser (JMS headers + JSON body) 

_JMS_PROPERTY_RE = PROPERTY_RE   # the pattern lives with the codec, src/raw_message.py

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
        _drop_claim_marker(delivery.work)

    def quarantine(self, delivery: FolderDelivery, reason: str) -> None:
        _move_to_failed(delivery.work, reason, self.failed_dir)
        _drop_claim_marker(delivery.work)

    def release(self, delivery: FolderDelivery) -> None:
        """Hand the file back to the folder, to be scanned again."""
        _release_claim(delivery.work, delivery.path)

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
    marked processed with nothing stored. Call release_scan(stats) if they cannot be stored; a file left
    claimed by a scanner that died is released after CLAIM_TIMEOUT_SECONDS and scanned again. Stable event
    ids make a second scan safe.

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
    files are released at once and scanned again.

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
    failures = 0        # consecutive scans that raised; the loop backs off while it is non-zero

    def _run_scan(reason: str) -> int:
        """Scan once; returns how many files it consumed (0 if another scan held the lock, or the scan failed)."""
        nonlocal failures
        if not scan_lock.acquire(blocking=False):
            # A scan is already in flight; whatever prompted this call will
            # be picked up by that scan (or the next reconciliation pass) --
            # safe to skip rather than queue up overlapping scans.
            return 0
        try:
            (_log.debug if reason == "poll" else _log.info)("swim_watch_scan_triggered", extra={"reason": reason})
            try:
                events, stats = scan_swim_folder(source_dir, max_files=max_files, defer_archive=True)
                if events and on_events is not None:
                    try:
                        on_events(events, stats)
                    except Exception:  # noqa: BLE001 -- the events were not stored; keep watching
                        _log.exception("swim_pipeline_failed_files_released",
                                       extra={"files": len(stats.pending), "events": len(events)})
                        release_scan(stats)      # back to the folder now, not after the claim timeout
                        failures += 1
                        return 0
                commit_scan(stats)
            except Exception:  # noqa: BLE001 -- a database error in the scan or its commit must not end the watcher
                # The watcher used to die here, and with it the feed's only consumer until the container was
                # restarted. Files the scan had claimed are released after CLAIM_TIMEOUT_SECONDS and scanned
                # again; stable event ids make that safe.
                failures += 1
                SWIM_WATCH_SCAN_FAILURES.inc()
                _log.exception("swim_watch_scan_failed", extra={"reason": reason, "consecutive_failures": failures})
                return 0
            finally:
                run_retention()
            failures = 0
            return stats.files_processed + stats.files_failed
        finally:
            scan_lock.release()

    def _backoff_seconds() -> float:
        """How long to wait before the next scan: the poll interval, or longer while scans are failing."""
        if not failures:
            return config.SWIM_WATCH_POLL_SECONDS
        return min(WATCH_BACKOFF_CAP_SECONDS, WATCH_BACKOFF_BASE_SECONDS * 2 ** (failures - 1))

    def _drain(reason: str) -> None:
        """
        Scan until a pass finds fewer than max_files. A pass takes at most max_files, so when it takes that
        many there is probably more waiting, and an event-driven watcher would otherwise leave it until the
        next file arrives or the next reconcile tick: 500 files per five minutes once the feed went quiet.
        """
        while _run_scan(reason) >= max_files and not _shutdown_requested.is_set():
            pass

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
                    _shutdown_requested.wait(_backoff_seconds())
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
                _drain("fs_event")
                last_reconcile = time.monotonic()
                if failures:
                    _shutdown_requested.wait(_backoff_seconds())
            elif time.monotonic() - last_reconcile >= reconcile_interval:
                _drain("reconcile")
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

def release_scan(stats: IngestStats) -> int:
    """
    Give back every file a defer_archive scan parsed, because its events could not be stored. They return to the
    folder at once, with their own timestamps, to be scanned again; left claimed they waited out the claim timeout,
    and a Temporal retry of the activity in the meantime found an empty folder and succeeded. Returns how many.
    """
    pending, stats.pending = stats.pending, []
    released = 0
    for source, delivery, _entry in pending:
        release = getattr(source, "release", None)
        if release is not None:
            release(delivery)
            released += 1
    if released:
        _log.warning("swim_ingest_claims_released_after_failure", extra={"count": released})
    return released


def is_poison(exc: BaseException) -> bool:
    """
    True when the failure is about the data, not the database: storing the same events again can only fail
    again. A range or encoding error, a value the driver cannot encode, a record the model refuses. An outage
    (connection refused, a timeout) is not poison: the files are good and must be tried again.
    """
    import asyncpg

    return isinstance(exc, (asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError,
                            ValueError, TypeError))


def _events_by_file(events: list[NASEvent], stats: IngestStats) -> list[tuple[int, list[NASEvent]]]:
    """(index into stats.pending, that file's events), in the order the scan added them."""
    out, at = [], 0
    for index, (_source, _delivery, entry) in enumerate(stats.pending):
        count = int(entry.get("event_count") or 0)
        out.append((index, events[at:at + count]))
        at += count
    return out


def isolate_poison(events: list[NASEvent], stats: IngestStats, store, batch_error: BaseException) -> None:
    """
    The pipeline failed on the whole batch. If the failure is about the data, store file by file: the files that
    can be stored are, and each one that cannot is quarantined with its reason, so one bad message does not hold
    the other 499 in the queue for ever. If it is about the database, nothing is judged: the claims are released
    and the error raised again, to be retried as a whole.
    `store(events)` is the pipeline, synchronous.
    """
    if not stats.pending or not is_poison(batch_error):
        # Nothing to isolate (the events did not come from this scan's files), or not a fault of the data.
        release_scan(stats)
        raise batch_error
    _log.warning("swim_ingest_batch_failed_isolating", extra={
        "files": len(stats.pending), "error": f"{type(batch_error).__name__}: {batch_error}"})
    kept, bad = [], 0
    for index, file_events in _events_by_file(events, stats):
        source, delivery, entry = stats.pending[index]
        try:
            if file_events:
                store(file_events)
            kept.append(stats.pending[index])
        except Exception as exc:  # noqa: BLE001
            if not is_poison(exc):
                # The database went away part way. Give everything back, the files already stored included:
                # stable event ids make storing them again harmless, and nothing is left half done.
                release_scan(stats)
                raise
            bad += 1
            reason = f"{type(exc).__name__}: {exc}"
            source.quarantine(delivery, reason)
            _append_log_entries([{**entry, "status": "failed", "error": reason[:500]}])
            stats.files_processed -= 1
            stats.files_failed += 1
            _log.warning("swim_ingest_file_quarantined_poison", extra={"file_name": delivery.name, "error": reason})
    stats.pending = kept
    _log.warning("swim_ingest_batch_isolated", extra={"stored_files": len(kept), "quarantined_files": bad})


def run_retention() -> None:
    """Age out what scans have left behind (see src/retention.py); rate-limited, and never raises."""
    retention.run_due(PROCESSED_DIR, FAILED_DIR)

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
#
# A claim does not touch the file's own timestamps. They are the message's arrival: the receipt time of its events,
# and the age the Kit-folder janitor deletes by. The claim's age is kept apart, as the modification time of a marker
# file beside it (.inflight/<name>.claim). The claim used to restamp the file instead, so a file released after a
# failure looked newer than it was (events stored with a receipt time, and so a latency, later than the truth) and was
# older than the janitor's age the moment it came back.
#
# A scan whose events could not be stored gives its claims back at once (release_scan); the timeout below is only
# for a scanner that died without doing so. It is shorter than the janitor's age (Cleanup-KitLog.ps1, 5 minutes) so
# that a file released after a crash is scanned again before the janitor can drop it.

INFLIGHT_DIR = ".inflight"
CLAIM_MARKER_SUFFIX = ".claim"

CLAIM_TIMEOUT_SECONDS = 240

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
    try:
        (inflight / f"{path.name}{CLAIM_MARKER_SUFFIX}").touch()     # claim time, for stale-claim release
    except OSError:
        pass                     # no marker: the file's own time is used, as it was before markers
    return claimed


def _claim_marker(work: Path) -> Path:
    return work.with_name(f"{work.name}{CLAIM_MARKER_SUFFIX}")


def _drop_claim_marker(work: Path) -> None:
    try:
        _claim_marker(work).unlink(missing_ok=True)
    except OSError:
        pass


def _release_claim(work: Path, path: Path) -> bool:
    """Give a claimed file back to its folder, timestamps untouched. False if it was not there to give back."""
    if work == path:
        return False                 # a read-only folder: it was never moved
    try:
        os.rename(work, path)
    except OSError:
        return False
    _drop_claim_marker(work)
    return True

def _release_stale_claims(source_dir: Path) -> int:
    """Return files claimed by a scanner that died back to the folder."""
    inflight = source_dir / INFLIGHT_DIR
    if not inflight.is_dir():
        return 0
    released = 0
    cutoff = time.time() - CLAIM_TIMEOUT_SECONDS
    for f in inflight.iterdir():
        try:
            if not f.is_file():
                continue
            if f.name.endswith(CLAIM_MARKER_SUFFIX):
                # A marker whose file is gone (archived, or released by another scanner) and that has outlived
                # the timeout is litter.
                if not f.with_name(f.name[: -len(CLAIM_MARKER_SUFFIX)]).exists() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                continue
            marker = _claim_marker(f)
            claimed_at = marker.stat().st_mtime if marker.exists() else f.stat().st_mtime
            if claimed_at < cutoff and not (source_dir / f.name).exists():
                os.rename(f, source_dir / f.name)
                marker.unlink(missing_ok=True)
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
