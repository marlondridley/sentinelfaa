"""scripts/backfill_critic.py picks the pending reviews that have no written AI summary, and only those."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src import storage
from src.execution_gate import stage_critical_alert
from src.models import AlertEvent, CriticResult

_SPEC = importlib.util.spec_from_file_location(
    "backfill_critic", Path(__file__).resolve().parent.parent / "scripts" / "backfill_critic.py")
backfill = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill)


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _rvr(airport: str) -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code=airport,
                      service_name="APDS_RVR", description=f"Low RVR 600 ft on runway 10 at {airport}")


async def test_only_reviews_without_a_written_summary_are_picked():
    summarised, old, unreviewed = _rvr("KBWI"), _rvr("KDCA"), _rvr("KIAD")
    await storage.save_alerts([summarised, old, unreviewed])
    for alert in (summarised, old, unreviewed):
        await stage_critical_alert(alert)
    await storage.save_critic_result(CriticResult(
        alert_id=summarised.alert_id, confidence=0.9, escalate=True, pattern="ISOLATED_FAULT",
        reasoning="r", bluf="Low RVR on KBWI 10; feeds healthy."))
    await storage.save_critic_result(CriticResult(
        alert_id=old.alert_id, confidence=0.7, escalate=False, pattern="SENSOR_ARTIFACT", reasoning="r"))

    wanted = await backfill._wanted()

    assert sorted(row["airport_code"] for _, row in wanted) == ["KDCA", "KIAD"]
