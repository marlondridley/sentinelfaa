"""
What a model call records about what it was given -- without the text.

Each model_calls row carries the prompt's hash and version, the hash of the redacted input that
was sent, and how many substitutions redaction made. A Critic verdict and a Watch Officer brief
carry the id of the call behind them, so a reviewer can go from what they were shown to the
record of how it was produced.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import tenacity

import src.llm_client as llm
from src import governance, storage
from src.model_telemetry import prompt_fingerprint, sha256_hex
from src.models import AlertEvent, CriticResult, NASEvent, WatchOfficerBrief
from src.prompts_loader import prompt_version, system_prompt, tool_schema
from src.redaction import redact_text, redact_text_counted
from src.storage.pool import get_db_pool

_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_TOOL = {"name": "submit_critique", "input_schema": {"type": "object", "required": ["pattern", "escalate", "confidence"]}}
_COMPLETE = {"pattern": "TRANSIENT", "escalate": False, "confidence": 0.8}


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    storage.override_db_path(":memory:")
    governance.reload_policy()
    monkeypatch.setattr(llm.LiteLLMClient._acall_with_retries.retry, "wait", tenacity.wait_none())
    yield
    storage.override_db_path(":memory:")


def _response(reply: dict):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="claude-haiku-4-5", _hidden_params={},
    )


def _script(monkeypatch, reply: dict):
    async def acompletion(**kwargs):
        return _response(reply)

    monkeypatch.setattr(llm.litellm, "acompletion", acompletion)


async def _rows() -> list[dict]:
    return await storage.get_model_calls_since(_EPOCH)


def _client():
    return llm.LiteLLMClient(model="openai/primary-model")


# ── the row ────────────────────────────────────────────────────────────────────

async def test_the_row_records_hashes_version_and_redaction_count_of_what_was_sent(monkeypatch):
    _script(monkeypatch, _COMPLETE)
    message = 'Alert context: {"aircraft_id": "AAL123", "runway": "01"} flight UAL1234 squawk=1200'
    sent, removed = redact_text_counted(message)
    assert removed >= 3

    await _client().acall_with_tool("system text", _TOOL, message, role="critic", prompt_version="2.1")

    (row,) = await _rows()
    assert row["prompt_hash"] == prompt_fingerprint("system text", _TOOL)
    assert row["prompt_version"] == "2.1"
    assert row["input_hash"] == sha256_hex(sent), "the hash is of what left the system, the redacted text"
    assert row["input_hash"] != sha256_hex(message)
    assert row["redaction_count"] == removed
    assert "AAL123" not in json.dumps(row, default=str), "hashes only: no prompt or input text is kept"


async def test_a_clean_input_records_zero_redactions_not_unknown(monkeypatch):
    _script(monkeypatch, _COMPLETE)
    await _client().acall_with_tool("s", _TOOL, "Runway 01 RVR 800", role="critic")
    (row,) = await _rows()
    assert row["redaction_count"] == 0
    assert row["prompt_version"] is None


async def test_a_refused_call_still_records_what_it_would_have_sent(monkeypatch):
    _script(monkeypatch, _COMPLETE)
    with pytest.raises(llm.LLMPolicyDenied):
        await _client().acall_with_tool("s", _TOOL, "x", role=None)
    (row,) = await _rows()
    assert row["outcome"] == "policy_denied" and row["prompt_hash"] and row["input_hash"]


def test_redact_text_counted_agrees_with_redact_text():
    for text in ("", "plain", "callsign=UAL1234 and N12345 at 38.85123, -77.03789", "<aircraftId>AAL1</aircraftId>",
                 "1 validation error [type=extra_forbidden, input_value='AAL123', input_type=str]"):
        assert redact_text_counted(text)[0] == redact_text(text)
    assert redact_text_counted("plain")[1] == 0
    assert redact_text_counted("N12345 then N54321")[1] == 2


# ── the verdict and the brief point at the call ────────────────────────────────

def _alert() -> AlertEvent:
    return AlertEvent(alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="APDS_RVR",
                      description="Low RVR 800 ft at touchdown on runway 01 at KDCA.")


_VERDICT = {"confidence": 0.8, "reasoning": "Isolated.", "pattern": "ISOLATED_FAULT", "escalate": True, "citations": []}


async def test_a_critic_verdict_carries_the_id_of_its_model_call_and_stores_it(monkeypatch):
    import src.critic_agent as critic_agent

    _script(monkeypatch, _VERDICT)
    monkeypatch.setattr(critic_agent.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: _client())
    alert = _alert()
    await storage.save_alert(alert)

    result = await critic_agent.CriticAgent().critique(alert)
    await storage.save_critic_result(result)

    (row,) = await _rows()
    assert result.call_id == row["call_id"]
    assert row["prompt_version"] == prompt_version("critic")
    assert row["prompt_hash"] == prompt_fingerprint(system_prompt("critic"), tool_schema("critic"))
    pool = await get_db_pool()
    assert await pool.fetchval("SELECT call_id FROM critic_results WHERE alert_id = $1", alert.alert_id) == row["call_id"]


async def test_a_verdict_with_no_model_call_has_no_call_id(monkeypatch):
    import src.critic_agent as critic_agent

    monkeypatch.setattr(critic_agent.settings, "CRITIC_ENABLED", False)
    assert (await critic_agent.CriticAgent().critique(_alert())).call_id is None


async def test_one_alerts_call_id_is_never_taken_for_the_next_alerts(monkeypatch):
    """The id is read from the context: a critic that made no call must not inherit the previous call's."""
    import src.critic_agent as critic_agent

    _script(monkeypatch, _VERDICT)
    monkeypatch.setattr(critic_agent.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: _client())
    answered = await critic_agent.CriticAgent().critique(_alert())
    assert answered.call_id

    monkeypatch.setattr(critic_agent.settings, "CRITIC_ENABLED", False)
    assert (await critic_agent.CriticAgent().critique(_alert())).call_id is None


_NARRATIVE = {
    "headline": "KDCA ISMC link degraded; data picture incomplete.",
    "what_changed": ["ISMC reported degraded"], "why_it_matters": ["Feed trust reduced"],
    "recommended_human_review": ["Verify ISMC against the facility"],
    "confidence": 0.7, "limitations": ["Single feed"],
}


async def test_a_brief_carries_the_id_of_its_model_call_and_a_rule_written_one_does_not(monkeypatch):
    import src.watch_officer_agent as wo

    _script(monkeypatch, _NARRATIVE)
    monkeypatch.setattr(wo.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(wo, "build_llm_client", lambda *a, **k: _client())
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    await storage.save_events([
        NASEvent(airport_code=ap, tracon="PCT", service_name="ISMC", message_type="link_status", event_time_utc=now,
                 received_time_utc=now, feed_status="operational", latency_ms=200.0, validation_status="PASSED")
        for ap in ("KDCA", "KIAD", "KBWI")])

    context = await wo.build_watch_officer_context()
    brief = await wo.agenerate_watch_officer_brief(context, use_llm=True)
    await storage.save_watch_officer_brief(brief)

    (row,) = await _rows()
    assert brief.llm_used and brief.call_id == row["call_id"]
    assert row["prompt_version"] == prompt_version("watch_officer")
    (stored,) = await storage.get_recent_watch_officer_briefs(limit=1)
    assert stored.call_id == row["call_id"]

    rule_written = await wo.agenerate_watch_officer_brief(context, use_llm=False)
    assert rule_written.call_id is None


# ── the migration is additive, and a database that has not got it yet still works ──

async def test_the_provenance_migration_is_additive_and_model_calls_stays_append_only(monkeypatch):
    import asyncpg

    _script(monkeypatch, _COMPLETE)
    await _client().acall_with_tool("s", _TOOL, "u", role="critic")   # a row for the trigger to refuse to change
    pool = await get_db_pool()
    assert await pool.fetchval(
        "SELECT count(*) FROM schema_migrations WHERE id = '2026_10_model_call_provenance'") == 1
    columns = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'model_calls'")}
    assert {"prompt_hash", "prompt_version", "input_hash", "redaction_count"} <= columns
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await pool.execute("UPDATE model_calls SET redaction_count = 0")


async def test_a_database_without_the_migration_still_takes_the_row_the_verdict_and_the_brief(monkeypatch):
    """A rolling deploy: new code, old schema. Nothing is lost and nothing raises."""
    pool = await get_db_pool()
    try:
        await pool.execute("ALTER TABLE model_calls DROP COLUMN prompt_hash, DROP COLUMN prompt_version, "
                           "DROP COLUMN input_hash, DROP COLUMN redaction_count")
        await pool.execute("ALTER TABLE critic_results DROP COLUMN call_id")
        await pool.execute("ALTER TABLE watch_officer_briefs DROP COLUMN call_id")

        _script(monkeypatch, _COMPLETE)
        await _client().acall_with_tool("s", _TOOL, "u", role="critic")
        assert len(await _rows()) == 1

        alert = _alert()
        await storage.save_alert(alert)
        await storage.save_critic_result(CriticResult(alert_id=alert.alert_id, reasoning="r", call_id="c1"))
        await storage.save_watch_officer_brief(WatchOfficerBrief(headline="h", call_id="c1"))
        assert (await storage.get_recent_watch_officer_briefs(limit=1))[0].headline == "h"
    finally:
        await pool.execute("ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS prompt_hash TEXT, "
                           "ADD COLUMN IF NOT EXISTS prompt_version TEXT, ADD COLUMN IF NOT EXISTS input_hash TEXT, "
                           "ADD COLUMN IF NOT EXISTS redaction_count INTEGER")
        await pool.execute("ALTER TABLE critic_results ADD COLUMN IF NOT EXISTS call_id TEXT")
        await pool.execute("ALTER TABLE watch_officer_briefs ADD COLUMN IF NOT EXISTS call_id TEXT")
