"""
The seam between whatever brings SWIM messages in and the code that understands them.

The JVM at the edge (the Jumpstart Kit; SCDS allows JMS only) hands over one raw
record per message: {message_id, headers, body, received_at}. Everything from
here on -- parsing, the STDDS extractors, validation, storage -- reads through
the small interface below and never learns where a message came from.

    Kit JVM --(record)--> RawMessageSource --> ingest_batch() --> events

Today the only source is a folder of record files (FolderSource in
src/swim_file_ingest.py). A broker-backed source (Kafka, say) is a second
implementation of the same three methods, not a change to the pipeline:

    poll(max_messages)      what is waiting, as Deliveries
    commit(delivery)        the events are stored: acknowledge, never deliver again
    quarantine(d, reason)   this can never succeed: set it aside with the reason

commit() is called only after the events are durably stored, so a source that
acknowledges there is at-least-once, and the idempotent event ids make a
redelivery harmless.

This module holds only data and codecs. It knows nothing about STDDS.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol, Sequence

# The record layout written by jumpstart/.../IngestFolderFileOutput.java.
ENVELOPE_FORMAT = "swim-raw/1"
ENVELOPE_EXTENSION = ".swimmsg"

PROPERTY_RE = re.compile(r"^Property name = (?P<key>[^;]+);\s*value = (?P<val>.*)$")


@dataclass(frozen=True)
class RawMessage:
    """One SWIM message exactly as the broker delivered it, before anything interprets it."""

    message_id: str                     # the SWIM identity, or a hash of the payload; stable across redelivery
    headers: dict[str, str]             # the JMS properties
    body: str                           # the payload text (JSON converted from the STDDS XML by the kit)
    received_at: datetime               # when the message reached the edge, UTC
    source_name: str = ""               # where it came from, for logs: a file name; a topic/offset for a broker
    convert_error: str | None = None    # set when the kit could not convert the payload; body is then as received


def derive_message_id(headers: dict[str, str], body: str) -> str:
    """
    The SWIM identity mex:globalID:msgSeqID when the message carries one, else
    sha256:<hex> of the payload. The same rule as the Java writer, so an id means
    the same thing whichever side computed it.
    """
    if headers.get("globalID") or headers.get("msgSeqID"):
        return f"{headers.get('mex', '')}:{headers.get('globalID', '')}:{headers.get('msgSeqID', '')}"
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _aware_utc(value: str, what: str) -> datetime:
    try:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{what} is not an ISO-8601 timestamp: {value!r}") from exc
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def message_from_envelope(text: str, source_name: str = "") -> RawMessage:
    """
    Read one record written by the Java edge. Strict on purpose: a record the reader
    cannot fully trust is quarantined with the reason, never half-ingested. A format
    version it does not know is refused so a newer writer cannot be misread silently.
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not a JSON record: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("a record must be a JSON object")
    if doc.get("format") != ENVELOPE_FORMAT:
        raise ValueError(f"unknown record format {doc.get('format')!r}; this reader understands {ENVELOPE_FORMAT!r}")
    for key, kind in (("message_id", str), ("received_at", str), ("headers", dict), ("body", str)):
        if not isinstance(doc.get(key), kind):
            raise ValueError(f"record field {key!r} is missing or not a {kind.__name__}")
    if not doc["message_id"]:
        raise ValueError("record field 'message_id' is empty")
    convert_error = doc.get("convert_error")
    if convert_error is not None and not isinstance(convert_error, str):
        raise ValueError("record field 'convert_error' must be a string when present")
    return RawMessage(
        message_id=doc["message_id"],
        headers={str(k): "" if v is None else str(v) for k, v in doc["headers"].items()},
        body=doc["body"],
        received_at=_aware_utc(doc["received_at"], "record field 'received_at'"),
        source_name=source_name,
        convert_error=convert_error,
    )


def message_from_kit_text(text: str, source_name: str, received_at: datetime) -> RawMessage:
    """
    Read a file in the stock kit format (MessageFileOutput): zero or more
    "Property name = <key>; value = <val>" lines, then the payload. Kept so files the
    stock kit already wrote, and captures, still ingest.
    """
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in text.splitlines():
        if not in_body:
            m = PROPERTY_RE.match(line)
            if m:
                headers[m.group("key").strip()] = m.group("val").strip()
                continue
            if not line.strip():
                continue
            in_body = True                      # the first non-header, non-blank line begins the payload
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return RawMessage(
        message_id=derive_message_id(headers, body),
        headers=headers,
        body=body,
        received_at=received_at,
        source_name=source_name,
    )


def envelope_json(message: RawMessage) -> str:
    """A record as the Java edge writes it. For tests and for any other producer that wants the same seam."""
    doc: dict[str, Any] = {
        "format": ENVELOPE_FORMAT,
        "message_id": message.message_id,
        "received_at": message.received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "headers": message.headers,
        "body": message.body,
    }
    if message.convert_error:
        doc["convert_error"] = message.convert_error
    return json.dumps(doc)


class Delivery(Protocol):
    """One message handed to the driver. `records()` is where the source's own knowledge of format ends."""

    name: str                       # short name: what the ingest log calls it
    ref: str                        # audit reference stored with each event (raw_message_path)
    received_at: datetime           # valid once records() has been called

    def records(self) -> tuple[list[dict], str | None]:
        """(records, parse error). Raises WithheldMessage for a message the FAA says must not be kept."""
        ...


@dataclass
class PollBatch:
    """What a source has for one pass. `deliveries` may be lazy: a folder claims each file as it is reached."""

    deliveries: Iterable[Delivery] = field(default_factory=list)
    planned: int = 0        # deliveries the source intends to hand over this pass
    detected: int = 0       # everything waiting, handled or not
    skipped: int = 0        # waiting but not handed over: already handled, over the cap, or taken elsewhere


class RawMessageSource(Protocol):
    def poll(self, max_messages: int) -> PollBatch: ...

    def commit(self, delivery: Delivery) -> None:
        """The message's events are stored (or it was deliberately withheld): never deliver it again."""
        ...

    def quarantine(self, delivery: Delivery, reason: str) -> None:
        """The message can never succeed: set it aside, with the reason, for a person."""
        ...


def deliveries(items: Sequence[Delivery]) -> PollBatch:
    """A batch over an in-memory list; what a test double or a small source needs."""
    return PollBatch(deliveries=list(items), planned=len(items), detected=len(items))
