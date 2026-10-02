"""
The agent register (policies/agent-register.yaml): the inventory behind the two policies.

    python -m src.agent_register           # one row per agent
    python -m src.agent_register --json

The register is data, validated here, and held against the policies and the code
by tests/test_agent_register.py. Nothing at runtime depends on it: the policies
decide, and this says who each agent is, who owns it, what it may touch and how
to stop it, so a reviewer, an auditor or a client can read one file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

REGISTER_PATH = Path(__file__).resolve().parent.parent / "policies" / "agent-register.yaml"
RiskTier = Literal["high", "moderate", "low"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromptRef(_Strict):
    name: str
    version: str


class Authority(_Strict):
    """What an agent may do to a staged action. Every one is false: the register cannot say otherwise."""

    decide: Literal[False]
    stage: Literal[False]
    write: Literal[False]
    execute: Literal[False]


class Disable(_Strict):
    """How to stop the agent today: a setting, its value, and the services to restart for it to apply."""

    setting: str
    value: str
    restart: list[str] = Field(min_length=1)


class Agent(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    purpose: str
    calls_model: bool
    model_setting: str | None = None
    prompts: list[PromptRef] = Field(default_factory=list)
    risk_tier: RiskTier
    risk_rationale: str
    owner: str = Field(min_length=1)
    last_reviewed: date
    runs_in: list[str] = Field(min_length=1)
    reads: list[str]
    responds: list[str]
    data_sent: str
    fallback: str
    authority: Authority
    disable: Disable

    @field_validator("owner", "purpose", "risk_rationale", "data_sent", "fallback")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @property
    def grants(self) -> set[str]:
        """The tools this agent is granted, as tool-use.yaml spells them: "<id>:<tool>"."""
        return {f"{self.id}:{tool}" for tool in (*self.reads, *self.responds)}


class Identity(_Strict):
    service: str
    agent: str
    runtime_switch: str


class Register(_Strict):
    version: str
    name: str
    description: str
    identity: Identity
    agents: list[Agent] = Field(min_length=1)
    evidence: list[str]

    def by_id(self) -> dict[str, Agent]:
        return {agent.id: agent for agent in self.agents}

    @field_validator("agents")
    @classmethod
    def _unique_ids(cls, agents: list[Agent]) -> list[Agent]:
        ids = [a.id for a in agents]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"agent ids registered twice: {duplicated}")
        return agents


def load_register(path: Path | None = None) -> Register:
    """Read and validate the register. An unknown key, a missing field or a true authority is an error."""
    return Register.model_validate(yaml.safe_load(Path(path or REGISTER_PATH).read_text(encoding="utf-8")))


def render_text(register: Register) -> str:
    lines = [f"{register.name} v{register.version}: {len(register.agents)} agents", "",
             f"{'agent':<22}{'tier':<10}{'model':<7}{'owner':<22}{'runs in':<42}reviewed"]
    for a in register.agents:
        lines.append(f"{a.id:<22}{a.risk_tier:<10}{'yes' if a.calls_model else 'no':<7}{a.owner:<22}"
                     f"{', '.join(a.runs_in):<42}{a.last_reviewed}")
    lines += ["", "No agent can decide, stage, write or execute. To stop one today (restart required):"]
    for a in register.agents:
        lines.append(f"  {a.id:<22}{a.disable.setting}={a.disable.value!r}  restart {', '.join(a.disable.restart)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.agent_register", description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print the register as JSON")
    args = parser.parse_args(argv)
    try:
        register = load_register()
    except Exception as exc:  # noqa: BLE001
        print(f"agent register: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(register.model_dump(mode="json"), indent=2) if args.json else render_text(register))
    return 0


if __name__ == "__main__":
    sys.exit(main())
