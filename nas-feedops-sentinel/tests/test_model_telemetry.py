"""
One model_calls record per logical model call (src/model_telemetry.py),
written by the client however the call ends. Driven through the real
LiteLLMClient with litellm.acompletion replaced, and read back from Postgres.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import tenacity

import src.llm_client as llm
from src import governance, storage
from src.model_telemetry import classify_outcome

_TOOL = {"name": "submit_critique", "input_schema": {"type": "object", "required": ["pattern", "escalate", "confidence"]}}
_COMPLETE = {"pattern": "TRANSIENT", "escalate": False, "confidence": 0.8}
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    storage.override_db_path(":memory:")
    governance.reload_policy()
    # Keep the real retry count; skip its backoff so transient failures are fast.
    monkeypatch.setattr(llm.LiteLLMClient._acall_with_retries.retry, "wait", tenacity.wait_none())
    yield


def _response(reply: dict, *, prompt=100, completion=20, model="deepseek-chat", cost: str | None = "0.0004"):
    hidden = {"additional_headers": {"llm_provider-x-litellm-response-cost": cost}} if cost is not None else {}
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        model=model,
        _hidden_params=hidden,
    )


class _Scripted:
    """Stands in for litellm.acompletion: returns or raises each scripted item in turn."""

    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    async def __call__(self, **kwargs):
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


async def _call(monkeypatch, *items, role="critic"):
    fake = _Scripted(*items)
    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    client = llm.LiteLLMClient(model="openai/primary-model", api_key="k")
    try:
        return await client.acall_with_tool("sys", _TOOL, "user", role=role), fake
    except llm.LLMError as exc:
        return exc, fake


async def _only_row() -> dict:
    rows = await storage.get_model_calls_since(_EPOCH)
    assert len(rows) == 1, rows
    return rows[0]


async def test_an_answered_call_records_outcome_tokens_cost_model_and_policy(monkeypatch):
    result, _ = await _call(monkeypatch, _response(_COMPLETE))
    assert result == _COMPLETE

    row = await _only_row()
    assert (row["role"], row["outcome"], row["attempts"], row["repaired"]) == ("critic", "ok", 1, False)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (100, 20)
    assert row["cost_usd"] == pytest.approx(0.0004)
    assert row["served_model"] == "deepseek-chat"
    assert (row["policy_action"], row["policy_rule"]) == ("audit", "advisory-roles")
    assert row["latency_ms"] >= 0 and row["error_type"] is None


async def test_the_repair_request_is_part_of_the_same_call(monkeypatch):
    await _call(monkeypatch, _response({"confidence": 0.8}), _response(_COMPLETE, prompt=150, completion=25))
    row = await _only_row()
    assert (row["outcome"], row["attempts"], row["repaired"]) == ("repaired", 2, True)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (250, 45)
    assert row["cost_usd"] == pytest.approx(0.0008)


async def test_an_answer_still_incomplete_after_the_repair_is_recorded_as_such(monkeypatch):
    result, _ = await _call(monkeypatch, _response({"confidence": 0.8}))
    assert result == {"confidence": 0.8}, "returned for the caller's guardrail to judge"
    assert (await _only_row())["outcome"] == "incomplete"


async def test_a_timeout_is_retried_then_recorded_once_with_every_attempt(monkeypatch):
    import litellm

    timeout = litellm.Timeout(message="timed out", model="primary-model", llm_provider="openai")
    result, fake = await _call(monkeypatch, timeout, timeout, timeout)
    assert isinstance(result, llm.LLMTransientError)
    assert fake.calls == 3
    row = await _only_row()
    assert (row["outcome"], row["attempts"], row["error_type"]) == ("timeout", 3, "Timeout")
    assert row["cost_usd"] is None, "sent, nothing came back: unknown, not zero"


async def test_unknown_cost_stays_unknown(monkeypatch):
    await _call(monkeypatch, _response(_COMPLETE, cost=None, model="model-with-no-price"))
    assert (await _only_row())["cost_usd"] is None


async def test_non_json_output_is_invalid_output(monkeypatch):
    garbage = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
        usage=SimpleNamespace(prompt_tokens=90, completion_tokens=3), model="deepseek-chat", _hidden_params={},
    )
    result, _ = await _call(monkeypatch, garbage)
    assert isinstance(result, llm.LLMPermanentError)
    row = await _only_row()
    assert (row["outcome"], row["prompt_tokens"]) == ("invalid_output", 90), "tokens were still spent"


async def test_a_lost_measurement_never_fails_the_call(monkeypatch):
    async def storage_down(row):
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(storage, "record_model_call", storage_down)
    result, _ = await _call(monkeypatch, _response(_COMPLETE))
    assert result == _COMPLETE


def _cause(exc_type_name: str, status: int | None = None, text: str = "boom") -> llm.LLMError:
    cause = type(exc_type_name, (Exception,), {})(text)
    if status is not None:
        cause.status_code = status
    err = llm.LLMTransientError(text)
    err.__cause__ = cause
    return err


@pytest.mark.parametrize("error, outcome", [
    (llm.LLMPolicyDenied("refused"), "policy_denied"),
    (llm.LLMPermanentError("Model returned non-JSON content: x"), "invalid_output"),
    (llm.LLMTransientError("Empty completion content."), "invalid_output"),
    (_cause("Timeout"), "timeout"),
    (_cause("RateLimitError", 429), "rate_limited"),
    (_cause("RateLimitError", 429, "GenerateRequestsPerDayPerProjectPerModel exceeded"), "quota_exhausted"),
    (_cause("AuthenticationError", 401), "auth_error"),
    (_cause("PermissionDeniedError", 403), "auth_error"),
    (_cause("BadRequestError", 400), "bad_request"),
    (_cause("ContextWindowExceededError"), "bad_request"),
    (_cause("ServiceUnavailableError", 503), "provider_error"),
    (_cause("APIConnectionError"), "provider_error"),
])
def test_failures_are_classified_into_outcome_classes(error, outcome):
    assert classify_outcome(error) == outcome
