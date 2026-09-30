"""
The taxi-time tools against a real database (src/surface_tools.py,
src/storage/repositories/surface_repo.py), through the tool-use policy, and
as the Planner's answer when no model is configured.

Surface events at KIAD, with the moment being asked about fixed at 12:00 UTC:

  UAL1  spot out 11:40  wheels off 11:52   taxi-out 12 min   (also a wheels-off at KDCA: another airport)
  DAL2  spot out 11:50  wheels off 11:56   taxi-out  6 min
  AAL3  spot out 11:20  wheels off 11:35   taxi-out 15 min   (finished 25 min ago: outside a 15 min window)
  SWA4  spot out 11:45  nothing since      in the departure queue, 15 min
  DAL7  spot out 11:59  wheels off 12:05   after the question: still in the queue, 1 min
  JBU5  spot out 11:30  spot in 11:41      went back to the gate
  ASA6  wheels on 11:50 spot in 11:54      taxi-in 4 min
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import agent_tools, storage, surface_tools
from src.models import NASEvent
from src.planner_agent import PlannerAgent

AS_OF = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _at(hh: int, mm: int) -> datetime:
    return AS_OF.replace(hour=hh, minute=mm)


def _event(kind: str, aircraft: str, at: datetime, airport: str = "KIAD") -> NASEvent:
    return NASEvent(airport_code=airport, tracon="PCT", service_name="SMES", message_type="SMES_SE",
                    event_time_utc=at, received_time_utc=at + timedelta(seconds=3),
                    aircraft_id=aircraft, track_id=f"t-{aircraft}", surface_event_type=kind, validation_status="PASSED")


async def _seed() -> None:
    for e in [
        _event("SPOT_OUT", "UAL1", _at(11, 40)), _event("WHEELS_OFF", "UAL1", _at(11, 52)),
        _event("WHEELS_OFF", "UAL1", _at(11, 55), "KDCA"),
        _event("SPOT_OUT", "DAL2", _at(11, 50)), _event("WHEELS_OFF", "DAL2", _at(11, 56)),
        _event("SPOT_OUT", "AAL3", _at(11, 20)), _event("WHEELS_OFF", "AAL3", _at(11, 35)),
        _event("SPOT_OUT", "SWA4", _at(11, 45)),
        _event("SPOT_OUT", "DAL7", _at(11, 59)), _event("WHEELS_OFF", "DAL7", _at(12, 5)),
        _event("SPOT_OUT", "JBU5", _at(11, 30)), _event("SPOT_IN", "JBU5", _at(11, 41)),
        _event("WHEELS_ON", "ASA6", _at(11, 50)), _event("SPOT_IN", "ASA6", _at(11, 54)),
    ]:
        await storage.save_event(e)


# -- the tools, as the Planner ------------------------------------------------------

async def test_taxi_out_stats_pairs_each_spot_out_with_the_same_aircrafts_next_wheels_off():
    await _seed()
    with agent_tools.acting_as("planner"):
        kiad = await surface_tools.read_taxi_out_stats("KIAD", 15, as_of=AS_OF)
        kdca = await surface_tools.read_taxi_out_stats("KDCA", 15, as_of=AS_OF)
    assert (kiad["completed"], kiad["avg_minutes"], kiad["max_minutes"]) == (2, 9.0, 12.0)
    assert kiad["longest"]["aircraft_id"] == "UAL1"
    assert kiad["hourly_trend"][0]["completed"] == 3, "the 15 minute window is not the trend"
    assert kdca["completed"] == 0, "a wheels-off at another airport is not this airport's taxi-out"
    assert "only 2 completed" in kiad["note"]


async def test_a_wheels_off_after_the_moment_asked_about_does_not_count():
    await _seed()
    with agent_tools.acting_as("planner"):
        result = await surface_tools.read_taxi_out_stats("KIAD", 60, as_of=AS_OF)
    assert "DAL7" not in [result["longest"]["aircraft_id"]] and result["completed"] == 3


async def test_the_departure_queue_is_who_left_the_gate_and_has_neither_taken_off_nor_come_back():
    await _seed()
    with agent_tools.acting_as("planner"):
        queue = await surface_tools.read_departure_queue("KIAD", as_of=AS_OF)
        long_only = await surface_tools.read_departure_queue("KIAD", min_minutes=10, as_of=AS_OF)
    assert [(f["aircraft_id"], f["taxiing_minutes"]) for f in queue["flights"]] == [("SWA4", 15.0), ("DAL7", 1.0)]
    assert [f["aircraft_id"] for f in long_only["flights"]] == ["SWA4"] and long_only["in_departure_queue"] == 2


async def test_gate_returns_and_taxi_in_and_a_flights_timeline():
    await _seed()
    with agent_tools.acting_as("planner"):
        returns = await surface_tools.read_gate_returns("KIAD", 120, as_of=AS_OF)
        taxi_in = await surface_tools.read_taxi_in_stats("KIAD", 30, as_of=AS_OF)
        timeline = await surface_tools.read_flight_timeline("UAL1", as_of=AS_OF)
    assert [(f["aircraft_id"], f["minutes_out"]) for f in returns["flights"]] == [("JBU5", 11.0)]
    assert (taxi_in["completed"], taxi_in["avg_minutes"]) == (1, 4.0)
    assert timeline["taxi_out"]["minutes"] == 12.0
    assert [e["airport"] for e in timeline["events"]] == ["KIAD", "KIAD", "KDCA"]


async def test_hours_after_the_last_event_the_answer_says_the_data_is_stale():
    await _seed()
    with agent_tools.acting_as("planner"):
        result = await surface_tools.read_taxi_out_stats("KIAD", 15, as_of=AS_OF + timedelta(hours=2))
    assert result["completed"] == 0 and result["freshness"]["stale"] is True
    assert "feed may be stopped" in result["freshness"]["note"]


async def test_a_quiet_airport_whose_position_reports_still_flow_is_not_called_stale():
    """Spot and wheels events are sparse; the feed is alive if any SMES message is recent."""
    await storage.save_event(NASEvent(
        airport_code="KBWI", tracon="PCT", service_name="SMES", message_type="SMES_AT",
        event_time_utc=_at(11, 58), received_time_utc=_at(11, 58), aircraft_id="SWA9",
        surface_event_type="POSITION_REPORT", validation_status="PASSED"))
    with agent_tools.acting_as("planner"):
        result = await surface_tools.read_taxi_out_stats("KBWI", 15, as_of=AS_OF)
    assert result["completed"] == 0 and result["freshness"]["stale"] is False


async def test_a_flight_that_pushed_back_returned_and_pushed_back_again_is_counted_once_from_its_last_pushback():
    """The first pushback and the wait at the gate are not taxi time, and the flight is one taxi-out, not two."""
    for e in [_event("SPOT_OUT", "EGL1", _at(11, 0)), _event("SPOT_IN", "EGL1", _at(11, 10)),
              _event("SPOT_OUT", "EGL1", _at(11, 40)), _event("SPOT_OUT", "EGL1", _at(11, 40) + timedelta(seconds=20)),
              _event("WHEELS_OFF", "EGL1", _at(11, 50))]:
        await storage.save_event(e)
    with agent_tools.acting_as("planner"):
        taxi_out = await surface_tools.read_taxi_out_stats("KIAD", 30, as_of=AS_OF)
        returns = await surface_tools.read_gate_returns("KIAD", 120, as_of=AS_OF)
    assert taxi_out["completed"] == 1
    assert 9.5 <= taxi_out["max_minutes"] <= 10.0, "from the last pushback (11:40:20), not the first (11:00)"
    assert [f["aircraft_id"] for f in returns["flights"]] == ["EGL1"] and returns["returned_to_gate"] == 1


# -- the boundary ---------------------------------------------------------------------

async def test_an_agent_that_was_not_granted_the_tool_is_refused_and_it_does_not_run():
    await _seed()
    with agent_tools.acting_as("critic"), pytest.raises(agent_tools.ToolCallDenied):
        await surface_tools.read_taxi_out_stats("KIAD", 15, as_of=AS_OF)


async def test_a_refused_tool_is_reported_in_the_result_instead_of_stopping_the_answer():
    await _seed()
    with agent_tools.acting_as("critic"):
        results = await surface_tools.gather("average taxi-out at IAD", as_of=AS_OF)
    assert results["unavailable"][0]["tool"] == "read_taxi_out_stats" and "refused" in results["unavailable"][0]["reason"]


async def test_gather_runs_only_what_the_question_needs():
    await _seed()
    with agent_tools.acting_as("planner"):
        results = await surface_tools.gather("How many aircraft are in the departure queue at IAD?", as_of=AS_OF)
        nothing = await surface_tools.gather("what is the current RVR at IAD?", as_of=AS_OF)
    assert list(results) == ["read_departure_queue"] and results["read_departure_queue"][0]["in_departure_queue"] == 2
    assert nothing == {}


# -- the Planner ----------------------------------------------------------------------

async def test_with_no_model_the_planner_answers_a_taxi_question_from_the_figures(monkeypatch):
    import src.planner_agent as planner

    await _seed()
    monkeypatch.setattr(planner.settings, "CRITIC_ENABLED", False)
    real_gather = surface_tools.gather

    async def at_noon(question, as_of=None):
        return await real_gather(question, as_of=AS_OF)

    monkeypatch.setattr(planner.surface_tools, "gather", at_noon)
    result = await PlannerAgent().query("What's the average taxi-out time at IAD over the last 15 minutes?")

    assert result.confidence is None, "no model made this assessment"
    assert "KIAD: average taxi-out 9.0 min over 2 flight(s)" in result.answer
    assert "longest 12.0 min, UAL1" in result.answer and "only 2 completed" in result.answer
    assert result.recommended_actions, "an answer is never left without a next step"


async def test_one_tool_failing_is_reported_and_the_others_still_answer(monkeypatch):
    await _seed()

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(storage, "get_departure_queue", boom)
    with agent_tools.acting_as("planner"):
        results = await surface_tools.gather("taxi-out at IAD, and the departure queue at IAD", as_of=AS_OF)
    assert results["read_taxi_out_stats"][0]["completed"] == 2, "the healthy tool still answered"
    assert results["unavailable"] == [{"tool": "read_departure_queue", "reason": "RuntimeError: database went away"}]
    assert surface_tools.has_figures(results) is True


async def _planner(monkeypatch, llm=None):
    import src.planner_agent as planner

    monkeypatch.setattr(planner.settings, "CRITIC_ENABLED", False)
    real_gather = surface_tools.gather

    async def at_noon(question, as_of=None):
        return await real_gather(question, as_of=AS_OF)

    monkeypatch.setattr(planner.surface_tools, "gather", at_noon)
    agent = PlannerAgent()
    agent._llm = llm
    return agent


async def test_a_taxi_answer_does_not_hide_an_active_critical_alert(monkeypatch):
    from src.models import AlertEvent

    await _seed()
    await storage.save_alert(AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KIAD",
                                        service_name="APDS_RVR", description="Low RVR 600 ft"))
    result = await (await _planner(monkeypatch)).query("What's the average taxi-out time at IAD?")
    assert "KIAD: average taxi-out 9.0 min" in result.answer
    assert "Alerts: " in result.answer and "CRITICAL alerts are active at: KIAD" in result.answer
    assert any("Escalate" in a for a in result.recommended_actions), "the alert's actions are kept"


async def test_a_review_queue_question_is_answered_from_the_alerts_not_the_departure_queue(monkeypatch):
    await _seed()
    result = await (await _planner(monkeypatch)).query("What is in the human review queue?")
    assert "departure" not in result.answer and "taxi" not in result.answer.lower()


async def test_a_model_answer_is_followed_by_the_flights_the_model_never_saw(monkeypatch):
    class Llm:
        async def acall_with_tool(self, **kwargs):
            self.message = kwargs["user_message"]
            return {"answer": "Average taxi-out at KIAD is 9.0 minutes over 2 flights.", "confidence": 0.8,
                    "citations": [], "recommended_actions": ["Keep watching."], "sub_queries": []}

    await _seed()
    llm = Llm()
    result = await (await _planner(monkeypatch, llm)).query("Which flight had the longest taxi-out at IAD?")
    assert result.answer.startswith("Average taxi-out at KIAD is 9.0 minutes")
    assert "From the records (flight identifiers are withheld from the model" in result.answer
    assert "longest 12.0 min, UAL1" in result.answer, "the callsign comes from the records, not the model"
    assert '"read_taxi_out_stats"' in llm.message, "the model was given the figures"


async def test_the_description_of_no_flights_does_not_read_as_a_zero_minute_taxi():
    await _seed()
    with agent_tools.acting_as("planner"):
        lines = surface_tools.describe(await surface_tools.gather("average taxi-out at BWI", as_of=AS_OF))
    assert lines and all("average taxi-out 0" not in line for line in lines)
    assert any("no taxi-out completed" in line for line in lines)


# ── phrasings that used to be misrouted ───────────────────────────────────────

def _tools(question: str) -> set[str]:
    return {tool for tool, _ in surface_tools.route(question)}


def test_still_taxiing_in_the_departure_queue_is_the_queue_not_a_taxi_in():
    """The preposition matched 'taxiing in' and suppressed the queue tool."""
    tools = _tools("Who is still taxiing in the departure queue at BWI?")
    assert "read_departure_queue" in tools and "read_taxi_in_stats" not in tools
    assert "read_taxi_in_stats" in _tools("What is the average taxi-in time at BWI?"), "a real taxi-in still is one"


def test_an_aircraft_type_is_not_a_callsign():
    """'CRJ700' and 'ATR72' have the shape of a callsign; they are types."""
    tools = _tools("Which CRJ700 and ATR72 flights have departed from BWI?")
    assert "read_flight_timeline" not in tools
    assert "read_flight_timeline" in _tools("When did UAL1234 push back?"), "a real callsign still is one"


def test_a_curly_apostrophe_is_read_like_a_straight_one():
    assert _tools("Which flights haven’t taken off at IAD?") == _tools("Which flights haven't taken off at IAD?")
    assert "read_departure_queue" in _tools("Which flights haven’t taken off at IAD?")


async def test_a_question_about_departures_that_matches_no_tool_says_nothing_was_fetched():
    """It used to return nothing at all, and the model answered as if it had consulted the surface data."""
    results = await surface_tools.gather("Have any flights departed the area recently?")
    assert "not_fetched" in results


@pytest.mark.parametrize("question, minutes", [
    ("average taxi-out at IAD over the last 1.5 hours", 90),
    ("average taxi-out at IAD over the last 2 hours", 120),
    ("average taxi-out at IAD over the last 45 minutes", 45),
    ("average taxi-out at IAD over the last 2 days", surface_tools.MAX_WINDOW_MINUTES),   # capped, not 15
    ("average taxi-out at IAD", surface_tools.DEFAULT_WINDOW_MINUTES)])
def test_the_window_asked_for_is_the_window_used(question, minutes):
    assert surface_tools._window_minutes(question) == minutes


def test_an_absurdly_long_number_is_not_parsed():
    """int() refuses more than 4,300 digits and raised out of the router."""
    question = "average taxi-out at IAD over the last " + "9" * 5000 + " minutes"

    assert surface_tools._window_minutes(question) == surface_tools.DEFAULT_WINDOW_MINUTES
    assert "read_taxi_out_stats" in _tools(question)
    assert "read_departure_queue" in _tools("queue at BWI longer than " + "9" * 5000 + " min")
