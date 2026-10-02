"""
The runtime halt of the AI layer (src/model_halt.py): a supervisor switches off every model call without a
restart; alerts are still staged; every change is audited by the database; an optional automatic trip is off
by default, counts only real faults, and is as visible as a human's halt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import tenacity
import yaml

import src.llm_client as llm
from src import config, governance, metrics, model_halt, storage
from src.auth import AuthorizationError, Principal, PrincipalSource
from src.demo_scenario import generate_cascade_failure_scenario
from src.pipeline import run_pipeline
from src.storage.pool import get_db_pool

APP = Path(__file__).resolve().parent.parent
_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_TOOL = {"name": "submit_critique", "input_schema": {"type": "object", "required": ["pattern"]}}
_REPLY = {"pattern": "TRANSIENT"}
_VERDICT = {"confidence": 0.8, "reasoning": "Isolated.", "pattern": "ISOLATED_FAULT", "escalate": True,
            "citations": [], "bluf": "Isolated fault; review."}


def _person(*roles: str, source: PrincipalSource = PrincipalSource.OIDC) -> Principal:
    return Principal(principal_id="sam", role="Supervisor", source=source, roles=frozenset(roles))


SUPERVISOR = _person("supervisor")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    storage.override_db_path(":memory:")
    governance.reload_policy()
    model_halt.reset()
    monkeypatch.setattr(llm.LiteLLMClient._acall_with_retries.retry, "wait", tenacity.wait_none())
    yield
    model_halt.reset()
    storage.override_db_path(":memory:")


class _Gateway:
    """A stand-in for litellm.acompletion that counts what reached it."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_REPLY)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5), model="claude-haiku-4-5",
            _hidden_params={})


@pytest.fixture
def gateway(monkeypatch):
    fake = _Gateway()
    monkeypatch.setattr(llm.litellm, "acompletion", fake)
    return fake


async def _ask(message: str = "Assess this alert.", **kw):
    return await llm.LiteLLMClient(model="openai/primary-model").acall_with_tool(
        "system", _TOOL, message, role="critic", **kw)


# ── The state ────────────────────────────────────────────────────────────────────────────────────────

async def test_the_ai_layer_runs_until_someone_halts_it():
    state = await model_halt.get_state()

    assert (state.halted, state.source) == (False, "none")


async def test_a_supervisor_halts_it_with_a_reason_and_the_state_says_who_and_why():
    state = await model_halt.halt(SUPERVISOR, "  the Critic is  quoting callsigns ")

    assert state.halted and state.source == "supervisor"
    assert state.reason == "the Critic is quoting callsigns"
    assert "sam" in state.actor and "supervisor" in state.actor
    assert (await model_halt.get_state(fresh=True)) == state


async def test_a_supervisor_resumes_it_and_a_reason_is_needed_for_that_too():
    await model_halt.halt(SUPERVISOR, "investigating")

    with pytest.raises(model_halt.HaltError, match="reason is required"):
        await model_halt.resume(SUPERVISOR, "   ")
    assert (await model_halt.get_state(fresh=True)).halted

    state = await model_halt.resume(SUPERVISOR, "fixed in the gateway config")

    assert not state.halted and state.reason == "fixed in the gateway config"


async def test_a_halt_without_a_reason_is_refused():
    with pytest.raises(model_halt.HaltError):
        await model_halt.halt(SUPERVISOR, "")
    assert not (await model_halt.get_state(fresh=True)).halted


async def test_a_long_reason_is_cut_to_a_bounded_length():
    state = await model_halt.halt(SUPERVISOR, "x" * 5000)
    assert len(state.reason) == model_halt.REASON_MAX_CHARS


# ── Who may ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("who", [
    _person("watch_officer"), _person("auditor"), _person(), _person("watch_officer", "auditor"),
    _person("supervisor", source=PrincipalSource.SELF_DECLARED),
    _person("supervisor", source=PrincipalSource.API_KEY),
])
async def test_only_a_verified_supervisor_halts_or_resumes(who):
    with pytest.raises(AuthorizationError):
        await model_halt.halt(who, "because")
    with pytest.raises(AuthorizationError):
        await model_halt.resume(who, "because")
    assert not (await model_halt.get_state(fresh=True)).halted


async def test_a_lab_run_may_allow_an_unverified_supervisor_like_every_other_decision(monkeypatch):
    monkeypatch.setattr(config.settings, "ALLOW_UNVERIFIED_DECISIONS", True)

    state = await model_halt.halt(_person("supervisor", source=PrincipalSource.SELF_DECLARED), "lab")

    assert state.halted and "[self-declared]" in state.actor


# ── The audit is the database's ──────────────────────────────────────────────────────────────────────

async def _halt_audit():
    rows = await storage.get_action_audit_log(limit=50)
    return [r for r in reversed(rows) if r["action_id"].startswith("model_halt:")]


async def test_every_halt_and_resume_is_in_the_append_only_audit_log_with_who_and_why():
    await model_halt.halt(SUPERVISOR, "investigating")
    await model_halt.resume(SUPERVISOR, "all clear")

    first, second = await _halt_audit()

    assert (first["previous_status"], first["new_status"]) == ("RUNNING", "HALTED")
    assert (second["previous_status"], second["new_status"]) == ("HALTED", "RUNNING")
    assert "sam" in first["actor"] and first["note"] == "supervisor: investigating"
    assert second["note"] == "supervisor: all clear"


async def test_a_change_made_behind_the_codes_back_is_audited_too():
    """The audit is a trigger, so even a write that skips model_halt.py leaves the row."""
    pool = await get_db_pool()

    await pool.execute("UPDATE model_halt SET halted = TRUE, reason = 'by hand', source = 'supervisor', actor = 'dba'")

    [row] = await _halt_audit()
    assert (row["new_status"], row["actor"], row["note"]) == ("HALTED", "dba", "supervisor: by hand")


async def test_the_state_row_cannot_be_deleted_so_no_row_never_means_running():
    pool = await get_db_pool()

    with pytest.raises(asyncpg.PostgresError, match="always has its one row"):
        await pool.execute("DELETE FROM model_halt")
    with pytest.raises(asyncpg.PostgresError):
        await pool.execute("INSERT INTO model_halt (id) VALUES (2)")


# ── What a process reads ─────────────────────────────────────────────────────────────────────────────

async def test_a_process_keeps_what_it_read_for_a_short_while_and_then_reads_again(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(model_halt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(config.settings, "MODEL_HALT_CACHE_SECONDS", 2.0)
    assert not (await model_halt.get_state()).halted

    pool = await get_db_pool()        # another service halts it
    await pool.execute("UPDATE model_halt SET halted = TRUE, reason = 'elsewhere', source = 'supervisor', actor = 'x'")

    assert not (await model_halt.get_state()).halted, "still inside the cache window"
    clock[0] += 2.5
    assert (await model_halt.get_state()).halted, "the window passed: the halt reached this process"


async def test_a_halt_made_in_this_process_is_seen_at_once():
    await model_halt.get_state()
    await model_halt.halt(SUPERVISOR, "now")

    assert (await model_halt.get_state()).halted


async def test_a_state_that_cannot_be_read_is_a_halt(monkeypatch):
    async def broken():
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(storage, "get_model_halt", broken)

    state = await model_halt.get_state(fresh=True)

    assert state.halted and state.source == "unreadable" and "could not be read" in state.reason


async def test_a_missing_row_is_a_halt_not_a_pass(monkeypatch):
    async def absent():
        return None

    monkeypatch.setattr(storage, "get_model_halt", absent)

    assert (await model_halt.get_state(fresh=True)).halted


async def test_a_database_the_migrate_job_has_not_reached_is_not_a_halt(monkeypatch):
    async def no_table():
        raise asyncpg.UndefinedTableError("relation model_halt does not exist")

    monkeypatch.setattr(storage, "get_model_halt", no_table)

    assert not (await model_halt.get_state(fresh=True)).halted, "a rolling deploy keeps working"


async def test_the_gauges_say_what_this_process_last_read():
    await model_halt.halt(SUPERVISOR, "a")
    assert metrics.MODEL_HALTED._value.get() == 1
    await model_halt.resume(SUPERVISOR, "b")
    assert metrics.MODEL_HALTED._value.get() == 0
    assert metrics.MODEL_HALT_CHECKED._value.get() > 0


# ── The chokepoint ───────────────────────────────────────────────────────────────────────────────────

async def test_while_halted_a_model_call_is_refused_before_anything_is_sent(gateway):
    await model_halt.halt(SUPERVISOR, "investigating a leak")

    with pytest.raises(llm.LLMPolicyDenied, match="model-halted") as refused:
        await _ask()

    assert gateway.calls == 0
    assert "investigating a leak" in str(refused.value)
    [row] = await storage.get_model_calls_since(_EPOCH)
    assert (row["outcome"], row["attempts"], row["policy_rule"], row["policy_action"]) == (
        "policy_denied", 0, "model-halted", "block")


async def test_after_a_resume_the_next_call_goes_through(gateway):
    await model_halt.halt(SUPERVISOR, "stop")
    with pytest.raises(llm.LLMPolicyDenied):
        await _ask()

    await model_halt.resume(SUPERVISOR, "go")

    assert await _ask() == _REPLY and gateway.calls == 1


async def test_a_halt_stops_every_role_not_only_the_one_that_prompted_it(gateway):
    await model_halt.halt(SUPERVISOR, "stop")

    for role in ("critic", "planner", "watch_officer", "feed_diagnostician", "correlation_analyst", "red_team_critic"):
        with pytest.raises(llm.LLMPolicyDenied, match="model-halted"):
            await llm.LiteLLMClient(model="openai/primary-model").acall_with_tool("s", _TOOL, "m", role=role)
    assert gateway.calls == 0


async def test_a_halt_that_cannot_be_read_stops_calls_too(gateway, monkeypatch):
    async def broken():
        raise ConnectionError("down")

    monkeypatch.setattr(storage, "get_model_halt", broken)

    with pytest.raises(llm.LLMPolicyDenied, match="model-halted"):
        await _ask()
    assert gateway.calls == 0


async def test_every_critical_alert_is_still_staged_while_the_ai_layer_is_halted(gateway, monkeypatch):
    """The mission never depended on a model: the halt costs advice, not alerts."""
    import src.critic_agent as critic_agent
    import src.pipeline as pipeline

    # Each module's own binding, as test_llm_budget does: another test reloads src.config, and a module that
    # imported `settings` before that holds the old object.
    monkeypatch.setattr(critic_agent.settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(pipeline.settings, "INVESTIGATIONS_ENABLED", False)
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: llm.LiteLLMClient(model="openai/primary-model"))
    await model_halt.halt(SUPERVISOR, "stop")

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")

    assert gateway.calls == 0 and summary["staged_count"] >= 1
    pending = await storage.get_pending_staged_actions()
    assert pending, "alerts are staged for a person whether or not a model answered"


async def test_a_refused_critic_says_the_ai_layer_is_halted_and_returns_no_verdict(gateway, monkeypatch):
    from src.critic_agent import CriticAgent
    from src.models import AlertEvent

    agent = CriticAgent()
    agent._llm = llm.LiteLLMClient(model="openai/primary-model")
    await model_halt.halt(SUPERVISOR, "stop")

    result = await agent.critique(AlertEvent(
        alert_type="ALERT_LOW_RVR", severity="CRITICAL", airport_code="KDCA", service_name="APDS_RVR",
        description="Low RVR 800 ft at touchdown on runway 01 at KDCA."))

    assert result.confidence is None and "model-halted" in result.reasoning


# ── The automatic trip: off by default, only for real faults, visible ────────────────────────────────

@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(config.settings, "MODEL_AUTO_HALT_ENABLED", True)
    monkeypatch.setattr(config.settings, "MODEL_AUTO_HALT_THRESHOLD", 3)
    monkeypatch.setattr(config.settings, "MODEL_AUTO_HALT_WINDOW_SECONDS", 300.0)


LEAK = "no-protected-values-in-prompts"


async def test_the_automatic_trip_is_off_unless_a_deployment_turns_it_on():
    assert config.settings.MODEL_AUTO_HALT_ENABLED is False
    for _ in range(50):
        assert await model_halt.note_violation(LEAK) is False

    assert not (await model_halt.get_state(fresh=True)).halted


async def test_it_trips_at_the_threshold_and_says_so(auto):
    assert [await model_halt.note_violation(LEAK) for _ in range(3)] == [False, False, True]

    state = await model_halt.get_state(fresh=True)
    assert state.halted and state.source == "auto" and state.actor == model_halt.AUTO_ACTOR
    assert "3 model calls were refused" in state.reason and LEAK in state.reason


async def test_a_trip_is_as_visible_as_a_human_halt_in_the_audit_log_and_the_metric(auto):
    before = metrics.MODEL_HALT_CHANGES.labels(to="halted", source="auto")._value.get()
    for _ in range(3):
        await model_halt.note_violation(LEAK)

    [row] = await _halt_audit()
    assert (row["new_status"], row["actor"]) == ("HALTED", model_halt.AUTO_ACTOR) and row["note"].startswith("auto: ")
    assert metrics.MODEL_HALT_CHANGES.labels(to="halted", source="auto")._value.get() == before + 1
    assert metrics.MODEL_HALTED._value.get() == 1


@pytest.mark.parametrize("rule", ["gateway-aliases-only", "prompt-size-ceiling", "output-budget-ceiling",
                                  "prompts-must-be-redacted", "model-halted", None])
async def test_a_refusal_that_is_not_a_fault_does_not_count_toward_it(auto, rule):
    """A misconfigured role or an oversized prompt is a bug to fix, not an attack to halt for."""
    for _ in range(20):
        assert await model_halt.note_violation(rule) is False

    assert not (await model_halt.get_state(fresh=True)).halted


async def test_a_credential_in_a_prompt_counts_too(auto):
    assert [await model_halt.note_violation("no-credentials-in-prompts") for _ in range(3)][-1] is True


async def test_refusals_spread_beyond_the_window_do_not_add_up(auto, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(model_halt.time, "monotonic", lambda: clock[0])

    for _ in range(2):
        await model_halt.note_violation(LEAK)
    clock[0] += 301
    assert await model_halt.note_violation(LEAK) is False
    assert not (await model_halt.get_state(fresh=True)).halted


async def test_it_trips_once_and_does_not_write_again_while_halted(auto):
    for _ in range(3):
        await model_halt.note_violation(LEAK)
    for _ in range(6):
        await model_halt.note_violation(LEAK)

    assert len(await _halt_audit()) == 1


async def test_only_a_supervisor_clears_an_automatic_trip(auto):
    for _ in range(3):
        await model_halt.note_violation(LEAK)

    with pytest.raises(AuthorizationError):
        await model_halt.resume(_person("watch_officer"), "looks fine")
    assert (await model_halt.get_state(fresh=True)).halted

    state = await model_halt.resume(SUPERVISOR, "traced to the Planner's context; fixed")
    assert not state.halted


async def test_a_trip_that_cannot_be_written_is_logged_and_never_raised(auto, monkeypatch):
    async def broken(**kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(storage, "set_model_halt", broken)

    assert [await model_halt.note_violation(LEAK) for _ in range(3)] == [False, False, False]


async def test_a_leak_found_at_the_chokepoint_counts_and_the_third_one_halts_everything(auto, gateway):
    for _ in range(3):
        with pytest.raises(llm.LLMPolicyDenied, match="no-protected-values-in-prompts"):
            await _ask("the aircraft UAL12AB went around", protected_values={"UAL12AB"})

    with pytest.raises(llm.LLMPolicyDenied, match="model-halted"):
        await _ask()
    assert gateway.calls == 0
    assert (await model_halt.get_state(fresh=True)).source == "auto"


# ── The policy, the alert, the runbook ───────────────────────────────────────────────────────────────

def test_the_policy_halts_before_any_other_rule_is_asked():
    rules = yaml.safe_load((APP / "policies" / "model-use.yaml").read_text(encoding="utf-8"))["rules"]
    by_name = {r["name"]: r for r in rules}

    assert by_name["model-halted"]["action"] == "block"
    assert by_name["model-halted"]["priority"] == max(r["priority"] for r in rules)


def test_a_halt_has_its_own_critical_alert_that_ignores_a_process_that_has_not_looked_lately():
    rules = {r["alert"]: r for g in yaml.safe_load((APP / "monitoring" / "alert-rules.yml").read_text(encoding="utf-8"))["groups"]
             for r in g["rules"]}
    halted = rules["ModelLayerHalted"]

    assert halted["labels"]["severity"] == "critical"
    assert "sentinel_model_halted" in halted["expr"] and "sentinel_model_halt_checked_timestamp_seconds" in halted["expr"]
    assert 'agent_policy_name!="model-halted"' in rules["ModelCallsRefusedByPolicy"]["expr"], \
        "a halt refuses every call; the generic warning would fire for each role as well"
    assert "ModelLayerHalted" in (APP / "docs" / "runbooks.md").read_text(encoding="utf-8")
