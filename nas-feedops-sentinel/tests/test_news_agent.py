"""
Tests for the refocused banner — answers "What needs attention right now?"

Verifies banner severity priorities:
  - RED ACTION when pending staged actions OR draft escalation packages exist
  - YELLOW ATTENTION for stale/degraded/missing feeds (no pending actions)
  - GREEN GOOD when all airports current with no pending review
"""
from __future__ import annotations

import pytest

from src import storage
from src.execution_gate import stage_critical_alert
from src.models import AlertEvent
from src.news_agent import build_fast_signal_items


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _critical_alert(**kwargs) -> AlertEvent:
    base = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 700 ft.",
        event_id="evt-1",
    )
    base.update(kwargs)
    return AlertEvent(**base)


class TestBannerFastSignal:
    def test_red_action_when_staged_actions_exist(self):
        stage_critical_alert(_critical_alert())
        items = build_fast_signal_items(
            airport_status={"KDCA": "OK", "KIAD": "OK", "KBWI": "OK", "KADW": "OK"},
            alerts=[], rvr_rows=[],
        )
        assert any(it["severity"] == "ACTION" for it in items)
        action_text = next(it["text"] for it in items if it["severity"] == "ACTION")
        assert "pending" in action_text.lower() or "review" in action_text.lower()

    def test_yellow_attention_when_feed_stale_no_pending(self):
        items = build_fast_signal_items(
            airport_status={"KDCA": "STALE", "KIAD": "OK", "KBWI": "OK", "KADW": "OK"},
            alerts=[], rvr_rows=[],
        )
        # Must contain at least one ATTENTION item
        assert any(it["severity"] == "ATTENTION" for it in items)
        # Must NOT contain an ACTION item (no pending review)
        assert not any(it["severity"] == "ACTION" for it in items)

    def test_green_good_when_all_clear(self):
        items = build_fast_signal_items(
            airport_status={"KDCA": "OK", "KIAD": "OK", "KBWI": "OK", "KADW": "OK"},
            alerts=[], rvr_rows=[],
        )
        # No pending, no stale → GOOD
        assert any(it["severity"] == "GOOD" for it in items)
        assert not any(it["severity"] == "ACTION" for it in items)

    def test_red_action_includes_low_rvr_safety(self):
        items = build_fast_signal_items(
            airport_status={"KDCA": "OK"},
            alerts=[],
            rvr_rows=[{"airport_code": "KDCA", "runway": "19", "rvr_touchdown": 700}],
        )
        # Sub-1200 ft RVR must surface as ACTION (safety-critical)
        assert any(it["severity"] == "ACTION" and "RVR" in it["text"].upper() for it in items)

    def test_banner_is_concise(self):
        # Even with multiple stale airports, banner stays focused (≤ 8 items)
        items = build_fast_signal_items(
            airport_status={"KDCA": "STALE", "KIAD": "STALE", "KBWI": "DEGRADED", "KADW": "OK"},
            alerts=[], rvr_rows=[],
        )
        assert len(items) <= 8

    def test_anchor_links_present(self):
        stage_critical_alert(_critical_alert())
        items = build_fast_signal_items(
            airport_status={"KDCA": "OK"}, alerts=[], rvr_rows=[],
        )
        # Every banner item must have an anchor for jump-to-section behavior
        assert all("anchor" in it for it in items)
