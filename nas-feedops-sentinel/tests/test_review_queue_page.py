"""
The review queue renders as a triage table (dashboard/pages/3_AI_Critic_and_Review.py).

Run headlessly through Streamlit's AppTest against a test database: one row
per review, not per reading; filter pills counted by facility and AI
suggestion; the AI summary, or an honest line saying why there is none. No sign-in is
configured here, so the page runs with a self-declared operator.

Every number on the page is rows of its table: a review someone answered
"continue monitoring" leaves the queue for the Monitoring view, and each tile
opens the rows it counts. The page used to lead with a tile counting Critic
verdicts per reading ("25 escalations suggested, awaiting a human decision
below") over a queue of four reviews, none of which it opened.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="the page renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src import storage  # noqa: E402
from src.auth import Decision, Principal, PrincipalSource  # noqa: E402
from src.execution_gate import decide_staged_action, stage_critical_alert  # noqa: E402
from src.models import AlertEvent, CriticResult  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "3_AI_Critic_and_Review.py"
SUPERVISOR = Principal("smith", "Supervisor", PrincipalSource.OIDC, roles=frozenset({"supervisor"}))


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
    kdca = _rvr("KDCA")
    await db.save_alerts([*kbwi, kiad, kdca])
    for alert in (*kbwi, kiad, kdca):
        await stage_critical_alert(alert)
    await db.save_critic_result(CriticResult(         # an older verdict: assessed, never summarised
        alert_id=kdca.alert_id, confidence=0.7, escalate=False, pattern="SENSOR_ARTIFACT", reasoning="r"))
    await db.save_critic_result(CriticResult(
        alert_id=kbwi[0].alert_id, confidence=0.86, escalate=True, pattern="WEATHER_CORRELATED",
        reasoning="r", bluf="Sustained low RVR on KBWI 10; feeds healthy, genuine weather likely."))

    at = AppTest.from_file(str(PAGE), default_timeout=60).run()

    assert not at.exception, [e.value for e in at.exception]
    [queue] = [d.value for d in at.dataframe if "AI suggests" in d.value.columns]
    assert len(queue) == 3, "one row per review: three KBWI readings are one review"
    rows = {r["Facility"]: r for r in queue.to_dict("records")}
    assert rows["KBWI"]["Readings"] == 3 and rows["KBWI"]["AI suggests"] == "Escalate"
    assert rows["KBWI"]["AI summary"].startswith("Sustained low RVR on KBWI 10")
    assert rows["KIAD"]["AI suggests"] == "No AI review" and rows["KIAD"]["AI summary"] == "Not reviewed by the AI yet."
    assert rows["KDCA"]["AI summary"] == "No written summary for this review. The AI says don't escalate, 70% confident."
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


# ── Every number is rows of the table ─────────────────────────────────────────

def _run(**query: str) -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    for key, value in query.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _queue(at: AppTest):
    [queue] = [d.value for d in at.dataframe if "AI suggests" in d.value.columns]
    return queue


def _tiles(at: AppTest) -> dict[str, tuple[str, str | None]]:
    """KPI tile label -> (its number, the link it opens or None)."""
    html = "\n".join(m.value for m in at.markdown)
    return {m.group(3): (m.group(4), m.group(2)) for m in re.finditer(
        r'<(a|div) class="kpi"(?: href="([^"]*)")?[^>]*>.*?<div class="kpi-label">([^<]+)</div>'
        r'<div class="kpi-value"[^>]*>([^<]*)', html, re.S)}


async def _three_reviews(db) -> None:
    """KBWI: the AI would escalate. KIAD: it would not. KDCA: it would, and someone is already monitoring it."""
    alerts = {airport: _rvr(airport) for airport in ("KBWI", "KIAD", "KDCA")}
    await db.save_alerts(list(alerts.values()))
    reviews = {airport: await stage_critical_alert(alert) for airport, alert in alerts.items()}
    for airport, escalate in (("KBWI", True), ("KIAD", False), ("KDCA", True)):
        await db.save_critic_result(CriticResult(
            alert_id=alerts[airport].alert_id, confidence=0.9, escalate=escalate, pattern="WEATHER_CORRELATED",
            reasoning="r", bluf=f"{airport} summary"))
    await decide_staged_action(reviews["KDCA"].action_id, Decision.MONITOR, SUPERVISOR, "watching the weather")


async def test_a_monitored_review_leaves_the_queue_and_is_listed_under_monitoring(db):
    await _three_reviews(db)

    assert sorted(_queue(_run())["Facility"]) == ["KBWI", "KIAD"], "what was answered is not asked again"

    [watched] = _queue(_run(show="monitoring")).to_dict("records")
    assert (watched["Facility"], watched["Note"]) == ("KDCA", "watching the weather")
    assert "smith" in watched["By"] and watched["Monitoring since (UTC)"] is not None


async def test_each_tile_counts_reviews_and_opens_the_rows_it_counts(db):
    await _three_reviews(db)

    tiles = _tiles(_run())

    assert tiles["Awaiting decision"] == ("2", "AI_Critic_and_Review?show=awaiting")
    assert tiles["AI suggests escalation"] == ("1", "AI_Critic_and_Review?show=escalate"), \
        "KDCA's escalation is being monitored: it is not awaiting anyone"
    assert tiles["Monitoring"] == ("1", "AI_Critic_and_Review?show=monitoring")
    for show, facilities in (("awaiting", ["KBWI", "KIAD"]), ("escalate", ["KBWI"]), ("monitoring", ["KDCA"])):
        assert sorted(_queue(_run(show=show))["Facility"]) == facilities, show
        assert len(facilities) == int(tiles[{"awaiting": "Awaiting decision", "escalate": "AI suggests escalation",
                                             "monitoring": "Monitoring"}[show]][0])


async def test_the_address_opens_its_list_whatever_pill_the_session_held(db):
    """The Monitoring tile's link once showed an empty Awaiting list: the session's held pill beat ?show=."""
    await _three_reviews(db)

    for first, held, then, tile, facilities in (
            ({}, "awaiting", "monitoring", "Monitoring", ["KDCA"]),
            ({"show": "monitoring"}, "monitoring", "awaiting", "Awaiting decision", ["KBWI", "KIAD"])):
        at = _run(**first)
        assert at.segmented_control(key="q_show").value == held
        at.query_params["show"] = then
        at.run()

        assert at.segmented_control(key="q_show").value == then, f"held {held}, then ?show={then}"
        assert sorted(_queue(at)["Facility"]) == facilities, f"held {held}, then ?show={then}"
        assert int(_tiles(at)[tile][0]) == len(_queue(at))


def _click_the_highlighted_pill(at: AppTest) -> AppTest:
    """A browser sends an empty selection when the highlighted pill is clicked. AppTest's unselect() cannot (for
    this widget it reads None as "no change"), so run with the pill's selection emptied in the widget states."""
    states = at._tree.get_widget_states()
    [pill] = [w for w in states.widgets if w.id == at.segmented_control(key="q_show").id]
    pill.string_array_value.data[:] = []
    return at._run(states)


async def test_a_pill_writes_its_list_into_the_address(db):
    await _three_reviews(db)
    at = _run()

    at.segmented_control(key="q_show").set_value("monitoring").run()
    assert at.query_params["show"] == ["monitoring"] and list(_queue(at)["Facility"]) == ["KDCA"]

    at.segmented_control(key="q_show").set_value("awaiting").run()
    assert at.query_params["show"] == ["awaiting"] and sorted(_queue(at)["Facility"]) == ["KBWI", "KIAD"]

    _click_the_highlighted_pill(at)
    assert at.segmented_control(key="q_show").value == "awaiting", "the highlighted pill stays highlighted"
    assert at.query_params["show"] == ["awaiting"] and sorted(_queue(at)["Facility"]) == ["KBWI", "KIAD"]


async def test_with_nothing_awaiting_the_page_opens_on_what_is_being_monitored(db):
    alert = _rvr("KDCA")
    await db.save_alerts([alert])
    review = await stage_critical_alert(alert)
    await decide_staged_action(review.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    at = _run()

    assert _tiles(at)["Awaiting decision"] == ("0", None), "nothing to open"
    assert list(_queue(at)["Facility"]) == ["KDCA"] and "Monitoring since (UTC)" in _queue(at).columns


async def test_the_critics_activity_counts_readings_and_is_not_the_queue(db):
    readings = [_rvr("KBWI") for _ in range(3)]
    await db.save_alerts(readings)
    for alert in readings:
        await stage_critical_alert(alert)
        await db.save_critic_result(CriticResult(
            alert_id=alert.alert_id, confidence=0.9, escalate=True, pattern="WEATHER_CORRELATED", reasoning="r"))

    at = _run()

    assert _tiles(at)["AI suggests escalation"][0] == "1", "three escalate verdicts on one condition are one review"
    assert len(_queue(at)) == 1
    [activity] = [c.value for c in at.caption if "the Critic assessed" in c.value]
    assert activity.startswith("Of the last 3 readings the Critic assessed: 3 said escalate")
    assert "Escalations suggested" not in "\n".join(m.value for m in at.markdown), "no per-reading tile over the queue"
