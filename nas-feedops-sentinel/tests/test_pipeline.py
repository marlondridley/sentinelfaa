"""
The pipeline invariant: every CRITICAL alert is staged for human review,
whatever the LLM does.

This is the product's headline control. Before these tests the pipeline
existed in three copies. src/pipeline.py staged correctly; src/main.py's copy
(the live SWIM watcher's path) raised out of CrewAI's kickoff() before
staging; the dashboard's ingest buttons never staged at all. The live
database held 84 CRITICAL alerts and 0 staged actions. So these tests pin
two things: run_pipeline() stages every CRITICAL alert under every LLM
outcome, and every entry point goes through run_pipeline().
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src import storage
from src.config import settings
from src.demo_scenario import generate_cascade_failure_scenario
from src.llm_client import LLMTransientError
from src.pipeline import run_pipeline

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


# ── LLM stand-ins ─────────────────────────────────────────────────────────────

_VALID_CRITIQUE = {
    "confidence": 0.8, "reasoning": "Correlated outage across services.",
    "pattern": "CASCADE_FAILURE", "escalate": True, "citations": [],
}
_VALID_BRIEF = {
    "headline": "Feed picture degraded at KDCA.", "what_changed": ["ISMC down"],
    "why_it_matters": ["Data picture incomplete"], "recommended_human_review": ["Verify"],
    "confidence": 0.6, "limitations": [],
}


class _ScriptedClient:
    """Returns a fixed structured output, or raises, for every call."""

    def __init__(self, output=None, exc: Exception | None = None):
        self._output, self._exc = output, exc

    async def acall_with_tool(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        tool = kwargs.get("tool", {}).get("name", "")
        if self._output is not None:
            return self._output
        return _VALID_BRIEF if "watch_officer" in tool else _VALID_CRITIQUE


_LLM_OUTCOMES = {
    "llm_succeeds": lambda: _ScriptedClient(),
    "llm_raises": lambda: _ScriptedClient(exc=LLMTransientError("proxy unreachable")),
    "llm_returns_garbage": lambda: _ScriptedClient(output={"confidence": 7.3, "nonsense": True}),
    "llm_unconfigured": lambda: None,
}


@pytest.mark.parametrize("outcome", list(_LLM_OUTCOMES))
async def test_every_critical_alert_is_staged_whatever_the_llm_does(monkeypatch, outcome):
    import src.critic_agent as critic_agent
    import src.watch_officer_agent as watch_officer

    make_client = _LLM_OUTCOMES[outcome]
    monkeypatch.setattr(settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: make_client())
    monkeypatch.setattr(watch_officer, "build_llm_client", lambda *a, **k: make_client())

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    alerts = await storage.get_recent_alerts(limit=500)
    critical = [a for a in alerts if a["severity"] == "CRITICAL"]
    pending = await storage.get_pending_staged_actions()

    assert critical, "scenario must produce CRITICAL alerts or this test measures nothing"
    assert summary["staged_count"] == len(critical)
    assert {p.alert_id for p in pending} == {a["alert_id"] for a in critical}
    for action in pending:
        assert await storage.get_ticket_by_action_id(action.action_id) is not None


async def test_rerunning_a_batch_does_not_duplicate_staged_actions(monkeypatch):
    """Staging is idempotent per alert: the same alerts staged twice stay one action each."""
    from src.execution_gate import stage_critical_alert

    monkeypatch.setattr(settings, "CRITIC_ENABLED", False)
    await run_pipeline(generate_cascade_failure_scenario(), label="TEST")
    pending = await storage.get_pending_staged_actions()
    alerts = {a["alert_id"]: a for a in await storage.get_recent_alerts(limit=500)}

    from src.models import AlertEvent
    for action in pending:
        a = alerts[action.alert_id]
        await stage_critical_alert(AlertEvent(
            alert_id=a["alert_id"], alert_type=a["alert_type"], severity=a["severity"],
            airport_code=a["airport_code"], service_name=a["service_name"],
            description=a["description"],
        ))
    assert len(await storage.get_pending_staged_actions()) == len(pending)


# ── Every entry point uses the one pipeline ──────────────────────────────────

def test_cli_watcher_path_delegates_to_run_pipeline(monkeypatch):
    import src.main as main

    fake = AsyncMock(return_value={"staged_count": 0})
    monkeypatch.setattr(main, "run_pipeline", fake)
    main._run_pipeline(generate_cascade_failure_scenario(), label="REPLAY")
    fake.assert_awaited_once()


def _calls_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            names.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
    return names


@pytest.mark.parametrize("path", [
    "dashboard/pages/5_Ingest_and_Replay.py",
    "src/main.py",
])
def test_no_entry_point_persists_alerts_outside_the_pipeline(path):
    """
    Writing alerts directly (save_alerts) is how the dashboard copy skipped
    staging. Only src/pipeline.py may do it.
    """
    calls = _calls_in(_REPO / path)
    assert "save_alerts" not in calls
    assert "check_alerts" not in calls
