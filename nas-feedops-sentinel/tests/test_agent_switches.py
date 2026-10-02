"""
An agent with its own switch stops when it is switched off: it builds no model client, asks no model, and the
reviewer gets what the rules wrote.

policies/agent-register.yaml names each agent's switch, and tests/test_agent_register.py holds that name to the
settings and to what compose forwards. These hold it to the code: a switch the register lists that the code does
not honour is a claim about how to stop an agent that is not true.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import src.investigation.specialists as specialists
import src.planner_agent as planner_agent
import src.watch_officer_agent as wo
from src import storage
from src.models import NASEvent


class _Client:
    """Records the calls it is asked for and answers with nothing useful: the tests are about whether it is asked."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def acall_with_tool(self, **kwargs):
        self.calls.append(kwargs)
        return {}


def _fresh_event(airport: str) -> NASEvent:
    t = datetime.now(timezone.utc) - timedelta(minutes=1)
    return NASEvent(airport_code=airport, tracon="PCT", service_name="ISMC", message_type="link_status",
                    event_time_utc=t, received_time_utc=t, feed_status="operational", latency_ms=200.0,
                    validation_status="PASSED")


# ── Planner ───────────────────────────────────────────────────────────────────────────────────────────

def test_the_planner_switch_stops_the_planner_building_a_model_client(monkeypatch):
    built: list[dict] = []
    monkeypatch.setattr(planner_agent, "build_llm_client", lambda **kwargs: built.append(kwargs) or _Client())
    monkeypatch.setattr(planner_agent.settings, "CRITIC_ENABLED", True)

    monkeypatch.setattr(planner_agent.settings, "PLANNER_ENABLED", False)
    assert planner_agent.PlannerAgent()._llm is None and not built

    monkeypatch.setattr(planner_agent.settings, "PLANNER_ENABLED", True)
    assert planner_agent.PlannerAgent()._llm is not None and built, "the control: switched on, it builds one"


def test_the_critic_switch_still_stops_the_planner_as_the_register_says(monkeypatch):
    monkeypatch.setattr(planner_agent, "build_llm_client", lambda **kwargs: _Client())
    monkeypatch.setattr(planner_agent.settings, "PLANNER_ENABLED", True)
    monkeypatch.setattr(planner_agent.settings, "CRITIC_ENABLED", False)

    assert planner_agent.PlannerAgent()._llm is None


# ── Watch Officer: the overview narrative ─────────────────────────────────────────────────────────────

async def test_the_watch_officer_switch_stops_the_overview_asking_a_model(monkeypatch):
    client = _Client()
    monkeypatch.setattr(wo, "build_llm_client", lambda *a, **k: client)
    monkeypatch.setattr(wo.settings, "CRITIC_ENABLED", True)
    await storage.save_events([_fresh_event(airport) for airport in ("KDCA", "KIAD", "KBWI")])   # the feed is up
    context = await wo.build_watch_officer_context()

    monkeypatch.setattr(wo.settings, "WATCH_OFFICER_ENABLED", False)
    brief = await wo.agenerate_watch_officer_brief(context, use_llm=True)
    assert not client.calls and brief.llm_used is False

    monkeypatch.setattr(wo.settings, "WATCH_OFFICER_ENABLED", True)
    await wo.agenerate_watch_officer_brief(context, use_llm=True)
    assert client.calls, "the control: switched on and the feed up, the model is asked"


# ── Watch Officer: the investigation brief a reviewer approves from ───────────────────────────────────

async def test_the_watch_officer_switch_stops_the_investigation_brief_asking_a_model(monkeypatch):
    client = _Client()
    monkeypatch.setattr(specialists, "build_llm_client", lambda *a, **k: client)
    monkeypatch.setattr(specialists.settings, "WATCH_OFFICER_ENABLED", False)

    with pytest.raises(specialists.ModelUnavailable, match="WATCH_OFFICER_ENABLED"):
        await specialists.ask_model("investigation_brief", "watch_officer", {"evidence": []})
    assert not client.calls

    # The switch is the Watch Officer's own: another investigation agent is still asked.
    await specialists.ask_model("feed_diagnostician", "feed_diagnostician", {"evidence": []})
    assert client.calls


async def test_the_watch_officer_is_asked_again_when_it_is_switched_back_on(monkeypatch):
    client = _Client()
    monkeypatch.setattr(specialists, "build_llm_client", lambda *a, **k: client)
    monkeypatch.setattr(specialists.settings, "WATCH_OFFICER_ENABLED", True)

    await specialists.ask_model("investigation_brief", "watch_officer", {"evidence": []})
    assert client.calls
