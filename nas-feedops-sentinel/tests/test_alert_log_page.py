"""
The Alert Log drills down from every number (dashboard/pages/4_Alert_Log.py).

Run headlessly through Streamlit's AppTest against the in-memory store:
- the severity tiles and the facility tiles are links that narrow the view,
  keep the rest of the filter, and open the matching records right under the
  tiles (?focus=records), or lead with the newest alert (?focus=newest);
- "Which rules are firing" plots each rule's alert count, not a row count
  that drew every rule at 1;
- it is the record, not the work queue: a CRITICAL alert says where its
  review stands and links to it. It used to say "Supervisor action required"
  and offer no way to the review where that action is taken.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="the page renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src import storage  # noqa: E402
from src.auth import Decision, Principal, PrincipalSource  # noqa: E402
from src.execution_gate import decide_staged_action, stage_critical_alert  # noqa: E402
from src.models import AlertEvent  # noqa: E402

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "4_Alert_Log.py"
NOW = datetime.now(timezone.utc)
SUPERVISOR = Principal("smith", "Supervisor", PrincipalSource.OIDC, roles=frozenset({"supervisor"}))


@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


def _alert(rule: str, severity: str, airport: str, minutes_ago: int, text: str = "") -> AlertEvent:
    return AlertEvent(alert_type=rule, severity=severity, airport_code=airport, service_name="TAIS",
                      description=text or f"{rule} at {airport}", triggered_at=NOW - timedelta(minutes=minutes_ago))


async def _seed(db) -> None:
    await db.save_alerts([
        *[_alert("ALERT_FEED_STALE", "WARNING", "KBWI", 30 + i) for i in range(3)],
        _alert("ALERT_HIGH_LATENCY", "WARNING", "KIAD", 20),
        _alert("ALERT_LOW_RVR", "CRITICAL", "KBWI", 5, text="the newest one: RVR 600 ft on 10"),
    ])


def _run(**query: str) -> AppTest:
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    for key, value in query.items():
        at.query_params[key] = value
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _html(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def _hrefs(at: AppTest) -> dict[str, str]:
    """KPI tile label -> its link."""
    return {m.group(2): m.group(1).replace("&amp;", "&")
            for m in re.finditer(r'<a class="kpi" href="([^"]+)".*?<div class="kpi-label">([^<]+)</div>', _html(at))}


async def test_every_triage_tile_drills_into_its_alerts(db):
    await _seed(db)
    links = _hrefs(_run())
    assert links["Matching alerts"] == "Alert_Log?focus=records"
    assert links["Critical"] == "Alert_Log?severity=CRITICAL&focus=records"
    assert links["Warning"] == "Alert_Log?severity=WARNING&focus=records"
    assert links["Newest alert"] == "Alert_Log?focus=newest"
    assert "Informational" not in links, "no link to an empty set"


async def test_a_drill_down_keeps_the_rest_of_the_filter(db):
    await _seed(db)
    links = _hrefs(_run(airport="KBWI"))
    assert links["Critical"] == "Alert_Log?severity=CRITICAL&airport=KBWI&focus=records"


async def test_a_facility_tile_shows_its_alerts_instead_of_reloading_the_same_page(db):
    """Filtered to WARNING at KBWI, the KBWI tile used to link to ?airport=KBWI: the same page, all severities."""
    await _seed(db)
    links = _hrefs(_run(severity="WARNING", airport="KBWI"))
    assert links["KBWI"] == "Alert_Log?severity=WARNING&airport=KBWI&focus=records"


async def test_the_drill_down_opens_the_records_under_the_tiles(db):
    await _seed(db)
    at = _run(severity="WARNING", airport="KBWI", focus="records")
    html = _html(at)
    assert html.index("Drill-down: 3 warning alerts at KBWI") < html.index("Where the alerts cluster")
    assert "Alert records" not in html, "the records are shown once, at the top"
    [records] = [d.value for d in at.dataframe]
    assert len(records) == 3 and set(records["Severity"]) == {"WARNING"}
    assert 'href="Alert_Log?severity=WARNING&amp;airport=KBWI"' in html or \
        'href="Alert_Log?severity=WARNING&airport=KBWI"' in html, "a way back out"


async def test_the_newest_tile_leads_with_the_newest_alert(db):
    await _seed(db)
    html = _html(_run(focus="newest"))
    assert html.index("the newest one: RVR 600 ft on 10") < html.index("Where the alerts cluster")


async def test_without_a_drill_down_the_records_stay_at_the_bottom(db):
    await _seed(db)
    html = _html(_run())
    assert "Drill-down" not in html
    assert html.index("Where the alerts cluster") < html.index("Alert records")


def _rule_chart(at: AppTest) -> dict:
    specs = [json.loads(c.proto.spec) for c in at.get("vega_lite_chart")]
    [spec] = [s for s in specs if "layer" in s]
    return spec


async def test_which_rules_are_firing_plots_alert_counts_not_rows(db):
    await _seed(db)
    spec = _rule_chart(_run())
    bars, labels = spec["layer"]
    x = bars["encoding"]["x"]
    assert (x.get("aggregate"), x.get("field")) == ("sum", "count"), \
        "each row is already a count: count() drew every rule at 1"
    assert labels["mark"]["type"] == "text" and labels["encoding"]["text"]["field"] == "total", \
        "each bar is labelled with its number"
    assert bars["encoding"]["y"]["sort"][0] == "ALERT_FEED_STALE", "the busiest rule first"


# ── The record, not the work queue ────────────────────────────────────────────

async def _staged_critical(db):
    critical = _alert("ALERT_LOW_RVR", "CRITICAL", "KBWI", 5, text="RVR 600 ft on 10")
    await db.save_alerts([critical, _alert("ALERT_FEED_STALE", "WARNING", "KBWI", 30)])
    return await stage_critical_alert(critical)


def _review_by_severity(at: AppTest) -> dict[str, str]:
    [records] = [d.value for d in at.dataframe]
    return dict(zip(records["Severity"], records["Review"]))


async def test_a_critical_alert_says_where_its_review_stands_and_links_to_it(db):
    review = await _staged_critical(db)

    at = _run(focus="newest")

    assert _review_by_severity(at) == {"CRITICAL": "Awaiting decision", "WARNING": "—"}
    html = _html(at)
    assert f'href="AI_Critic_and_Review?review={review.action_id}"' in html, "the newest alert's card opens its review"
    assert "1 review(s) awaiting a decision" in html and "Supervisor action required" not in html


async def test_the_review_column_follows_what_was_decided(db):
    review = await _staged_critical(db)
    await decide_staged_action(review.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    at = _run(focus="newest")
    assert _review_by_severity(at)["CRITICAL"] == "Monitoring"
    assert f'href="AI_Critic_and_Review?review={review.action_id}"' in _html(at), "still open, still reachable"

    await decide_staged_action(review.action_id, Decision.REJECT, SUPERVISOR, "sensor artefact")

    at = _run(focus="newest")
    html = _html(at)
    assert _review_by_severity(at)["CRITICAL"] == "Rejected"
    assert "Review rejected by smith" in html
    assert f'href="AI_Critic_and_Review?review={review.action_id}"' in html, "decided, it opens as the record"
    assert "No review awaiting a decision" in html
