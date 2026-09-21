"""Tests for the shared LLM-prompt redaction helpers (src/redaction.py)."""
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
