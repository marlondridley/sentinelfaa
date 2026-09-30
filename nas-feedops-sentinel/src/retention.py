"""
Retention: what the feed leaves behind ages out, so the disk does not fill and the Kit does not halt.

The Kit writes ~20 files a second. Every message the scanner ingests is archived into data/processed/, every
one it rejects into data/failed/, and every live event lands in nas_events. Nothing removed any of it, so a
week of running was a million-plus files and a table that only grew, until the disk was full and the Kit's
writer halted the feed. run_due() is the one call that keeps that bounded. It does three things:

  * archived messages older than PROCESSED_RETENTION_HOURS are deleted from data/processed/;
  * rejected messages older than FAILED_RETENTION_DAYS are deleted from data/failed/;
  * live events older than LIVE_EVENT_RETENTION_DAYS are deleted from nas_events, except any that an alert or a
    review points at. Those are the evidence for a decision and stay. Replay, API and synthetic events are
    never touched: an operator put them there.

It is called by whoever runs scans (the watcher and the Temporal ingest activity), not on a timer of its own,
so there is nothing extra to start. It is rate-limited to once per RETENTION_INTERVAL_MINUTES, stops after
RETENTION_BUDGET_SECONDS so a large backlog is worked off across passes, never stalls a scan for long, and never
raises: a failed prune is logged and tried again next time.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import settings
from src.logging_config import get_logger
from src.metrics import RETENTION_PRUNED

_log = get_logger(__name__)

# Rows deleted per statement: small enough that a prune never holds the table for long.
_EVENT_BATCH = 5_000

_lock = threading.Lock()
_last_run: float | None = None        # time.monotonic() of the last pass


def prune_directory(directory: Path, max_age: timedelta, *, deadline: float, now: float | None = None) -> int:
    """
    Delete the regular files in `directory` whose mtime is older than `max_age`; returns how many.

    Only files directly in the folder: never a subdirectory, and never a dotfile (a .gitkeep, or the ingest
    folder's own bookkeeping). Stops when time.monotonic() passes `deadline`, leaving the rest for next time.
    A file that cannot be removed (still open on Windows, a permission) is skipped.
    """
    if not directory.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max_age.total_seconds()
    deleted = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if time.monotonic() >= deadline:
                break
            try:
                if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                os.unlink(entry.path)
                deleted += 1
            except OSError:
                continue
    return deleted


def prune_live_events(before: datetime, *, deadline: float) -> int:
    """Age live events out of nas_events (see the module docstring); returns how many rows went."""
    from src import storage
    from src.async_bridge import run_async

    total = 0
    while time.monotonic() < deadline:
        removed = run_async(storage.prune_live_events(before, _EVENT_BATCH))
        total += removed
        if removed < _EVENT_BATCH:
            break
    return total


def run_due(processed_dir: Path, failed_dir: Path, *, force: bool = False) -> dict[str, int] | None:
    """
    Run a retention pass if one is due; returns what it removed, or None when it was not due.

    `processed_dir` and `failed_dir` are the archive folders the caller's scans use. A setting of 0 turns that
    rule off. Safe to call from any thread and from every scan: a second caller during a pass, or one that
    arrives before RETENTION_INTERVAL_MINUTES has passed, returns None at once.
    """
    global _last_run
    interval = settings.RETENTION_INTERVAL_MINUTES * 60
    if not force and _last_run is not None and time.monotonic() - _last_run < interval:
        return None
    if not _lock.acquire(blocking=False):
        return None
    try:
        _last_run = time.monotonic()
        deadline = _last_run + settings.RETENTION_BUDGET_SECONDS
        result = {"processed_files": 0, "failed_files": 0, "live_events": 0}
        try:
            if settings.PROCESSED_RETENTION_HOURS > 0:
                result["processed_files"] = prune_directory(
                    processed_dir, timedelta(hours=settings.PROCESSED_RETENTION_HOURS), deadline=deadline)
            if settings.FAILED_RETENTION_DAYS > 0:
                result["failed_files"] = prune_directory(
                    failed_dir, timedelta(days=settings.FAILED_RETENTION_DAYS), deadline=deadline)
            if settings.LIVE_EVENT_RETENTION_DAYS > 0:
                result["live_events"] = prune_live_events(
                    datetime.now(timezone.utc) - timedelta(days=settings.LIVE_EVENT_RETENTION_DAYS),
                    deadline=deadline)
        except Exception as exc:  # noqa: BLE001 -- retention must never break a scan; it is tried again next pass
            _log.error("retention_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        for kind, count in result.items():
            if count:
                RETENTION_PRUNED.labels(kind=kind).inc(count)
        if any(result.values()):
            _log.info("retention_pruned", extra=result)
        return result
    finally:
        _lock.release()
