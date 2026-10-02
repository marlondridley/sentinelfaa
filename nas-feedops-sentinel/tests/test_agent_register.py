"""
The agent register (policies/agent-register.yaml) agrees with the policies, the prompts, the compose file and
the code. The register is an inventory; these tests are what make it a control: an agent cannot be added,
granted a tool, re-prompted or renamed in one place without the register changing in the same pull request.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src import agent_register as ar
from src.agent_tools import TOOLS
from src.config import Settings

APP = Path(__file__).resolve().parent.parent
REGISTER = ar.load_register()


def _policy(name: str) -> dict:
    return yaml.safe_load((APP / "policies" / name).read_text(encoding="utf-8"))


def _rule(policy: dict, name: str) -> dict:
    return next(r for r in policy["rules"] if r["name"] == name)


def _raw() -> dict:
    return copy.deepcopy(yaml.safe_load(ar.REGISTER_PATH.read_text(encoding="utf-8")))


# ── The register agrees with the policies ────────────────────────────────────────────────────────────

def test_the_agents_that_call_a_model_are_exactly_the_roles_the_model_policy_allows():
    allowed = set(_rule(_policy("model-use.yaml"), "advisory-roles")["condition"]["value"])
    registered = {a.id for a in REGISTER.agents if a.calls_model}

    assert registered == allowed


def test_every_registered_agent_is_granted_exactly_the_tools_the_register_lists():
    granted = set(_rule(_policy("tool-use.yaml"), "registered-grants")["condition"]["value"])
    registered = set().union(*(a.grants for a in REGISTER.agents))

    assert registered == granted, {"only_in_register": sorted(registered - granted),
                                   "only_in_policy": sorted(granted - registered)}


def test_every_agent_in_the_tool_policy_is_registered():
    roles = {g.split(":")[0] for g in _rule(_policy("tool-use.yaml"), "registered-grants")["condition"]["value"]}
    assert roles == set(REGISTER.by_id())


def test_an_agent_is_only_ever_granted_reads_and_responses():
    for agent in REGISTER.agents:
        for tool in agent.reads:
            assert TOOLS[tool].access == "read", f"{agent.id} lists {tool} as a read"
        for tool in agent.responds:
            assert TOOLS[tool].access == "respond", f"{agent.id} lists {tool} as a response"


def test_an_agent_that_calls_a_model_responds_and_one_that_does_not_has_nothing_to_respond_with():
    for agent in REGISTER.agents:
        assert bool(agent.responds) == agent.calls_model, agent.id
        assert (agent.model_setting is not None and bool(agent.prompts)) == agent.calls_model, agent.id


# ── ... with the prompts ─────────────────────────────────────────────────────────────────────────────

def _prompt_versions() -> dict[str, str]:
    return {p.stem: str(yaml.safe_load(p.read_text(encoding="utf-8"))["version"])
            for p in (APP / "prompts").glob("*.yaml")}


def test_every_prompt_belongs_to_a_registered_agent():
    registered = {p.name for a in REGISTER.agents for p in a.prompts}
    assert registered == set(_prompt_versions())


def test_a_prompt_changed_since_it_was_reviewed_fails_until_the_register_is_updated():
    versions = _prompt_versions()
    stale = [f"{a.id}: {p.name} is {versions[p.name]}, reviewed at {p.version}"
             for a in REGISTER.agents for p in a.prompts if versions[p.name] != p.version]
    assert not stale, "bump the prompt's entry and last_reviewed together:\n" + "\n".join(stale)


# ── ... with the settings and the deployment ─────────────────────────────────────────────────────────

def test_every_setting_the_register_names_exists():
    names = set(Settings.model_fields)
    named = {a.model_setting for a in REGISTER.agents if a.model_setting} | {a.disable.setting for a in REGISTER.agents}
    assert named <= names, sorted(named - names)


def test_every_service_the_register_names_is_in_the_compose_file():
    services = set(yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))["services"])
    named = {s for a in REGISTER.agents for s in (*a.runs_in, *a.disable.restart)}
    assert named <= services, sorted(named - services)


def test_an_agent_is_stopped_by_restarting_only_services_it_runs_in():
    for agent in REGISTER.agents:
        assert set(agent.disable.restart) <= set(agent.runs_in), agent.id


def test_every_piece_of_evidence_the_register_cites_exists():
    missing = [path for path in REGISTER.evidence if not (APP / path).is_file()]
    assert not missing, missing


# ── ... with the code ────────────────────────────────────────────────────────────────────────────────

def _acting_as_literals() -> set[str]:
    found: set[str] = set()
    for path in (APP / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "acting_as"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
    return found


def test_every_agent_the_code_acts_as_is_registered():
    acted = _acting_as_literals()
    assert acted, "the walk found no acting_as() calls: the test is no longer looking at anything"
    assert acted <= set(REGISTER.by_id()), sorted(acted - set(REGISTER.by_id()))


def test_every_registered_agent_that_the_code_names_literally_is_still_acted_as():
    # The specialists act under spec.name, a variable; the rest name themselves in a literal.
    literal = {"critic", "planner", "watch_officer", "investigation"}
    assert literal <= _acting_as_literals()


# ── The register itself ──────────────────────────────────────────────────────────────────────────────

def test_the_shipped_register_loads_and_no_agent_has_any_authority():
    assert len(REGISTER.agents) == 7
    for agent in REGISTER.agents:
        assert not any(agent.authority.model_dump().values()), agent.id


@pytest.mark.parametrize("mutate, message", [
    (lambda raw: raw["agents"][0]["authority"].update(decide=True), "decide"),
    (lambda raw: raw["agents"][0]["authority"].update(stage=True), "stage"),
    (lambda raw: raw["agents"][0].update(owner="  "), "owner"),
    (lambda raw: raw["agents"][0].update(risk_tier="critical"), "risk_tier"),
    (lambda raw: raw["agents"][0].update(last_reviewed="yesterday"), "last_reviewed"),
    (lambda raw: raw["agents"][0].update(unexpected="x"), "unexpected"),
    (lambda raw: raw["agents"][0].pop("fallback"), "fallback"),
    (lambda raw: raw["agents"][0].update(id="Bad Id"), "id"),
    (lambda raw: raw["agents"][0]["disable"].update(restart=[]), "restart"),
    (lambda raw: raw["agents"].append(copy.deepcopy(raw["agents"][0])), "registered twice"),
])
def test_a_register_that_breaks_a_rule_does_not_load(mutate, message):
    raw = _raw()
    mutate(raw)

    with pytest.raises(ValidationError, match=message):
        ar.Register.model_validate(raw)


def test_the_register_can_be_read_from_another_path(tmp_path):
    path = tmp_path / "register.yaml"
    path.write_text(ar.REGISTER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert ar.load_register(path).name == REGISTER.name


# ── The command line ─────────────────────────────────────────────────────────────────────────────────

def test_the_command_prints_one_row_per_agent_and_how_to_stop_each(capsys):
    assert ar.main([]) == 0
    out = capsys.readouterr().out

    assert "7 agents" in out and "No agent can decide, stage, write or execute" in out
    for agent in REGISTER.agents:
        assert agent.id in out
    assert "CRITIC_ENABLED='0'" in out


def test_the_command_can_print_json(capsys):
    assert ar.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert [a["id"] for a in data["agents"]] == [a.id for a in REGISTER.agents]
    assert data["agents"][0]["authority"] == {"decide": False, "stage": False, "write": False, "execute": False}


def test_a_register_that_cannot_be_read_is_exit_code_two(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ar, "REGISTER_PATH", tmp_path / "missing.yaml")

    assert ar.main([]) == 2
    assert "agent register:" in capsys.readouterr().err
