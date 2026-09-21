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

from src.config import settings
from src.logging_config import get_correlation_id, get_logger
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
    ) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.acall_with_tool(
                    system_prompt, tool, user_message, max_tokens, model, temperature
                )
            )
        raise LLMPermanentError(
            "call_with_tool() cannot block inside a running event loop; "
            "use acall_with_tool() instead."
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(LLMTransientError),
        reraise=True,
    )
    async def acall_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        schema_text = json.dumps(tool.get("input_schema", {}), indent=2)
        augmented_system = (
            f"{system_prompt}\n\n"
            f"You MUST respond with a single JSON object matching this schema:\n"
            f"{schema_text}\n"
            f"Do not include any text outside the JSON object."
        )
        cid = get_correlation_id()
        target_model = model or self._model

        # Mandatory redaction: every caller of acall_with_tool() goes through
        # this one chokepoint before anything reaches the model host, so a
        # caller can't forget to scrub identifying NAS telemetry (aircraft
        # tail numbers, squawk codes, raw lat/lon, callsigns, ...) out of its
        # prompt. See src/redaction.py for what's covered and why.
        redacted_user_message = redact_text(user_message)

        _log.info(
            "litellm_completion_start",
            extra={"model": target_model, "tool": tool.get("name")},
        )

        try:
            response = await litellm.acompletion(
                model=target_model,
                messages=[
                    {"role": "system", "content": augmented_system},
                    {"role": "user", "content": redacted_user_message},
                ],
                api_base=self._api_base,
                api_key=self._api_key or None,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                extra_headers={"x-correlation-id": cid} if cid and cid != "-" else None,
            )
        except Exception as exc:  # noqa: BLE001 -- normalized immediately below
            err = classify(exc)
            _log.warning(
                "litellm_completion_failed",
                extra={
                    "model": target_model,
                    "retriable": err.retriable,
                    "error": str(err),
                },
            )
            raise err from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            # Malformed envelope from the proxy -- usually a transport-layer
            # truncation rather than a modelling problem.
            raise LLMTransientError(
                f"Unexpected LiteLLM response shape: {exc}"
            ) from exc

        if not content:
            raise LLMTransientError("Empty completion content.")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            if temperature == 0.0:
                # Deterministic call: a re-roll reproduces the same malformed
                # text, so this is permanent -- the prompt/schema needs fixing.
                raise LLMPermanentError(
                    f"Model returned non-JSON content: {exc}"
                ) from exc
            # Non-zero temperature: a retry may legitimately produce valid
            # JSON next time, so this is worth one more attempt rather than
            # failing fast.
            raise LLMTransientError(
                f"Model returned non-JSON content: {exc}"
            ) from exc


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