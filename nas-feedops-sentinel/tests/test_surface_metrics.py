"""
Taxi-time arithmetic (src/surface_metrics.py) and the question router
(src/surface_tools.route). Pure: no database, no model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import surface_metrics as sm
from src import surface_tools

AS_OF = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _t(hh: int, mm: int) -> datetime:
    return AS_OF.replace(hour=hh, minute=mm)


def _pair(aircraft, out, off, airport="KIAD"):
    return {"aircraft_id": aircraft, "airport_code": airport, "spot_out_at": out, "wheels_off_at": off}


# -- taxi_stats -----------------------------------------------------------------

def test_the_window_counts_only_taxis_that_finished_inside_it_but_the_trend_keeps_the_hour():
    pairs = [_pair("UAL1", _t(11, 40), _t(11, 52)), _pair("DAL2", _t(11, 50), _t(11, 56)),
             _pair("AAL3", _t(11, 20), _t(11, 35))]                      # ended 25 min ago: outside a 15 min window
    result = sm.taxi_stats(pairs, "spot_out_at", "wheels_off_at", airport="KIAD", window_minutes=15,
                           as_of=AS_OF, latest_event=_t(11, 59))
    assert (result["completed"], result["avg_minutes"], result["max_minutes"]) == (2, 9.0, 12.0)
    assert result["longest"]["aircraft_id"] == "UAL1"
    assert result["hourly_trend"] == [{"hour_utc": _t(11, 0).isoformat(), "completed": 3, "avg_minutes": 11.0}]


def test_a_small_sample_is_said_to_be_small():
    pairs = [_pair("UAL1", _t(11, 40), _t(11, 52))]
    result = sm.taxi_stats(pairs, "spot_out_at", "wheels_off_at", airport="KIAD", window_minutes=30,
                           as_of=AS_OF, latest_event=_t(11, 59))
    assert "only 1 completed" in result["note"]


def test_no_completed_taxi_gives_no_average_rather_than_zero():
    result = sm.taxi_stats([], "spot_out_at", "wheels_off_at", airport="KBWI", window_minutes=15,
                           as_of=AS_OF, latest_event=_t(11, 59))
    assert result["completed"] == 0 and result["avg_minutes"] is None and result["max_minutes"] is None
    assert "No taxi completed" in result["note"]


def test_stale_surface_data_is_flagged_so_a_quiet_answer_is_not_mistaken_for_a_quiet_airport():
    fresh = sm.freshness(_t(11, 55), AS_OF)
    stale = sm.freshness(_t(9, 0), AS_OF)
    empty = sm.freshness(None, AS_OF)
    assert (fresh["stale"], stale["stale"], empty["stale"]) == (False, True, True)
    assert stale["minutes_since"] == 180.0 and "feed may be stopped" in stale["note"]


# -- queue, gate returns, timeline ------------------------------------------------

def test_the_departure_queue_lists_longest_waiting_first_and_filters_by_minutes():
    rows = [{"aircraft_id": "SWA4", "airport_code": "KIAD", "spot_out_at": _t(11, 45)},
            {"aircraft_id": "DAL7", "airport_code": "KIAD", "spot_out_at": _t(11, 59)}]
    everyone = sm.queue_summary(rows, airport="KIAD", as_of=AS_OF, latest_event=_t(11, 59))
    long_only = sm.queue_summary(rows, airport="KIAD", as_of=AS_OF, latest_event=_t(11, 59), min_minutes=10)
    assert [f["aircraft_id"] for f in everyone["flights"]] == ["SWA4", "DAL7"]
    assert everyone["in_departure_queue"] == 2 and everyone["flights"][0]["taxiing_minutes"] == 15.0
    assert (long_only["in_departure_queue"], long_only["matching"]) == (2, 1)


def test_a_timeline_works_out_taxi_out_taxi_in_and_a_return_to_the_gate():
    def ev(kind, at, airport="KIAD"):
        return {"surface_event_type": kind, "event_time_utc": at, "airport_code": airport, "runway": None}

    full = sm.flight_timeline("UAL1", [ev("SPOT_OUT", _t(10, 0)), ev("WHEELS_OFF", _t(10, 14)),
                                       ev("WHEELS_ON", _t(11, 0), "KDCA"), ev("SPOT_IN", _t(11, 4), "KDCA")], as_of=AS_OF)
    assert full["taxi_out"]["minutes"] == 14.0 and full["taxi_in"]["minutes"] == 4.0
    assert full["returned_to_gate"] is False

    back = sm.flight_timeline("JBU5", [ev("SPOT_OUT", _t(10, 0)), ev("SPOT_IN", _t(10, 11))], as_of=AS_OF)
    assert back["returned_to_gate"] is True and back["taxi_out"] is None

    assert "No surface events" in sm.flight_timeline("XXX1", [], as_of=AS_OF)["note"]


# -- the router ---------------------------------------------------------------------

def _calls(question):
    return [(tool, args) for tool, args in surface_tools.route(question)]


def test_a_taxi_out_question_names_its_airport_and_window():
    assert _calls("What's the average taxi-out time at IAD over the last 15 minutes?") == [
        ("read_taxi_out_stats", {"airport": "KIAD", "window_minutes": 15})]
    assert _calls("average taxi out at dca in the past hour")[0][1] == {"airport": "KDCA", "window_minutes": 60}
    assert _calls("taxi-out at BWI over the last 2 hours")[0][1]["window_minutes"] == 120


def test_a_question_that_names_no_airport_asks_all_of_them():
    airports = {args["airport"] for _, args in _calls("what is the average taxi-out time right now?")}
    assert {"KIAD", "KDCA", "KBWI"} <= airports


def test_queue_questions_use_the_departure_queue_and_read_minutes_from_the_question():
    tool, args = _calls("Which flights have been taxiing out at BWI for more than 30 minutes right now?")[0]
    assert (tool, args) == ("read_departure_queue", {"airport": "KBWI", "min_minutes": 30.0})
    assert ("read_departure_queue", {"airport": "KDCA", "min_minutes": 0.0}) in _calls(
        "How many aircraft are in the departure queue at DCA?")


def test_gate_return_and_taxi_in_and_a_named_flight_are_recognised():
    assert _calls("Did any flights return to the gate at IAD today?")[0] == (
        "read_gate_returns", {"airport": "KIAD", "window_minutes": 24 * 60})
    assert _calls("What's the average taxi-in time at IAD right now?")[0][0] == "read_taxi_in_stats"
    assert _calls("When did AAL567 push back, and when did it take off?") == [
        ("read_flight_timeline", {"aircraft_id": "AAL567"})], "a question about one flight pulls no airport-wide stats"


@pytest.mark.parametrize("question", [
    "What's the current RVR at IAD?", "Is anything unusual happening?", "status briefing", "why is the feed stale",
    "the total of THE flights",                 # capitalised words are not callsigns without digits
    "What is in the human review queue?",       # the review queue is not the departure queue
    "How many items are in the review queue at KIAD?",
    "When did rwy01 last change at IAD?",       # runway 01 is not a flight
])
def test_other_questions_use_no_taxi_tools(question):
    assert surface_tools.route(question) == []


def test_a_callsign_is_found_however_it_is_typed():
    for text in ("When did UAL1234 push back?", "when did ual1234 push back?", "When did Ual1234 push back?"):
        assert surface_tools.route(text) == [("read_flight_timeline", {"aircraft_id": "UAL1234"})], text


def test_a_departure_queue_is_recognised_by_name_or_by_airport():
    for text in ("How many aircraft are in the departure queue at DCA?", "what's the queue at IAD?",
                 "How long is the taxi queue?"):
        assert surface_tools.route(text)[0][0] == "read_departure_queue", text


def test_taxi_time_with_no_direction_asks_for_both():
    tools = [tool for tool, _ in surface_tools.route("What is the average taxi time at IAD?")]
    assert tools == ["read_taxi_out_stats", "read_taxi_in_stats"]


async def test_a_taxi_question_the_router_cannot_read_says_nothing_was_fetched_instead_of_staying_silent():
    """No database is touched: nothing matched."""
    result = await surface_tools.gather("How long are departures waiting to get airborne at Dulles?")
    assert set(result) == {"not_fetched"} and "do not state a taxi time" in result["not_fetched"].lower()
    assert surface_tools.has_figures(result) is False
    assert "No surface figures were fetched" in " ".join(surface_tools.describe(result))
    assert await surface_tools.gather("what is the current RVR at IAD?") == {}


# -- wording, the suspect flag, and describe() ------------------------------------------

@pytest.mark.parametrize("minutes, label", [(1, "1 minute"), (15, "15 minutes"), (60, "1 hour"),
                                            (90, "90 minutes"), (120, "2 hours"), (1440, "24 hours")])
def test_a_window_is_worded_the_way_a_person_says_it(minutes, label):
    assert sm.window_label(minutes) == label


def test_today_is_reported_as_the_last_24_hours_not_as_1440_minutes():
    result = sm.taxi_stats([], "a", "b", airport="KIAD", window_minutes=1440, as_of=AS_OF, latest_event=_t(11, 59))
    assert result["window_label"] == "last 24 hours" and "last 24 hours" in result["note"]


def test_a_flight_out_for_45_minutes_with_no_takeoff_is_marked_as_probably_departed():
    rows = [{"aircraft_id": "OLD1", "airport_code": "KIAD", "spot_out_at": _t(10, 30)},      # 90 min
            {"aircraft_id": "NEW2", "airport_code": "KIAD", "spot_out_at": _t(11, 50)}]      # 10 min
    result = sm.queue_summary(rows, airport="KIAD", as_of=AS_OF, latest_event=_t(11, 59))
    flags = {f["aircraft_id"]: f["suspect"] for f in result["flights"]}
    assert flags == {"OLD1": True, "NEW2": False}
    assert result["possibly_departed"] == 1 and "probably not received" in result["note"]
    assert "take-off probably not recorded" in " ".join(surface_tools.describe({"read_departure_queue": [result]}))


def test_describe_words_every_kind_of_result_and_names_the_flights():
    def ev(kind, at, airport="KIAD"):
        return {"surface_event_type": kind, "event_time_utc": at, "airport_code": airport, "runway": None}

    returns = sm.gate_return_summary(
        [{"aircraft_id": "JBU5", "airport_code": "KIAD", "spot_out_at": _t(11, 30), "spot_in_at": _t(11, 41)}],
        airport="KIAD", window_minutes=1440, as_of=AS_OF, latest_event=_t(11, 59))
    timeline = sm.flight_timeline("UAL1", [ev("SPOT_OUT", _t(10, 0)), ev("WHEELS_OFF", _t(10, 14))], as_of=AS_OF)
    text = " ".join(surface_tools.describe({
        "read_gate_returns": [returns], "read_flight_timeline": [timeline],
        "unavailable": [{"tool": "read_taxi_in_stats", "reason": "refused"}]}))
    assert "JBU5 (11.0 min out)" in text and "last 24 hours" in text
    assert "UAL1: SPOT_OUT at KIAD 10:00Z, WHEELS_OFF at KIAD 10:14Z" in text and "taxi-out 14.0 min" in text
    assert "read_taxi_in_stats was unavailable (refused)" in text


def test_the_same_call_is_never_made_twice():
    calls = surface_tools.route("taxi-out taxi out pushback at IAD, IAD")
    assert len(calls) == len(set((t, tuple(sorted(a.items()))) for t, a in calls))


def test_an_absurd_window_is_capped_to_a_day():
    assert _calls("taxi-out at IAD over the last 9999 hours")[0][1]["window_minutes"] == 24 * 60
    assert timedelta(minutes=surface_tools.MAX_WINDOW_MINUTES) == timedelta(days=1)
