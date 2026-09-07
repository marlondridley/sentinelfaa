"""
Tests for CriticAgent.

The Anthropic client is mocked so no API key or network is required.
Tests verify:
  - Degraded path when API key is absent
  - Degraded path when API call raises
  - Happy path with a mocked tool-use response
  - Context builder with an empty DB
  - Context builder with events in DB
  - save_critic_result / round-trip through storage
"""
from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.models import AlertEvent, CriticResult
from src import storage
from src.context_router import ContextRouter
from src.critic_agent import CriticAgent, _format_user_message


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db():
    """Each test gets a fresh in-memory database."""
    storage.override_db_path(":memory:")
    yield
    storage.reset_db()


def _sample_alert(**kwargs) -> AlertEvent:
    defaults = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 800 ft at touchdown on runway 01 at KDCA.",
        event_id="evt-001",
    )
    defaults.update(kwargs)
    return AlertEvent(**defaults)


def _mock_tool_response(
    confidence: float = 0.85,
    reasoning: str = "Evidence is strong.",
    citations: list[str] | None = None,
    pattern: str = "ISOLATED_FAULT",
    escalate: bool = True,
) -> dict:
    return {
        "confidence": confidence,
        "reasoning": reasoning,
        "citations": citations or [],
        "pattern": pattern,
        "escalate": escalate,
    }


# ── Degraded-path tests ───────────────────────────────────────────────────────

def test_no_api_key_returns_degraded_result(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_ENDPOINT", raising=False)
    agent = CriticAgent()
    alert = _sample_alert()
    result = agent.critique(alert)

    assert result.alert_id == alert.alert_id
    assert result.confidence is None
    assert result.escalate is False
    assert "no LLM provider" in result.reasoning.lower() or "unavailable" in result.reasoning.lower()


def test_api_error_returns_degraded_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CRITIC_ENABLED", "1")

    mock_llm = MagicMock()
    mock_llm.call_with_tool.side_effect = RuntimeError("network timeout")

    with patch("src.critic_agent.build_llm_client", return_value=mock_llm):
        agent = CriticAgent()
        alert = _sample_alert()
        result = agent.critique(alert)

    assert result.confidence is None
    assert "network timeout" in result.reasoning


def test_critic_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CRITIC_ENABLED", "0")

    # Reload config so the env change takes effect
    import importlib
    from src import config as cfg
    importlib.reload(cfg)

    with patch("src.critic_agent.config", cfg):
        agent = CriticAgent()
        alert = _sample_alert()
        result = agent.critique(alert)

    assert result.confidence is None
    assert "CRITIC_ENABLED" in result.reasoning

    # Restore
    monkeypatch.setenv("CRITIC_ENABLED", "1")
    importlib.reload(cfg)


# ── Happy-path test ───────────────────────────────────────────────────────────

def test_happy_path_returns_critic_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CRITIC_ENABLED", "1")

    mock_response = _mock_tool_response(
        confidence=0.91,
        reasoning="ISMC also degraded; multi-service impact suggests cascade.",
        citations=["evt-aaa", "evt-bbb"],
        pattern="CASCADE_FAILURE",
        escalate=True,
    )

    mock_llm = MagicMock()
    mock_llm.call_with_tool.return_value = mock_response

    with patch("src.critic_agent.build_llm_client", return_value=mock_llm):
        agent = CriticAgent()
        alert = _sample_alert()
        result = agent.critique(alert)

    assert result.alert_id == alert.alert_id
    assert abs(result.confidence - 0.91) < 1e-6
    assert result.pattern == "CASCADE_FAILURE"
    assert result.escalate is True
    assert "cascade" in result.reasoning.lower()
    assert "evt-aaa" in result.citations


# ── Context-builder tests ─────────────────────────────────────────────────────

def test_build_context_empty_db():
    alert = _sample_alert()
    router = ContextRouter()
    ctx = router.build_alert_context(airport_code=alert.airport_code)

    assert ctx["total_recent_events"] == 0
    assert ctx["service_summary"] == {}
    assert ctx["recent_alerts"] == []
    assert ctx["window_minutes"] == 30


def test_build_context_with_events(monkeypatch):
    from src.models import NASEvent
    from src.storage import save_event, save_alert

    now = datetime.utcnow()
    for i in range(3):
        save_event(NASEvent(
            airport_code="KDCA",
            tracon="PCT",
            service_name="APDS_RVR",
            message_type="rvr_report",
            event_time_utc=now,
            received_time_utc=now,
            latency_ms=500.0,
            runway="01",
            rvr_touchdown=900,
            validation_status="PASSED",
        ))

    alert = _sample_alert()
    save_alert(alert)

    router = ContextRouter()
    ctx = router.build_alert_context(airport_code=alert.airport_code)

    assert ctx["total_recent_events"] == 3
    assert "APDS_RVR" in ctx["service_summary"]
    svc = ctx["service_summary"]["APDS_RVR"]
    assert svc["count"] == 3
    assert svc["avg_latency_ms"] == 500.0
    assert len(svc["sample_event_ids"]) == 3
    assert len(ctx["recent_alerts"]) == 1
    assert ctx["recent_alerts"][0]["alert_type"] == "ALERT_LOW_RVR"


# ── Format message test ───────────────────────────────────────────────────────

def test_format_user_message_contains_key_fields():
    alert = _sample_alert()
    ctx = {
        "window_minutes": 30,
        "total_recent_events": 7,
        "service_summary": {
            "APDS_RVR": {
                "count": 7,
                "feed_statuses": [],
                "sample_event_ids": ["id-1"],
                "avg_latency_ms": 300.0,
                "max_latency_ms": 450.0,
            }
        },
        "recent_alerts": [],
    }
    msg = _format_user_message(alert, ctx)

    assert "ALERT_LOW_RVR" in msg
    assert "KDCA" in msg
    assert "APDS_RVR" in msg
    assert "count=7" in msg
    assert "avg_lat=300.0ms" in msg


# ── Storage round-trip test ───────────────────────────────────────────────────

def test_save_and_retrieve_critic_result():
    alert = _sample_alert()
    result = CriticResult(
        alert_id=alert.alert_id,
        confidence=0.75,
        reasoning="Solid evidence, isolated fault.",
        citations=["id-x", "id-y"],
        pattern="ISOLATED_FAULT",
        escalate=True,
    )
    storage.save_critic_result(result)

    conn = storage._get_conn()
    row = conn.execute(
        "SELECT * FROM critic_results WHERE alert_id = ?", (alert.alert_id,)
    ).fetchone()

    assert row is not None
    assert abs(row["confidence"] - 0.75) < 1e-6
    assert row["pattern"] == "ISOLATED_FAULT"
    assert row["escalate"] == 1
    import json
    assert json.loads(row["citations"]) == ["id-x", "id-y"]
