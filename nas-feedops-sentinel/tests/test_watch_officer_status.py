"""
The Watch Officer's status tells the truth about now (results.md W-01, W-02).

W-01  one airport's stale feed turned the status AMBER and left a genuine CRITICAL at another airport out of the
      headline, while the same alert with healthy feeds was RED.
W-02  a brief is stored once; after the feed died behind a GREEN one, the dashboard kept showing "ALL CLEAR".
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from src import watch_officer_assessment as wa
from src.models import WatchOfficerBrief
from src.watch_officer_freshness import current_view
from tests.test_watch_officer_assessment import NOW, _alert, _healthy_context

_RECENT = NOW - timedelta(minutes=10)
_LOW_RVR = "Low RVR 800 ft at touchdown on runway 01 at {airport}"


def _critical(airport="KIAD", alert_id="c1"):
    return _alert(alert_id, "ALERT_LOW_RVR", airport, _RECENT, _LOW_RVR.format(airport=airport))


def _context(statuses: dict, *, alerts=(), **extra) -> dict:
    return _healthy_context(
        airport_status=statuses, alerts=list(alerts), critical_alerts=[a for a in alerts if a["severity"] == "CRITICAL"],
        critical_alert_count=sum(a["severity"] == "CRITICAL" for a in alerts), **extra)


# ── W-01 ───────────────────────────────────────────────────────────────────────

def test_a_critical_at_a_healthy_airport_is_red_even_while_another_feed_is_stale():
    healthy = wa.assess(_context({"KDCA": "OK", "KIAD": "OK", "KBWI": "OK"}, alerts=[_critical("KIAD")]), now=NOW)
    degraded = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"}, alerts=[_critical("KIAD")]), now=NOW)

    assert healthy.status == "RED"
    assert degraded.data_health.state == "DEGRADED"
    assert degraded.status == "RED", "the same alert must not turn AMBER because a different airport went stale"
    assert "1 CRITICAL ALERT(S) NEED A DECISION" in degraded.status_line and "KDCA" in degraded.status_line
    assert "1 critical alert(s) at KIAD" in degraded.bluf and "Data is stale or degraded at KDCA" in degraded.bluf


def test_the_count_is_in_the_headline():
    a = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"},
                           alerts=[_critical("KIAD", "c1"), _critical("KBWI", "c2")]), now=NOW)
    assert a.status == "RED" and a.status_line.startswith("2 CRITICAL ALERT(S)")
    assert "at KBWI, KIAD" in a.bluf


def test_a_critical_at_the_degraded_airport_is_reported_as_unverifiable_not_dropped():
    a = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"}, alerts=[_critical("KDCA")]), now=NOW)
    assert a.status == "AMBER", "its own feed is the degraded one: the picture there cannot be trusted either way"
    assert "1 critical alert(s) at KDCA cannot be verified while that feed is degraded" in a.bluf


def test_both_kinds_at_once_go_red_and_mention_the_unverified_one():
    a = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"},
                           alerts=[_critical("KIAD", "c1"), _critical("KDCA", "c2")]), now=NOW)
    assert a.status == "RED" and a.status_line.startswith("1 CRITICAL ALERT(S)")
    assert "at KIAD" in a.bluf and "at KDCA cannot be verified" in a.bluf


def test_a_backlog_with_a_current_critical_is_red_too():
    a = wa.assess(_context({"KDCA": "OK", "KIAD": "OK", "KBWI": "OK"}, alerts=[_critical("KIAD")],
                           avg_latency_ms=900_000), now=NOW)
    assert a.data_health.scope == "backlog" and a.status == "RED" and "catching up" in a.status_line


def test_a_degraded_feed_with_no_critical_is_still_amber_as_before():
    a = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"}), now=NOW)
    assert a.status == "AMBER" and a.status_line == "DATA PICTURE: DEGRADED — KDCA"


def test_an_old_critical_is_history_and_does_not_redden_a_degraded_picture():
    old = _alert("c9", "ALERT_LOW_RVR", "KIAD", NOW - timedelta(hours=9), _LOW_RVR.format(airport="KIAD"))
    a = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"}, alerts=[old]), now=NOW)
    assert a.status == "AMBER"


def test_the_brief_still_fits_above_the_fold_and_claims_nothing_about_the_airspace():
    a = wa.assess(_context({"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK"}, alerts=[_critical("KIAD"), _critical("KDCA", "c2")]), now=NOW)
    assert a.above_fold_words <= wa.ABOVE_FOLD_MAX_WORDS
    assert wa.overreach(f"{a.status_line} {a.bluf}") is None


# ── W-02 ───────────────────────────────────────────────────────────────────────

def _stored_green_brief(hours_ago: float) -> WatchOfficerBrief:
    context = _healthy_context()
    green = wa.assess(context, now=NOW - timedelta(hours=hours_ago))
    assert green.status == "GREEN"
    return WatchOfficerBrief(generated_at=NOW - timedelta(hours=hours_ago), headline="All normal.", assessment=green)


def _dead_feed_context() -> dict:
    return _healthy_context(latest_event_at=(NOW - timedelta(hours=2)).isoformat(),
                            airport_status={"KDCA": "STALE", "KIAD": "STALE", "KBWI": "STALE"})


def test_a_green_brief_is_not_shown_as_all_clear_after_the_feed_dies():
    stored = _stored_green_brief(hours_ago=3)
    shown, notice = current_view(stored, _dead_feed_context(), now=NOW)

    assert stored.assessment.status == "GREEN", "what was stored is what it was"
    assert shown.assessment.status == "RED" and shown.assessment.status_line.startswith("DATA PICTURE: DOWN")
    assert "ALL CLEAR" not in shown.assessment.status_line
    assert "it was GREEN, it is now RED" in notice and "3 h" in notice or "about 3 h" in notice


def test_a_brief_older_than_the_down_threshold_carries_a_notice_even_if_nothing_changed():
    shown, notice = current_view(_stored_green_brief(hours_ago=3), _healthy_context(), now=NOW)
    assert shown.assessment.status == "GREEN"
    assert notice and "recomputed from the current data" in notice


def test_a_fresh_brief_with_an_unchanged_status_has_no_notice():
    stored = _stored_green_brief(hours_ago=0.1)
    shown, notice = current_view(stored, _healthy_context(), now=NOW)
    assert notice is None and shown.assessment.status == "GREEN"


def test_the_threshold_is_the_pipeline_down_threshold():
    just_under = NOW - timedelta(minutes=wa.DOWN_AFTER_MINUTES - 1)
    just_over = NOW - timedelta(minutes=wa.DOWN_AFTER_MINUTES + 1)
    for generated, expect in ((just_under, False), (just_over, True)):
        stored = WatchOfficerBrief(generated_at=generated, assessment=wa.assess(_healthy_context(), now=generated))
        assert bool(current_view(stored, _healthy_context(), now=NOW)[1]) is expect


def test_a_brief_stored_before_the_assessment_existed_gets_one():
    legacy = WatchOfficerBrief(generated_at=NOW - timedelta(minutes=1), headline="Old layout.")
    shown, notice = current_view(legacy, _healthy_context(), now=NOW)
    assert shown.assessment is not None and notice is None
    assert shown.headline == "Old layout."


def test_the_narrative_is_kept_and_only_the_assessment_is_replaced():
    stored = _stored_green_brief(hours_ago=3).model_copy(update={"what_changed": ["X happened"], "llm_used": True})
    shown, _ = current_view(stored, _dead_feed_context(), now=NOW)
    assert (shown.what_changed, shown.llm_used, shown.brief_id) == (["X happened"], True, stored.brief_id)


def test_the_dashboard_shows_the_current_view():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "current_view(_wo_latest, run(build_watch_officer_context()))" in source
    assert "brief_view.render(_wo_latest)" not in source, "the stored status is never what is rendered"


@pytest.mark.parametrize("hours", [0.5, 6, 30])
def test_the_notice_says_how_old_the_brief_is(hours):
    _, notice = current_view(_stored_green_brief(hours_ago=hours), _healthy_context(), now=NOW)
    assert notice and "ago" in notice
