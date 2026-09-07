"""
Abstract LLM client with retry/circuit-breaker logic.

The agents (Critic, Planner) call this interface, not Anthropic's SDK directly.
This lets Peraton swap Anthropic for an internal NVIDIA NIM endpoint (or any
OpenAI-compatible service) without touching agent logic.

Two implementations:
  AnthropicLLMClient — uses anthropic.Anthropic with native tool_use
  NIMLLMClient       — uses httpx against an OpenAI-compatible /v1/chat/completions
                        endpoint (NVIDIA NIM, vLLM, TGI, etc.) with JSON-mode fallback

Selection is controlled by LLM_PROVIDER env var: "anthropic" (default) or "nim".
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src import config


class LLMError(Exception):
    """Raised when the LLM call fails after all retries."""


class LLMClient(Protocol):
    def call_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
    ) -> dict[str, Any]:
        """Call the LLM and return the tool-use arguments as a dict."""
        ...


# ── Anthropic implementation ──────────────────────────────────────────────────

class AnthropicLLMClient:
    """Native Anthropic client with prompt caching and forced tool use."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
        reraise=True,
    )
    def call_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
    ) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user_message}],
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                return dict(block.input)

        raise LLMError(f"Model did not call tool '{tool['name']}'.")


# ── NIM / OpenAI-compatible implementation ────────────────────────────────────

class NIMLLMClient:
    """
    OpenAI-compatible client for NVIDIA NIM, vLLM, TGI, and similar.

    Uses JSON-mode + schema in the system prompt to enforce structured output,
    since not all NIM containers support OpenAI's native function calling.
    """

    def __init__(self, endpoint: str, model: str, api_key: str = "") -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(timeout=30.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, ConnectionError, TimeoutError)),
        reraise=True,
    )
    def call_with_tool(
        self,
        system_prompt: str,
        tool: dict[str, Any],
        user_message: str,
        max_tokens: int = 768,
    ) -> dict[str, Any]:
        schema_text = json.dumps(tool["input_schema"], indent=2)
        augmented_system = (
            f"{system_prompt}\n\n"
            f"You MUST respond with a single JSON object matching this schema exactly:\n"
            f"{schema_text}\n"
            f"Do not include any text outside the JSON object."
        )

        # Follow OpenAI SDK convention: endpoint includes its own version path
        # (e.g., .../v1 for vanilla NIM, .../v1beta/openai for Gemini). Backward
        # compat: if user supplied a bare host without a version segment, append /v1.
        base = self._endpoint
        if not any(seg in base for seg in ("/v1", "/openai")):
            base = f"{base}/v1"
        response = self._client.post(
            f"{base}/chat/completions",
            headers=self._headers,
            json={
                "model": self._model,
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": augmented_system},
                    {"role": "user", "content": user_message},
                ],
            },
        )
        response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
            return json.loads(content)
        except (KeyError, json.JSONDecodeError) as exc:
            raise LLMError(f"Could not parse NIM response: {exc}") from exc


# ── Factory ───────────────────────────────────────────────────────────────────

def build_llm_client() -> LLMClient | None:
    """
    Build the configured LLM client.

    Returns None when no provider is reachable so callers can degrade gracefully.
    """
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

    if provider == "nim":
        endpoint = os.getenv("NIM_ENDPOINT", "")
        if not endpoint:
            return None
        return NIMLLMClient(
            endpoint=endpoint,
            model=os.getenv("NIM_MODEL", "meta/llama3-8b-instruct"),
            api_key=os.getenv("NIM_API_KEY", ""),
        )

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    return AnthropicLLMClient(api_key=api_key, model=config.CRITIC_MODEL)
