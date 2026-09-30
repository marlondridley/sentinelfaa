"""
The Watch Officer brief on the Overview page (dashboard/lib/brief_view.py): a fixed shape, read at a glance.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="brief_view renders through Streamlit")

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from lib import brief_view  # noqa: E402

from src.models import BriefAction, BriefAssessment, DataHealth, WatchOfficerBrief  # noqa: E402


def _brief(**assessment) -> WatchOfficerBrief:
    a = BriefAssessment(
        status=assessment.pop("status", "RED"), status_line="DATA PICTURE: DOWN — our feed, not the airports",
        bluf="Our feed stopped about 12 h ago. This is our pipeline, not the airports.",
        actions=assessment.pop("actions", [BriefAction(action="Confirm the feed is live.", owner="Pipeline on-call",
                                                      done_when="All airports show OK for 5 min.")]),
        confidence_label="HIGH", confidence_reason="all airports stale together.",
        data_health=DataHealth(state="DOWN", scope="pipeline"), **assessment)
    return WatchOfficerBrief(headline=a.bluf, generated_at=datetime(2026, 9, 30, 1, 1, tzinfo=timezone.utc), assessment=a)


def test_the_top_of_the_brief_is_the_status_the_bottom_line_and_the_confidence():
    html = brief_view.summary_html(_brief())
    assert "DATA PICTURE: DOWN — our feed, not the airports" in html
    assert "This is our pipeline, not the airports." in html
    assert "Confidence <b" in html and "HIGH" in html and "2026-09-30 01:01 UTC" in html
    assert 'class="tip"' in html and "not about the airspace" in html, "what the confidence is about is one hover away"


@pytest.mark.parametrize("status, tone_colour", [("RED", "#FA999C"), ("AMBER", "#FBB360"), ("GREEN", "#72CA9B")])
def test_the_status_colour_follows_the_status(status, tone_colour):
    assert tone_colour in brief_view.summary_html(_brief(status=status))


def test_every_action_shows_its_owner_and_when_it_is_done():
    html = brief_view.actions_html(_brief())
    assert "Do now" in html and "Confirm the feed is live." in html
    assert "Owner: <b" in html and "Pipeline on-call" in html and "Done when: All airports show OK for 5 min." in html


def test_no_actions_says_so_instead_of_leaving_a_gap():
    html = brief_view.actions_html(_brief(actions=[], status="GREEN"))
    assert "No action is needed" in html and "Owner:" not in html


def test_text_from_a_model_or_an_alert_cannot_inject_markup():
    brief = _brief()
    brief.assessment.bluf = '<img src=x onerror="alert(1)">'
    brief.assessment.actions[0].action = "<script>x()</script>"
    html = brief_view.summary_html(brief) + brief_view.actions_html(brief)
    assert "<img" not in html and "<script" not in html and "&lt;img" in html


def test_a_brief_stored_before_the_assessment_existed_keeps_the_older_layout(monkeypatch):
    seen = []
    monkeypatch.setattr(brief_view, "render_legacy", lambda brief: seen.append(brief))
    monkeypatch.setattr(brief_view.st, "markdown", lambda *a, **k: seen.append("new layout"))
    old = WatchOfficerBrief(headline="written before the frame existed")
    brief_view.render(old)
    assert seen == [old], "no assessment: the legacy renderer, not the new one"


def test_the_incident_lines_say_whether_each_is_current_history_or_an_artifact():
    from src.models import BriefIncident

    when = datetime(2026, 9, 25, 19, 52, tzinfo=timezone.utc)
    brief = _brief(incidents=[
        BriefIncident(started_at=when, ended_at=when, airports=["KDCA"], alert_types=["feed disconnected"], alert_count=3,
                      reviews=3, historical=True),
        BriefIncident(started_at=when, ended_at=when, airports=["KBWI", "KIAD"], alert_types=["low rvr"], alert_count=2,
                      reviews=2, historical=True, artifact=True),
        BriefIncident(started_at=when, ended_at=when, airports=["KIAD"], alert_types=["feed stale"], alert_count=1)])
    lines = brief_view._incident_lines(brief)
    assert lines[0].endswith("history") and lines[1].endswith("sensor artifact") and lines[2].endswith("current")
    assert "KBWI, KIAD" in lines[1] and "2 alert(s)" in lines[1]
