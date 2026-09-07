"""
Maintenance utilities — currently the Kit-log cleanup primitive.

The FAA SCDS Jumpstart Kit writes ~20 files/sec to its log/ folder. Without
periodic cleanup the folder will hit 1.7M files per day. This module provides
a single-pass purge primitive that mirrors scripts/Cleanup-KitLog.ps1, so the
Streamlit dashboard can invoke it on every refresh without shelling out to
PowerShell.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple


def purge_old_kit_files(
    log_path: Path,
    max_age_minutes: int = 5,
) -> Tuple[int, int]:
    """
    Delete files in log_path whose mtime is older than now - max_age_minutes.

    Returns (files_removed, files_remaining).

    Safe under concurrent Kit writes — files that disappear or are locked
    during the sweep are silently skipped. Never raises.
    """
    if not log_path.exists() or not log_path.is_dir():
        return (0, 0)

    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    removed = 0
    remaining = 0

    for entry in log_path.iterdir():
        if not entry.is_file():
            continue
        try:
            mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
        except (FileNotFoundError, OSError):
            continue
        if mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
            except (FileNotFoundError, PermissionError, OSError):
                # Kit may still be writing — leave it for the next pass
                remaining += 1
        else:
            remaining += 1

    return (removed, remaining)
