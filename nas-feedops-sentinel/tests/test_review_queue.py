"""
The review queue is defined once (src/review_queue.py), in reviews, not readings.

Three things went wrong while each screen worked it out for itself:
- "Continue monitoring" leaves a review PENDING, so a review someone had
  already answered stayed in the queue as if nobody had;
- the queue's "AI suggests" read the Critic's verdict on the review's first
  reading only, though a review covers every reading of its condition;
- a tile counted Critic verdicts (one per reading, decided or not) and said
  they were awaiting a decision in a queue that listed reviews.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import review_queue, storage
from src.auth import Decision, Principal, PrincipalSource, format_actor
from src.execution_gate import decide_staged_action, stage_critical_alert
from src.models import AlertEvent, CriticResult
from src.review_queue import ESCALATE, NO_AI_REVIEW, NO_ESCALATION

SUPERVISOR = Principal("smith", "Supervisor", PrincipalSource.OIDC, roles=frozenset({"supervisor"}))
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _rvr(airport: str, origin: str = "live") -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code=airport, service_name="APDS_RVR",
                      description=f"Low RVR 600 ft at touchdown on runway 10 at {airport}", data_origin=origin)


async def _stage(*alerts: AlertEvent):
    await storage.save_alerts(list(alerts))
    return [await stage_critical_alert(a) for a in alerts]


def _verdict(alert: AlertEvent, escalate: bool, confidence: float | None, minutes_ago: int) -> CriticResult:
    return CriticResult(alert_id=alert.alert_id, confidence=confidence, escalate=escalate, pattern="WEATHER_CORRELATED",
                        reasoning="r", evaluated_at=NOW - timedelta(minutes=minutes_ago))


def _ids(reviews) -> list[str]:
    return [r.action.action_id for r in reviews]


async def test_continue_monitoring_takes_a_review_out_of_the_queue_and_keeps_it_open():
    kdca, kiad = await _stage(_rvr("KDCA"), _rvr("KIAD"))
    await decide_staged_action(kdca.action_id, Decision.MONITOR, SUPERVISOR, "watching the weather")

    reviews = await review_queue.open_reviews()

    assert set(_ids(reviews)) == {kdca.action_id, kiad.action_id}, "a monitored review is still open"
    assert _ids(review_queue.awaiting(reviews)) == [kiad.action_id], "but nobody is being asked about it again"
    [watched] = review_queue.monitored(reviews)
    assert watched.action.action_id == kdca.action_id
    assert (watched.ticket.decided_by, watched.ticket.decision_note) == (format_actor(SUPERVISOR), "watching the weather")


async def test_a_monitored_review_can_still_be_decided_and_then_leaves_both_lists():
    [kdca] = await _stage(_rvr("KDCA"))
    await decide_staged_action(kdca.action_id, Decision.MONITOR, SUPERVISOR, "watching")
    await decide_staged_action(kdca.action_id, Decision.REJECT, SUPERVISOR, "sensor artefact")

    assert await review_queue.open_reviews() == []


async def test_the_ai_suggestion_is_the_newest_verdict_on_any_reading_the_review_covers():
    first, later = _rvr("KBWI"), _rvr("KBWI")
    review, joined = await _stage(first, later)
    assert joined is None, "a second reading of the condition joins the first's review"
    await storage.save_critic_result(_verdict(first, escalate=False, confidence=0.7, minutes_ago=30))
    await storage.save_critic_result(_verdict(later, escalate=True, confidence=0.9, minutes_ago=5))

    [opened] = await review_queue.open_reviews()

    assert opened.action.occurrences == 2, "two readings of one condition are one review"
    assert opened.action.alert_id == first.alert_id, "the review itself names only its first reading"
    assert (opened.suggests, opened.confidence) == (ESCALATE, 0.9)
    assert (opened.verdict["verdicts"], opened.verdict["escalations"]) == (2, 1)
    assert _ids(review_queue.escalations([opened])) == [review.action_id]


async def test_no_verdict_and_a_degraded_verdict_are_both_no_ai_review():
    unassessed, degraded, assessed = _rvr("KDCA"), _rvr("KIAD"), _rvr("KBWI")
    await _stage(unassessed, degraded, assessed)
    await storage.save_critic_result(_verdict(degraded, escalate=True, confidence=None, minutes_ago=5))
    await storage.save_critic_result(_verdict(assessed, escalate=False, confidence=0.8, minutes_ago=5))

    by_airport = {r.action.airport_code: r for r in await review_queue.open_reviews()}

    assert by_airport["KDCA"].suggests == NO_AI_REVIEW
    assert by_airport["KIAD"].suggests == NO_AI_REVIEW, "a Critic that was unavailable did not suggest escalating"
    assert by_airport["KBWI"].suggests == NO_ESCALATION
    assert review_queue.escalations(list(by_airport.values())) == []


async def test_an_escalation_someone_is_monitoring_is_not_awaiting_a_decision():
    alert = _rvr("KDCA")
    [review] = await _stage(alert)
    await storage.save_critic_result(_verdict(alert, escalate=True, confidence=0.9, minutes_ago=5))
    assert _ids(review_queue.escalations(await review_queue.open_reviews())) == [review.action_id]

    await decide_staged_action(review.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    reviews = await review_queue.open_reviews()
    assert review_queue.escalations(reviews) == [] and review_queue.monitored(reviews)[0].suggests == ESCALATE


async def test_a_replayed_faults_review_is_listed_and_marked_not_live():
    live, replayed = await _stage(_rvr("KDCA"), _rvr("KIAD", origin="replay"))

    by_id = {r.action.action_id: r for r in await review_queue.open_reviews()}

    assert by_id[live.action_id].live is True
    assert by_id[replayed.action_id].live is False, "real work, but not the live feed's verdict"


async def test_the_watch_officers_brief_reads_the_same_queue():
    """Its rules (tests/test_watch_officer_assessment.py) count awaiting and monitored apart; this is where it learns which is which."""
    from src.watch_officer_agent import build_watch_officer_context

    kdca, _kiad = await _stage(_rvr("KDCA"), _rvr("KIAD"))
    await decide_staged_action(kdca.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    context = await build_watch_officer_context()

    assert {r["airport_code"]: r["monitoring"] for r in context["pending_actions"]} == {"KDCA": True, "KIAD": False}
    assert context["pending_actions_count"] == 2
