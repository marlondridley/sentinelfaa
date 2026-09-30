"""
The Critic is a paid call, and its verdict is thrown away when the alert joins a review that is already open.

An alert that a review already covers (a Temporal retry of the same batch), or whose condition one covers (a
later reading of the same low RVR), used to be sent to the Critic on every pass and every retry. Only an alert
that would open a review needs a verdict.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import config, storage
from src.models import CriticResult, NASEvent
from src.pipeline import run_pipeline


class _CountingCritic:
    """Stands in for CriticAgent: records which alerts it was asked about."""

    asked: list[str] = []

    async def critique(self, alert):
        _CountingCritic.asked.append(alert.alert_id)
        return CriticResult(alert_id=alert.alert_id, confidence=0.7, reasoning="r", pattern="ISOLATED_FAULT",
                            escalate=True)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    storage.override_db_path(":memory:")
    _CountingCritic.asked = []
    monkeypatch.setattr("src.pipeline.CriticAgent", _CountingCritic)
    monkeypatch.setattr(config.settings, "INVESTIGATIONS_ENABLED", False)
    yield
    storage.override_db_path(":memory:")


def _low_rvr(runway: str = "01", airport: str = "KDCA") -> NASEvent:
    t = datetime.now(timezone.utc)
    return NASEvent(airport_code=airport, tracon="PCT", service_name="APDS_RVR", message_type="APDS_RR",
                    event_time_utc=t, received_time_utc=t, latency_ms=10.0, runway=runway, rvr_touchdown=200,
                    feed_status="operational", validation_status="PASSED", data_origin="live")


async def test_the_first_reading_of_a_condition_gets_a_verdict():
    await run_pipeline([_low_rvr()])

    assert len(_CountingCritic.asked) == 1
    assert len(await storage.get_pending_staged_actions()) == 1


async def test_a_later_reading_of_a_condition_that_a_review_covers_is_not_sent_to_the_critic():
    await run_pipeline([_low_rvr()])
    _CountingCritic.asked.clear()

    await run_pipeline([_low_rvr()])                     # the same runway still low: a new alert, the same condition

    assert _CountingCritic.asked == []
    [review] = await storage.get_pending_staged_actions()
    assert len(await storage.get_covered_alert_ids(review.action_id)) == 2, "it still joined the review"


async def test_a_retry_of_a_batch_that_was_already_staged_does_not_ask_the_critic_again():
    batch = [_low_rvr()]
    await run_pipeline(batch)
    _CountingCritic.asked.clear()

    await run_pipeline(batch)                            # what a Temporal retry does

    assert _CountingCritic.asked == []
    assert len(await storage.get_pending_staged_actions()) == 1


async def test_a_different_condition_still_gets_its_verdict():
    await run_pipeline([_low_rvr(runway="01")])
    _CountingCritic.asked.clear()

    await run_pipeline([_low_rvr(runway="19")])

    assert len(_CountingCritic.asked) == 1
    assert len(await storage.get_pending_staged_actions()) == 2


async def test_if_coverage_cannot_be_looked_up_every_alert_still_goes_to_the_critic(monkeypatch):
    await run_pipeline([_low_rvr()])
    _CountingCritic.asked.clear()

    async def unavailable(*_a, **_k):
        raise RuntimeError("database is down")

    monkeypatch.setattr("src.pipeline.get_ongoing_condition_keys", unavailable)

    await run_pipeline([_low_rvr()])

    assert len(_CountingCritic.asked) == 1, "an optimisation, never a gate"
