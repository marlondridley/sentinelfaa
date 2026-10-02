"""
The outbound leak check and pseudonyms (src/redaction.py), and the rules that hold them in place.

The pattern scrub is a guess about what an identifier looks like. These tests pin what sits behind it:
  - count_leaks() looks for the actual values the caller read from its records, so it finds what no pattern
    anticipated;
  - pseudonymize_structured() keeps who-is-who for a model without telling it who;
  - every call to the model passes its protected values, and a call that leaks one is refused.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
import yaml

from src.redaction import (
    REDACTED,
    Pseudonymizer,
    collect_sensitive_values,
    count_leaks,
    is_redacted,
    pseudonymize_structured,
    redact_structured,
    redact_text,
    redact_text_counted,
)

APP = Path(__file__).resolve().parent.parent


# ── collect_sensitive_values: what must not be sent ───────────────────────────────────────────────────

def test_values_under_sensitive_keys_are_collected_at_any_depth():
    record = {"flights": [{"aircraft_id": "AAL1234", "airport": "KDCA"}], "pos": {"latitude": 38.8951}}
    assert collect_sensitive_values(record) == {"AAL1234", "38.8951"}


def test_several_records_are_read_together():
    assert collect_sensitive_values({"callsign": "UAL5678"}, [{"registration": "G-ABCD"}]) == {"UAL5678", "G-ABCD"}


def test_a_raw_payload_carried_as_a_json_string_is_read_for_what_it_holds():
    payload = json.dumps({"flight": {"callsign": "RPA4451A", "altitude": 11000}, "note": "ok"})
    assert collect_sensitive_values({"raw_payload": payload}) == {"RPA4451A", "11000"}


def test_values_too_short_to_mean_anything_are_not_collected():
    # A 4-digit code cannot be told from a threshold in prose; the keyed and pattern layers cover it.
    assert collect_sensitive_values({"beacon_code": "1200", "squawk": 7700, "callsign": "AB"}) == set()


def test_a_float_is_judged_by_its_digits_like_an_integer_not_by_its_length_as_text():
    # "100.0" is five characters but three digits: as common in prose as 100. It used to be collected, and then
    # compared as the run of digits 1000.
    assert collect_sensitive_values({"altitude": 100.0}) == set()
    assert collect_sensitive_values({"altitude": 3000.0}) == set()                  # four digits, like a 4-digit code
    assert collect_sensitive_values({"altitude": 12000.0}) == {"12000.0"}           # five: kept, as written


def test_pseudonyms_and_the_placeholder_are_not_values_to_withhold():
    assert collect_sensitive_values({"aircraft_id": "AC-1", "callsign": REDACTED}) == set()


def test_keys_are_matched_without_regard_to_case_like_the_scrub():
    assert collect_sensitive_values({"AircraftId": "N12345", "CALLSIGN": "AAL1234"}) == {"N12345", "AAL1234"}


# ── count_leaks: look for the values, not their shape ────────────────────────────────────────────────

def test_a_value_standing_alone_is_found_in_any_case():
    assert count_leaks("the aal1234 departure was late", {"AAL1234"}) == 1


def test_a_value_split_by_spaces_or_punctuation_is_still_found():
    assert count_leaks("flight AAL 1234 and aal-1234 both", {"AAL1234"}) == 1      # one value, counted once
    assert count_leaks("G ABCD went around", {"G-ABCD"}) == 1
    assert count_leaks("N 12345 diverted", {"N12345"}) == 1


def test_a_value_inside_a_longer_word_is_not_a_leak():
    assert count_leaks("XAAL1234Y and AAL12345", {"AAL1234"}) == 0


def test_the_count_is_distinct_values_not_occurrences():
    text = "AAL1234 then AAL1234 then UAL5678"
    assert count_leaks(text, {"AAL1234", "UAL5678", "DAL9999"}) == 2


def test_a_coordinate_is_found_with_or_without_its_sign():
    assert count_leaks("position 77.0364 west", {"-77.0364"}) == 1
    assert count_leaks("position -77.0364 west", {"-77.0364"}) == 1


def test_a_number_is_compared_as_written_so_a_run_of_digits_in_a_word_is_not_a_leak():
    assert count_leaks("order abc770364def", {"-77.0364"}) == 0


def test_nothing_to_look_for_or_nothing_to_look_in_is_no_leak():
    assert count_leaks("AAL1234", set()) == 0
    assert count_leaks("", {"AAL1234"}) == 0
    assert count_leaks(None, {"AAL1234"}) == 0                                       # type: ignore[arg-type]


def test_the_answer_is_a_count_and_never_the_values():
    found = count_leaks("AAL1234", {"AAL1234"})
    assert isinstance(found, int) and found == 1


def test_a_number_is_found_as_the_same_number_not_as_a_shorter_or_longer_run_of_digits():
    # The decimal point and the sign are not digits to match across: 38.8951 is not the run 388951, and 12000.0 is
    # not the run 120000. Each used to be found in a number it was only a digit-run of.
    assert count_leaks("id 388951 raised", {"38.8951"}) == 0
    assert count_leaks("at 38.8951 north", {"38.8951"}) == 1
    assert count_leaks("altitude 120000", {"12000.0"}) == 0
    assert count_leaks("altitude 12000", {"12000.0"}) == 1                              # the same number, written plain
    assert count_leaks("level 12000.0 ft", {"12000"}) == 1
    assert count_leaks("level 12000.50 ft", {"12000"}) == 0                              # a different number


def test_a_value_under_five_digits_is_not_looked_for_however_it_is_written():
    # A threshold in prose, not an identifier. 100.0 used to be normalised to 1000 and found in "RVR below 1000 ft".
    assert count_leaks("RVR below 1000 ft", {"100.0"}) == 0
    assert count_leaks("RVR below 3000 ft", {"3000.0", "3000"}) == 0
    assert count_leaks("a 100.0 knot wind", {"100.0"}) == 0


def test_digits_split_by_separators_are_an_identifier_found_by_their_run_of_digits():
    # Not one number (a phone-like reference, a track number written 12-3456), so no decimal point to respect:
    # compared as the run of digits, and only when that run is long enough to mean something.
    assert count_leaks("ref 123456789 raised", {"123-456-789"}) == 1
    assert count_leaks("ref 123-456-789 raised", {"123-456-789"}) == 1
    assert count_leaks("ref 1234 raised", {"12-34"}) == 0


def test_a_number_inside_an_identifier_or_a_word_is_not_a_standalone_number():
    assert count_leaks("event evt-12345 raised", {"12345"}) == 0
    assert count_leaks("order abc12345def", {"12345"}) == 0
    assert count_leaks("altitude 12345.", {"12345"}) == 1
    assert count_leaks("(12345)", {"12345"}) == 1


def test_a_table_of_values_does_not_hide_a_short_identifier():
    # The check used to keep the 2000 longest values and drop the rest: the callsigns and tail numbers first,
    # exactly the ones it is for, with nothing to say it had looked at only some of them.
    table = {f"ID{n:06d}" for n in range(6000)}
    for identifier in ("AAL1234", "N12X"):
        assert count_leaks(f"the {identifier} departure was late", table | {identifier}) == 1
    assert count_leaks("nothing relevant here", table) == 0


def test_every_value_in_a_large_table_is_checked_not_only_some():
    table = {f"ID{n:06d}" for n in range(6000)}
    text = " ".join(sorted(table)[::7])                                               # every 7th value appears
    assert count_leaks(text, table) == len(sorted(table)[::7])


# ── The reason it exists: what the patterns miss ─────────────────────────────────────────────────────

# Identifiers written in a shape the callsign and tail-number patterns do not anticipate. The scrub leaves
# them in; the check, which knows the value, does not.
UNANTICIPATED = ["UAL12AB", "G-ABCD", "VH-XYZ", "C-FABC", "D-EABC"]


@pytest.mark.parametrize("identifier", UNANTICIPATED)
def test_the_patterns_miss_these_and_the_value_check_catches_them(identifier):
    prompt = f"Description: the aircraft {identifier} reported a go-around"
    assert identifier in redact_text(prompt), "premise: the pattern scrub does not know this shape"
    assert is_redacted(redact_text(prompt)), "and the stability check cannot see it either: it asks the same patterns"
    assert count_leaks(redact_text(prompt), collect_sensitive_values({"callsign": identifier})) == 1


@pytest.mark.parametrize("identifier", ["AAL1234", "UAL5678", "N12345", "RPA4451A"])
def test_what_the_patterns_do_catch_leaves_nothing_for_the_check(identifier):
    prompt = f"Description: {identifier} reported a go-around"
    assert count_leaks(redact_text(prompt), {identifier}) == 0


# The corpus: known-sensitive identifiers in the shapes real feeds and people produce, each placed in the
# sentences that carry them to a model. The property is the whole point of the check, and a new shape that
# breaks it is a failing test, not a finding for an auditor:
#
#     whatever the scrub leaves in, the value check finds.
CORPUS = [
    "AAL1234", "UAL5678", "DAL9", "RPA4451A", "N12345", "N9XY", "N123AB",                 # what the patterns know
    "UAL12AB", "G-ABCD", "VH-XYZ", "C-FABC", "D-EABC", "9V-SMA", "JA8089", "HL7700",      # what they do not
    "A7CC0B", "b4de8d", "ADF2A1",                                                           # hex addresses
    "38.8951", "-77.0364", "11000",                                                         # positions, altitudes
    "GUFI-2026-0827-000123", "ACID-77AB",                                                   # plan identifiers
]
TEMPLATES = [
    "Description: {x} reported a go-around at KDCA",
    '{{"note": "{x} lost track", "airport": "KIAD"}}',
    "Prior alert: surface event for {x}; see also the ticket for {x}.",
    "Operator note: called about {x}, then again about {x}",
]


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("identifier", CORPUS)
def test_whatever_the_scrub_leaves_in_the_value_check_finds(identifier, template):
    prompt = redact_text(template.format(x=identifier))
    protected = collect_sensitive_values({"aircraft_id": identifier, "latitude": identifier, "callsign": identifier})
    if identifier.lstrip("-").replace(".", "").isdigit() and len(identifier.replace(".", "").lstrip("-")) < 5:
        pytest.skip("too short to mean anything: covered by the keyed and pattern layers")
    still_there = bool(re.search(re.escape(identifier.lstrip("-")), prompt, re.IGNORECASE))
    assert not still_there or count_leaks(prompt, protected) >= 1, f"{identifier!r} reached the prompt unnoticed"


@pytest.mark.parametrize("identifier", [i for i in CORPUS if not i.lstrip("-")[:1].isdigit()])
def test_a_projected_payload_never_carries_an_identifier_to_the_model(identifier):
    payload = {"flights": [{"aircraft_id": identifier, "latitude": 38.8951, "event": "go-around"}]}
    sent = redact_text(json.dumps(pseudonymize_structured(payload, Pseudonymizer())))
    assert identifier not in sent
    assert count_leaks(sent, collect_sensitive_values(payload)) == 0


# ── Pseudonyms ───────────────────────────────────────────────────────────────────────────────────────

def test_the_same_flight_gets_the_same_token_whatever_its_case_or_spacing():
    pseudonyms = Pseudonymizer()
    assert pseudonyms.token("AAL1234") == pseudonyms.token(" aal1234 ") == "AC-1"
    assert pseudonyms.token("UAL5678") == "AC-2"


def test_tokens_do_not_carry_across_prompts():
    first, second = Pseudonymizer(), Pseudonymizer()
    first.token("AAL1234")
    assert second.token("UAL5678") == "AC-1"                 # the same token names a different flight here


def test_an_identity_key_keeps_its_place_with_a_token_and_a_measurement_is_dropped():
    payload = {"flights": [{"aircraft_id": "AAL1234", "latitude": 38.9, "minutes": 4},
                           {"aircraft_id": "UAL5678", "altitude": 3000}], "airport": "KDCA"}
    assert pseudonymize_structured(payload, Pseudonymizer()) == {
        "flights": [{"aircraft_id": "AC-1", "minutes": 4}, {"aircraft_id": "AC-2"}], "airport": "KDCA"}


def test_the_same_flight_keeps_one_token_across_a_payload():
    payload = {"a": {"callsign": "AAL1234"}, "b": [{"flight_id": "aal1234"}]}
    out = pseudonymize_structured(payload, Pseudonymizer())
    assert out["a"]["callsign"] == out["b"][0]["flight_id"] == "AC-1"


def test_a_sensitive_key_holding_more_than_a_plain_value_is_dropped_whole():
    payload = {"aircraft_id": {"nested": "AAL1234"}, "callsign": ["AAL1234"], "registration": "   ", "keep": 1}
    assert pseudonymize_structured(payload, Pseudonymizer()) == {"keep": 1}


def test_a_measurement_key_is_dropped_exactly_as_before():
    payload = {"beacon_code": "1200", "raw_payload": "{}", "lat": 1.0, "airport_code": "KDCA"}
    assert pseudonymize_structured(payload, Pseudonymizer()) == redact_structured(payload) == {"airport_code": "KDCA"}


def test_no_identifier_survives_pseudonymizing():
    payload = {"flights": [{"aircraft_id": "AAL1234", "tail_number": "N12345", "callsign": "UAL5678"}]}
    sent = json.dumps(pseudonymize_structured(payload, Pseudonymizer()))
    assert count_leaks(sent, collect_sensitive_values(payload)) == 0
    assert not re.search(r"AAL1234|N12345|UAL5678", sent)


def test_the_second_pass_over_a_serialized_prompt_leaves_a_pseudonym_alone():
    sent = json.dumps(pseudonymize_structured({"aircraft_id": "AAL1234", "n": 1}, Pseudonymizer()))
    out, changed = redact_text_counted(sent)
    assert out == sent and changed == 0
    assert is_redacted(sent)


def test_the_second_pass_still_blanks_a_real_identifier_next_to_a_pseudonym():
    out, changed = redact_text_counted('{"aircraft_id": "AC-1", "callsign": "AAL1234"}')
    assert '"AC-1"' in out and "AAL1234" not in out and changed == 1


def test_a_value_that_only_looks_like_a_token_is_still_blanked():
    out = redact_text('{"aircraft_id": "AC-1X"}')
    assert "AC-1X" not in out


def test_reveal_puts_the_flights_back_for_the_operator():
    pseudonyms = Pseudonymizer()
    pseudonyms.token("AAL1234")
    pseudonyms.token("ual5678")
    assert pseudonyms.reveal("AC-2 waited longer than AC-1.") == "ual5678 waited longer than AAL1234."


def test_reveal_leaves_a_token_the_model_made_up_and_text_that_only_resembles_one():
    pseudonyms = Pseudonymizer()
    pseudonyms.token("AAL1234")
    assert pseudonyms.reveal("AC-9 and NAC-1 and AC-1X") == "AC-9 and NAC-1 and AC-1X"
    assert Pseudonymizer().reveal("AC-1") == "AC-1"


# ── Every call to the model passes its protected values ──────────────────────────────────────────────

def _calls_to_the_model():
    for path in sorted((APP / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("acall_with_tool", "call_with_tool")):
                yield path.relative_to(APP).as_posix(), node


def test_every_call_site_hands_the_chokepoint_its_protected_values():
    """
    A caller that forgets leaves the pattern scrub as the only defence. This fails when a new call site is
    added without the argument, so the omission is a decision someone has to make in review.
    """
    sites = list(_calls_to_the_model())
    assert sites, "the walk found no call sites: the test is no longer looking at anything"
    missing = [f"{path}:{node.lineno}" for path, node in sites if "protected_values" not in {k.arg for k in node.keywords}]
    assert not missing, f"model calls without protected_values: {missing}"


# ── The policy and the alert ─────────────────────────────────────────────────────────────────────────

def test_the_policy_blocks_a_leak_ahead_of_every_rule_but_credentials():
    rules = yaml.safe_load((APP / "policies" / "model-use.yaml").read_text(encoding="utf-8"))["rules"]
    by_name = {r["name"]: r for r in rules}
    leak = by_name["no-protected-values-in-prompts"]
    assert leak["action"] == "block"
    assert leak["priority"] > max(r["priority"] for n, r in by_name.items()
                                  if n not in ("no-protected-values-in-prompts", "no-credentials-in-prompts"))
    assert leak["priority"] < by_name["no-credentials-in-prompts"]["priority"]


def test_a_blocked_leak_raises_its_own_alert_and_is_not_double_counted_as_a_warning():
    rules = {r["alert"]: r for g in yaml.safe_load((APP / "monitoring" / "alert-rules.yml").read_text(encoding="utf-8"))["groups"]
             for r in g["rules"]}
    leak = rules["ProtectedValueBlockedFromPrompt"]
    assert leak["labels"]["severity"] == "critical"
    assert "no-protected-values-in-prompts" in leak["expr"] and 'agt_audit_action="model_call"' in leak["expr"]
    assert "no-protected-values-in-prompts" in rules["ModelCallsRefusedByPolicy"]["expr"], \
        "the generic warning must exclude it, or one block pages twice"
    assert "ProtectedValueBlockedFromPrompt" in (APP / "docs" / "runbooks.md").read_text(encoding="utf-8")


# ── Corners of the helpers ───────────────────────────────────────────────────────────────────────────

def test_a_bare_value_in_a_validation_error_ends_at_the_next_space():
    out = redact_text("1 validation error for NASEvent\n  Input should be valid [input_value=AAL1234 extra words, input_type=str]")
    assert "AAL1234" not in out and "extra words" in out


def test_the_second_pass_leaves_an_xml_pseudonym_alone_and_blanks_a_real_one():
    out = redact_text("<aircraftId>AC-1</aircraftId><callsign>AAL1234</callsign>")
    assert "<aircraftId>AC-1</aircraftId>" in out and "AAL1234" not in out


def test_a_sensitive_key_holding_a_list_or_an_object_is_read_value_by_value():
    record = {"callsign": ["AAL1234", {"inner": "UAL5678"}], "registration": ("G-ABCD",)}
    assert collect_sensitive_values(record) == {"AAL1234", "UAL5678", "G-ABCD"}


def test_a_document_too_deep_to_be_a_record_is_not_followed_forever():
    deep: dict = {"callsign": "AAL1234"}
    for _ in range(30):
        deep = {"wrap": deep}
    assert collect_sensitive_values(deep) == set()


def test_values_too_short_to_compare_are_ignored_and_a_check_with_only_those_finds_nothing():
    assert count_leaks("AB went around and A1 too", {"AB", "A1", "x"}) == 0
    assert count_leaks("AAL1234 went around", {"AB", "AAL1234"}) == 1
