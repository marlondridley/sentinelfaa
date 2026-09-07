"""CrewAI LLM factory — always routed through the LiteLLM proxy."""
from __future__ import annotations

import os
from crewai import LLM

from src.config import settings
from src.logging_config import get_logger

_log = get_logger(__name__)


def get_configured_llm() -> LLM:
    """Return a CrewAI LLM instance pointed at the LiteLLM proxy."""
    model_name = settings.CRITIC_MODEL
    if not (model_name.startswith("openai/") or model_name.startswith("anthropic/")):
        model_name = f"openai/{model_name}"

    _log.info(
        "crewai_llm_configured",
        extra={"model": model_name, "api_base": settings.LITELLM_PROXY_URL},
    )
    return LLM(
        model=model_name,
        base_url=settings.LITELLM_PROXY_URL,
        api_key=settings.LITELLM_API_KEY or "sk-placeholder",
    )


get_llm = get_configured_llm