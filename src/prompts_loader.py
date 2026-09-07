"""
Externalized prompt and tool-schema loader.

Prompts live in prompts/*.yaml so they can be versioned, A/B tested, and
hot-swapped without code changes. Each YAML file defines a system prompt
and a tool schema (forced structured output).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


@lru_cache(maxsize=16)
def load_prompt(name: str) -> dict[str, Any]:
    """Load and cache a prompt definition by name (e.g., 'critic', 'planner')."""
    path = _PROMPTS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt definition not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def system_prompt(name: str) -> str:
    return load_prompt(name)["system_prompt"]


def tool_schema(name: str) -> dict[str, Any]:
    return load_prompt(name)["tool"]
