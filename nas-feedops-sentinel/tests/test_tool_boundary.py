"""
The agent layer's tool boundary (src/agent_tools.py, policies/tool-use.yaml).

Every tool an advisory agent invokes is put to an Agent Governance Toolkit
policy first: the output tool it hands a model, and every read that builds
its context or evidence. Agents never decide, write or escalate. The human
gate stays at escalation; these tests show no agent can reach it from the
other side, and that every decision goes to AGT's audit trail through OTel.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src import agent_tools, governance, storage, telemetry
from src.agent_tools import TOOLS, ToolCallDenied, acting_as
from src.auth import Decision, Principal, PrincipalSource
from src.config import settings
from src.models import AlertEvent

REPO = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(settings.TOOL_POLICY_PATH.read_text(encoding="utf-8"))
GRANTS = next(r for r in POLICY["rules"] if r["name"] == "registered-grants")["condition"]["value"]
AGENTS = sorted({grant.split(":")[0] for grant in GRANTS})

_spans, _logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
telemetry.setup()
telemetry.add_span_processor(SimpleSpanProcessor(_spans))
get_logger_provider().add_log_record_processor(SimpleLogRecordProcessor(_logs))


@pytest.fixture(autouse=True)
def isolated():
    storage.override_db_path(":memory:")
    governance.reload_policy()
    _spans.clear()
    _logs.clear()
    yield
    governance.reload_policy()


def _decide(role, tool):
    return governance.evaluate_tool_call(role=role, tool=tool, access=TOOLS[tool].access if tool in TOOLS else "unknown")


# ── The policy ───────────────────────────────────────────────────────────────

def test_the_policy_is_a_toolkit_policy_that_denies_by_default_and_uses_rules_not_an_allowlist():
    from agent_os.policies import PolicyDocument

    document = PolicyDocument.model_validate(POLICY)
    assert (document.name, document.defaults.action.value) == ("sentinel-tool-use", "deny")
    assert document.tool_allowlist == [], "PolicyEvaluator ignores tool_allowlist (tests/test_agt_known_bugs.py)"


@pytest.mark.parametrize("grant", GRANTS)
def test_every_registered_grant_is_allowed_and_audited(grant):
    role, tool = grant.split(":")
    decision = _decide(role, tool)
    assert (decision.allowed, decision.action, decision.rule) == (True, "audit", "registered-grants")


def test_every_grant_names_a_known_tool_and_every_prompt_tool_is_registered():
    assert {grant.split(":")[1] for grant in GRANTS} <= set(TOOLS)
    prompt_tools = {yaml.safe_load(p.read_text(encoding="utf-8"))["tool"]["name"]
                    for p in (REPO / "prompts").glob("*.yaml")}
    assert prompt_tools <= {name for name, tool in TOOLS.items() if tool.access == "respond"}


@pytest.mark.parametrize("role, tool", [
    ("planner", "submit_critique"),              # another agent's output tool
    ("critic", "read_operating_picture"),        # another agent's reads
    ("feed_diagnostician", "collect_evidence"),  # specialists answer on the evidence they are given
    ("critic", "run_shell"),                     # a tool nobody registered
])
def test_anything_not_granted_is_refused(role, tool):
    decision = _decide(role, tool)
    assert (decision.allowed, decision.rule) == (False, None)


@pytest.mark.parametrize("role", AGENTS)
@pytest.mark.parametrize("tool, action, rule", [
    ("decide_staged_action", "block", "agents-never-decide-or-write"),
    ("stage_for_human_review", "deny", "escalation-belongs-to-the-rules"),
])
def test_no_agent_decides_or_escalates(role, tool, action, rule):
    decision = _decide(role, tool)
    assert (decision.allowed, decision.action, decision.rule) == (False, action, rule)


def test_a_tool_call_on_nobodys_behalf_is_refused():
    assert _decide(None, "read_alert_context").rule == "agents-must-identify-themselves"


@pytest.mark.parametrize("content", [
    None,
    "name: open\ndefaults: {action: allow}\nrules: []\n",
    "name: allowlist\ndefaults: {action: deny}\ntool_allowlist: [read_alert_context]\nrules: []\n",
])
def test_an_unusable_tool_policy_refuses_every_tool(monkeypatch, tmp_path, content):
    path = tmp_path / "tool-use.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(settings, "TOOL_POLICY_PATH", path)
    governance.reload_policy()
    decision = _decide("critic", "read_alert_context")
    assert decision.allowed is False and decision.policy is None


# ── The boundary in the running system ───────────────────────────────────────

async def test_a_governed_read_runs_inside_an_execute_tool_span():
    async def read() -> str:
        return "context"

    with acting_as("critic"):
        assert await agent_tools.invoke("read_alert_context", read) == "context"
    [span] = [s for s in _spans.get_finished_spans() if s.name == "execute_tool read_alert_context"]
    attrs = dict(span.attributes)
    assert attrs[telemetry.OPERATION] == "execute_tool"
    assert (attrs[telemetry.TOOL_NAME], attrs[telemetry.AGENT_NAME]) == ("read_alert_context", "critic")
    assert (attrs[telemetry.AGT_DECISION], attrs[telemetry.AGT_RULE]) == ("audit", "registered-grants")


async def test_a_refused_read_never_runs():
    ran = []

    async def read():
        ran.append(True)

    with acting_as("planner"), pytest.raises(ToolCallDenied):
        await agent_tools.invoke("read_alert_context", read)
    assert ran == []


def _supervisor() -> Principal:
    return Principal(principal_id="jsmith", role="Watch Officer", source=PrincipalSource.PASSWORD_GRANT,
                     roles=frozenset({"watch_officer", "supervisor"}))


async def _staged_action():
    from src.execution_gate import stage_critical_alert

    alert = AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code="KDCA",
                       service_name="ISMC", description="test")
    await storage.save_alerts([alert])
    return await stage_critical_alert(alert)       # the alert rules, not an agent: allowed


async def test_an_agent_cannot_decide_even_with_a_supervisors_credentials():
    from src.execution_gate import decide_staged_action

    action = await _staged_action()
    audit_before = await storage.get_action_audit_log(action_id=action.action_id)
    with acting_as("critic"), pytest.raises(ToolCallDenied, match="agents-never-decide-or-write"):
        await decide_staged_action(action.action_id, Decision.APPROVE, _supervisor(), "from an agent")
    assert [a.action_id for a in await storage.get_pending_staged_actions()] == [action.action_id]
    assert await storage.get_action_audit_log(action_id=action.action_id) == audit_before

    # The same person, deciding as themselves, is the human gate working as designed.
    result = await decide_staged_action(action.action_id, Decision.APPROVE, _supervisor(), "reviewed")
    assert result.status == "APPROVED"


async def test_an_agent_cannot_stage_an_action():
    from src.execution_gate import stage_critical_alert

    alert = AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code="KBWI",
                       service_name="ISMC", description="test")
    await storage.save_alerts([alert])
    with acting_as("watch_officer"), pytest.raises(ToolCallDenied, match="escalation-belongs-to-the-rules"):
        await stage_critical_alert(alert)
    assert await storage.get_pending_staged_actions() == []


async def test_a_model_is_never_handed_another_agents_tool(monkeypatch):
    import src.llm_client as llm

    async def must_not_be_called(**kwargs):
        raise AssertionError("a refused call reached the model")

    monkeypatch.setattr(llm.litellm, "acompletion", must_not_be_called)
    client = llm.LiteLLMClient(model="openai/primary-model")
    with pytest.raises(llm.LLMPolicyDenied, match="tool-use policy"):
        await client.acall_with_tool("sys", {"name": "submit_operational_brief", "input_schema": {}}, "user",
                                     role="critic")
    from datetime import datetime, timezone

    [row] = await storage.get_model_calls_since(datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert (row["outcome"], row["policy_action"], row["attempts"]) == ("policy_denied", "deny", 0)


async def test_evidence_collection_is_governed_and_a_refusal_fails_the_investigation_closed(monkeypatch, tmp_path):
    from src.investigation.workflow import run_investigation
    from src.models import StagedAction

    deny_all = tmp_path / "deny-all.yaml"
    deny_all.write_text("name: deny-all\ndefaults: {action: deny}\nrules: []\n", encoding="utf-8")
    monkeypatch.setattr(settings, "TOOL_POLICY_PATH", deny_all)
    governance.reload_policy()

    alert = AlertEvent(alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", airport_code="KDCA",
                       service_name="ISMC", description="test")
    inv = await run_investigation(StagedAction(alert_id=alert.alert_id), alert, use_llm=False)
    assert inv.status == "FAILED" and inv.verify()
    [step] = inv.steps
    assert (step.step, step.outcome) == ("collect_evidence", "error") and "ToolCallDenied" in step.detail


def test_every_decision_is_written_to_agts_audit_trail_through_otel():
    _decide("critic", "decide_staged_action")
    records = [r.log_record for r in _logs.get_finished_logs()]
    attrs = dict(records[-1].attributes)
    assert (attrs["agt.agent.id"], attrs["agt.audit.action"], attrs["agt.audit.decision"]) == (
        "critic", "tool_call", "block")
    assert (attrs["agt.audit.meta.tool"], attrs["agt.audit.meta.rule"]) == (
        "decide_staged_action", "agents-never-decide-or-write")
