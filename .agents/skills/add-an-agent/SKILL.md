---
name: add-an-agent
description: Add a new advisory agent to Sentinel, or change an existing agent's prompt, tools, model or switch, without breaking the agent register. Use when asked to add, rename, re-prompt, re-grant or remove an agent, specialist or model-calling role, or when test_agent_register, test_tool_boundary or test_governance fail after such a change. Covers the register entry, the model-use and tool-use grants, the versioned prompt, the settings and compose switch, the code path every model call must take, and the tests and docs to update.
compatibility: Applies to nas-feedops-sentinel (Python, with policies/ and prompts/ treated as code). Tests run in Docker, see the ci-like-test-run skill.
metadata:
  area: agents
---

# Add an agent

An agent here is a **role string** (its `id`) that five things agree on: a prompt, a register entry, the model-use policy, the tool-use policy, and the code that calls `acting_as("<id>")`. `tests/test_agent_register.py` holds them together, so an agent cannot be added, granted a tool, re-prompted or renamed in one place without the others changing in the same pull request. That test is your to-do list.

## What does not change

- **Every agent is advisory.** It reads and it responds. It never decides, stages, writes or executes. The register's `authority` fields can only be `false` (the loader refuses anything else), and `policies/tool-use.yaml` blocks those accesses. The human gate is `src/execution_gate.py`. If the request is for an agent that acts, stop and tell the owner it contradicts the design (`docs/adr/0001-rules-decide-models-explain.md`).
- **Rules decide, models explain.** Counts, posture and verdicts are computed by rules. A model writes text around them. So every agent has a deterministic fallback for when the model is off, halted, refused or wrong.
- **Prompts, policies and the register are review-gated** (`CODEOWNERS`). Changing a register entry is a review: bump `last_reviewed` with the change.
- **No new gateway alias.** `policies/model-use.yaml` allows only the alias in its `gateway-aliases-only` rule. Do not add one without the owner.
- **An agent has no credential of its own.** It runs inside a service and uses that service's identity, so the role string is only as strong as the code that sets it. Do not claim otherwise in the register.

## Order of work

1. **Decide the contract before code:** the `id` (lowercase letters, digits, underscores), purpose, whether it calls a model, what it reads, what it responds with, what data it is sent, its risk tier, and its fallback. Risk tier: `high` if a person reads its output before deciding on a staged action, `moderate` for advisory text with no decision record behind it, `low` if it is deterministic.
2. **Write the register entry first** (step 7 below), then run `tests/test_agent_register.py`. Its failures name what is still missing; work through them.
3. Do steps 2 to 9 in whatever order the failures lead you. Finish with the full run.

## The checklist

### 1. Prompt: `prompts/<name>.yaml`

Keys: `name`, `version` (a string, for example `"1.0"`), `description`, `system_prompt`, and `tool` with `name`, `description` and `input_schema`. The tool is the structured output the model is forced to return. Load it only through `src/prompts_loader.py` (`system_prompt`, `tool_schema`, `prompt_version`): `system_prompt()` appends the note on what `<untrusted_data>` means, and `tests/test_untrusted.py` requires every prompt to carry it. Every prompt file must belong to a registered agent.

### 2. Tools: `src/agent_tools.py`

Add a `Tool(name, access, description)` to `TOOLS` for each new read (`"read"`) and for the prompt's output tool (`"respond"`). Never `decide`, `write`, `execute` or `escalate`. The prompt's tool name must be a registered `respond` tool.

### 3. Settings: `src/config.py`

- `<ID>_ENABLED: bool = True`, **and add it to the `_parse_bool` validator's list**, or `"0"` will not turn it off.
- `<ID>_MODEL: str = ""`, defaulted to `LLM_MODEL` in `_defaults`. A specialist inside an investigation reuses `INVESTIGATION_MODEL` and `INVESTIGATIONS_ENABLED` instead, and its entry says so in `disable.note`.

### 4. Switch in deployment

- `nas-feedops-sentinel/docker-compose.yml`: forward `<ID>_ENABLED: ${<ID>_ENABLED:-1}` to **every service the entry lists in `disable.restart`**. The test requires exactly that `${NAME:-default}` form, with a default that is not the off value. Forward the model setting too if `.env` should be able to change it (the test does not require it).
- `nas-feedops-sentinel/.env.example`: add the switch with a one-line comment.
- `CRITIC_ENABLED=0` also stops every live agent. If your agent builds its client behind `settings.CRITIC_ENABLED and settings.<ID>_ENABLED`, say so in `disable.note`.

### 5. Code: the path every model call must take

Standalone agent: copy the shape of `src/planner_agent.py`. Investigation specialist: a class in `src/investigation/specialists.py` (see the end of this file). In both:

- Wrap the entry point in `with agent_tools.acting_as("<id>")`, with **the id as a string literal**. The test finds agents by walking the source for literals.
- Make reads through `agent_tools.invoke("<tool>", fn)` so the tool-use policy is asked first.
- Build the model's input from **data, not instructions**. Wrap anything typed by a person, written by a model or taken from a feed in `fence()` or `fence_fields()` (`src/untrusted.py`). Pass structured context through `pseudonymize_structured(..., Pseudonymizer())`, and pass `collect_sensitive_values(...)` as `protected_values` (`src/redaction.py`) so the call is refused if a protected value is still in the prompt.
- Reach the model **only** through `build_llm_client(model=settings.<ID>_MODEL).acall_with_tool(..., role="<id>", prompt_version=prompt_version("<name>"), protected_values=...)`. That one function applies redaction, the model-use policy, the supervisor halt, the budget and telemetry. Never call a provider SDK or an HTTP endpoint directly.
- On any model failure, **fall back to the deterministic result** and mark it as the fallback with its reason. Do not raise to the user. Check the model's output before showing it (the Planner uses `OutputGuardrail`).

### 6. Policies

- `policies/model-use.yaml`, rule `advisory-roles`: add the `id` to its `value` list, if the agent calls a model.
- `policies/tool-use.yaml`, rule `registered-grants`: add `<id>:<tool>` for each read and each response. Leave every other rule alone; the block and deny rules above it are the boundary.

### 7. Register: `policies/agent-register.yaml`

One entry with every field (unknown or missing keys fail to load): `id`, `name`, `purpose`, `calls_model`, `model_settings`, `prompts` (`name` and a `version` equal to the prompt file's), `risk_tier`, `risk_rationale`, `owner`, `last_reviewed` (a date), `runs_in`, `reads`, `responds`, `data_sent`, `fallback`, `authority` (all `false`) and `disable` (`setting`, `value: "0"`, `restart`, `note`).

Rules the tests enforce: `calls_model` is true exactly when `model_settings`, `prompts` and `responds` are all non-empty; `reads` and `responds` match the `TOOLS` access; `disable.restart` is a subset of `runs_in`; every service named exists in compose. Add any new test file to the register's `evidence` list; each path must exist.

### 8. Tests to extend

The register test enforces consistency, but **several lists are written out by hand and nothing reminds you**. Update each:

| File | What to add |
|---|---|
| `tests/test_agent_register.py` | `MODEL_SETTING_READERS` (which module reads `settings.<X>_MODEL`); the `len(REGISTER.agents) == 7` and `"7 agents"` assertions; the `literal` set if the agent is named by a literal |
| `tests/test_governance.py` | the role list for "registered advisory roles are allowed and audited" |
| `tests/test_model_halt.py` | the role list for "a halt stops every role" |
| `tests/test_untrusted.py` | the prompt name list for "every system prompt carries the note" |
| `tests/test_agent_switches.py` | a test that switching it off builds no client and asks no model |
| `tests/test_tool_boundary.py` | grants are read from the policy; add a refused case if another agent's tool is tempting |

Add the agent's own tests, including the fallback path. Tests that need a database use the disposable one, never the live stack (see the `real-data-only` skill).

### 9. Prose that no test checks

`nas-feedops-sentinel/docs/agent-governance.md` states the agent count and names ("Seven principals act in the advisory layer", "all four investigation specialists"). The module list in `nas-feedops-sentinel/README.md` names the agent files. Search for the count words and the names with `git grep` and update them.

## Changing an agent that exists

- **A prompt's text changes:** bump `version` in the prompt file, the matching `prompts[].version` in the register, and `last_reviewed`, in the same change. The test compares version strings only, so **it cannot catch a text change without a version bump**. That discipline is yours.
- **A tool is added or removed:** `TOOLS` in `src/agent_tools.py`, the grants in `policies/tool-use.yaml` and the register's `reads` or `responds`, together.
- **Rename or remove:** the `id` is in the register, both policies, the `acting_as` literal, the test lists above, the prose, and for a specialist the investigation record. Run `git grep` for the old id and expect to touch most of them.

## Investigation specialist: what else changes

A specialist is a class in `src/investigation/specialists.py` with `name`, `question`, `conclusions`, `prompt`, `deterministic(view)` and `async run(view, use_llm)`. It asks the model through `ask_model(prompt, name, payload)` and checks every claim cites evidence with `ground(...)`. `src/investigation/workflow.py` runs it under `acting_as(spec.name)`, the one place the tests allow a variable there.

Beyond the checklist, find and update the places that list the specialists: `AgentName` in `src/investigation/record.py`, `AGENT_STEPS` and `PARALLEL` in `src/evaluation_types.py`, the stage lists in `src/evaluation.py` and `src/evaluation_metrics.py`, the wiring in `src/investigation/workflow.py`, and the evaluation fixtures (`tests/evaluation_cases.py`, `tests/fixtures/evaluation_golden.json`, `tests/test_evaluation_evidence.py`). An evaluation golden file changing is a deliberate, reviewed decision: understand why before you update it, and never edit it only to make a test pass.

## Verify

Run, in the test image (the `ci-like-test-run` skill shows how): `test_agent_register`, `test_governance`, `test_tool_boundary`, `test_untrusted`, `test_model_halt`, `test_agent_switches`, then the full suite and ruff. Run `python -m src.agent_register` and read your agent's row.

## Report

List the files touched by group (prompt, tools, settings, compose, code, policies, register, tests, prose), the tests that ran with their summary line, and the register row. Say what was not exercised, in particular any live model call.
