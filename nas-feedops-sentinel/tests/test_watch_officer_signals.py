"""
The Watch Officer's rules before any model writes a word (src/watch_officer_agent.py): normalising the
model's narrative, rebuilding events from storage rows, the signals, the posture and trust, and what the
model call is handed. No database and no gateway.
"""
from __future__ import annotations

import pytest

import src.watch_officer_agent as wo
from src.models import BriefAssessment, DataHealth


# ── The model's narrative is normalised, not trusted ─────────────────────────────────────────────────

def test_a_missing_headline_gets_a_plain_placeholder_and_a_present_one_is_trimmed():
    assert wo.RawLLMNarrative(headline=None).headline == "Operational watch summary unavailable."
    assert wo.RawLLMNarrative(headline="  All quiet  ").headline == "All quiet"


@pytest.mark.parametrize("given, expected", [
    (None, []),
    ("one line", ["one line"]),
    (["a", 2, None], ["a", "2", "None"]),
    (7, ["7"]),
])
def test_every_narrative_list_is_a_list_of_strings_whatever_the_model_sent(given, expected):
    narrative = wo.RawLLMNarrative(headline="h", what_changed=given, limitations=given)
    assert narrative.what_changed == expected and narrative.limitations == expected


# ── Rebuilding events from storage rows ──────────────────────────────────────────────────────────────

def _row(**overrides):
    row = {"event_id": "e1", "airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC",
           "message_type": "link_status", "event_time_utc": "2026-10-01T12:00:00+00:00",
           "received_time_utc": "2026-10-01T12:00:01+00:00"}
    row.update(overrides)
    return row


def test_timestamps_stored_as_text_are_parsed():
    [event], failed = wo.rebuild_events([_row()])

    assert failed == 0 and event.event_time_utc.year == 2026 and event.received_time_utc.second == 1


@pytest.mark.parametrize("stored, expected", [
    ('["a", 2]', ["a", "2"]),            # JSON text
    ('{"not": "a list"}', []),           # JSON that is not a list
    ("not json", []),                    # not JSON at all
    ([1, "b"], ["1", "b"]),              # already a list
])
def test_validation_errors_come_back_as_a_list_of_strings(stored, expected):
    [event], failed = wo.rebuild_events([_row(validation_errors=stored)])

    assert failed == 0 and event.validation_errors == expected


def test_a_row_that_cannot_be_rebuilt_is_counted_and_the_rest_still_are():
    events, failed = wo.rebuild_events([_row(), _row(airport_code="XXXX", id=7), _row(event_id="e3")])

    assert failed == 1 and [e.event_id for e in events] == ["e1", "e3"]


# ── Signals ──────────────────────────────────────────────────────────────────────────────────────────

def test_airport_statuses_become_signals():
    signals = wo._detect_signals({
        "airport_status": {"KDCA": "STALE", "KIAD": "DISCONNECTED", "KBWI": "DEGRADED", "KADW": "OK"},
        "pending_actions_count": 1, "critical_alert_count": 2,
        "events_in_window": 80, "active_aircraft": 0, "avg_latency_ms": 300_000,
        "recent_critic_results": [{"escalate": False}, {"escalate": True}],
    })

    assert signals["stale_airports"] == ["KDCA"]
    assert signals["missing_airports"] == ["KIAD"]
    assert signals["degraded_airports"] == ["KBWI"]
    assert signals["multi_airport_degradation"] is True
    assert signals["pending_actions_exist"] and signals["critical_alerts_exist"]
    assert signals["critic_escalation_exists"] and signals["latency_severe"]
    assert signals["no_active_aircraft"] and signals["position_event_mismatch"]
    assert not signals["draft_tickets_exist"] and not signals["no_recent_events"]


def test_an_empty_context_is_quiet_but_unknown():
    signals = wo._detect_signals({})

    assert signals["no_recent_events"] and signals["no_active_aircraft"]
    assert not signals["latency_severe"] and not signals["multi_airport_degradation"]


# ── Posture and trust ────────────────────────────────────────────────────────────────────────────────

def _signals(**on):
    base = {"stale_airports": [], "missing_airports": [], "degraded_airports": [], "latency_severe": False,
            "position_event_mismatch": False, "pending_actions_exist": False, "draft_tickets_exist": False,
            "no_recent_events": False, "no_active_aircraft": False}
    base.update(on)
    return base


def _assessment(state="OK", reviews=0, criticals=0):
    return BriefAssessment(current_review_count=reviews, current_critical_count=criticals,
                           data_health=DataHealth(state=state))


@pytest.mark.parametrize("signals, context, assessment, posture", [
    (_signals(draft_tickets_exist=True), {}, None, "ACTION"),
    (_signals(pending_actions_exist=True), {}, None, "ACTION"),
    (_signals(), {}, _assessment(reviews=1), "ACTION"),
    (_signals(), {}, _assessment(criticals=1), "ACTION"),
    (_signals(), {}, _assessment("UNKNOWN"), "UNKNOWN"),
    (_signals(), {}, _assessment("DOWN"), "ATTENTION"),
    (_signals(), {}, _assessment("DEGRADED"), "ATTENTION"),
    (_signals(stale_airports=["KDCA"]), {}, None, "ATTENTION"),
    (_signals(latency_severe=True), {"events_in_window": 5}, None, "ATTENTION"),
    (_signals(), {"active_aircraft": 3}, None, "NORMAL"),
    (_signals(), {"events_in_window": 51}, None, "NORMAL"),
    (_signals(), {"events_in_window": 5}, None, "GOOD"),
    (_signals(), {}, None, "UNKNOWN"),
])
def test_the_posture_follows_the_assessment_then_the_signals(signals, context, assessment, posture):
    assert wo._derive_posture(signals, context, assessment) == posture


@pytest.mark.parametrize("signals, context, assessment, trust", [
    (_signals(), {"events_in_window": 9}, _assessment("DOWN"), "DEGRADED"),
    (_signals(no_recent_events=True), {}, None, "UNKNOWN"),
    (_signals(missing_airports=["KIAD"]), {"events_in_window": 9}, None, "INCOMPLETE"),
    (_signals(no_active_aircraft=True), {"events_in_window": 9}, None, "INCOMPLETE"),
    (_signals(stale_airports=["KDCA"]), {"events_in_window": 9}, None, "DEGRADED"),
    (_signals(degraded_airports=["KBWI"]), {"events_in_window": 9}, None, "DEGRADED"),
    (_signals(latency_severe=True), {"events_in_window": 9}, None, "DEGRADED"),
    (_signals(), {"events_in_window": 9}, None, "TRUSTWORTHY"),
    (_signals(), {"events_in_window": 0}, None, "UNKNOWN"),
])
def test_trust_is_what_the_data_health_and_signals_allow(signals, context, assessment, trust):
    assert wo._derive_trust(signals, context, assessment) == trust


# ── What the model call is handed ────────────────────────────────────────────────────────────────────

async def test_no_gateway_means_no_model_narrative(monkeypatch):
    monkeypatch.setattr(wo, "build_llm_client", lambda model=None: None)

    with pytest.raises(RuntimeError, match="no LLM client configured"):
        await wo._llm_narrative({}, {})


async def test_the_model_is_handed_a_projection_and_the_values_it_must_not_receive(monkeypatch):
    seen: dict = {}

    class _Client:
        async def acall_with_tool(self, **kwargs):
            seen.update(kwargs)
            return {"headline": "ok"}

    monkeypatch.setattr(wo, "build_llm_client", lambda model=None: _Client())
    monkeypatch.setattr(wo, "project_for_model", lambda context, signals, assessment: {
        "triggering_alert": {"aircraft_id": "AAL1234", "note": "go-around", "airport": "KDCA"}})
    context = {"triggering_alert": {"aircraft_id": "AAL1234", "latitude": 38.8951}}

    narrative = await wo._llm_narrative(context, {})

    assert narrative.headline == "ok"
    assert seen["protected_values"] == {"AAL1234", "38.8951"}, "read from the raw snapshot before the projection drops them"
    assert "AAL1234" not in seen["user_message"] and "AC-1" in seen["user_message"], "the flight goes out as a token"
    assert seen["role"] == "watch_officer"
