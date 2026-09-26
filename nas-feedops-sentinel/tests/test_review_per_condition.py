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
from src.execution_gate import condition_key, decide_staged_action, stage_critical_alert
from src.models import AlertEvent, StagedAction


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
    after = await decide_staged_action(folded.action_id, Decision.APPROVE, officer, "late click")
    assert after is None or after.status == "MERGED"
