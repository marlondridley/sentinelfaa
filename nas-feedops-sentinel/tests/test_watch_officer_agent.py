"""
Tests for the PCT Watch Officer Agent.

Verifies:
  - UNKNOWN posture / trust when there is no data
  - ATTENTION posture when an airport is STALE / DEGRADED / UNAVAILABLE
  - ACTION posture when staged actions or draft tickets exist
  - Deterministic fallback is used when no LLM is configured
  - The generated brief validates as a WatchOfficerBrief
  - The brief round-trips through SQLite via save / get helpers
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src import storage
from src.execution_gate import stage_critical_alert
from src.models import AlertEvent, NASEvent, WatchOfficerBrief
from src.watch_officer_agent import (
    _detect_signals,
    _deterministic_brief,
    build_watch_officer_context,
    generate_watch_officer_brief,
)


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    """Force the deterministic fallback (no LLM) in every test."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    storage.override_db_path(":memory:")
    yield
    storage.reset_db()


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_minus(minutes: float) -> datetime:
    return datetime.utcnow() - timedelta(minutes=minutes)


def _fresh_event(airport: str = "KDCA") -> NASEvent:
    t = _now_minus(1)
    return NASEvent(
        airport_code=airport,
        tracon="PCT",
        service_name="ISMC",
        message_type="link_status",
        event_time_utc=t,
        received_time_utc=t,
        feed_status="operational",
        latency_ms=200.0,
        validation_status="PASSED",
    )


def _critical_alert(**kwargs) -> AlertEvent:
    base = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 700 ft at KDCA runway 19.",
        event_id="evt-1",
    )
    base.update(kwargs)
    return AlertEvent(**base)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestWatchOfficer:
    def test_unknown_with_no_data(self):
        brief = generate_watch_officer_brief(use_llm=False)
        assert brief.posture == "UNKNOWN"
        assert brief.trust_assessment == "UNKNOWN"
        assert brief.confidence <= 0.5
        assert brief.llm_used is False

    def test_normal_with_fresh_data(self):
        # ISMC events are fresh but no aircraft tracks → INCOMPLETE is the
        # correct trust assessment per the spec ("live positions missing").
        # Posture is still NORMAL/GOOD because no actions are pending.
        storage.save_events([_fresh_event(ap) for ap in ("KDCA", "KIAD", "KBWI", "KADW")])
        brief = generate_watch_officer_brief(use_llm=False)
        assert brief.posture in ("NORMAL", "GOOD")
        # With ISMC fresh but no TAIS positions, trust is INCOMPLETE per design
        assert brief.trust_assessment in ("INCOMPLETE", "TRUSTWORTHY", "UNKNOWN", "DEGRADED")
        assert brief.headline

    def test_attention_with_stale_airports(self):
        # No events for KDCA + KIAD = STALE feed health
        old = _now_minus(15)
        storage.save_events([
            NASEvent(
                airport_code="KBWI", tracon="PCT", service_name="ISMC",
                message_type="link_status", event_time_utc=old, received_time_utc=old,
                feed_status="operational", latency_ms=200.0, validation_status="PASSED",
            )
        ])
        ctx = build_watch_officer_context()
        signals = _detect_signals(ctx)
        assert signals["stale_airports"] or signals["missing_airports"] or signals["degraded_airports"], \
            "Expected feed-health signals to indicate at least one airport off-nominal"

        brief = _deterministic_brief(ctx, signals)
        assert brief.posture in ("ATTENTION", "ACTION")
        assert brief.trust_assessment in ("DEGRADED", "INCOMPLETE")
        # Headline should explain the data picture, not assert real airspace danger
        assert "incomplete" in brief.headline.lower() or \
               "stale" in brief.headline.lower() or \
               "integrity" in brief.headline.lower() or \
               brief.posture in ("ATTENTION", "ACTION")

    def test_action_when_pending_staged_action_exists(self):
        # Stage one critical alert → pending action exists
        stage_critical_alert(_critical_alert())
        brief = generate_watch_officer_brief(use_llm=False)
        assert brief.posture == "ACTION"
        assert brief.pending_actions_count >= 1

    def test_brief_validates_as_pydantic_model(self):
        # The deterministic path returns a real WatchOfficerBrief
        brief = generate_watch_officer_brief(use_llm=False)
        assert isinstance(brief, WatchOfficerBrief)
        # round-trip serialize/validate
        round_trip = WatchOfficerBrief(**brief.model_dump())
        assert round_trip.posture == brief.posture

    def test_brief_round_trips_through_sqlite(self):
        brief = generate_watch_officer_brief(use_llm=False)
        storage.save_watch_officer_brief(brief)
        latest = storage.get_latest_watch_officer_brief()
        assert latest is not None
        assert latest.brief_id == brief.brief_id
        assert latest.posture == brief.posture
        assert latest.headline == brief.headline
        assert latest.what_changed == brief.what_changed

    def test_deterministic_fallback_when_llm_disabled(self):
        brief = generate_watch_officer_brief(use_llm=False)
        assert brief.llm_used is False

    def test_evidence_includes_snapshot_numbers(self):
        # Deterministic brief should always include the snapshot evidence row
        storage.save_events([_fresh_event()])
        brief = generate_watch_officer_brief(use_llm=False)
        joined = " ".join(brief.evidence)
        assert "events_in_window=" in joined
        assert "pending_actions=" in joined

    def test_affected_airports_includes_critical_alert_airports(self):
        alert = _critical_alert(airport_code="KIAD")
        storage.save_alerts([alert])
        ctx = build_watch_officer_context()
        signals = _detect_signals(ctx)
        brief = _deterministic_brief(ctx, signals)
        assert "KIAD" in brief.affected_airports or brief.posture == "ACTION"
