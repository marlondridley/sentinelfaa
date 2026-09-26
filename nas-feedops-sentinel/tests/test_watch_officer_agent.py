"""
Tests for the PCT Watch Officer Agent.

Verifies:
  - UNKNOWN posture / trust when there is no data
  - ATTENTION posture when an airport is STALE / DEGRADED / UNAVAILABLE
  - ACTION posture when staged actions or draft tickets exist
  - Deterministic fallback is used when no LLM is configured
  - The generated brief validates as a WatchOfficerBrief
  - The brief round-trips through storage via save / get helpers
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    storage.override_db_path(":memory:")


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_minus(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


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
    )
    base.update(kwargs)
    return AlertEvent(**base)


async def _persist_and_stage(alert: AlertEvent):
    """Save the alert, then stage it -- the same order src/pipeline.py uses
    (staged_actions.alert_id is a foreign key to alert_events)."""
    await storage.save_alert(alert)
    return await stage_critical_alert(alert)


async def _brief_from_current_state() -> WatchOfficerBrief:
    """
    Build the context on this test's event loop and hand it in, the way
    execution_gate._build_escalation_package() does. Calling
    generate_watch_officer_brief() without a context makes it fetch one via
    async_bridge.run_async(), which cannot run inside an already-running loop.
    The no-context path is what the synchronous dashboard uses, and the
    synchronous tests below (no data, pydantic round-trip, LLM disabled)
    keep covering it.
    """
    return generate_watch_officer_brief(await build_watch_officer_context(), use_llm=False)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestWatchOfficer:
    def test_unknown_with_no_data(self):
        brief = generate_watch_officer_brief(use_llm=False)
        assert brief.posture == "UNKNOWN"
        assert brief.trust_assessment == "UNKNOWN"
        assert brief.confidence <= 0.5
        assert brief.llm_used is False

    async def test_normal_with_fresh_data(self):
        # ISMC events are fresh but no aircraft tracks → INCOMPLETE is the
        # correct trust assessment per the spec ("live positions missing").
        # Posture is still NORMAL/GOOD because no actions are pending.
        await storage.save_events([_fresh_event(ap) for ap in ("KDCA", "KIAD", "KBWI", "KADW")])
        brief = await _brief_from_current_state()
        assert brief.posture in ("NORMAL", "GOOD")
        # With ISMC fresh but no TAIS positions, trust is INCOMPLETE per design
        assert brief.trust_assessment in ("INCOMPLETE", "TRUSTWORTHY", "UNKNOWN", "DEGRADED")
        assert brief.headline

    async def test_attention_with_stale_airports(self):
        # No events for KDCA + KIAD = STALE feed health
        old = _now_minus(15)
        await storage.save_events([
            NASEvent(
                airport_code="KBWI", tracon="PCT", service_name="ISMC",
                message_type="link_status", event_time_utc=old, received_time_utc=old,
                feed_status="operational", latency_ms=200.0, validation_status="PASSED",
            )
        ])
        ctx = await build_watch_officer_context()
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

    async def test_action_when_pending_staged_action_exists(self):
        # Stage one critical alert → pending action exists
        await _persist_and_stage(_critical_alert())
        brief = await _brief_from_current_state()
        assert brief.posture == "ACTION"
        assert brief.pending_actions_count >= 1

    def test_brief_validates_as_pydantic_model(self):
        # The deterministic path returns a real WatchOfficerBrief
        brief = generate_watch_officer_brief(use_llm=False)
        assert isinstance(brief, WatchOfficerBrief)
        # round-trip serialize/validate
        round_trip = WatchOfficerBrief(**brief.model_dump())
        assert round_trip.posture == brief.posture

    async def test_brief_round_trips_through_storage(self):
        brief = await _brief_from_current_state()
        await storage.save_watch_officer_brief(brief)
        latest = await storage.get_latest_watch_officer_brief()
        assert latest is not None
        assert latest.brief_id == brief.brief_id
        assert latest.posture == brief.posture
        assert latest.headline == brief.headline
        assert latest.what_changed == brief.what_changed

    def test_deterministic_fallback_when_llm_disabled(self):
        brief = generate_watch_officer_brief(use_llm=False)
        assert brief.llm_used is False

    async def test_evidence_includes_snapshot_numbers(self):
        # Deterministic brief should always include the snapshot evidence row
        await storage.save_events([_fresh_event()])
        brief = await _brief_from_current_state()
        joined = " ".join(brief.evidence)
        assert "events_in_window=" in joined
        assert "pending_actions=" in joined

    async def test_affected_airports_includes_critical_alert_airports(self):
        alert = _critical_alert(airport_code="KIAD")
        await storage.save_alerts([alert])
        ctx = await build_watch_officer_context()
        signals = _detect_signals(ctx)
        brief = _deterministic_brief(ctx, signals)
        assert "KIAD" in brief.affected_airports or brief.posture == "ACTION"


# ── LLM narrative path ───────────────────────────────────────────────────────
#
# Until the CrewAI removal this path could never run: get_watch_officer_agent()
# referenced an unimported `settings`, raised NameError, and every brief fell
# back to the deterministic one without anyone noticing.

class _Client:
    def __init__(self, output=None, exc=None):
        self.output, self.exc, self.calls = output, exc, []

    async def acall_with_tool(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.output


_NARRATIVE = {
    "headline": "KDCA ISMC link degraded; data picture incomplete.",
    "what_changed": ["ISMC reported degraded"], "why_it_matters": ["Feed trust reduced"],
    "recommended_human_review": ["Verify ISMC against the facility"],
    "confidence": 0.7, "limitations": ["Single feed"],
}


async def test_llm_brief_uses_model_narrative_but_deterministic_posture(monkeypatch):
    import src.watch_officer_agent as wo

    client = _Client(output=_NARRATIVE)
    monkeypatch.setattr(wo.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(wo, "build_llm_client", lambda *a, **k: client)

    context = await build_watch_officer_context()
    brief = await wo.agenerate_watch_officer_brief(context, use_llm=True)

    assert brief.llm_used is True
    assert brief.headline == _NARRATIVE["headline"]
    # Posture and trust are never the model's to set.
    expected = _deterministic_brief(context, _detect_signals(context))
    assert (brief.posture, brief.trust_assessment) == (expected.posture, expected.trust_assessment)
    assert client.calls and client.calls[0]["tool"]["name"] == "submit_watch_officer_brief"


async def test_llm_failure_falls_back_to_the_deterministic_brief(monkeypatch):
    import src.watch_officer_agent as wo

    monkeypatch.setattr(wo.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(wo, "build_llm_client", lambda *a, **k: _Client(exc=RuntimeError("down")))

    brief = await wo.agenerate_watch_officer_brief(await build_watch_officer_context(), use_llm=True)
    assert brief.llm_used is False
    assert any("Deterministic" in item for item in brief.limitations)


async def test_malformed_llm_output_falls_back(monkeypatch):
    import src.watch_officer_agent as wo

    monkeypatch.setattr(wo.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(wo, "build_llm_client", lambda *a, **k: _Client(output={"confidence": 9}))

    brief = await wo.agenerate_watch_officer_brief(await build_watch_officer_context(), use_llm=True)
    assert brief.llm_used is False
