"""
Shared data-fetch and reconstruction helpers used by more than one page.

Each page fetches its own data on every rerun -- Streamlit's multipage
model means a page's script only runs when that page is the one being
viewed, so there's no single shared "live_events" to hand between pages
the way the old one-file dashboard could. What *is* shared, and what
belongs here, is the parsing/formatting logic: reconstructing NASEvent
objects from raw storage rows, the readiness/status label thresholds, and
the PCT airport reference data -- so every page applies it identically
instead of six pages carrying six slightly-drifting copies.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src import config
from src.async_bridge import run_async as run
from src.logging_config import get_logger
from src.models import NASEvent
from src import storage

_log = get_logger(__name__)

# Kit log folder path (resolved via the data/raw/swim_live junction).
KIT_LOG_PATH = config.SWIM_RAW_FOLDER

STATUS_EMOJI = {
    "OK": "🟢",
    "STALE": "🟡",
    "DEGRADED": "🟠",
    "UNAVAILABLE": "🔴",
    "DISCONNECTED": "🔴",
    "NO DATA": "⚫",
}
SEVERITY_EMOJI = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}

PCT_AIRPORTS = {
    "KDCA": {"name": "Reagan National",       "lat": 38.8521, "lon": -77.0377},
    "KIAD": {"name": "Dulles International",  "lat": 38.9531, "lon": -77.4565},
    "KBWI": {"name": "Baltimore-Washington",  "lat": 39.1754, "lon": -76.6683},
    "KADW": {"name": "Joint Base Andrews",    "lat": 38.8108, "lon": -76.8669},
}


def status_label(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '⚪')} {status}"


def format_ts(value: Any, length: int = 19) -> str:
    """
    Render a timestamp field from a raw storage dict as `YYYY-MM-DD HH:MM:SS`
    (or a shorter/longer prefix per `length`), accepting either a real
    datetime (asyncpg returns TIMESTAMPTZ columns as datetime objects, not
    strings -- e.g. action_audit_log.created_at, planner_results.evaluated_at)
    or an already-ISO-formatted string, so callers don't each need their own
    `isinstance` check. Returns "" for anything else (None, unparseable).
    """
    if isinstance(value, datetime):
        return value.isoformat()[:length]
    if isinstance(value, str):
        return value[:length]
    return ""


def readiness_label(value, good_if, warn_if, bad_if, lower_is_better=False):
    """Return (emoji, status_text) based on thresholds."""
    if value is None:
        return ("⚪", "no data")
    if lower_is_better:
        if value <= good_if: return ("🟢", "healthy")
        if value <= warn_if: return ("🟡", "watch")
        if value <= bad_if:  return ("🟠", "degraded")
        return ("🔴", "critical")
    if value >= good_if: return ("🟢", "healthy")
    if value >= warn_if: return ("🟡", "watch")
    if value >= bad_if:  return ("🟠", "degraded")
    return ("🔴", "critical")


def parse_json_str_list(raw: Any, field_name: str = "json_field") -> list[str]:
    """Safely decode and validate that a value or JSON string is a list[str]."""
    if raw is None:
        return []
    parsed = raw
    if isinstance(raw, str):
        raw_stripped = raw.strip()
        if not raw_stripped or raw_stripped == "[]":
            return []
        try:
            parsed = json.loads(raw_stripped)
        except json.JSONDecodeError as exc:
            _log.warning(
                "json_decode_failed",
                extra={"field": field_name, "error": str(exc), "raw_sample": raw[:100]},
            )
            return []
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "json_parse_error",
                extra={"field": field_name, "error": str(exc)},
            )
            return []

    if isinstance(parsed, list):
        return [str(item) for item in parsed if item is not None]

    _log.warning(
        "json_shape_mismatch",
        extra={"field": field_name, "expected": "list[str]", "actual_type": type(parsed).__name__},
    )
    return []


def reconstruct_nas_events(raw_rows: list[dict]) -> list[NASEvent]:
    """Reconstruct NASEvent instances from raw storage dictionaries with structured error logging."""
    events: list[NASEvent] = []
    for row in raw_rows:
        try:
            r = dict(row)
            if isinstance(r.get("event_time_utc"), str):
                r["event_time_utc"] = datetime.fromisoformat(r["event_time_utc"])
            if isinstance(r.get("received_time_utc"), str):
                r["received_time_utc"] = datetime.fromisoformat(r["received_time_utc"])

            if isinstance(r.get("validation_errors"), (str, list)):
                r["validation_errors"] = parse_json_str_list(
                    r.get("validation_errors"), "validation_errors"
                )

            events.append(NASEvent(**r))
        except Exception as exc:
            _log.warning(
                "nas_event_reconstruct_failed",
                extra={
                    "event_id": row.get("event_id", "unknown") if isinstance(row, dict) else "unknown",
                    "error": str(exc),
                    "raw_sample": str(row)[:150],
                },
            )
    return events


def fetch_live_events(limit: int = 1000) -> list[NASEvent]:
    """
    Fetch recent events and reconstruct them into NASEvent objects in one
    call -- the pattern every page that needs live telemetry (the ticker,
    Live Airspace, Feed Health) repeats, previously inlined three times
    with slightly different fetch/reconstruct pairings in the single-file
    dashboard.
    """
    raw_rows = run(storage.get_recent_events(limit=limit))
    return reconstruct_nas_events(raw_rows)
