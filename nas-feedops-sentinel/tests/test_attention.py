"""
The Dashboard's attention strip (src/attention.py): what needs the watch
officer now, most urgent first, each item linking to its exact subject.

It replaced a scrolling BREAKING ticker whose items linked only to a page
name, so a click landed on a generic page the officer then had to search.
Every item now carries its page and the filters that open that page on the
thing it names.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src import storage
from src.attention import ACTION, ATTENTION, GOOD, build_attention_items
from src.auth import Decision, Principal, PrincipalSource
from src.execution_gate import decide_staged_action, stage_critical_alert
from src.models import AlertEvent

ALL_OK = {"KDCA": "OK", "KIAD": "OK", "KBWI": "OK", "KADW": "OK"}
SUPERVISOR = Principal("smith", "Supervisor", PrincipalSource.OIDC, roles=frozenset({"supervisor"}))


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _critical(airport: str = "KDCA") -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code=airport,
                      service_name="APDS_RVR", description="Low RVR 700 ft.")


async def _stage(*alerts: AlertEvent):
    await storage.save_alerts(list(alerts))
    return [await stage_critical_alert(a) for a in alerts]


async def test_one_pending_review_links_straight_to_that_review():
    [review] = await _stage(_critical())
    [item] = [i for i in await build_attention_items(ALL_OK, [], []) if i["severity"] == ACTION]
    assert (item["page"], item["params"]) == ("critic", {"review": review.action_id})
    assert "LOW_RVR at KDCA" in item["text"]


async def test_several_pending_reviews_link_to_the_queue():
    await _stage(_critical("KDCA"), _critical("KIAD"))
    [item] = [i for i in await build_attention_items(ALL_OK, [], []) if i["severity"] == ACTION]
    assert (item["page"], item["params"], item["text"]) == ("critic", {}, "2 reviews awaiting a decision")


async def test_a_review_someone_is_monitoring_is_attention_not_an_action():
    """It used to read "2 reviews awaiting a decision" for as long as either stayed open, answered or not."""
    kdca, kiad = await _stage(_critical("KDCA"), _critical("KIAD"))
    await decide_staged_action(kdca.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    items = await build_attention_items(ALL_OK, [], [])

    assert [(i["severity"], i["page"], i["params"]) for i in items] == [
        (ACTION, "critic", {"review": kiad.action_id}),
        (ATTENTION, "critic", {"show": "monitoring"}),
    ]
    assert items[1]["text"] == "1 review(s) being monitored"


async def test_low_rvr_links_to_that_airports_rvr():
    items = await build_attention_items(
        ALL_OK, [], [{"airport_code": "KIAD", "runway": "01C", "rvr_touchdown": 600}])
    [item] = [i for i in items if i["severity"] == ACTION]
    assert (item["page"], item["params"]) == ("health", {"airport": "KIAD", "focus": "rvr"})
    assert item["text"] == "Low RVR KIAD runway 01C: 600 ft at touchdown (minimum 1,200)"


async def test_a_stale_feed_links_to_that_airports_status_and_is_not_an_action():
    items = await build_attention_items({**ALL_OK, "KBWI": "STALE"}, [], [])
    assert [(i["severity"], i["page"], i["params"]) for i in items] == [
        (ATTENTION, "health", {"airport": "KBWI", "focus": "status"})]


async def test_critical_alerts_without_a_review_link_to_the_filtered_alert_log():
    unreviewed = _critical("KIAD")
    await storage.save_alerts([unreviewed])                      # saved, never staged: no review covers it
    alerts = [{"alert_id": unreviewed.alert_id, "severity": "CRITICAL"}, {"alert_id": "w", "severity": "WARNING"}]
    items = await build_attention_items(ALL_OK, alerts, [])
    assert [(i["text"], i["page"], i["params"]) for i in items] == [
        ("1 CRITICAL alert(s) not covered by any review", "alerts", {"severity": "CRITICAL"})]


async def test_critical_alerts_a_review_covers_are_not_a_gap_open_or_decided():
    from src.auth import Decision, Principal, PrincipalSource
    from src.execution_gate import decide_staged_action

    covered, other = _critical("KDCA"), _critical("KIAD")
    [review, _] = await _stage(covered, other)
    officer = Principal("jsmith", "Watch Officer", PrincipalSource.OIDC, roles=frozenset({"watch_officer"}))
    await decide_staged_action(review.action_id, Decision.REJECT, officer, "artefact")
    alerts = [{"alert_id": a.alert_id, "severity": "CRITICAL"} for a in (covered, other)]
    items = await build_attention_items(ALL_OK, alerts, [])
    assert [i["severity"] for i in items] == [ACTION], "only the still-pending review shows"
    assert "KIAD" in items[0]["text"] and not any("not covered" in i["text"] for i in items)


async def test_a_gap_is_not_hidden_by_an_unrelated_pending_review():
    await _stage(_critical("KDCA"))                              # an open review for something else
    gap = _critical("KIAD")
    await storage.save_alerts([gap])
    items = await build_attention_items(ALL_OK, [{"alert_id": gap.alert_id, "severity": "CRITICAL"}], [])
    assert any("1 CRITICAL alert(s) not covered by any review" == i["text"] for i in items)


async def test_low_rvr_at_any_position_counts_but_a_replayed_or_stale_reading_does_not():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    midpoint = {"airport_code": "KBWI", "runway": "10", "rvr_touchdown": 1500, "rvr_midpoint": 900,
                "rvr_rollout": 1400, "data_origin": "live", "event_time_utc": now}
    [item] = await build_attention_items(ALL_OK, [], [midpoint])
    assert item["text"] == "Low RVR KBWI runway 10: 900 ft at midpoint (minimum 1,200)"
    replayed = {**midpoint, "data_origin": "replay"}
    old = {**midpoint, "event_time_utc": now - timedelta(days=3)}
    assert [i["severity"] for i in await build_attention_items(ALL_OK, [], [replayed, old])] == [GOOD]


async def test_the_most_urgent_comes_first_and_all_clear_only_when_nothing_else():
    await _stage(_critical())
    items = await build_attention_items({**ALL_OK, "KIAD": "STALE"}, [], [])
    assert [i["severity"] for i in items] == [ACTION, ATTENTION]
    assert [(i["severity"], i["page"]) for i in await build_attention_items(ALL_OK, [], [])] == [(ACTION, "critic")]


async def test_all_clear_and_no_data():
    assert [(i["severity"], i["page"]) for i in await build_attention_items(ALL_OK, [], [])] == [(GOOD, "overview")]
    assert [(i["severity"], i["page"]) for i in await build_attention_items({}, [], [])] == [(ATTENTION, "ingest")]


def test_every_destination_is_a_dashboard_page():
    pytest.importorskip("streamlit", reason="the page map is the dashboard's")
    dashboard = str(Path(__file__).resolve().parent.parent / "dashboard")
    if dashboard not in sys.path:
        sys.path.insert(0, dashboard)
    from lib import theme

    for page in ("critic", "health", "alerts", "ingest", "overview"):
        assert page in theme.PAGES


async def test_the_overview_shows_the_strip_linking_to_the_review():
    pytest.importorskip("streamlit", reason="the Overview renders through Streamlit")
    from streamlit.testing.v1 import AppTest

    [review] = await _stage(_critical("KBWI"))
    app = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()

    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(m.value for m in at.markdown)
    assert "Needs attention" in html and f"AI_Critic_and_Review?review={review.action_id}" in html
    assert "BREAKING" not in html
