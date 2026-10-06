"""
Replay engine for NAS FeedOps Sentinel.

Loads captured JSON message files from data/raw/, parses them into NASEvents,
and returns them for the standard validation + alert pipeline.

File format: each .json file may contain a single dict or a list of dicts.
Timestamps must be ISO-8601 strings; latency_ms is recalculated on load.
"""
from __future__ import annotations

import json
from datetime import datetime

from pathlib import Path

from src import config
from src.models import NASEvent, stable_id
from src.logging_config import get_logger

_log = get_logger(__name__)

def load_raw_messages(source_dir: Path | None = None) -> list[dict]:
    """Read all .json files in source_dir and return a flat list of raw dicts."""
    directory = source_dir or config.RAW_DIR
    messages: list[dict] = []

    for json_file in sorted(directory.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for position, item in enumerate(data):
                    item["_source_file"] = str(json_file)
                    item["_source_index"] = position
                messages.extend(data)
            elif isinstance(data, dict):
                data["_source_file"] = str(json_file)
                data["_source_index"] = 0
                messages.append(data)
            else:
                _log.warning(
                    "replay_skip_unexpected_type",
                    extra={"file_name": json_file.name, "root_type": type(data).__name__},
                )
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning(
                "replay_file_read_error",
                extra={"file_name": json_file.name, "error": str(exc)},
            )

    return messages


def parse_raw_message(raw: dict) -> tuple[NASEvent | None, str | None]:
    """
    Convert a raw dict into a NASEvent.
    Returns (event, None) on success or (None, error_string) on failure.
    """
    source_file = raw.pop("_source_file", None)
    source_index = raw.pop("_source_index", None)
    # Replaying the same file twice must not duplicate its events.
    if source_file and not raw.get("event_id"):
        raw["event_id"] = stable_id("replay", Path(source_file).name, source_index)
    try:
        # Coerce ISO timestamp strings to datetime objects
        for ts_field in ("event_time_utc", "received_time_utc"):
            if ts_field in raw and isinstance(raw[ts_field], str):
                raw[ts_field] = datetime.fromisoformat(raw[ts_field])

        # Recalculate latency from the parsed timestamps
        if "event_time_utc" in raw and "received_time_utc" in raw:
            delta = raw["received_time_utc"] - raw["event_time_utc"]
            raw["latency_ms"] = delta.total_seconds() * 1_000

        if source_file:
            raw["raw_message_path"] = source_file
        raw["data_origin"] = "replay"

        return NASEvent(**raw), None
    except Exception as exc:  # noqa: BLE001

        return None, f"Parse error ({source_file}): {exc}"


def replay_from_directory(
    source_dir: Path | None = None,
) -> tuple[list[NASEvent], list[str]]:
    """
    Load and parse all JSON messages from source_dir.
    Returns (valid_events, parse_error_strings).
    """
    source_dir = source_dir or config.RAW_DIR
    raw_messages = load_raw_messages(source_dir)
    _log.info(
        "replay_messages_found",
        extra={"raw_message_count": len(raw_messages), "source_dir": str(source_dir)},
    )
    events: list[NASEvent] = []
    errors: list[str] = []
    for raw in raw_messages:
        event, error = parse_raw_message(raw)
        if event:
            events.append(event)
        else:
            errors.append(error or "Unknown parse error")
    _log.info(
    "replay_parse_complete",
    extra={
        "parsed_events_count": len(events),
        "parse_error_count": len(errors),
        "source_dir": str(source_dir),
        },
    )
    return events, errors