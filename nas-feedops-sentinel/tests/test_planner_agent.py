"""
Tests for PlannerAgent, ContextRouter, and demo scenario.
No API key required — Anthropic client is mocked throughout.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src import storage
from src.context_router import ContextRouter
from src.demo_scenario import generate_cascade_failure_scenario
from src.models import NASEvent, PlannerResult
from src.planner_agent import PlannerAgent


def _format_planner_message(question: str, context: dict) -> str:
    """Helper function for testing message formatting assertions."""
    summary = context.get("summary", {})
    escalation_required = summary.get("escalation_required", False)
    escalation_str = f"ESCALATION REQUIRED: {escalation_required}"
    if escalation_required:
        escalation_str += " [ESCALATE]"

    return (
        f"Question: {question}\n\n"
        f"System Status Summary:\n{escalation_str}\n\n"
        f"Current System Context:\n{json.dumps(context, indent=2)}"
    )

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _mock_planner_response(
    answer: str = "KDCA is experiencing a cascade failure.",
    sub_queries: list[str] | None = None,
    confidence: float = 0.88,
    citations: list[str] | None = None,
    recommended_actions: list[str] | None = None,
) -> dict:
    return {
        "answer": answer,
        "sub_queries": sub_queries or ["Feed health status?", "Active alerts?"],
        "confidence": confidence,
        "citations": citations or [],
        "recommended_actions": recommended_actions or ["Notify duty manager."],
    }


# ── Degraded-path tests ───────────────────────────────────────────────────────

def test_no_api_key_returns_degraded(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_ENDPOINT", raising=False)
    agent = PlannerAgent()
    result = agent.query("What is the status?")

    assert result.confidence is None
    assert "no LLM provider" in result.answer.lower() or "unavailable" in result.answer.lower()
    assert result.question == "What is the status?"


def test_api_error_returns_degraded(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    mock_llm = MagicMock()
    mock_llm.call_with_tool.side_effect = RuntimeError("connection refused")

    with patch("src.planner_agent.build_llm_client", return_value=mock_llm):
        agent = PlannerAgent()
        result = agent.query("What is the status?")

    assert result.confidence is None
    assert "connection refused" in result.answer


# ── Happy-path test ───────────────────────────────────────────────────────────

def test_happy_path_returns_planner_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("CRITIC_ENABLED", "1")

    mock_response = _mock_planner_response(
        answer="KDCA is experiencing a CASCADE_FAILURE across ISMC and APDS_RVR.",
        sub_queries=["Feed status per airport?", "Active CRITICAL alerts?", "RVR readings?"],
        confidence=0.92,
        citations=["evt-001", "evt-002"],
        recommended_actions=[
            "Notify KDCA tower supervisor of ISMC disconnection.",
            "Initiate low-visibility procedures on runways 01 and 19.",
        ],
    )

    mock_llm = MagicMock()
    mock_llm.call_with_tool.return_value = mock_response

    with patch("src.planner_agent.build_llm_client", return_value=mock_llm):
        agent = PlannerAgent()
        result = agent.query("What is happening at PCT TRACON right now?")

    assert abs(result.confidence - 0.92) < 1e-6
    assert "CASCADE_FAILURE" in result.answer
    assert len(result.sub_queries) == 3
    assert len(result.recommended_actions) == 2
    assert "evt-001" in result.citations


# ── ContextRouter tests ───────────────────────────────────────────────────────

def test_build_status_context_empty_db():
    router = ContextRouter()
    ctx = router.build_status_context()

    assert "airports" in ctx
    assert "active_alerts" in ctx
    assert "summary" in ctx
    assert ctx["summary"]["total_events_30m"] == 0
    assert ctx["summary"]["critical_alerts"] == 0
    for ap in ("KDCA", "KIAD", "KBWI", "KADW"):
        assert ap in ctx["airports"]
        assert ctx["airports"][ap]["overall_status"] == "NO DATA"


def test_build_status_context_with_events():
    now = datetime.now(timezone.utc)
    storage.save_event(NASEvent(
        airport_code="KDCA", tracon="PCT", service_name="ISMC",
        message_type="link_status", event_time_utc=now, received_time_utc=now,
        latency_ms=300.0, feed_status="degraded", validation_status="PASSED",
    ))

    router = ContextRouter()
    ctx = router.build_status_context()

    assert ctx["airports"]["KDCA"]["overall_status"] == "DEGRADED"
    assert ctx["airports"]["KDCA"]["event_count_30m"] == 1
    assert "ISMC" in ctx["airports"]["KDCA"]["services"]


def test_build_alert_context_delegates_to_router():
    now = datetime.now(timezone.utc)
    for _ in range(4):
        storage.save_event(NASEvent(
            airport_code="KIAD", tracon="PCT", service_name="APDS_RVR",
            message_type="rvr_report", event_time_utc=now, received_time_utc=now,
            latency_ms=210.0, runway="01", rvr_touchdown=3000,
            validation_status="PASSED",
        ))

    router = ContextRouter()
    ctx = router.build_alert_context(airport_code="KIAD")

    assert ctx["total_recent_events"] == 4
    assert "APDS_RVR" in ctx["service_summary"]
    assert ctx["service_summary"]["APDS_RVR"]["count"] == 4


# ── Demo scenario tests ───────────────────────────────────────────────────────

def test_demo_scenario_generates_events():
    events = generate_cascade_failure_scenario()
    assert len(events) >= 30

    airports = {e.airport_code for e in events}
    assert "KDCA" in airports
    assert "KIAD" in airports

    services = {e.service_name for e in events}
    assert "ISMC" in services
    assert "APDS_RVR" in services
    assert "SMES" in services


def test_demo_scenario_contains_cascade_signals():
    events = generate_cascade_failure_scenario()

    kdca_ismc = [e for e in events if e.airport_code == "KDCA" and e.service_name == "ISMC"]
    statuses = [e.feed_status for e in kdca_ismc if e.feed_status]
    assert "disconnected" in statuses
    assert "degraded" in statuses

    kdca_rvr = [e for e in events if e.airport_code == "KDCA" and e.service_name == "APDS_RVR"]
    low_rvr = [e for e in kdca_rvr if e.rvr_touchdown and e.rvr_touchdown < 1200]
    assert len(low_rvr) >= 2

    kdca_smes = [e for e in events if e.airport_code == "KDCA" and e.service_name == "SMES"]
    assert len(kdca_smes) >= 10


def test_demo_scenario_healthy_contrast():
    events = generate_cascade_failure_scenario()

    healthy = [
        e for e in events
        if e.airport_code in ("KIAD", "KBWI", "KADW")
        and e.service_name == "APDS_RVR"
        and e.rvr_touchdown is not None
        and e.rvr_touchdown > 1200
    ]
    assert len(healthy) >= 3


# ── Storage round-trip test ───────────────────────────────────────────────────

def test_save_and_retrieve_planner_result():
    result = PlannerResult(
        question="What is the status of KDCA?",
        answer="KDCA is experiencing a cascade failure.",
        sub_queries=["Feed health?", "Active alerts?"],
        confidence=0.88,
        citations=["id-a", "id-b"],
        recommended_actions=["Notify tower.", "Initiate LVP."],
    )
    storage.save_planner_result(result)

    briefs = storage.get_recent_planner_results(limit=5)
    assert len(briefs) == 1
    assert briefs[0]["question"] == result.question
    assert json.loads(briefs[0]["recommended_actions"]) == result.recommended_actions
    assert abs(briefs[0]["confidence"] - 0.88) < 1e-6


# ── Format message test ───────────────────────────────────────────────────────

def test_format_planner_message_contains_key_fields():
    ctx = {
        "snapshot_time": "2026-05-19T14:00:00",
        "window_minutes": 30,
        "airports": {
            "KDCA": {
                "overall_status": "DISCONNECTED",
                "event_count_30m": 25,
                "services": {
                    "ISMC": {
                        "status": "DISCONNECTED",
                        "count": 5,
                        "feed_statuses": ["disconnected"],
                        "sample_event_ids": ["id-1"],
                    }
                },
                "rvr": {"01": {"touchdown_ft": 600, "midpoint_ft": 700, "rollout_ft": 900, "time_utc": ""}},
            }
        },
        "active_alerts": [
            {
                "alert_id": "al-1",
                "alert_type": "ALERT_ISMC_LINK_DEGRADED",
                "severity": "CRITICAL",
                "airport_code": "KDCA",
                "service_name": "ISMC",
                "description": "ISMC disconnected at KDCA.",
                "triggered_at": "2026-05-19T14:00:00",
                "critic": {"confidence": 0.91, "pattern": "CASCADE_FAILURE", "escalate": True, "reasoning": "..."},
            }
        ],
        "summary": {
            "total_events_30m": 25,
            "critical_alerts": 1,
            "warning_alerts": 0,
            "escalation_required": True,
        },
    }
    msg = _format_planner_message("What is happening at KDCA?", ctx)

    assert "KDCA" in msg
    assert "DISCONNECTED" in msg
    assert "ALERT_ISMC_LINK_DEGRADED" in msg
    assert "CASCADE_FAILURE" in msg
    assert "600" in msg  # RVR value
    assert "[ESCALATE]" in msg
    assert "ESCALATION REQUIRED: True" in msg
