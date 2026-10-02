"""
The home page is the Dashboard (dashboard/pages/0_Dashboard.py, reached through
dashboard/app.py's navigation).

- The sidebar used to list the pages by file name, so the home page was called
  "app" and the order was the order of the files. It now lists them in the
  order the work runs, and each address is the one every drill-down link uses.
- The Dashboard breaks the review queue out: each review awaiting a decision
  is a line that opens that review, counted the way the Review Queue counts
  (src/review_queue.py), so the number here is those rows there.
"""
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="the Dashboard renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src import storage  # noqa: E402
from src.auth import Decision, Principal, PrincipalSource  # noqa: E402
from src.execution_gate import decide_staged_action, stage_critical_alert  # noqa: E402
from src.models import AlertEvent, CriticResult, NASEvent  # noqa: E402

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
APP = DASHBOARD / "app.py"
SUPERVISOR = Principal("smith", "Supervisor", PrincipalSource.OIDC, roles=frozenset({"supervisor"}))


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _navigation() -> list[tuple[str, str, str, str]]:
    """app.py's NAVIGATION, read without running it: (section, page key, script, label)."""
    [value] = [node.value for node in ast.parse(APP.read_text(encoding="utf-8")).body
               if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "NAVIGATION"]
    return list(ast.literal_eval(value))


def test_the_sidebar_lists_the_pages_in_working_order_and_home_is_the_dashboard():
    if str(DASHBOARD) not in sys.path:
        sys.path.insert(0, str(DASHBOARD))
    from lib import theme

    navigation = _navigation()

    assert navigation[0] == ("Operate", "overview", "pages/0_Dashboard.py", "Dashboard")
    assert [label for *_, label in navigation] == [
        "Dashboard", "Review Queue", "Alert Log", "Live Airspace", "Feed Health", "Ingest and Replay",
        "Agent Evaluation"], "decide, then look up, then observe"
    assert theme.PAGES["overview"] == "/", "the Dashboard is the home page"
    assert sorted(key for _, key, _, _ in navigation) == sorted(theme.PAGES), \
        "every page a drill-down link can name is in the sidebar, at that address, and no other"
    assert all((DASHBOARD / script).is_file() for _, _, script, _ in navigation)


def _dashboard() -> str:
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, [e.value for e in at.exception]
    return "\n".join(m.value for m in at.markdown)


def _rvr(airport: str) -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code=airport, service_name="APDS_RVR",
                      description=f"Low RVR 600 ft at touchdown on runway 10 at {airport}", data_origin="live")


async def _stage(*alerts: AlertEvent):
    await storage.save_alerts(list(alerts))
    return [await stage_critical_alert(a) for a in alerts]


def _tile(html: str, label: str) -> str:
    return re.search(r'<div class="kpi-label">' + re.escape(label) + r'</div><div class="kpi-value"[^>]*>([^<]*)',
                     html).group(1)


async def test_the_dashboard_breaks_out_each_review_awaiting_a_decision():
    kiad_alert, kbwi_alert, kdca_alert = _rvr("KIAD"), _rvr("KBWI"), _rvr("KDCA")
    kiad, kbwi, kdca = await _stage(kiad_alert, kbwi_alert, kdca_alert)
    await storage.save_critic_result(CriticResult(
        alert_id=kbwi_alert.alert_id, confidence=0.86, escalate=True, pattern="WEATHER_CORRELATED", reasoning="r"))
    await decide_staged_action(kdca.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    html = _dashboard()

    rows = re.findall(r'<a href="AI_Critic_and_Review\?review=([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    assert [action_id for action_id, _ in rows] == [kbwi.action_id, kiad.action_id], \
        "one line per review awaiting a decision, what the AI would escalate first; the monitored one is not asked again"
    assert "KBWI" in rows[0][1] and "LOW_RVR" in rows[0][1] and "AI suggests escalate · 86%" in rows[0][1]
    assert "1 being monitored" in html and "AI_Critic_and_Review?show=monitoring" in html
    assert "Not reviewed by the AI" in rows[1][1]
    assert _tile(html, "Awaiting decision") == "2"


async def test_with_nothing_awaiting_the_breakout_says_so():
    html = _dashboard()
    assert "Nothing is awaiting a decision." in html and _tile(html, "Awaiting decision") == "0"
    assert "AI_Critic_and_Review?review=" not in html


async def test_a_monitored_review_is_still_an_open_critical_condition_and_the_verdict_says_so():
    now = datetime.now(timezone.utc)
    await storage.save_events([NASEvent(
        airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="STATUS", event_time_utc=now,
        received_time_utc=now, latency_ms=100.0, feed_status="operational", validation_status="PASSED",
        data_origin="live")])
    [review] = await _stage(_rvr("KDCA"))
    await decide_staged_action(review.action_id, Decision.MONITOR, SUPERVISOR, "watching")

    html = _dashboard()

    assert re.search(r"\bRED\b", html), "monitoring a condition does not make the feed trustworthy"
    assert "1 CRITICAL review(s) open (1 being monitored)" in html
    assert _tile(html, "Awaiting decision") == "0"
