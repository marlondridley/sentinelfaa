"""
The Watch Officer's brief is decided by rules before any model writes (src/watch_officer_assessment.py).

The scenario that prompted it: our own feed had been down about 12 hours, every airport went stale at the
same moment, and the brief still described an airport infrastructure fault, counted four-day-old alerts as
current, read an RVR of 0 ft as a reading, and asked three people to "investigate". Each rule below is
checked against that scenario and against the ordinary cases.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import watch_officer_assessment as wa

NOW = datetime(2026, 9, 30, 1, 1, tzinfo=timezone.utc)


def _alert(alert_id, kind, airport, when, description="", severity="CRITICAL"):
    return {"alert_id": alert_id, "alert_type": kind, "severity": severity, "airport_code": airport,
            "service_name": "X", "description": description, "triggered_at": when}


def _review(alert_id, kind, airport, seen, **extra):
    return {"action_id": f"act-{alert_id}", "alert_id": alert_id, "alert_type": kind, "airport_code": airport,
            "created_at": seen, "first_seen_at": seen, "last_seen_at": seen, **extra}


def _outage_context() -> dict:
    """Feed down ~11.7 h; five reviews from Sept 25-26; two 'RVR 0 ft' alerts that are a sensor code."""
    d25, d26 = datetime(2026, 9, 25, 19, 52, tzinfo=timezone.utc), datetime(2026, 9, 25, 23, 20, tzinfo=timezone.utc)
    alerts = [
        _alert("a1", "ALERT_FEED_DISCONNECTED", "KDCA", d25),
        _alert("a2", "ALERT_LOW_RVR", "KDCA", d25 + timedelta(seconds=1), "Low RVR 900 ft at touchdown on runway 01 at KDCA"),
        _alert("a3", "ALERT_ISMC_LINK_DEGRADED", "KDCA", d25 + timedelta(seconds=2)),
        _alert("a4", "ALERT_LOW_RVR", "KBWI", d26, "Low RVR 0 ft at touchdown on runway 10 at KBWI"),
        _alert("a5", "ALERT_LOW_RVR", "KIAD", d26 + timedelta(minutes=4), "Low RVR 0 ft at rollout on runway 19C at KIAD"),
    ]
    return {
        "snapshot_time": NOW.isoformat(),
        "latest_event_at": (NOW - timedelta(minutes=42_018 / 60)).isoformat(),
        "airport_status": {"KDCA": "STALE", "KIAD": "STALE", "KBWI": "STALE", "KADW": "NO DATA"},
        "events_in_window": 500, "avg_latency_ms": 42_018_000,
        "alerts": alerts, "critical_alerts": alerts, "critical_alert_count": 5,
        "pending_actions": [_review(a["alert_id"], a["alert_type"], a["airport_code"], a["triggered_at"]) for a in alerts],
        "pending_actions_count": 5,
        "recent_critic_results": [{"alert_id": "a1", "escalate": True, "confidence": 0.85}],
    }


def _healthy_context(**overrides) -> dict:
    context = {"snapshot_time": NOW.isoformat(), "latest_event_at": (NOW - timedelta(minutes=1)).isoformat(),
               "airport_status": {"KDCA": "OK", "KIAD": "OK", "KBWI": "OK", "KADW": "NO DATA"},
               "events_in_window": 500, "avg_latency_ms": 2_000, "alerts": [], "critical_alerts": [],
               "critical_alert_count": 0, "pending_actions": [], "pending_actions_count": 0,
               "recent_critic_results": []}
    context.update(overrides)
    return context


# -- the scenario that prompted this ---------------------------------------------------------------

def test_rule_1_and_2_a_dead_feed_leads_the_brief_and_blames_our_pipeline_not_the_airports():
    a = wa.assess(_outage_context(), now=NOW)
    assert a.data_health.state == "DOWN" and a.data_health.scope == "pipeline"
    assert a.status == "RED" and a.status_line == "DATA PICTURE: DOWN — our feed, not the airports"
    assert "not the airports" in a.bluf and "Don't use this data for operational awareness" in a.bluf
    assert "about 12 h ago" in a.bluf and "all 3 airports went stale together" in a.bluf
    assert wa.overreach(f"{a.status_line} {a.bluf}") is None, "no claim about controllers or the airspace"
    assert (a.confidence_label, "stale together" in a.confidence_reason) == ("HIGH", True)


def test_rule_4_four_day_old_alerts_are_history_and_rvr_zero_is_an_artifact_not_a_reading():
    a = wa.assess(_outage_context(), now=NOW)
    assert (a.current_alert_count, a.current_critical_count) == (0, 0), "nothing from the last 6 hours"
    assert a.artifact_alert_count == 2, "the two RVR 0 ft alerts"
    assert a.historical_alert_count == 3, "the genuine ones, all old"
    assert (a.current_review_count, a.historical_review_count) == (0, 5)
    assert all(i.historical for i in a.incidents)


def test_rule_3_alerts_that_began_together_across_airports_are_one_incident():
    a = wa.assess(_outage_context(), now=NOW)
    assert len(a.incidents) == 2, "19:52 at KDCA, and 23:20-23:24 at KBWI and KIAD: two causes, not five reviews"
    by_size = sorted(a.incidents, key=lambda i: i.alert_count)
    assert (by_size[0].airports, by_size[0].alert_count, by_size[0].artifact) == (["KBWI", "KIAD"], 2, True)
    assert (by_size[1].airports, by_size[1].alert_count, by_size[1].artifact) == (["KDCA"], 3, False)
    assert sum(i.reviews for i in a.incidents) == 5, "each of the five reviews belongs to an incident"


def test_rule_5_every_action_has_an_owner_and_a_done_when_and_there_are_at_most_three():
    a = wa.assess(_outage_context(), now=NOW)
    assert 1 <= len(a.actions) <= 3
    for act in a.actions:
        assert act.action and act.owner and act.done_when, act
    assert a.actions[0].owner == wa.PIPELINE_ONCALL and "Feed Health" in a.actions[0].done_when
    close = next(x for x in a.actions if x.action.startswith("Close"))
    assert "5 stale review(s) as feed outage" in close.action and close.owner == wa.WATCH_OFFICER
    assert not any(x.action.lower().startswith(("investigate", "confirm and investigate")) for x in a.actions)


def test_rule_6_the_part_read_at_a_glance_fits_in_120_words():
    a = wa.assess(_outage_context(), now=NOW)
    assert a.above_fold_words == wa.above_the_fold_words(a) <= wa.ABOVE_FOLD_MAX_WORDS
    assert a.watch, "still room for what to keep an eye on"


def test_rule_7_what_disagrees_is_flagged_not_published_as_if_it_agreed():
    a = wa.assess(_outage_context(), now=NOW)
    joined = " ".join(a.consistency_warnings)
    assert "1 AI escalation(s) concern alerts older than 6 h" in joined
    assert not any("Review counts disagree" in w for w in a.consistency_warnings)
    assert "are on record but none" not in joined, "a warning that is always present would be ignored"


def test_what_to_keep_an_eye_on_is_the_real_looking_rvr_and_the_lag():
    a = wa.assess(_outage_context(), now=NOW)
    assert any(w.startswith("KDCA RVR 900 ft") and "may be real" in w for w in a.watch)
    assert any("Lag 42,018 s" in w and "under 30 s" in w for w in a.watch)


# -- the ordinary cases ---------------------------------------------------------------------------------

def test_a_healthy_feed_with_nothing_open_is_green_and_asks_for_nothing():
    a = wa.assess(_healthy_context(), now=NOW)
    assert (a.status, a.data_health.state, a.actions, a.watch) == ("GREEN", "OK", [], [])
    assert a.status_line.startswith("ALL CLEAR") and a.confidence_label == "HIGH"


def test_a_current_critical_alert_on_a_healthy_feed_is_red_with_a_decision_for_the_duty_manager():
    now_alert = _alert("c1", "ALERT_FEED_DISCONNECTED", "KIAD", NOW - timedelta(minutes=20))
    context = _healthy_context(alerts=[now_alert], critical_alerts=[now_alert], critical_alert_count=1,
                               pending_actions=[_review("c1", "ALERT_FEED_DISCONNECTED", "KIAD", NOW - timedelta(minutes=20))],
                               pending_actions_count=1)
    a = wa.assess(context, now=NOW)
    assert (a.status, a.current_critical_count, a.current_review_count) == ("RED", 1, 1)
    assert a.status_line == "1 CRITICAL ALERT(S) NEED A DECISION"
    assert [(x.owner) for x in a.actions] == [wa.DUTY_MANAGER] and "Approve, Reject or Monitor" in a.actions[0].done_when
    assert a.consistency_warnings == []


def test_one_stale_airport_is_degraded_not_down():
    context = _healthy_context(airport_status={"KDCA": "OK", "KIAD": "STALE", "KBWI": "OK", "KADW": "NO DATA"})
    a = wa.assess(context, now=NOW)
    assert (a.data_health.state, a.data_health.scope, a.status) == ("DEGRADED", "airport", "AMBER")
    assert a.status_line == "DATA PICTURE: DEGRADED — KIAD"
    assert a.actions[0].action == "Restore the feed at KIAD." and a.confidence_label == "MEDIUM"


def test_a_large_lag_on_an_otherwise_current_feed_is_a_backlog_not_a_live_picture():
    a = wa.assess(_healthy_context(avg_latency_ms=1_800_000), now=NOW)
    assert (a.data_health.state, a.data_health.scope, a.status) == ("DEGRADED", "backlog", "AMBER")
    assert "a backlog, not live" in a.status_line and "Average lag is under 30 s" in a.actions[0].done_when


def test_no_events_at_all_is_unknown_not_fine():
    a = wa.assess(_healthy_context(events_in_window=0, latest_event_at=None, airport_status={}), now=NOW)
    assert (a.data_health.state, a.status, a.confidence_label) == ("UNKNOWN", "GREY", "LOW")
    assert a.actions[0].owner == wa.PIPELINE_ONCALL


def test_an_airport_that_never_reports_is_not_counted_as_down():
    context = _healthy_context(airport_status={"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK", "KADW": "NO DATA"})
    assert wa.assess(context, now=NOW).data_health.state == "DEGRADED", "one of three reporting airports is stale"


def test_actions_never_exceed_three_even_when_everything_is_wrong():
    fresh = NOW - timedelta(minutes=10)
    now_alert = _alert("n1", "ALERT_FEED_DISCONNECTED", "KDCA", fresh)
    context = _outage_context()
    context["alerts"].append(now_alert)
    context["pending_actions"].append(_review("n1", "ALERT_FEED_DISCONNECTED", "KDCA", fresh))
    context["pending_actions_count"] = 6
    a = wa.assess(context, now=NOW)
    assert len(a.actions) == wa.MAX_ACTIONS == 3
    assert [x.owner for x in a.actions] == [wa.PIPELINE_ONCALL, wa.WATCH_OFFICER, wa.DUTY_MANAGER]
    assert "once the feed is current" in a.actions[2].action, "reviews raised while the feed was down wait for it"
    assert any("raised after the last data arrived" in w for w in a.consistency_warnings)


def test_a_review_with_no_timestamp_is_treated_as_current_not_silently_called_old():
    context = _healthy_context(pending_actions=[{"action_id": "x", "alert_id": "z", "alert_type": "ALERT_FEED_STALE",
                                                 "airport_code": "KIAD"}], pending_actions_count=1)
    a = wa.assess(context, now=NOW)
    assert (a.current_review_count, a.historical_review_count) == (1, 0)


@pytest.mark.parametrize("description, artifact", [
    ("Low RVR 0 ft at touchdown on runway 10", True), ("Low RVR 900 ft at touchdown", False),
    ("Low RVR 600 ft at rollout", False), ("Low RVR 10 ft at midpoint", False)])
def test_only_a_bare_zero_rvr_is_a_sensor_artifact(description, artifact):
    assert wa.is_artifact({"alert_type": "ALERT_LOW_RVR", "description": description}) is artifact
    assert wa.is_artifact({"alert_type": "ALERT_FEED_STALE", "description": description}) is False


# -- a model's bottom line ------------------------------------------------------------------------------------------

def test_a_model_may_not_write_the_bottom_line_while_the_pipeline_is_down():
    a = wa.assess(_outage_context(), now=NOW)
    used, reason = wa.accept_model_bluf(a, "Airport infrastructure at DCA has failed.")
    assert used == a.bluf and "pipeline state decides" in reason


def test_a_model_bottom_line_is_accepted_when_it_fits_and_refused_when_it_overreaches():
    a = wa.assess(_healthy_context(alerts=[], airport_status={"KDCA": "OK", "KIAD": "STALE", "KBWI": "OK"}), now=NOW)
    assert wa.accept_model_bluf(a, "KIAD data is stale; the rest of the feed is current.") == (
        "KIAD data is stale; the rest of the feed is current.", None)
    for text, why in [("Stale tactical information is reaching controllers at IAD.", "tactical information"),
                      ("KIAD is stale " + "and so on " * 20, "longer than 40"),
                      ("All is well.", "does not say the data is degraded"), ("", "no bottom line")]:
        used, reason = wa.accept_model_bluf(a, text)
        assert used == a.bluf and why in reason, (text, reason)


# -- the brief built from it, and what the model is shown ------------------------------------------------

def test_the_rule_based_brief_for_the_outage_says_one_consistent_thing():
    import src.watch_officer_agent as wo

    context = _outage_context()
    brief = wo._deterministic_brief(context, wo._detect_signals(context))

    assert (brief.posture, brief.trust_assessment) == ("ATTENTION", "DEGRADED"), "not ACTION: nothing current awaits a decision"
    assert brief.headline == brief.assessment.bluf and brief.assessment.status == "RED"
    assert brief.critical_alert_count == 0, "no critical alert from the last 6 hours"
    assert brief.affected_airports == ["KBWI", "KDCA", "KIAD"]
    assert brief.confidence == 0.9
    assert all("Owner:" in line and "Done when:" in line for line in brief.recommended_human_review)
    joined = " ".join(brief.evidence)
    assert "critical_alerts=0" in joined and "older (history)" in joined and "critical_alerts=5" not in joined
    assert any(w.startswith("Check: ") for w in brief.limitations), "what disagrees is shown, not hidden"
    assert "FAA" not in brief.headline and "controller" not in brief.headline.lower()


def test_the_model_is_shown_the_assessment_and_which_alerts_are_history_or_artifacts():
    from src.watch_officer_agent import _detect_signals
    from src.watch_officer_projection import project_for_model

    context = _outage_context()
    projection = project_for_model(context, _detect_signals(context))

    assert projection["assessment"]["data_health"]["state"] == "DOWN"
    assert projection["assessment"]["alerts"] == {"from_last_6h": 0, "critical_from_last_6h": 0,
                                                  "older_history": 3, "sensor_artifacts": 2}
    conditions = projection["what_changed"]["alert_conditions"]
    assert conditions and all(c["historical"] for c in conditions), "four days old: never current"
    assert {c["airport"] for c in conditions if c["sensor_artifact"]} == {"KBWI", "KIAD"}, "the RVR 0 ft ones"
    assert len(projection["assessment"]["incidents"]) == 2


# -- review findings ---------------------------------------------------------------------------------------------------

def test_a_healthy_brief_does_not_carry_a_warning_just_because_an_old_critical_alert_exists():
    old = _alert("old1", "ALERT_FEED_DISCONNECTED", "KIAD", NOW - timedelta(days=3))
    context = _healthy_context(alerts=[old], critical_alerts=[old], critical_alert_count=1)
    a = wa.assess(context, now=NOW)
    assert a.status == "GREEN" and a.consistency_warnings == []
    assert a.historical_alert_count == 1, "it is counted as history, in the numbers"


@pytest.mark.parametrize("statuses", [
    {"KDCA": "STALE", "KIAD": "STALE", "KBWI": "STALE", "KADW": "STALE"},          # down, four reporting
    {"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK", "KADW": "NO DATA"},              # one stale
    {"KDCA": "STALE", "KIAD": "DEGRADED", "KBWI": "STALE", "KADW": "OK"},          # three of four wrong
    {"KDCA": "OK", "KIAD": "OK", "KBWI": "OK", "KADW": "NO DATA"}])
@pytest.mark.parametrize("age_minutes", [1, 90, 700])
@pytest.mark.parametrize("lag_ms", [2_000, 900_000, 42_018_000])
@pytest.mark.parametrize("busy", [False, True])
def test_the_length_cap_holds_in_every_shape_of_brief(statuses, age_minutes, lag_ms, busy):
    """Feed states x how long ago x lag x whether reviews and alerts are open: at most 3 actions, each owned, <= 120 words."""
    old = [_alert(f"o{i}", "ALERT_LOW_RVR", "KDCA", NOW - timedelta(days=2, minutes=i * 45),
                  "Low RVR 900 ft at touchdown on runway 01 at KDCA") for i in range(3)]
    fresh = _alert("f1", "ALERT_FEED_DISCONNECTED", "KIAD", NOW - timedelta(minutes=5))
    alerts = [*old, fresh] if busy else []
    context = _healthy_context(
        airport_status=statuses, latest_event_at=(NOW - timedelta(minutes=age_minutes)).isoformat(),
        avg_latency_ms=lag_ms, alerts=alerts, critical_alerts=alerts, critical_alert_count=len(alerts),
        pending_actions=[_review(x["alert_id"], x["alert_type"], x["airport_code"], x["triggered_at"]) for x in alerts],
        pending_actions_count=len(alerts))
    a = wa.assess(context, now=NOW)
    assert a.above_fold_words == wa.above_the_fold_words(a) <= wa.ABOVE_FOLD_MAX_WORDS, (a.status_line, a.above_fold_words)
    assert len(a.actions) <= wa.MAX_ACTIONS and all(x.action and x.owner and x.done_when for x in a.actions)
    assert a.bluf and a.status_line


def test_a_review_on_a_brand_new_sensor_artifact_is_not_called_stale():
    artifact = _alert("z1", "ALERT_LOW_RVR", "KIAD", NOW - timedelta(minutes=3), "Low RVR 0 ft at rollout on runway 19C at KIAD")
    context = _healthy_context(alerts=[artifact], critical_alerts=[artifact], critical_alert_count=1,
                               pending_actions=[_review("z1", "ALERT_LOW_RVR", "KIAD", NOW - timedelta(minutes=3))],
                               pending_actions_count=1)
    a = wa.assess(context, now=NOW)
    assert (a.current_review_count, a.historical_review_count, a.current_critical_count) == (0, 1, 0)
    assert a.actions[0].action == "Close 1 review(s) (1 on RVR 0 ft sensor codes) as history."
    assert "stale" not in a.actions[0].action


@pytest.mark.parametrize("text, claim", [
    ("Stale tactical information is reaching controllers at IAD.", "tactical information"),
    ("Data is reaching air traffic controllers late.", "air traffic controllers"),
    ("The airspace is unsafe until the feed recovers.", "unsafe"),
    ("A collision risk cannot be excluded.", "collision")])
def test_the_overreach_check_catches_claims_about_the_airspace(text, claim):
    assert wa.overreach(text) == claim


@pytest.mark.parametrize("text", [
    "The Kafka controller is down and the broker has reconnected.",
    "Network separation between the two feeds is intact.",
    "A tactical fix to the parser is queued; the data feed is current.",
    "KIAD data is stale; the rest of the feed is current."])
def test_the_overreach_check_spares_benign_uses_of_the_same_words(text):
    assert wa.overreach(text) is None


def test_the_thresholds_are_settings_with_documented_defaults():
    from src.config import settings

    assert (wa.ALERT_MAX_AGE_HOURS, wa.DOWN_AFTER_MINUTES, wa.BACKLOG_LAG_SECONDS, wa.CLUSTER_GAP_MINUTES) == (
        settings.WATCH_ALERT_MAX_AGE_HOURS, settings.WATCH_PIPELINE_DOWN_MINUTES,
        settings.WATCH_BACKLOG_LAG_SECONDS, settings.WATCH_CLUSTER_GAP_MINUTES) == (6, 15, 300, 30)


def test_alerts_raised_while_data_was_still_arriving_are_not_blamed_on_the_outage():
    """The feed stopped 3 hours ago; alerts from before it stopped are genuine, and get no outage warning."""
    before = _alert("b1", "ALERT_HIGH_LATENCY", "KIAD", NOW - timedelta(hours=4), severity="WARNING")
    after = _alert("b2", "ALERT_FEED_STALE", "KIAD", NOW - timedelta(minutes=30), severity="WARNING")
    context = _healthy_context(latest_event_at=(NOW - timedelta(hours=3)).isoformat(),
                               airport_status={"KDCA": "STALE", "KIAD": "STALE", "KBWI": "STALE"},
                               alerts=[before], critical_alerts=[])
    assert not any("raised after the last data" in w for w in wa.assess(context, now=NOW).consistency_warnings)
    context["alerts"] = [before, after]
    warnings = wa.assess(context, now=NOW).consistency_warnings
    assert warnings == ["1 alert(s) were raised after the last data arrived: they describe the outage, not the airports."]
