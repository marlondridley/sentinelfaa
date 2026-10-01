"""
Maintenance utilities — currently the Kit-log cleanup primitive.

The FAA SCDS Jumpstart Kit writes ~20 files/sec to its log/ folder. Without
periodic cleanup the folder will hit 1.7M files per day. This module provides
a single-pass purge primitive that mirrors scripts/Cleanup-KitLog.ps1, so the
Streamlit dashboard can invoke it on every refresh without shelling out to
PowerShell.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.logging_config import get_logger
from src.metrics import SWIM_FILES_DROPPED_UNPROCESSED

_log = get_logger(__name__)


def _audit(actor: str, previous: str, new: str, note: str, action_id: str | None = None) -> str:
    from src import storage
    from src.async_bridge import run_async

    return run_async(storage.record_operator_action(
        "kit_purge", actor, previous, new, note, action_id=action_id))


def purge_old_kit_files(
    log_path: Path,
    max_age_minutes: int = 5,
    *,
    actor: str | None = None,
) -> tuple[int, int]:
    """
    Delete files in log_path whose mtime is older than now - max_age_minutes.

    Returns (files_removed, files_remaining).

    `actor` is the person who asked (auth.format_actor). Given one, the request is written to the append-only
    audit trail BEFORE anything is deleted -- if it cannot be, nothing is -- and the outcome after. A person
    deleting messages nobody has ingested is a decision, and is recorded like one.

    Ingested messages are moved out of this folder, so a message file still here is one nobody has
    ingested: deleting it is a drop, and is counted and logged as one.

    Safe under concurrent Kit writes — files that disappear or are locked
    during the sweep are silently skipped. Never raises.
    """
    if not log_path.exists() or not log_path.is_dir():
        return (0, 0)

    audit_id = None
    if actor:
        audit_id = _audit(actor, "WAITING", "DELETE_REQUESTED",
                          f"delete message files older than {max_age_minutes} minute(s) from the Kit log")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    removed = 0
    remaining = 0
    dropped = 0

    for entry in log_path.iterdir():
        if not entry.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        except (FileNotFoundError, OSError):
            continue
        if mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
                dropped += _is_message_file(entry)
            except (FileNotFoundError, PermissionError, OSError):
                # Kit may still be writing — leave it for the next pass
                remaining += 1
        else:
            remaining += 1

    if dropped:
        SWIM_FILES_DROPPED_UNPROCESSED.inc(dropped)
        _log.warning("swim_janitor_dropped_unprocessed",
                     extra={"count": dropped, "older_than_minutes": max_age_minutes, "folder": str(log_path),
                            "actor": actor or "janitor"})
    if actor:
        try:
            _audit(actor, "DELETE_REQUESTED", "DELETED",
                   f"deleted {removed} file(s), {dropped} of them messages never ingested; {remaining} remain",
                   action_id=audit_id)
        except Exception as exc:  # noqa: BLE001 -- the request is already on record; the deletion is done
            _log.error("kit_purge_outcome_not_audited", extra={"actor": actor, "removed": removed, "error": str(exc)})
    return (removed, remaining)


def _is_message_file(entry: Path) -> bool:
    """A file the ingest would have taken: not the kit's own messages.log, not a dotfile."""
    from src.swim_file_ingest import SCAN_EXTENSIONS   # here, not at the top: it pulls in the whole ingest

    return not entry.name.startswith(".") and entry.suffix.lower() in SCAN_EXTENSIONS
