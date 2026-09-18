"""
Replay engine for NAS FeedOps Sentinel.

Loads captured JSON message files from data/raw/, parses them into NASEvents,
and returns them for the standard validation + alert pipeline.

File format: each .json file may contain a single dict or a list of dicts.
Timestamps must be ISO-8601 strings; latency_ms is recalculated on load.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from pathlib import Path

from src import config
from src.models import NASEvent
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
                for item in data:
                    item["_source_file"] = str(json_file)
                messages.extend(data)
            elif isinstance(data, dict):
                data["_source_file"] = str(json_file)
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


def write_sample_replay_file(
    events: list[NASEvent],
    output_path: Path | None = None,
) -> Path:
    """
    Serialize a list of NASEvents to a JSON file in data/raw/.
    The output file can be loaded by replay_from_directory() in a later run.
    """
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        output_path = config.RAW_DIR / f"sample_{timestamp}.json"

    payload = []
    for event in events:
        d = event.model_dump()
        d["event_time_utc"] = d["event_time_utc"].isoformat()
        d["received_time_utc"] = d["received_time_utc"].isoformat()
        payload.append(d)

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log.info(
        "sample_replay_file_written",
        extra={"event_count": len(events), "output_path": str(output_path)},
    )
    return output_path