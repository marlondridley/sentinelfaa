"""
Shared data-fetch and reconstruction helpers used by more than one page.

Each page fetches its own data on every rerun -- Streamlit's multipage
model means a page's script only runs when that page is the one being
viewed, so there's no single shared "live_events" to hand between pages
the way the old one-file dashboard could. What *is* shared, and what
belongs here, is the parsing/formatting logic: reconstructing NASEvent
objects from raw storage rows, the readiness/status label thresholds, and
the airport reference data -- so every page applies it identically
instead of six pages carrying six slightly-drifting copies.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from src import airports, config
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

def _in_scope_airports() -> dict[str, dict]:
    """
    The airports this deployment supports (config.SUPPORTED_AIRPORTS), registry airports first in the
    registry's order, then any the registry does not know: those have no coordinates, so no map marker.
    """
    known = {c: a for c, a in airports.REGISTRY.items() if c in config.SUPPORTED_AIRPORTS}
    table = {c: {"name": a.name, "lat": a.lat, "lon": a.lon} for c, a in known.items()}
    for code in sorted(config.SUPPORTED_AIRPORTS - set(known)):
        table[code] = {"name": code, "lat": None, "lon": None}
    return table


AIRPORTS = _in_scope_airports()


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


def waited(since: datetime | None) -> str:
    """How long ago, in the largest two units that matter to someone waiting: '12 min', '3 h 5 min', '2 d 4 h'."""
    if since is None:
        return "—"
    minutes = max(0, int((datetime.now(timezone.utc) - since).total_seconds() // 60))
    if minutes >= 1440:
        return f"{minutes // 1440} d {minutes % 1440 // 60} h"
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def as_utc(value: Any) -> datetime | None:
    """
    Coerce a timestamp field from a storage row into an aware UTC datetime.

    Rows arrive from two places with two shapes: asyncpg hands back real
    datetimes for TIMESTAMPTZ columns, and anything that round-tripped
    through JSON hands back ISO strings. A naive datetime is assumed to be
    UTC rather than local -- every timestamp this system stores is UTC by
    construction (see NASEvent.event_time_utc), and guessing local time
    would silently shift every chart by the operator's offset.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def bucket_counts(
    rows: list[dict],
    field: str,
    *,
    buckets: int = 12,
    minutes_per_bucket: int = 60,
    predicate: Any = None,
) -> list[int]:
    """
    Count rows per time bucket, oldest bucket first, for the KPI sparklines.

    Returns a fixed-length list so a quiet period renders as a flat line at
    zero rather than as a missing segment -- "nothing happened in the last
    three hours" is information, and a sparkline that silently drops those
    buckets would draw it as though the last event were current.
    """
    now = datetime.now(timezone.utc)
    counts = [0] * buckets
    width = timedelta(minutes=minutes_per_bucket)
    window_start = now - width * buckets
    for row in rows:
        if predicate is not None and not predicate(row):
            continue
        ts = as_utc(row.get(field))
        if ts is None or ts < window_start or ts > now:
            continue
        index = int((ts - window_start) / width)
        counts[min(index, buckets - 1)] += 1
    return counts


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
