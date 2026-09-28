"""
The seam between the Java edge and the Python side: the raw record, its codecs, and
the source interface.

The Jumpstart Kit JVM (SCDS allows JMS only) writes one `swim-raw/1` record per
message. tests/fixtures/swim_raw/ holds records the real IngestFolderFileOutput
wrote from the ten real captures in tests/fixtures/swim_kit/capture_2026-08-27, so
the Python reader is tested against the Java writer's own bytes, not against a
Python imitation of them. tests/test_ingest_output_java.py regenerates them with
the real writer when a JDK is present and fails if they have drifted.

Pure codec tests: nothing here needs a database.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.raw_message import (
    ENVELOPE_FORMAT,
    RawMessage,
    deliveries,
    derive_message_id,
    envelope_json,
    message_from_envelope,
    message_from_kit_text,
)

TESTS = Path(__file__).parent
RAW = TESTS / "fixtures" / "swim_raw"
CAPTURE = TESTS / "fixtures" / "swim_kit" / "capture_2026-08-27"
RECORDS = sorted((RAW / "capture_2026-08-27").glob("*.swimmsg"))
CAPTURES = sorted(p for p in CAPTURE.iterdir() if p.is_file())
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _record(**overrides) -> str:
    doc = {
        "format": ENVELOPE_FORMAT, "message_id": "TDES:1:2", "received_at": "2026-09-26T12:00:00Z",
        "headers": {"mex": "TDES"}, "body": "{}",
    }
    doc.update(overrides)
    return json.dumps({k: v for k, v in doc.items() if v is not ...})


# ── the record ────────────────────────────────────────────────────────────────

def test_a_record_survives_the_round_trip():
    message = RawMessage(
        message_id="TDES:51190821:1874", headers={"mex": "TDES", "tracon": "PCT"},
        body='{"a": 1}', received_at=NOW, source_name="x.swimmsg")
    assert message_from_envelope(envelope_json(message), "x.swimmsg") == message


def test_a_conversion_failure_travels_with_the_record():
    message = RawMessage("sha256:ab", {}, "not xml", NOW, convert_error="payload converted to an empty JSON object")
    assert message_from_envelope(envelope_json(message)).convert_error == message.convert_error


@pytest.mark.parametrize("stamp, expected", [
    ("2026-09-26T12:00:00Z", NOW),
    ("2026-09-26T12:00:00.250Z", NOW + timedelta(milliseconds=250)),
    ("2026-09-26T14:00:00+02:00", NOW),
    ("2026-09-26T12:00:00", NOW),                 # no offset: taken to be UTC
])
def test_received_at_is_always_aware_utc(stamp, expected):
    got = message_from_envelope(_record(received_at=stamp)).received_at
    assert got == expected and got.utcoffset() == timedelta(0)


def test_header_values_are_text():
    message = message_from_envelope(_record(headers={"n": 4, "empty": None, "s": "x"}))
    assert message.headers == {"n": "4", "empty": "", "s": "x"}


@pytest.mark.parametrize("text, why", [
    ("{not json", "not a JSON record"),
    ("[1, 2]", "must be a JSON object"),
    (_record(format="swim-raw/2"), "unknown record format"),
    (_record(format=...), "unknown record format"),
    (_record(message_id=...), "'message_id'"),
    (_record(message_id=""), "'message_id' is empty"),
    (_record(message_id=7), "'message_id'"),
    (_record(received_at=...), "'received_at'"),
    (_record(received_at="yesterday"), "not an ISO-8601"),
    (_record(headers=[]), "'headers'"),
    (_record(headers=...), "'headers'"),
    (_record(body=...), "'body'"),
    (_record(body={"already": "parsed"}), "'body'"),
    (_record(convert_error=5), "'convert_error'"),
])
def test_a_record_that_cannot_be_trusted_is_refused_with_the_reason(text, why):
    with pytest.raises(ValueError, match=why):
        message_from_envelope(text)


# ── the message id: one rule, on both sides of the seam ────────────────────────

def test_the_swim_identity_is_the_message_id_when_there_is_one():
    assert derive_message_id({"mex": "TDES", "globalID": "9", "msgSeqID": "4"}, "x") == "TDES:9:4"


def test_either_half_of_the_identity_is_enough():
    assert derive_message_id({"mex": "TAIS", "msgSeqID": "4"}, "x") == "TAIS::4"


def test_without_an_identity_the_message_id_is_a_hash_of_the_payload():
    assert derive_message_id({"mex": "TAIS"}, "body") == "sha256:" + hashlib.sha256(b"body").hexdigest()


def test_the_hash_is_of_utf8_not_of_the_platform_encoding():
    assert derive_message_id({}, "café") == "sha256:" + hashlib.sha256("café".encode("utf-8")).hexdigest()


# ── files the stock kit already wrote ─────────────────────────────────────────

def test_a_stock_kit_file_reads_as_headers_then_payload():
    text = ("Property name = mex; value = TDES\nProperty name = globalID; value = 7\n"
            "Property name = msgSeqID; value = 3\n\n{\"a\": 1}\n")
    message = message_from_kit_text(text, "1787863346003", NOW)
    assert message.headers == {"mex": "TDES", "globalID": "7", "msgSeqID": "3"}
    assert message.body == '{"a": 1}'
    assert message.message_id == "TDES:7:3"
    assert (message.received_at, message.source_name) == (NOW, "1787863346003")


def test_a_stock_kit_file_without_headers_is_all_payload():
    message = message_from_kit_text('{"a": 1}\n', "f", NOW)
    assert message.headers == {} and message.body == '{"a": 1}'
    assert message.message_id.startswith("sha256:")


def test_a_property_line_inside_the_payload_is_payload():
    text = 'Property name = mex; value = TDES\n{"x": 1}\nProperty name = late; value = not a header\n'
    message = message_from_kit_text(text, "f", NOW)
    assert message.headers == {"mex": "TDES"}
    assert "Property name = late" in message.body


# ── what the real Java writer produced ────────────────────────────────────────

def test_there_is_one_record_per_capture():
    assert len(RECORDS) == len(CAPTURES) == 10


@pytest.mark.parametrize("record, capture", list(zip(RECORDS, CAPTURES)), ids=[p.name for p in CAPTURES])
def test_the_java_writers_record_is_the_captured_message(record, capture):
    message = message_from_envelope(record.read_text(encoding="utf-8"), record.name)
    original = message_from_kit_text(capture.read_text(encoding="utf-8-sig"), capture.name, NOW)
    assert message.headers == original.headers
    assert message.body == original.body
    assert message.convert_error is None


@pytest.mark.parametrize("record", RECORDS, ids=[p.name for p in RECORDS])
def test_the_java_and_python_sides_agree_on_the_message_id(record):
    message = message_from_envelope(record.read_text(encoding="utf-8"), record.name)
    assert message.message_id == derive_message_id(message.headers, message.body)
    assert message.message_id.count(":") == 2 and not message.message_id.startswith("sha256")


def test_the_hash_fallback_agrees_across_the_seam():
    (record,) = (RAW / "no_identity").glob("*.swimmsg")
    message = message_from_envelope(record.read_text(encoding="utf-8"), record.name)
    assert "globalID" not in message.headers and "msgSeqID" not in message.headers
    assert message.message_id.startswith("sha256:")
    assert message.message_id == derive_message_id(message.headers, message.body)


# ── the source interface ──────────────────────────────────────────────────────

def test_an_in_memory_batch_counts_what_it_holds():
    batch = deliveries([object(), object()])
    assert (batch.planned, batch.detected, batch.skipped) == (2, 2, 0)
    assert len(list(batch.deliveries)) == 2
