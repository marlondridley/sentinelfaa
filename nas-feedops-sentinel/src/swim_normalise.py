"""
From one raw record to a validated NASEvent: stable identity, UTC timestamps, RVR and latency coercion.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.models import NASEvent, content_key, stable_id

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

    if raw.pop("_headers_only", False):
        # A message with routing headers and no readable body (empty, or truncated so it is not JSON) has no airport and no observation. It used to be
        # given the TRACON as an airport ("PCT" became "KPCT"), which validation rejected as "airport not
        # supported": true, and no help to whoever was looking at why the message failed.
        return None, (f"no readable payload: the message has headers only, or a body that is not JSON "
                      f"(mex={raw.get('service_name')}, msgType={raw.get('message_type')}), so there is nothing to record")

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
