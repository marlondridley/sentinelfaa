"""
LiteLLM-backed client. All agent model calls route through the proxy.

Uses litellm.acompletion with api_base=settings.LITELLM_PROXY_URL and
api_key=settings.LITELLM_API_KEY.
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

_log = get_logger(__name__)


class LLMError(Exception):
    """Raised when the LLM call fails after all retries."""


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
    ) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.acall_with_tool(
                    system_prompt, tool, user_message, max_tokens, model
                )
            )
        raise LLMError(
            "call_with_tool() cannot block inside a running event loop; "
            "use acall_with_tool() instead."
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((LLMError, ConnectionError, TimeoutError)),
        reraise=True,
    )
    async def acall_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
        model: str | None = None,
    ) -> dict[str, Any]:
        schema_text = json.dumps(tool.get("input_schema", {}), indent=2)
        augmented_system = (
            f"{system_prompt}\n\n"
            f"You MUST respond with a single JSON object matching this schema:\n"
            f"{schema_text}\n"
            f"Do not include any text outside the JSON object."
        )
        cid = get_correlation_id()
        _log.info(
            "litellm_completion_start",
            extra={"model": model or self._model, "tool": tool.get("name")},
        )
        response = await litellm.acompletion(
            model=model or self._model,
            messages=[
                {"role": "system", "content": augmented_system},
                {"role": "user", "content": user_message},
            ],
            api_base=self._api_base,
            api_key=self._api_key or None,
            max_tokens=max_tokens,
            temperature=0.0,
            response_format={"type": "json_object"},
            extra_headers={"x-correlation-id": cid} if cid and cid != "-" else None,
        )
        try:
            content = response.choices[0].message.content
            if not content:
                raise LLMError("Empty completion content.")
            return json.loads(content)
        except (AttributeError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(f"Could not parse LiteLLM response: {exc}") from exc


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
