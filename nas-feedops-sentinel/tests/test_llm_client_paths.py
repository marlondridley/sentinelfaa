"""
The LiteLLM client's error taxonomy and completion handling (src/llm_client.py): how a provider failure is
classified, what a malformed or unusable completion becomes, the synchronous entry point, and when no client
is built at all. The gateway itself is replaced.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

import src.llm_client as llm
from src.config import settings


# ── classify(): one provider failure, one verdict ────────────────────────────────────────────────────

def test_an_error_already_in_the_taxonomy_is_returned_as_it_is():
    err = llm.LLMTransientError("already classified")
    assert llm.classify(err) is err


def test_an_authentication_failure_is_permanent():
    exc = litellm.exceptions.AuthenticationError(message="bad key", llm_provider="p", model="m")
    assert isinstance(llm.classify(exc), llm.LLMPermanentError)


def test_an_exhausted_daily_quota_is_permanent_even_though_it_looks_like_a_rate_limit():
    assert isinstance(llm.classify(RuntimeError("429 RequestsPerDay exceeded")), llm.LLMPermanentError)


def test_a_connection_failure_to_the_gateway_is_its_own_class():
    exc = litellm.exceptions.APIConnectionError(message="refused", llm_provider="p", model="m")
    assert isinstance(llm.classify(exc), llm.LLMConnectionError)
    assert isinstance(llm.classify(ConnectionResetError("reset")), llm.LLMConnectionError)


def test_a_timeout_is_transient_and_not_a_connection_failure():
    exc = litellm.exceptions.Timeout(message="slow", model="m", llm_provider="p")
    verdict = llm.classify(exc)
    assert isinstance(verdict, llm.LLMTransientError) and not isinstance(verdict, llm.LLMConnectionError)


@pytest.mark.parametrize("status, expected", [
    (404, llm.LLMPermanentError),          # a client error is never worth repeating
    (418, llm.LLMPermanentError),
    (429, llm.LLMTransientError),          # listed as worth another attempt
    (501, llm.LLMTransientError),          # a server error that is not in the list
    ("502", llm.LLMTransientError),        # the status arrives as text from some transports
])
def test_the_http_status_decides_when_the_exception_type_does_not(status, expected):
    exc = RuntimeError("upstream said no")
    exc.status_code = status
    assert isinstance(llm.classify(exc), expected)


def test_a_status_that_is_not_a_number_is_ignored():
    exc = RuntimeError("odd")
    exc.code = "not-a-number"
    assert isinstance(llm.classify(exc), llm.LLMTransientError)


@pytest.mark.parametrize("exc", [TimeoutError("took too long"), asyncio.TimeoutError(), ValueError("who knows")])
def test_timeouts_and_unknown_failures_get_one_round_of_retries(exc):
    assert isinstance(llm.classify(exc), llm.LLMTransientError)


def test_a_name_litellm_does_not_have_is_dropped_not_fatal():
    assert llm._exc_names("NoSuchErrorInThisVersion", "Timeout") == llm._exc_names("Timeout")


# ── One completion ───────────────────────────────────────────────────────────────────────────────────

def _response(content, finish_reason=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)])


@pytest.fixture
def gateway(monkeypatch):
    """Replace the gateway call; set .reply to a response or an exception."""
    state = SimpleNamespace(reply=None, kwargs=None)

    async def _acompletion(**kwargs):
        state.kwargs = kwargs
        if isinstance(state.reply, BaseException):
            raise state.reply
        return state.reply

    monkeypatch.setattr(llm.litellm, "acompletion", _acompletion)
    return state


async def _complete(client, temperature=0.0, cid=None, role=None):
    return await client._complete([{"role": "user", "content": "hi"}], "openai/primary-model", 100, temperature, cid, role)


async def test_a_completion_returns_the_parsed_object_and_the_raw_text(gateway):
    gateway.reply = _response('```json\n{"a": 1}\n```')
    parsed, raw = await _complete(llm.LiteLLMClient(model="openai/primary-model"), cid="abc", role="critic")

    assert parsed == {"a": 1} and raw.startswith("```json")
    assert gateway.kwargs["extra_headers"] == {"x-litellm-tags": "role:critic", "x-correlation-id": "abc"}


async def test_no_correlation_header_is_sent_without_a_correlation_id(gateway):
    gateway.reply = _response('{"a": 1}')
    await _complete(llm.LiteLLMClient(model="openai/primary-model"), cid="-", role="critic")

    assert gateway.kwargs["extra_headers"] == {"x-litellm-tags": "role:critic"}


async def test_a_call_with_no_role_is_tagged_unregistered_in_the_gateway_spend_record(gateway):
    gateway.reply = _response('{"a": 1}')
    await _complete(llm.LiteLLMClient(model="openai/primary-model"))

    assert gateway.kwargs["extra_headers"] == {"x-litellm-tags": f"role:{llm.UNREGISTERED_ROLE}"}
    assert llm.UNREGISTERED_ROLE == "unregistered"


async def test_a_gateway_failure_is_normalised_before_it_reaches_the_caller(gateway):
    gateway.reply = ConnectionResetError("reset")

    with pytest.raises(llm.LLMConnectionError):
        await _complete(llm.LiteLLMClient(model="openai/primary-model"))


@pytest.mark.parametrize("reply", [SimpleNamespace(choices=[]), SimpleNamespace(), None])
async def test_a_malformed_envelope_is_a_transient_error(gateway, reply):
    gateway.reply = reply
    with pytest.raises(llm.LLMTransientError, match="Unexpected LiteLLM response shape"):
        await _complete(llm.LiteLLMClient(model="openai/primary-model"))


async def test_an_empty_completion_is_transient(gateway):
    gateway.reply = _response("")
    with pytest.raises(llm.LLMTransientError, match="Empty completion"):
        await _complete(llm.LiteLLMClient(model="openai/primary-model"))


@pytest.mark.parametrize("content, wording", [("", "Empty completion content"), ("{\"a\":", "non-JSON content")])
async def test_a_reply_cut_off_at_the_token_limit_is_permanent_so_it_is_not_billed_again(gateway, content, wording):
    gateway.reply = _response(content, finish_reason="length")
    with pytest.raises(llm.LLMPermanentError, match=wording):
        await _complete(llm.LiteLLMClient(model="openai/primary-model"))


async def test_bad_json_from_a_deterministic_call_is_permanent_and_from_a_warm_call_is_worth_one_more_try(gateway):
    gateway.reply = _response("not json at all")
    client = llm.LiteLLMClient(model="openai/primary-model")

    with pytest.raises(llm.LLMPermanentError):
        await _complete(client, temperature=0.0)
    with pytest.raises(llm.LLMTransientError):
        await _complete(client, temperature=0.7)


async def test_json_that_is_not_an_object_is_permanent(gateway):
    gateway.reply = _response("[1, 2, 3]")
    with pytest.raises(llm.LLMPermanentError, match="JSON list, not an object"):
        await _complete(llm.LiteLLMClient(model="openai/primary-model"))


# ── The synchronous entry point ──────────────────────────────────────────────────────────────────────

def test_the_synchronous_call_runs_the_async_one_and_passes_the_protected_values_through(monkeypatch):
    client = llm.LiteLLMClient(model="openai/primary-model")
    seen: dict = {}

    async def _acall(*args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return {"ok": True}

    monkeypatch.setattr(client, "acall_with_tool", _acall)

    result = client.call_with_tool("sys", {"name": "t"}, "user", role="critic", protected_values={"AAL1234"})

    assert result == {"ok": True}
    assert seen["args"][:3] == ("sys", {"name": "t"}, "user") and seen["args"][6] == "critic"
    assert seen["kwargs"] == {"protected_values": {"AAL1234"}}


async def test_the_synchronous_call_refuses_to_block_inside_a_running_loop():
    client = llm.LiteLLMClient(model="openai/primary-model")
    with pytest.raises(llm.LLMPermanentError, match="cannot block inside a running event loop"):
        client.call_with_tool("sys", {"name": "t"}, "user")


# ── When no client is built ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, key, critic, built", [
    ("", "k", True, False),                       # no gateway configured
    ("http://gw", "", False, False),              # no key, and the critic is off
    ("http://gw", "", True, False),               # no key
    ("http://gw", "k", True, True),
])
def test_a_client_is_built_only_when_the_gateway_and_its_key_are_configured(monkeypatch, url, key, critic, built):
    monkeypatch.setattr(settings, "LITELLM_PROXY_URL", url)
    monkeypatch.setattr(settings, "LITELLM_API_KEY", key)
    monkeypatch.setattr(settings, "CRITIC_ENABLED", critic)

    client = llm.build_llm_client(model="openai/primary-model")

    assert (client is not None) is built
