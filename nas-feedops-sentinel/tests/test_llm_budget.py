"""
What the model layer may spend (src/llm_guard.py, src/llm_client.py).

One retry layer (the gateway's): the client retries only a request that never reached it, once.
A circuit breaker per model alias turns a dead gateway into an immediate "Critic unavailable" and a
Prometheus alert, instead of a timeout per alert. A pipeline pass has a token budget.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
import tenacity
import yaml

import src.llm_client as llm
from src import governance, llm_guard, storage
from src.config import settings
from src.demo_scenario import generate_cascade_failure_scenario
from src.llm_guard import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from src.metrics import LLM_CIRCUIT_STATE
from src.models import AlertEvent
from src.pipeline import run_pipeline

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_TOOL = {"name": "submit_critique", "input_schema": {"type": "object", "required": ["pattern", "escalate", "confidence"]}}
_COMPLETE = {"pattern": "TRANSIENT", "escalate": False, "confidence": 0.8}
_VERDICT = {"confidence": 0.8, "reasoning": "Isolated.", "pattern": "ISOLATED_FAULT", "escalate": True, "citations": [],
            "bluf": "Isolated fault; review."}
_ALIAS = "openai/primary-model"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    storage.override_db_path(":memory:")
    governance.reload_policy()
    llm_guard.reset_breakers()
    monkeypatch.setattr(llm.LiteLLMClient._acall_with_retries.retry, "wait", tenacity.wait_none())
    yield
    llm_guard.reset_breakers()
    storage.override_db_path(":memory:")


def _response(reply: dict, tokens: tuple[int, int] = (100, 20)):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))],
        usage=SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
        model="claude-haiku-4-5", _hidden_params={},
    )


class _Scripted:
    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    async def __call__(self, **kwargs):
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _install(monkeypatch, *items) -> _Scripted:
    fake = _Scripted(*items)
    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    return fake


def _client() -> llm.LiteLLMClient:
    return llm.LiteLLMClient(model=_ALIAS)


async def _ask():
    return await _client().acall_with_tool("sys", _TOOL, "user", role="critic")


def _timeout():
    return litellm.Timeout(message="timed out", model="primary-model", llm_provider="openai")


def _unavailable():
    return litellm.InternalServerError(message="gateway down", model="primary-model", llm_provider="openai")


async def _outcomes() -> list[str]:
    return [r["outcome"] for r in await storage.get_model_calls_since(_EPOCH)]


# ── one retry layer ─────────────────────────────────────────────────────────────

async def test_a_timeout_is_not_retried_by_the_client(monkeypatch):
    fake = _install(monkeypatch, _timeout())
    with pytest.raises(llm.LLMTransientError):
        await _ask()
    assert fake.calls == 1, "the gateway already retried and fell back; a second request only doubles the bill"
    (row,) = await storage.get_model_calls_since(_EPOCH)
    assert (row["outcome"], row["attempts"]) == ("timeout", 1)


@pytest.mark.parametrize("make", [_unavailable, lambda: litellm.RateLimitError(
    message="slow down", model="primary-model", llm_provider="openai")])
async def test_a_failure_the_gateway_answered_is_not_retried(monkeypatch, make):
    fake = _install(monkeypatch, make())
    with pytest.raises(llm.LLMTransientError):
        await _ask()
    assert fake.calls == 1


async def test_a_reply_that_was_billed_but_empty_is_not_retried(monkeypatch):
    empty = _response({})
    empty.choices[0].message.content = ""
    fake = _install(monkeypatch, empty)
    with pytest.raises(llm.LLMTransientError):
        await _ask()
    assert fake.calls == 1


async def test_a_request_that_never_reached_the_gateway_is_retried_once(monkeypatch):
    refused = litellm.APIConnectionError(message="connection refused", model="primary-model", llm_provider="openai")
    fake = _install(monkeypatch, refused, _response(_COMPLETE))
    assert await _ask() == _COMPLETE
    assert fake.calls == 2

    fake = _install(monkeypatch, refused)
    with pytest.raises(llm.LLMConnectionError):
        await _ask()
    assert fake.calls == 2, "one retry, not three"


async def test_the_connection_retry_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "LLM_CONNECT_ATTEMPTS", 1)
    fake = _install(monkeypatch, ConnectionError("refused"))
    with pytest.raises(llm.LLMConnectionError):
        await _ask()
    assert fake.calls == 1


# ── the breaker ─────────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _breaker() -> tuple[CircuitBreaker, _Clock]:
    clock = _Clock()
    return CircuitBreaker("openai/test-model", clock=clock), clock


def test_the_breaker_opens_after_consecutive_unavailable_outcomes_only(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BREAKER_THRESHOLD", 3)
    breaker, _ = _breaker()
    for outcome in ("timeout", "provider_error", "ok", "timeout", "rate_limited"):
        breaker.record(outcome)
    assert breaker.state == CLOSED, "a success between failures resets the count"
    for outcome in ("invalid_output", "bad_request", "invalid_output"):
        breaker.record(outcome)
    assert breaker.state == CLOSED, "an unusable reply or a rejected request is not an outage"
    for outcome in ("provider_error", "timeout", "auth_error"):
        breaker.record(outcome)
    assert breaker.state == OPEN and not breaker.allow()
    assert LLM_CIRCUIT_STATE.labels(model="openai/test-model")._value.get() == 2


def test_after_the_cooldown_one_probe_decides(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BREAKER_THRESHOLD", 2)
    monkeypatch.setattr(settings, "LLM_BREAKER_COOLDOWN_SECONDS", 60.0)
    breaker, clock = _breaker()
    breaker.record("timeout")
    breaker.record("timeout")
    clock.now += 59
    assert not breaker.allow()

    clock.now += 2
    assert breaker.allow() and breaker.state == HALF_OPEN
    assert not breaker.allow(), "only one probe is let through"
    breaker.record("timeout")
    assert breaker.state == OPEN and not breaker.allow(), "a failed probe opens it again for a full cooldown"

    clock.now += 61
    assert breaker.allow()
    breaker.record("ok")
    assert breaker.state == CLOSED and breaker.allow() and breaker.allow()


def test_a_probe_that_was_not_sent_frees_the_slot(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BREAKER_THRESHOLD", 1)
    breaker, clock = _breaker()
    breaker.record("timeout")
    clock.now += 1000
    assert breaker.allow()
    breaker.release()
    assert breaker.allow()


async def test_repeated_gateway_failures_open_the_circuit_and_stop_sending(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BREAKER_THRESHOLD", 3)
    fake = _install(monkeypatch, _unavailable())
    for _ in range(3):
        with pytest.raises(llm.LLMTransientError):
            await _ask()
    assert fake.calls == 3

    with pytest.raises(llm.LLMCircuitOpen):
        await _ask()
    assert fake.calls == 3, "nothing was sent while the circuit is open"
    assert await _outcomes() == ["provider_error"] * 3 + ["circuit_open"]
    assert LLM_CIRCUIT_STATE.labels(model=_ALIAS)._value.get() == 2
    refused = (await storage.get_model_calls_since(_EPOCH))[-1]
    assert refused["attempts"] == 0 and refused["cost_usd"] == 0.0


async def test_the_circuit_closes_again_when_the_probe_is_answered(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BREAKER_THRESHOLD", 1)
    monkeypatch.setattr(settings, "LLM_BREAKER_COOLDOWN_SECONDS", 0.0)
    fake = _install(monkeypatch, _unavailable(), _response(_COMPLETE))
    with pytest.raises(llm.LLMTransientError):
        await _ask()
    assert await _ask() == _COMPLETE, "the cooldown is over: this call is the probe"
    assert llm_guard.breaker_for(_ALIAS).state == CLOSED
    assert fake.calls == 2


# ── the Critic ─────────────────────────────────────────────────────────────────

def _alert() -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="APDS_RVR",
                      description="Low RVR 800 ft at touchdown on runway 01 at KDCA.")


async def test_the_critic_reports_itself_unavailable_at_once_when_the_circuit_is_open(monkeypatch):
    import src.critic_agent as critic_agent

    monkeypatch.setattr(critic_agent.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: _client())
    monkeypatch.setattr(settings, "LLM_BREAKER_THRESHOLD", 1)
    fake = _install(monkeypatch, _unavailable())
    agent = critic_agent.CriticAgent()
    first = await agent.critique(_alert())
    assert first.confidence is None and "error" in first.reasoning.lower()
    assert fake.calls == 1

    context_reads = 0
    real = critic_agent._router.build_alert_context

    async def counting(*a, **k):
        nonlocal context_reads
        context_reads += 1
        return await real(*a, **k)

    monkeypatch.setattr(critic_agent._router, "build_alert_context", counting)
    second = await agent.critique(_alert())

    assert second.confidence is None and "unavailable" in second.reasoning.lower() and "circuit" in second.reasoning.lower()
    assert fake.calls == 1 and context_reads == 0, "no database read and no request for the next alert"


async def test_every_critical_alert_is_still_staged_while_the_circuit_is_open(monkeypatch):
    import src.critic_agent as critic_agent

    monkeypatch.setattr(settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(settings, "INVESTIGATIONS_ENABLED", False)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: _client())
    llm_guard.breaker_for(_ALIAS).state = OPEN
    llm_guard.breaker_for(_ALIAS).opened_at = 10 ** 12
    fake = _install(monkeypatch, _response(_VERDICT))

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    assert fake.calls == 0 and summary["staged_count"] >= 1


# ── the alert ──────────────────────────────────────────────────────────────────

def test_prometheus_alerts_when_a_circuit_is_open_or_a_budget_is_spent():
    rules = yaml.safe_load((Path(__file__).resolve().parent.parent / "monitoring" / "alert-rules.yml").read_text(encoding="utf-8"))
    by_name = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    assert "nas_llm_circuit_state" in by_name["ModelCircuitOpen"]["expr"]
    assert "nas_llm_budget_refusals_total" in by_name["LLMPassBudgetExhausted"]["expr"]


def test_prometheus_warns_when_the_application_key_has_used_80_percent_of_its_gateway_budget():
    """The gateway's own budget metrics, as the pinned LiteLLM image names and labels them (litellm 1.101.0)."""
    monitoring = Path(__file__).resolve().parent.parent / "monitoring"
    rules = yaml.safe_load((monitoring / "alert-rules.yml").read_text(encoding="utf-8"))
    (group,) = [g for g in rules["groups"] if any(r.get("alert") == "LLMBudgetNearlyUsed" for r in g["rules"])]
    rule = next(r for r in group["rules"] if r.get("alert") == "LLMBudgetNearlyUsed")
    expr = " ".join(rule["expr"].split())

    assert group["name"] == "nas-sentinel-ai"
    assert 'litellm_remaining_api_key_budget_metric{api_key_alias="sentinel-app"}' in expr
    assert 'litellm_api_key_max_budget_metric{api_key_alias="sentinel-app"} > 0' in expr, "no division by a zero budget"
    assert expr.endswith(">= 0.8")
    assert rule["for"] == "15m" and rule["labels"] == {"severity": "warning"}
    assert "80%" in rule["annotations"]["summary"]
    description = rule["annotations"]["description"]
    assert "LITELLM_APP_BUDGET_USD" in description and "30-day reset" in description
    assert "falls back to rules" in description and "still staged" in description
    jobs = {j["job_name"]: j for j in yaml.safe_load((monitoring / "prometheus.yml").read_text(encoding="utf-8"))["scrape_configs"]}
    assert jobs["nas-litellm"]["static_configs"] == [{"targets": ["nas-litellm:4000"], "labels": {"service": "litellm", "tracon": "PCT"}}]
    assert jobs["nas-litellm"]["metrics_path"] == "/metrics/"
    assert not {"authorization", "basic_auth", "bearer_token", "bearer_token_file"} & set(jobs["nas-litellm"]), \
        "no key in prometheus.yml: the gateway's /metrics takes none"


# ── the gateway's spend record ───────────────────────────────────────────────────

async def test_every_request_to_the_gateway_is_tagged_with_the_calling_role_the_repair_included(monkeypatch):
    sent: list[dict] = []
    replies = [_response({"pattern": "TRANSIENT"}), _response(_COMPLETE)]       # the first lacks keys: one repair

    async def gateway(**kwargs):
        sent.append(kwargs["extra_headers"])
        return replies[len(sent) - 1]

    monkeypatch.setattr(llm.litellm, "acompletion", gateway)
    assert await _ask() == _COMPLETE

    assert [h["x-litellm-tags"] for h in sent] == ["role:critic", "role:critic"]
    (row,) = await storage.get_model_calls_since(_EPOCH)
    assert (row["role"], row["attempts"]) == ("critic", 2), "one model_calls row, two gateway requests"


# ── the per-pass token budget ─────────────────────────────────────────────────────

async def test_a_call_past_the_budget_is_refused_and_not_sent(monkeypatch):
    fake = _install(monkeypatch, _response(_COMPLETE))
    with llm_guard.pass_budget(limit=1) as budget:
        with pytest.raises(llm.LLMBudgetExceeded):
            await _ask()
    assert fake.calls == 0 and budget.spent == 0 and budget.reserved == 0
    (row,) = await storage.get_model_calls_since(_EPOCH)
    assert (row["outcome"], row["attempts"]) == ("budget_exhausted", 0)


async def test_the_budget_is_settled_with_real_usage(monkeypatch):
    fake = _install(monkeypatch, _response(_COMPLETE, tokens=(300, 40)))
    with llm_guard.pass_budget(limit=100_000) as budget:
        await _ask()
        await _ask()
    assert fake.calls == 2 and budget.spent == 680 and budget.reserved == 0


async def test_concurrent_calls_cannot_overshoot_the_budget_together(monkeypatch):
    """The Critic's calls run together: each holds its share while it runs, so five cannot all pass a check of zero spent."""
    estimate = llm._estimate_tokens("sys", _TOOL, "user", 768)

    async def slow(**kwargs):
        await asyncio.sleep(0.05)
        return _response(_COMPLETE)

    monkeypatch.setattr(llm.litellm, "acompletion", slow)
    with llm_guard.pass_budget(limit=estimate * 2 + 10) as budget:
        results = await asyncio.gather(*(_ask() for _ in range(5)), return_exceptions=True)

    assert sum(1 for r in results if r == _COMPLETE) == 2
    assert sum(isinstance(r, llm.LLMBudgetExceeded) for r in results) == 3
    assert budget.spent == 240 and budget.reserved == 0


async def test_a_call_that_timed_out_is_charged_its_estimate_because_it_may_have_been_billed(monkeypatch):
    _install(monkeypatch, _timeout())
    estimate = llm._estimate_tokens("sys", _TOOL, "user", 768)
    with llm_guard.pass_budget(limit=100_000) as budget:
        with pytest.raises(llm.LLMTransientError):
            await _ask()
    assert budget.spent == estimate


async def test_a_refused_call_does_not_spend_budget(monkeypatch):
    llm_guard.breaker_for(_ALIAS).state = OPEN
    llm_guard.breaker_for(_ALIAS).opened_at = 10 ** 12
    _install(monkeypatch, _response(_COMPLETE))
    with llm_guard.pass_budget(limit=100_000) as budget:
        with pytest.raises(llm.LLMCircuitOpen):
            await _ask()
    assert budget.spent == 0 and budget.reserved == 0


async def test_no_budget_outside_a_pass_means_no_cap(monkeypatch):
    fake = _install(monkeypatch, _response(_COMPLETE))
    assert llm_guard.current_budget() is None
    assert await _ask() == _COMPLETE and fake.calls == 1


async def test_a_pipeline_pass_that_has_spent_its_budget_still_stages_every_alert(monkeypatch):
    import src.critic_agent as critic_agent

    monkeypatch.setattr(settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(settings, "INVESTIGATIONS_ENABLED", False)
    monkeypatch.setattr(settings, "LLM_TOKEN_BUDGET_PER_PASS", 1)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: _client())
    fake = _install(monkeypatch, _response(_VERDICT))

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    assert fake.calls == 0, "the budget allowed nothing, so nothing was sent"
    assert summary["staged_count"] >= 1 and summary["llm_tokens_spent"] == 0
    outcomes = await _outcomes()
    assert outcomes and set(outcomes) == {"budget_exhausted"}


async def test_the_pass_budget_covers_the_critic_calls_together(monkeypatch):
    import src.critic_agent as critic_agent

    monkeypatch.setattr(settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(settings, "INVESTIGATIONS_ENABLED", False)
    monkeypatch.setattr(settings, "MAX_CRITIC_PER_PASS", 50)
    monkeypatch.setattr(settings, "LLM_TOKEN_BUDGET_PER_PASS", 2_500)
    monkeypatch.setattr(llm, "_estimate_tokens", lambda *a, **k: 1_000)   # two calls fit; the rest are refused
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: _client())

    async def slow(**kwargs):
        await asyncio.sleep(0.3)        # long enough that every Critic call has reserved before the first ends
        return _response(_VERDICT, tokens=(50, 10))

    monkeypatch.setattr(llm.litellm, "acompletion", slow)

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    outcomes = await _outcomes()
    assert outcomes.count("ok") == 2 and outcomes.count("budget_exhausted") >= 1, outcomes
    assert summary["llm_tokens_spent"] == 120
