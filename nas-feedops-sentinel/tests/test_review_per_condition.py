"""
One review per condition, not per reading.

On 2026-09-25 two low-RVR episodes staged 340 separate CRITICAL reviews (one
per reading, per sensor position), and the review queue stopped being
reviewable. A staged action is now the review for its condition -- alert
type, airport, service -- and covers every CRITICAL alert of that condition
until CONDITION_QUIET_MINUTES pass without one (src/execution_gate.py,
staged_actions_repo.create). Every CRITICAL alert is still covered by exactly
one review; the reconciler folds reviews staged before this into one per
condition, recording each fold.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from src import storage
from src.auth import Decision, Principal, PrincipalSource
from src.config import settings
from src.execution_gate import DecisionNotRecorded, condition_key, decide_staged_action, stage_critical_alert
from src.models import AlertEvent, OperationalTicket, StagedAction


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _rvr(airport: str = "KBWI", alert_type: str = "ALERT_LOW_RVR") -> AlertEvent:
    return AlertEvent(alert_type=alert_type, severity="CRITICAL", airport_code=airport,
                      service_name="APDS_RVR", description=f"Low RVR 600 ft at {airport}")


async def _stage(*alerts: AlertEvent) -> list[StagedAction | None]:
    await storage.save_alerts(list(alerts))
    return [await stage_critical_alert(a) for a in alerts]


async def test_readings_of_one_condition_are_one_review_covering_them_all(db):
    first, *repeats = await _stage(*(_rvr() for _ in range(5)))
    assert first is not None and repeats == [None] * 4, "only the first reading opens a review"
    [review] = await db.get_pending_staged_actions()
    assert review.action_id == first.action_id
    assert review.occurrences == 5 and review.first_seen_at <= review.last_seen_at
    assert len(await db.get_covered_alert_ids(review.action_id)) == 5
    assert await db.get_ticket_by_action_id(review.action_id) is not None


async def test_different_conditions_are_different_reviews(db):
    opened = await _stage(_rvr("KBWI"), _rvr("KIAD"), _rvr("KBWI", "ALERT_ISMC_LINK_DEGRADED"))
    assert all(opened)
    assert len({a.condition_key for a in await db.get_pending_staged_actions()}) == 3


async def test_a_condition_quiet_for_the_window_opens_a_new_review(db, monkeypatch):
    monkeypatch.setattr(settings, "CONDITION_QUIET_MINUTES", 0)
    first, second = await _stage(_rvr(), _rvr())
    assert first and second and first.action_id != second.action_id


async def test_a_decided_review_keeps_covering_its_ongoing_condition(db):
    officer = Principal("jsmith", "Watch Officer", PrincipalSource.OIDC, roles=frozenset({"watch_officer"}))
    [first] = await _stage(_rvr())
    await decide_staged_action(first.action_id, Decision.REJECT, officer, "sensor artefact")
    [again] = await _stage(_rvr())
    assert again is None, "a reading minutes after the decision is the same event, not a new review"
    assert await db.get_pending_staged_actions() == []
    assert len(await db.get_covered_alert_ids(first.action_id)) == 2


async def test_concurrent_passes_cannot_open_two_reviews_for_one_condition(db):
    alerts = [_rvr() for _ in range(8)]
    await db.save_alerts(alerts)
    opened = [a for a in await asyncio.gather(*(stage_critical_alert(a) for a in alerts)) if a]
    assert len(opened) == 1
    assert (await db.get_pending_staged_actions())[0].occurrences == 8


async def test_reviews_staged_before_this_are_merged_one_per_condition(db):
    from src.reconcile import reconcile_unstaged

    legacy = [_rvr() for _ in range(3)] + [_rvr("KIAD")]
    await db.save_alerts(legacy)
    for alert in legacy:                     # as they were staged before: one review per reading
        await db.create_staged_action(StagedAction(
            alert_id=alert.alert_id, airport_code=alert.airport_code, service_name=alert.service_name,
            alert_type=alert.alert_type, idempotency_key=f"legacy-{alert.alert_id}",
            condition_key=condition_key(alert)))

    summary = await reconcile_unstaged()

    assert summary["merged"] == 2
    pending = await db.get_pending_staged_actions()
    assert sorted(p.occurrences for p in pending) == [1, 3]
    merged = [a for a in await db.get_recent_staged_actions(limit=10) if a.status == "MERGED"]
    survivor = next(p for p in pending if p.occurrences == 3)
    for action in merged:
        [audit] = [e for e in await db.get_action_audit_log(action_id=action.action_id) if e["new_status"] == "MERGED"]
        assert audit["actor"] == "system" and survivor.action_id in audit["note"]
    assert (await reconcile_unstaged())["merged"] == 0, "idempotent"
    assert summary["still_unstaged"] == 0, "a merged review's readings are still covered"


async def test_the_reconciler_raises_a_stale_alert_for_a_live_feed_that_went_quiet(db):
    from datetime import datetime, timezone

    from src import config
    from src.models import NASEvent
    from src.reconcile import reconcile_unstaged

    def event(airport: str, service: str, age_s: float, origin: str = "live") -> NASEvent:
        t = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        return NASEvent(airport_code=airport, tracon="PCT", service_name=service, message_type="m",
                        event_time_utc=t, received_time_utc=t, latency_ms=100.0, data_origin=origin,
                        feed_status="operational")

    quiet = config.STALE_FEED_SECONDS + 120
    await db.save_events([event("KIAD", "SMES", quiet), event("KDCA", "SMES", 5),
                          event("KBWI", "SMES", quiet, origin="replay")])

    summary = await reconcile_unstaged()

    stale = [a for a in await db.get_recent_alerts(limit=20) if a["alert_type"] == "ALERT_FEED_STALE"]
    assert [(a["airport_code"], a["service_name"]) for a in stale] == [("KIAD", "SMES")]
    assert summary["stale_feeds"] == 1
    assert (await reconcile_unstaged())["stale_feeds"] == 1
    assert len([a for a in await db.get_recent_alerts(limit=20) if a["alert_type"] == "ALERT_FEED_STALE"]) == 1, \
        "the same silence upserts one alert per window"


async def test_a_merged_review_cannot_be_decided(db):
    officer = Principal("jsmith", "Watch Officer", PrincipalSource.OIDC, roles=frozenset({"watch_officer"}))
    a, b = _rvr(), _rvr()
    await db.save_alerts([a, b])
    for alert in (a, b):
        await db.create_staged_action(StagedAction(
            alert_id=alert.alert_id, alert_type=alert.alert_type, airport_code=alert.airport_code,
            service_name=alert.service_name, idempotency_key=f"legacy-{alert.alert_id}",
            condition_key=condition_key(alert)))
    assert await db.merge_pending_duplicates(timedelta(minutes=30)) == 1
    [folded] = [x for x in await db.get_recent_staged_actions(limit=5) if x.status == "MERGED"]
    with pytest.raises(DecisionNotRecorded, match="already MERGED"):
        await decide_staged_action(folded.action_id, Decision.APPROVE, officer, "late click")


async def test_a_merged_reviews_ticket_is_not_a_draft_awaiting_a_decision(db):
    a, b = _rvr(), _rvr()
    await db.save_alerts([a, b])
    for alert in (a, b):
        await db.create_staged_action(StagedAction(
            alert_id=alert.alert_id, alert_type=alert.alert_type, airport_code=alert.airport_code,
            service_name=alert.service_name, idempotency_key=f"legacy-{alert.alert_id}",
            condition_key=condition_key(alert)))
        await db.create_operational_ticket(OperationalTicket(
            action_id=(await db.get_recent_staged_actions(limit=1))[0].action_id,
            title="t", severity="CRITICAL", idempotency_key=f"ticket-{alert.alert_id}"))
    assert len(await db.get_draft_operational_tickets()) == 2
    assert await db.merge_pending_duplicates(timedelta(minutes=30)) == 1
    [survivor] = await db.get_pending_staged_actions()
    [draft] = await db.get_draft_operational_tickets()
    assert draft.action_id == survivor.action_id, "only the review still awaiting a decision has a draft"


def _rvr_on(runway: str, origin: str = "live") -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KBWI",
                      service_name="APDS_RVR", data_origin=origin,
                      description=f"Low RVR 600 ft at touchdown on runway {runway} at KBWI (threshold 1200 ft).")


async def test_a_rejected_artefact_on_one_runway_does_not_cover_another_runway(db):
    officer = Principal("jsmith", "Watch Officer", PrincipalSource.OIDC, roles=frozenset({"watch_officer"}))
    [first] = await _stage(_rvr_on("10"))
    await decide_staged_action(first.action_id, Decision.REJECT, officer, "sensor artefact")
    [other, same] = await _stage(_rvr_on("33L"), _rvr_on("10"))
    assert other is not None, "a different runway is a different condition: it gets its own review"
    assert same is None, "the runway that was just rejected is still the same event"
    assert [p.action_id for p in await db.get_pending_staged_actions()] == [other.action_id]


async def test_replay_and_live_alerts_are_different_conditions(db):
    live, replay = await _stage(_rvr_on("10", "live"), _rvr_on("10", "replay"))
    assert live is not None and replay is not None
    assert condition_key(_rvr_on("10", "live")) != condition_key(_rvr_on("10", "replay"))


async def test_a_decided_review_stops_covering_a_condition_that_keeps_firing(db, monkeypatch):
    officer = Principal("jsmith", "Watch Officer", PrincipalSource.OIDC, roles=frozenset({"watch_officer"}))
    [first] = await _stage(_rvr())
    await decide_staged_action(first.action_id, Decision.REJECT, officer, "sensor artefact")
    monkeypatch.setattr(settings, "CONDITION_QUIET_MINUTES", 0)      # the decision is now outside the window
    [again] = await _stage(_rvr())
    assert again is not None, "a condition still firing after the decision is put in front of a person again"
    assert [p.action_id for p in await db.get_pending_staged_actions()] == [again.action_id]


# ── a merge and a joining alert must not cross ────────────────────────────────
#
# create() picks the PENDING review a new alert joins under the condition's advisory lock. A merge
# that ran without it could fold that review into another and commit in between, leaving the alert
# attached to a MERGED review that no one is ever shown.

async def _legacy_pair(db) -> None:
    """Two PENDING reviews of one condition, as staged before one-review-per-condition."""
    a, b = _rvr(), _rvr()
    await db.save_alerts([a, b])
    for alert in (a, b):
        await db.create_staged_action(StagedAction(
            alert_id=alert.alert_id, alert_type=alert.alert_type, airport_code=alert.airport_code,
            service_name=alert.service_name, idempotency_key=f"legacy-{alert.alert_id}",
            condition_key=condition_key(alert)))


async def _alerts_on_merged_reviews() -> int:
    from src.storage.pool import get_db_pool

    return await (await get_db_pool()).fetchval(
        """SELECT count(*) FROM staged_action_alerts x JOIN staged_actions s ON s.action_id = x.action_id
           WHERE s.status = 'MERGED'""")


async def test_a_merge_waits_for_the_review_an_alert_is_joining_and_strands_nothing(db, monkeypatch):
    from src.storage.repositories.staged_actions_repo import StagedActionsRepository as Repo

    await _legacy_pair(db)
    newcomer = _rvr()
    await db.save_alerts([newcomer])
    joining, proceed = asyncio.Event(), asyncio.Event()
    real_cover = Repo._cover

    async def slow_cover(conn, alert_id, action_id, at):
        if alert_id == newcomer.alert_id:
            joining.set()                       # it has chosen the review to join, and holds the condition's lock
            await proceed.wait()
        await real_cover(conn, alert_id, action_id, at)

    monkeypatch.setattr(Repo, "_cover", staticmethod(slow_cover))
    staging = asyncio.create_task(stage_critical_alert(newcomer))
    try:
        await asyncio.wait_for(joining.wait(), 10)
        merging = asyncio.create_task(db.merge_pending_duplicates(timedelta(minutes=30)))
        await asyncio.sleep(0.5)
        assert not merging.done(), "the merge has to wait for the alert being attached"
    finally:
        proceed.set()                       # never leave the joining alert parked, whatever the assertion said
    await asyncio.wait_for(staging, 10)
    assert await asyncio.wait_for(merging, 10) == 1
    assert await _alerts_on_merged_reviews() == 0
    [review] = await db.get_pending_staged_actions()
    assert review.occurrences == 3, "the two legacy readings and the newcomer, all on the one review"


async def test_merging_and_staging_at_once_neither_deadlock_nor_strand(db):
    await _legacy_pair(db)
    newcomers = [_rvr() for _ in range(12)]
    await db.save_alerts(newcomers)

    await asyncio.wait_for(asyncio.gather(
        *(stage_critical_alert(a) for a in newcomers),
        *(db.merge_pending_duplicates(timedelta(minutes=30)) for _ in range(4)),
    ), 60)
    await db.merge_pending_duplicates(timedelta(minutes=30))        # what the reconciler's next pass does

    assert await _alerts_on_merged_reviews() == 0
    [review] = await db.get_pending_staged_actions()
    assert review.occurrences == 14 and len(await db.get_covered_alert_ids(review.action_id)) == 14
