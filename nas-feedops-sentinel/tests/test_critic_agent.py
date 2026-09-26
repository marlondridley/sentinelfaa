"""
Tests for CriticAgent.

The LLM client is always controlled explicitly -- patched to None or to a
mock -- so no test reaches a real provider, even when the suite runs in the
app container where .env supplies LITELLM_API_KEY.
Tests verify:
  - Degraded path when API key is absent
  - Degraded path when API call raises
  - Happy path with a mocked tool-use response
  - Context builder with an empty DB
  - Context builder with events in DB
  - save_critic_result / round-trip through storage
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import storage
from src.context_router import ContextRouter
from src.critic_agent import CriticAgent, _format_user_message
from src.models import AlertEvent, CriticResult

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db():
    """Each test gets a fresh in-memory database."""
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _sample_alert(**kwargs) -> AlertEvent:
    defaults = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 800 ft at touchdown on runway 01 at KDCA.",
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

async def test_no_api_key_returns_degraded_result(monkeypatch):
    monkeypatch.setattr("src.critic_agent.settings.CRITIC_ENABLED", True)
    monkeypatch.setattr("src.critic_agent.build_llm_client", lambda *a, **k: None)
    agent = CriticAgent()
    alert = _sample_alert()
    result = await agent.critique(alert)

    assert result.alert_id == alert.alert_id
    assert result.confidence is None
    assert result.escalate is False
    assert "no LLM provider" in result.reasoning.lower() or "unavailable" in result.reasoning.lower()


async def test_api_error_returns_degraded_result(monkeypatch):
    # settings is read once at import, so flags are patched on the object
    # rather than through the environment.
    monkeypatch.setattr("src.critic_agent.settings.CRITIC_ENABLED", True)

    mock_llm = MagicMock()
    mock_llm.acall_with_tool = AsyncMock(side_effect=RuntimeError("network timeout"))

    with patch("src.critic_agent.build_llm_client", return_value=mock_llm):
        agent = CriticAgent()
        alert = _sample_alert()
        result = await agent.critique(alert)

    assert result.confidence is None
    assert "network timeout" in result.reasoning


async def test_critic_disabled_via_env(monkeypatch):
    # critic_agent reads the settings object it imported, so the flag is
    # patched there. (This used to patch src.critic_agent.config and reload
    # src.config -- the module has no `config` attribute any more, and the
    # reload replaced the settings singleton for every later test.)
    monkeypatch.setattr("src.critic_agent.settings.CRITIC_ENABLED", False)

    agent = CriticAgent()
    alert = _sample_alert()
    result = await agent.critique(alert)

    assert result.confidence is None
    assert "CRITIC_ENABLED" in result.reasoning


# ── Happy-path test ───────────────────────────────────────────────────────────

async def test_happy_path_returns_critic_result(monkeypatch):
    monkeypatch.setattr("src.critic_agent.settings.CRITIC_ENABLED", True)

    mock_response = _mock_tool_response(
        confidence=0.91,
        reasoning="ISMC also degraded; multi-service impact suggests cascade.",
        citations=["evt-aaa", "evt-bbb"],
        pattern="CASCADE_FAILURE",
        escalate=True,
    )

    mock_llm = MagicMock()
    mock_llm.acall_with_tool = AsyncMock(return_value=mock_response)

    with patch("src.critic_agent.build_llm_client", return_value=mock_llm):
        agent = CriticAgent()
        alert = _sample_alert()
        result = await agent.critique(alert)

    assert result.alert_id == alert.alert_id
    assert abs(result.confidence - 0.91) < 1e-6
    assert result.pattern == "CASCADE_FAILURE"
    assert result.escalate is True
    assert "cascade" in result.reasoning.lower()
    assert "evt-aaa" in result.citations


# ── Output bounding ───────────────────────────────────────────────────────────
#
# Critic output is recorded in critic_results and shown on the staged action a
# human approves, so what the model returns must be bounded before it is
# stored. Each case below was stored verbatim before the output guardrail was
# wired into CriticAgent.

async def _critique_with_model_output(monkeypatch, **overrides) -> CriticResult:
    monkeypatch.setattr("src.critic_agent.settings.CRITIC_ENABLED", True)
    mock_llm = MagicMock()
    mock_llm.acall_with_tool = AsyncMock(return_value={**_mock_tool_response(), **overrides})
    with patch("src.critic_agent.build_llm_client", return_value=mock_llm):
        return await CriticAgent().critique(_sample_alert())


@pytest.mark.parametrize("model_value", ["false", "False", "0", "no"])
async def test_string_false_escalate_is_recorded_as_false(monkeypatch, model_value):
    """bool("false") is True: the model's "do not escalate" must not be inverted."""
    result = await _critique_with_model_output(monkeypatch, escalate=model_value)
    assert result.escalate is False
    assert result.confidence is not None, "a well-formed answer must not be rejected"


async def test_out_of_bounds_confidence_is_rejected(monkeypatch):
    result = await _critique_with_model_output(monkeypatch, confidence=7.3)
    assert result.confidence is None
    assert "rejected by guardrail" in result.reasoning


async def test_invented_pattern_is_rejected(monkeypatch):
    result = await _critique_with_model_output(monkeypatch, pattern="MADE_UP_PATTERN")
    assert result.confidence is None
    assert result.pattern == "UNKNOWN"
    assert "rejected by guardrail" in result.reasoning


# ── Context-builder tests ─────────────────────────────────────────────────────

async def test_build_context_empty_db():
    alert = _sample_alert()
    router = ContextRouter()
    ctx = await router.build_alert_context(airport_code=alert.airport_code)

    assert ctx["total_recent_events"] == 0
    assert ctx["service_summary"] == {}
    assert ctx["recent_alerts"] == []
    assert ctx["window_minutes"] == 30


async def test_build_context_with_events(monkeypatch):
    from src.models import NASEvent
    from src.storage import save_alert, save_event

    now = datetime.now(timezone.utc)
    for i in range(3):
        await save_event(NASEvent(
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
    await save_alert(alert)

    router = ContextRouter()
    ctx = await router.build_alert_context(airport_code=alert.airport_code)

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

async def test_save_and_retrieve_critic_result():
    alert = _sample_alert()
    # critic_results.alert_id is a foreign key to alert_events.
    await storage.save_alert(alert)
    result = CriticResult(
        alert_id=alert.alert_id,
        confidence=0.75,
        reasoning="Solid evidence, isolated fault.",
        citations=["id-x", "id-y"],
        pattern="ISOLATED_FAULT",
        escalate=True,
    )
    await storage.save_critic_result(result)

    rows = await storage.get_recent_critic_results(limit=5)
    assert len(rows) == 1
    row = rows[0]

    assert row["alert_id"] == alert.alert_id
    assert abs(row["confidence"] - 0.75) < 1e-6
    assert row["pattern"] == "ISOLATED_FAULT"
    assert row["escalate"] is True
    import json
    assert json.loads(row["citations"]) == ["id-x", "id-y"]


def test_exhausted_daily_quota_is_not_retried():
    """A per-day quota 429 will not clear for hours; retrying it drains other keys."""
    from src.llm_client import LLMPermanentError, LLMTransientError, classify

    quota = Exception('RateLimitError: geminiException - {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}')
    assert isinstance(classify(quota), LLMPermanentError)
    burst = type("RateLimitError", (Exception,), {"status_code": 429})("Too many requests, slow down")
    assert isinstance(classify(burst), LLMTransientError)


# ── Schema repair in the shared LLM client ───────────────────────────────────

class _FakeCompletion:
    """Stands in for litellm.acompletion, returning scripted JSON replies."""

    def __init__(self, *replies):
        import json as _json
        self.replies = [_json.dumps(r) for r in replies]
        self.calls = []
        self.kwargs = []

    async def __call__(self, **kwargs):
        from types import SimpleNamespace
        self.calls.append(kwargs["messages"])
        self.kwargs.append(kwargs)
        content = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


_TOOL = {"name": "submit_critique", "input_schema": {"type": "object", "required": ["pattern", "escalate", "confidence"]}}


async def test_missing_required_keys_get_exactly_one_repair_request(monkeypatch):
    import src.llm_client as llm

    fake = _FakeCompletion({"confidence": 0.8}, {"confidence": 0.8, "pattern": "TRANSIENT", "escalate": False})
    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    out = await llm.LiteLLMClient(model="openai/primary-model").acall_with_tool("sys", _TOOL, "user", role="critic")
    assert out == {"confidence": 0.8, "pattern": "TRANSIENT", "escalate": False}
    assert len(fake.calls) == 2
    assert "missing required keys: pattern, escalate" in fake.calls[1][-1]["content"]
    assert "pattern, escalate, confidence" in fake.calls[0][0]["content"], "required keys named up front"


async def test_complete_reply_needs_no_repair_and_bad_repair_is_not_retried(monkeypatch):
    import src.llm_client as llm

    complete = _FakeCompletion({"confidence": 0.8, "pattern": "TRANSIENT", "escalate": False})
    monkeypatch.setattr(llm.litellm, "acompletion", complete)
    await llm.LiteLLMClient(model="openai/primary-model").acall_with_tool("sys", _TOOL, "user", role="critic")
    assert len(complete.calls) == 1

    stubborn = _FakeCompletion({"confidence": 0.8})
    monkeypatch.setattr(llm.litellm, "acompletion", stubborn)
    out = await llm.LiteLLMClient(model="openai/primary-model").acall_with_tool("sys", _TOOL, "user", role="critic")
    assert out == {"confidence": 0.8}, "returned as-is for the caller's guardrail to reject"
    assert len(stubborn.calls) == 2


# ── The client reaches models only through the LiteLLM proxy ─────────────────

async def test_every_call_goes_to_the_configured_proxy_as_an_alias(monkeypatch):
    import src.llm_client as llm

    fake = _FakeCompletion({"confidence": 0.8, "pattern": "TRANSIENT", "escalate": False})
    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    # The client's own settings object: other tests reload src.config.
    monkeypatch.setattr(llm.settings, "LITELLM_PROXY_URL", "http://router:4000/v1")
    monkeypatch.setattr(llm.settings, "LITELLM_API_KEY", "sk-virtual")
    await llm.LiteLLMClient().acall_with_tool("sys", _TOOL, "user", model="primary-model", role="critic")
    sent = fake.kwargs[0]
    assert (sent["api_base"], sent["api_key"], sent["model"]) == ("http://router:4000/v1", "sk-virtual", "openai/primary-model")


@pytest.mark.parametrize("route", ["deepseek/deepseek-chat", "gemini/gemini-3.8-flash", "anthropic/claude"])
async def test_a_provider_route_never_reaches_litellm(monkeypatch, route):
    import src.llm_client as llm

    fake = _FakeCompletion({"confidence": 0.8, "pattern": "TRANSIENT", "escalate": False})
    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    with pytest.raises(llm.LLMPermanentError, match="provider route"):
        await llm.LiteLLMClient().acall_with_tool("sys", _TOOL, "user", model=route, role="critic")
    assert fake.calls == []
