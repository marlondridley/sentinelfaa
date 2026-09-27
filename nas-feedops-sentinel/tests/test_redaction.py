"""Tests for the shared LLM-prompt redaction helpers (src/redaction.py)."""
import pytest

from src.redaction import REDACTED, SENSITIVE_FIELDS, redact_structured, redact_text


def test_redact_structured_drops_sensitive_keys():
    payload = {
        "aircraft_id": "N12345",
        "beacon_code": "1200",
        "airport_code": "KDCA",
        "note": "fine to keep",
    }
    out = redact_structured(payload)
    assert "aircraft_id" not in out
    assert "beacon_code" not in out
    assert out["airport_code"] == "KDCA"
    assert out["note"] == "fine to keep"


def test_redact_structured_recurses_into_nested_dicts_and_lists():
    payload = {
        "alerts": [
            {"squawk": "7700", "severity": "critical"},
            {"latitude": 38.9, "longitude": -77.0, "service": "TERMINAL_AUTO"},
        ],
    }
    out = redact_structured(payload)
    assert out["alerts"][0] == {"severity": "critical"}
    assert out["alerts"][1] == {"service": "TERMINAL_AUTO"}


def test_redact_structured_all_sensitive_fields_are_dropped():
    payload = {field: "x" for field in SENSITIVE_FIELDS}
    payload["keep_me"] = "y"
    out = redact_structured(payload)
    assert out == {"keep_me": "y"}


def test_redact_structured_passes_through_non_dict_list():
    assert redact_structured("hello") == "hello"
    assert redact_structured(42) == 42
    assert redact_structured(None) is None


def test_redact_text_scrubs_keyed_sensitive_fields_in_json_text():
    text = '{"aircraft_id": "N999XY", "latitude": 38.895, "longitude": -77.036}'
    out = redact_text(text)
    assert "N999XY" not in out
    assert "38.895" not in out
    assert "-77.036" not in out
    assert REDACTED in out


def test_redact_text_scrubs_tail_number_in_narrative_text():
    text = "Aircraft N12345 diverted after a low-fuel declaration."
    out = redact_text(text)
    assert "N12345" not in out
    assert REDACTED in out


def test_redact_text_scrubs_bare_coordinate_pair():
    text = "Last known position 38.8951,-77.0364 near the threshold."
    out = redact_text(text)
    assert "38.8951" not in out
    assert "-77.0364" not in out


def test_redact_text_leaves_non_sensitive_text_untouched():
    text = "KDCA TERMINAL_AUTO feed is healthy with 42 active tracks."
    assert redact_text(text) == text


def test_redact_text_handles_empty_and_none_gracefully():
    assert redact_text("") == ""
    assert redact_text(None) is None


def test_redact_text_scrubs_the_value_quoted_by_a_pydantic_validation_error():
    """Ingest failure text goes to the Feed Diagnostician: the offending value must not."""
    from datetime import datetime, timezone

    from pydantic import ValidationError

    from src.models import NASEvent

    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError) as failure:
        NASEvent(airport_code="KDCA", tracon="PCT", service_name="TAIS", message_type="m",
                 event_time_utc=now, received_time_utc=now, latency_ms=1.0, callsign="AAL1234")
    text = str(failure.value)
    assert "AAL1234" in text, "the premise: pydantic puts the value in the message"

    out = redact_text(text)
    assert "AAL1234" not in out
    assert "extra_forbidden" in out and REDACTED in out, "the reason survives, the value does not"


@pytest.mark.parametrize("quoted", ["'x y, z'", '"a, b"', "{'lat': 38.9, 'lon': -77.0}", "[38.9, -77.0]", "38.9"])
def test_redact_text_scrubs_any_shape_of_quoted_input_value(quoted):
    out = redact_text(f"bad [type=float_parsing, input_value={quoted}, input_type=str] more text")
    assert quoted not in out and "input_type=str] more text" in out


def test_vehicle_ids_are_sensitive():
    assert "vehicle_id" in SENSITIVE_FIELDS



# ── a value that nests: pydantic quotes the offending input as a Python repr ───
#
# The first pattern stopped an input_value dict at its first closing brace, so everything after an
# inner one went out in the clear.

_NESTED = [
    "{'flightPlan': {'acid': 'AAL123'}, 'note': 'UAL9 to KDCA'}",
    "{'a': [{'b': 'SWA55'}, 'DAL7'], 'c': 1}",
    "[{'acid': 'AAL123'}, ['UAL9', {'x': 'DAL7'}]]",
    "('AAL123', ('UAL9',), {'k': 'SWA55'})",
    "{'braces in a string': '} UAL9 {', 'after': 'DAL7'}",
    "{'k': 'it\\'s AAL9', 'after': 'DAL7'}",
    "'it\\'s AAL9'",
    '"double \'quoted\' AAL9"',
]


@pytest.mark.parametrize("value", _NESTED)
def test_a_nested_input_value_is_scrubbed_to_its_end(value):
    out = redact_text(f"bad [type=extra_forbidden, input_value={value}, input_type=dict] the rest")

    assert out == "bad [type=extra_forbidden, input_value=[REDACTED], input_type=dict] the rest"


@pytest.mark.parametrize("identifier", ["AAL123", "UAL9", "DAL7", "SWA55"])
def test_no_identifier_survives_any_nested_shape(identifier):
    for value in _NESTED:
        assert identifier not in redact_text(f"[type=x, input_value={value}, input_type=dict]")


def test_a_value_pydantic_cut_short_is_scrubbed_up_to_the_next_field_and_no_further():
    # pydantic elides the middle of a long repr, so it may never balance.
    text = "e [type=t, input_value={'a': {'b': 'AAL1', 'c': 'DAL2...x', input_type=dict] kept UAL3"
    assert redact_text(text) == "e [type=t, input_value=[REDACTED], input_type=dict] kept UAL3"


def test_a_value_with_no_type_after_it_is_scrubbed_to_the_end_of_the_line_only():
    out = redact_text("bad input_value={'a': {'acid': 'AAL1'}, 'b': 'UAL2'\nnext line SWA3")
    assert out == "bad input_value=[REDACTED]\nnext line SWA3"


def test_several_errors_in_one_message_are_each_scrubbed():
    text = ("1 [type=a, input_value={'x': {'y': 'AAL1'}}, input_type=dict]\n"
            "2 [type=b, input_value='UAL2', input_type=str]\n"
            "3 [type=c, input_value=[{'z': 'DAL3'}], input_type=list]")
    out = redact_text(text)
    assert out.count("input_value=[REDACTED]") == 3
    assert not any(i in out for i in ("AAL1", "UAL2", "DAL3"))
    assert [ln.split("]")[0].split("[type=")[1].split(",")[0] for ln in out.splitlines()] == ["a", "b", "c"]


@pytest.mark.parametrize("scalar", ["123", "38.9", "None", "True", "abc"])
def test_a_bare_value_ends_at_its_separator(scalar):
    out = redact_text(f"x [type=t, input_value={scalar}, input_type=int] y")
    assert out == "x [type=t, input_value=[REDACTED], input_type=int] y"


def test_a_value_that_closes_the_error_bracket_is_not_run_past_it():
    assert redact_text("(see [type=t, input_value=5] and UAL9") == "(see [type=t, input_value=[REDACTED]] and UAL9"
