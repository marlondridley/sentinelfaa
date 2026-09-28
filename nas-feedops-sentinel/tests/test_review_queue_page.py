"""
The review queue renders as a triage table (dashboard/pages/3_AI_Critic_and_Review.py).

Run headlessly through Streamlit's AppTest against a test database: one row
per review, not per reading; filter pills counted by facility and AI
suggestion; the AI summary, or an honest "No AI summary yet". No sign-in is
configured here, so the page runs with a self-declared operator.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="the page renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src import storage  # noqa: E402
from src.execution_gate import stage_critical_alert  # noqa: E402
from src.models import AlertEvent, CriticResult  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "3_AI_Critic_and_Review.py"


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _rvr(airport: str) -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code=airport,
                      service_name="APDS_RVR", description=f"Low RVR 600 ft at touchdown on runway 10 at {airport}")


async def test_the_queue_is_one_row_per_review_with_counted_filters_and_summaries(db):
    kbwi = [_rvr("KBWI") for _ in range(3)]
    kiad = _rvr("KIAD")
    await db.save_alerts([*kbwi, kiad])
    for alert in (*kbwi, kiad):
        await stage_critical_alert(alert)
    await db.save_critic_result(CriticResult(
        alert_id=kbwi[0].alert_id, confidence=0.86, escalate=True, pattern="WEATHER_CORRELATED",
        reasoning="r", bluf="Sustained low RVR on KBWI 10; feeds healthy, genuine weather likely."))

    at = AppTest.from_file(str(PAGE), default_timeout=60).run()

    assert not at.exception, [e.value for e in at.exception]
    [queue] = [d.value for d in at.dataframe if "AI suggests" in d.value.columns]
    assert len(queue) == 2, "one row per review: three KBWI readings are one review"
    rows = {r["Facility"]: r for r in queue.to_dict("records")}
    assert rows["KBWI"]["Readings"] == 3 and rows["KBWI"]["AI suggests"] == "Escalate"
    assert rows["KBWI"]["AI summary"].startswith("Sustained low RVR on KBWI 10")
    assert rows["KIAD"]["AI suggests"] == "No AI review" and rows["KIAD"]["AI summary"] == "No AI summary yet"
    labels = [o for p in at.get("button_group") for o in p.proto.options] if at.get("button_group") else []
    assert not labels or any("KBWI (1)" in str(o) for o in labels)


async def test_a_critic_verdict_opens_its_review_and_its_evidence_explains_itself(db):
    from datetime import datetime, timezone

    from src.models import NASEvent

    reading = NASEvent(airport_code="KBWI", tracon="PCT", service_name="APDS_RVR", message_type="APDS_RR",
                       event_time_utc=datetime(2026, 9, 25, 23, 20, tzinfo=timezone.utc),
                       received_time_utc=datetime(2026, 9, 25, 23, 20, 2, tzinfo=timezone.utc),
                       runway="10", rvr_touchdown=600, rvr_midpoint=800)
    await db.save_events([reading])
    alert = _rvr("KBWI")
    await db.save_alerts([alert])
    await stage_critical_alert(alert)
    await db.save_critic_result(CriticResult(
        alert_id=alert.alert_id, confidence=0.9, escalate=True, pattern="WEATHER_CORRELATED", reasoning="r",
        citations=[reading.event_id, "evt-aged-out"]))

    at = AppTest.from_file(str(PAGE), default_timeout=60).run()

    assert not at.exception, [e.value for e in at.exception]
    assert "Review escalation" in [b.label for b in at.button], "an escalation verdict leads to its review"
    html = " ".join(m.value for m in at.markdown)
    assert "APDS_RVR · KBWI · 2026-09-25 23:20:00 UTC · runway 10 · RVR td/mid/ro 600/800" in html
    assert "No longer in the event store" in html


def test_the_console_offers_no_deploy_or_developer_menu():
    import tomllib
    config = tomllib.loads((PAGE.parent.parent.parent / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["client"]["toolbarMode"] == "minimal"


async def test_a_link_that_names_a_review_opens_it(db):
    [alert] = [_rvr("KBWI")]
    await db.save_alerts([alert])
    review = await stage_critical_alert(alert)

    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.query_params["review"] = review.action_id
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert any(m.value.startswith("**AI summary:**") for m in at.markdown), "the review drawer opened"
    assert "review" not in at.query_params, "once: closing the drawer must not reopen it"
