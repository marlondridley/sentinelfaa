"""
The model-use policy (policies/model-use.yaml), enforced with the Microsoft
Agent Governance Toolkit before every model call (src/governance.py).

These tests pin the policy's decisions: changing what a model may receive is
a change to that file, and it should fail here until the table below agrees.
"""
from __future__ import annotations

import json
import uuid

import pytest

from src import governance, storage
from src.config import settings

_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    governance.reload_policy()
    yield
    governance.reload_policy()


def _decide(**overrides) -> governance.GovernanceDecision:
    call = dict(role="critic", model="openai/primary-model", prompt="Assess this alert.",
                max_tokens=1000, redacted=True)
    call.update(overrides)
    return governance.evaluate_model_call(**call)


def test_the_policy_file_is_a_valid_toolkit_policy_that_denies_by_default():
    from agent_os.policies import PolicyDocument

    document = PolicyDocument.from_yaml(settings.MODEL_POLICY_PATH)
    assert document.name == "sentinel-model-use"
    assert document.defaults.action.value == "deny"


@pytest.mark.parametrize("role", [
    "critic", "planner", "watch_officer", "feed_diagnostician", "correlation_analyst", "red_team_critic",
])
def test_registered_advisory_roles_are_allowed_and_audited(role):
    decision = _decide(role=role)
    assert (decision.allowed, decision.action, decision.rule) == (True, "audit", "advisory-roles")


@pytest.mark.parametrize("overrides, rule", [
    ({"role": None}, None),                                   # unregistered: nothing allows it
    ({"role": "autonomous_executor"}, None),
    ({"model": "gemini/gemini-3.8-flash"}, "gateway-aliases-only"),
    ({"model": "openai/some-other-alias"}, "gateway-aliases-only"),
    ({"redacted": False}, "prompts-must-be-redacted"),
    ({"prompt": "x" * 120_001}, "prompt-size-ceiling"),
    ({"max_tokens": 4096}, "output-budget-ceiling"),
    ({"prompt": f"context ... api_key={_KEY}"}, "no-credentials-in-prompts"),
])
def test_refusals_name_the_rule_that_refused(overrides, rule):
    decision = _decide(**overrides)
    assert decision.allowed is False
    assert decision.rule == rule


def test_a_credential_is_blocked_even_for_a_registered_role_under_every_limit():
    decision = _decide(prompt="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    assert (decision.allowed, decision.action) == (False, "block")


def test_sentinel_payload_shapes_are_not_mistaken_for_credentials():
    """Hashes, UUIDs, hex addresses and bitmasks fill investigation payloads."""
    prompt = json.dumps({
        "record_sha256": "3f9a0c4be2d1a7f6c5b4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2",
        "event_id": str(uuid.uuid4()),
        "acAddress": "a7cc0b",
        "status": "0000000000000000000000000000000000000000000000000000140000000fff",
        "max_tokens": 1200,
        "token_count": 5,
    })
    assert _decide(prompt=prompt).allowed


def test_every_context_field_is_present_and_never_none():
    """The toolkit treats a missing field as no match, so a deny rule on it would never fire."""
    context = governance.build_context(role=None, model=None, prompt="", max_tokens=0, redacted=False)
    assert {"role", "model", "prompt_chars", "max_tokens", "redacted", "credentials_found"} <= set(context)
    assert all(value is not None for value in context.values())


@pytest.mark.parametrize("content", [
    None,                                                     # no file
    "rules: not-a-list\n",                                    # invalid
    "::: not yaml\n",                                         # parses; toolkit loads it as allow-all
    "name: no-defaults\n",                                    # toolkit default is allow
    "name: typo\ndefaults: {action: deny}\nrule: []\n",       # unknown key
    "name: open\ndefaults: {action: allow}\nrules: []\n",     # explicitly allow-by-default
])
def test_a_missing_or_malformed_policy_refuses_every_call(monkeypatch, tmp_path, content):
    path = tmp_path / "model-use.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(settings, "MODEL_POLICY_PATH", path)
    governance.reload_policy()

    decision = _decide()
    assert decision.allowed is False
    assert decision.policy is None
    assert "unavailable" in decision.reason


async def test_a_refused_call_sends_nothing_and_is_recorded_as_costing_nothing(monkeypatch):
    import src.llm_client as llm

    async def must_not_be_called(**kwargs):
        raise AssertionError("a refused call reached the model")

    monkeypatch.setattr(llm.litellm, "acompletion", must_not_be_called)
    client = llm.LiteLLMClient(model="openai/primary-model", api_key="k")

    with pytest.raises(llm.LLMPolicyDenied):
        await client.acall_with_tool("sys", {"name": "t", "input_schema": {}}, "user")   # no role

    [row] = await storage.get_model_calls_since(_long_ago())
    assert (row["role"], row["outcome"], row["attempts"], row["cost_usd"]) == (
        "unregistered", "policy_denied", 0, 0.0)
    assert row["policy_action"] == "deny"


async def test_a_refusal_degrades_the_critic_to_its_rules_like_any_model_failure(monkeypatch, tmp_path):
    import src.llm_client as llm
    from src.critic_agent import CriticAgent
    from src.models import AlertEvent

    deny_all = tmp_path / "deny-all.yaml"
    deny_all.write_text("name: deny-all\ndefaults: {action: deny}\nrules: []\n", encoding="utf-8")
    monkeypatch.setattr(settings, "MODEL_POLICY_PATH", deny_all)
    governance.reload_policy()

    agent = CriticAgent()
    agent._llm = llm.LiteLLMClient(model="openai/primary-model", api_key="k")
    result = await agent.critique(AlertEvent(
        alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA",
        service_name="APDS_RVR", description="Low RVR 800 ft at touchdown on runway 01 at KDCA.",
    ))

    assert result.confidence is None, "degraded, as for any model failure"
    assert "model-use policy" in result.reasoning


def _long_ago():
    from datetime import datetime, timezone

    return datetime(2000, 1, 1, tzinfo=timezone.utc)
