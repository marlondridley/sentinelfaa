"""
Time to a staged review must not depend on how many alerts come with it.

run_pipeline() used to stage each CRITICAL alert and draft its Watch Officer ticket before moving to
the next, so the last alert of a burst waited behind every ticket ahead of it. Now every review is
staged first, then the tickets are drafted a few at a time.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src import execution_gate, storage
from src.config import settings
from src.demo_scenario import generate_cascade_failure_scenario
from src.pipeline import run_pipeline


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _record(monkeypatch, *, draft_seconds: float = 0.05):
    """Log every staging and every ticket draft, in order, and how many drafts overlap."""
    log: list[str] = []
    in_flight = {"now": 0, "peak": 0}
    real_create = storage.create_staged_action

    async def create(action, **kwargs):
        log.append("stage")
        return await real_create(action, **kwargs)

    async def draft(staged, alert, critic):
        log.append("draft")
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(draft_seconds)
        in_flight["now"] -= 1

    monkeypatch.setattr(storage, "create_staged_action", create)
    monkeypatch.setattr(execution_gate, "_build_escalation_package", draft)
    monkeypatch.setattr(settings, "CRITIC_ENABLED", False)
    monkeypatch.setattr(settings, "INVESTIGATIONS_ENABLED", False)
    return log, in_flight


async def test_every_review_is_staged_before_any_ticket_is_drafted(monkeypatch):
    log, _ = _record(monkeypatch)

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    assert summary["staged_count"] >= 2, "the scenario must open several reviews or this measures nothing"
    assert log.count("draft") == summary["staged_count"]
    assert "draft" not in log[: log.count("stage")], log
    assert log.index("draft") >= max(i for i, e in enumerate(log) if e == "stage"), log


async def test_tickets_are_drafted_concurrently_and_never_above_the_cap(monkeypatch):
    log, in_flight = _record(monkeypatch)
    monkeypatch.setattr(settings, "TICKET_DRAFT_CONCURRENCY", 2)

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    assert summary["staged_count"] >= 3, "the cap is only exercised by three or more drafts"
    assert in_flight["peak"] == 2


async def test_a_failing_ticket_does_not_stop_the_others(monkeypatch):
    drafted: list[str] = []

    async def draft(staged, alert, critic):
        if staged.action_id == "boom":
            raise RuntimeError("watch officer down")
        drafted.append(staged.action_id)

    monkeypatch.setattr(execution_gate, "_build_escalation_package", draft)
    alert = SimpleNamespace(alert_id="a")
    reviews = [SimpleNamespace(action_id=name) for name in ("one", "boom", "two")]

    await execution_gate.draft_escalation_tickets([(r, alert, None) for r in reviews], concurrency=1)

    assert drafted == ["one", "two"]


async def test_staging_alone_can_leave_the_ticket_to_the_caller(monkeypatch):
    from src.models import AlertEvent

    log, _ = _record(monkeypatch)
    alert = AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="APDS_RVR",
                       description="RVR 600 ft", data_origin="live")
    await storage.save_alert(alert)
    action = await execution_gate.stage_critical_alert(alert, draft_ticket=False)

    assert action is not None and "draft" not in log
