"""
LiteLLM-backed client. All agent model calls route through the proxy.

Uses litellm.acompletion with api_base=settings.LITELLM_PROXY_URL and
api_key=settings.LITELLM_API_KEY.

Error taxonomy
--------------
LLMError            -- base, kept as the repo's single normalized exception type
  LLMTransientError -- worth retrying (timeout, connection reset, 429, 5xx)
  LLMPermanentError -- never retry (401/403 auth, 400 bad request, 404 model
                       not found, context-window overflow, content policy)

Only LLMTransientError is retry-eligible. Callers that catch LLMError keep
working unchanged.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

import litellm
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from opentelemetry.trace import SpanKind

from src import telemetry
from src.agent_tools import ToolCallDenied
from src.agent_tools import authorize as authorize_tool
from src.config import settings
from src.governance import UNREGISTERED_ROLE, evaluate_model_call
from src.logging_config import get_correlation_id, get_logger
from src.model_telemetry import ModelCall, classify_outcome
from src.model_telemetry import record as record_model_call
from src.redaction import redact_text

_log = get_logger(__name__)


# --------------------------------------------------------------------------
# Exception taxonomy
# --------------------------------------------------------------------------
class LLMError(Exception):
    """Raised when the LLM call fails. Base type for all client failures."""

    retriable: bool = False


class LLMTransientError(LLMError):
    """A failure that may succeed on a subsequent attempt."""

    retriable = True


class LLMPermanentError(LLMError):
    """A failure that will never succeed on retry. Fail fast."""

    retriable = False


class LLMPolicyDenied(LLMPermanentError):
    """The model-use policy (src/governance.py) refused the call; nothing was sent."""


def _exc_names(*names: str) -> tuple[type[BaseException], ...]:
    """
    Resolve litellm exception classes by name, tolerating version drift.

    litellm renames/adds exception types between releases, so anything we
    can't resolve is simply dropped rather than blowing up at import time.
    """
    resolved: list[type[BaseException]] = []
    for name in names:
        cls = getattr(litellm.exceptions, name, None) or getattr(litellm, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            resolved.append(cls)
    return tuple(resolved)


_TRANSIENT_EXC = _exc_names(
    "Timeout",
    "APIConnectionError",
    "RateLimitError",
    "InternalServerError",
    "ServiceUnavailableError",
    "APIResponseValidationError",
)

_PERMANENT_EXC = _exc_names(
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "BadRequestError",
    "UnprocessableEntityError",
    "ContextWindowExceededError",
    "ContentPolicyViolationError",
    "UnsupportedParamsError",
)

# Status codes that are worth another attempt.
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def classify(exc: BaseException) -> LLMError:
    """
    Map a provider/transport exception onto the normalized taxonomy.

    Classification order: explicit exception type, then HTTP status code,
    then transport-level errors, then a conservative default.
    """
    if isinstance(exc, LLMError):
        return exc

    detail = f"{type(exc).__name__}: {exc}"

    # An exhausted daily quota comes back as a 429 like any rate limit, but it
    # will not clear for hours. Retrying it only spends the next key's quota:
    # observed live, failing calls became dozens of upstream requests (client
    # retries x proxy retries x deployments) and drained a second free-tier
    # key. Treated as permanent so the caller falls back to rules at once.
    text = str(exc)
    if "PerDay" in text or "exceeded your current quota" in text:
        return LLMPermanentError(detail)

    if _PERMANENT_EXC and isinstance(exc, _PERMANENT_EXC):
        return LLMPermanentError(detail)

    if _TRANSIENT_EXC and isinstance(exc, _TRANSIENT_EXC):
        return LLMTransientError(detail)

    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    if status is not None:
        if status in _TRANSIENT_STATUS:
            return LLMTransientError(detail)
        if 400 <= status < 500:
            return LLMPermanentError(detail)
        if status >= 500:
            return LLMTransientError(detail)

    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return LLMTransientError(detail)

    # Unknown failure: allow one round of retries rather than failing a call
    # that might have been a blip. Everything above narrows this down.
    return LLMTransientError(detail)


class LLMClient(Protocol):
    def call_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
        model: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        ...

    async def acall_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
        model: str | None = None,
        temperature: float = 0.0,
        role: str | None = None,
    ) -> dict[str, Any]:
        ...


class LiteLLMClient:
    """Structured-output client over the LiteLLM proxy."""

    def __init__(
        self,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._model = model or settings.CRITIC_MODEL
        self._api_base = api_base or settings.LITELLM_PROXY_URL
        self._api_key = api_key if api_key is not None else settings.LITELLM_API_KEY

    def call_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
        model: str | None = None,
        temperature: float = 0.0,
        role: str | None = None,
    ) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.acall_with_tool(
                    system_prompt, tool, user_message, max_tokens, model, temperature, role
                )
            )
        raise LLMPermanentError(
            "call_with_tool() cannot block inside a running event loop; "
            "use acall_with_tool() instead."
        )

    async def acall_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
        model: str | None = None,
        temperature: float = 0.0,
        role: str | None = None,
    ) -> dict[str, Any]:
        """
        One governed, measured call.

        The model-use policy is asked first (src/governance.py); a refusal
        raises LLMPolicyDenied and nothing is sent. Every call -- refused,
        failed or answered -- is one OpenTelemetry chat span and one
        model_calls record (src/model_telemetry.py), with its retries and
        the schema repair counted as part of it. `role` names the advisory role calling; a call
        without one is evaluated as unregistered, which the policy refuses.
        """
        target_model = model or self._model

        # Mandatory redaction: every caller of acall_with_tool() goes through
        # this one chokepoint before anything reaches the model host, so a
        # caller can't forget to scrub identifying NAS telemetry (aircraft
        # tail numbers, squawk codes, raw lat/lon, callsigns, ...) out of its
        # prompt. See src/redaction.py for what's covered and why.
        redacted_user_message = redact_text(user_message)

        # The call's OpenTelemetry span (src/telemetry.py): record() sets its
        # attributes, so exceptions are neither recorded nor turned into a
        # status here -- their messages can quote the model's reply.
        with telemetry.tracer().start_as_current_span(
            f"{telemetry.CHAT} {target_model}", kind=SpanKind.CLIENT,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            call = ModelCall(
                role=role or UNREGISTERED_ROLE, model_alias=target_model,
                correlation_id=get_correlation_id(), subject=telemetry.current_subject(),
                max_tokens=max_tokens, temperature=temperature, span=span,
            )
            decision = evaluate_model_call(
                role=role, model=target_model, prompt=f"{system_prompt}\n{redacted_user_message}",
                max_tokens=max_tokens, redacted=True,
            )
            which = "model-use"
            if decision.allowed:
                # The tool boundary: a role may hand a model only its own
                # output tool (policies/tool-use.yaml).
                try:
                    authorize_tool(str(tool.get("name") or ""), role=role)
                except ToolCallDenied as exc:
                    decision, which = exc.decision, "tool-use"
            call.policy_action, call.policy_rule, call.policy_name = decision.action, decision.rule, decision.policy
            if not decision.allowed:
                refusal = LLMPolicyDenied(
                    f"Refused by the {which} policy (rule: {decision.rule or 'default'}): {decision.reason}"
                )
                await record_model_call(call, "policy_denied", refusal)
                raise refusal

            try:
                result = await self._acall_with_retries(
                    system_prompt, tool, redacted_user_message, max_tokens, target_model, temperature, call,
                )
            except Exception as exc:
                await record_model_call(call, classify_outcome(exc), exc)
                raise
            await record_model_call(
                call, "incomplete" if call.incomplete else "repaired" if call.repaired else "ok",
            )
            return result

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(LLMTransientError),
        reraise=True,
    )
    async def _acall_with_retries(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        redacted_user_message: str,
        max_tokens: int,
        target_model: str,
        temperature: float,
        call: ModelCall,
    ) -> dict[str, Any]:
        schema = tool.get("input_schema", {})
        required = list(schema.get("required") or [])
        schema_text = json.dumps(schema, indent=2)
        # JSON mode guarantees JSON, not this schema -- DeepSeek in particular
        # returned objects missing required keys under a large context. Naming
        # the required keys outright makes that less likely; the one repair
        # request below handles the rest.
        augmented_system = (
            f"{system_prompt}\n\n"
            f"You MUST respond with a single JSON object matching this schema:\n"
            f"{schema_text}\n"
            + (f"The object MUST contain every one of these keys: {', '.join(required)}.\n" if required else "")
            + "Do not include any text outside the JSON object."
        )
        cid = get_correlation_id()

        _log.info(
            "litellm_completion_start",
            extra={"model": target_model, "tool": tool.get("name"), "role": call.role},
        )

        messages = [
            {"role": "system", "content": augmented_system},
            {"role": "user", "content": redacted_user_message},
        ]
        call.repaired = call.incomplete = False
        result, content = await self._complete(messages, target_model, max_tokens, temperature, cid, call)

        # One repair request, never more: if the object parsed but lacks
        # required keys, show the model its answer and name what is missing.
        # Whatever comes back still goes through the caller's guardrail.
        missing = [k for k in required if k not in result]
        if missing:
            _log.warning("litellm_schema_repair", extra={"model": target_model, "missing": missing})
            call.repaired = True
            messages += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    f"Your JSON object is missing required keys: {', '.join(missing)}. "
                    "Return the complete object with every required key, and nothing else."
                )},
            ]
            result, _ = await self._complete(messages, target_model, max_tokens, temperature, cid, call)
            call.incomplete = any(k not in result for k in required)
        return result

    async def _complete(
        self,
        messages: list[dict[str, str]],
        target_model: str,
        max_tokens: int,
        temperature: float,
        cid: str | None,
        call: ModelCall | None = None,
    ) -> tuple[dict[str, Any], str]:
        """One completion: returns (parsed JSON object, raw content). Raises LLMError."""
        if call is not None:
            call.attempts += 1
        try:
            response = await litellm.acompletion(
                model=target_model,
                messages=messages,
                api_base=self._api_base,
                api_key=self._api_key or None,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                # Bounded: without it a call inherits litellm's multi-minute
                # default and pins a worker slot. Timeout is a transient error
                # (classify() below), so tenacity retries it.
                timeout=settings.LLM_TIMEOUT_SECONDS,
                extra_headers={"x-correlation-id": cid} if cid and cid != "-" else None,
            )
        except Exception as exc:  # noqa: BLE001 -- normalized immediately below
            err = classify(exc)
            _log.warning(
                "litellm_completion_failed",
                extra={"model": target_model, "retriable": err.retriable, "error": str(err)},
            )
            raise err from exc

        if call is not None:
            # Tokens and cost are spent once a completion comes back, whether
            # or not its content turns out to be usable.
            call.add_response(response)

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            # Malformed envelope from the proxy -- usually a transport-layer
            # truncation rather than a modelling problem.
            raise LLMTransientError(f"Unexpected LiteLLM response shape: {exc}") from exc

        if not content:
            raise LLMTransientError("Empty completion content.")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            if temperature == 0.0:
                # Deterministic call: a re-roll reproduces the same malformed
                # text, so this is permanent -- the prompt/schema needs fixing.
                raise LLMPermanentError(f"Model returned non-JSON content: {exc}") from exc
            # Non-zero temperature: a retry may legitimately produce valid
            # JSON next time, so this is worth one more attempt.
            raise LLMTransientError(f"Model returned non-JSON content: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMPermanentError(f"Model returned JSON {type(parsed).__name__}, not an object")
        return parsed, content


def build_llm_client(model: str | None = None) -> LLMClient | None:
    """
    Build the LiteLLM client.

    Returns None when the proxy is not configured so callers can degrade.
    """
    if not settings.LITELLM_PROXY_URL:
        return None
    if not settings.LITELLM_API_KEY and not settings.CRITIC_ENABLED:
        return None
    if not settings.LITELLM_API_KEY:
        return None
    return LiteLLMClient(model=model)