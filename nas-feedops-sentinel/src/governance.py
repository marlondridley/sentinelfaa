"""
Agent governance, enforced with the Microsoft Agent Governance Toolkit.

Two policies, both data rather than code, both evaluated by agent-os-kernel's
PolicyEvaluator (first match by priority wins, default deny, fail closed on
an evaluation error):

  policies/model-use.yaml  every model call: which role, which gateway alias,
                           what limits. Asked first by
                           LiteLLMClient.acall_with_tool() (evaluate_model_call).
  policies/tool-use.yaml   every tool an agent invokes: the output tool it
                           hands a model, and each read that builds its
                           context or evidence. Asked by src/agent_tools.py
                           (evaluate_tool_call). Agents never decide, write
                           or escalate; the human gate stays at escalation.

A refused call never leaves the process; the caller falls back to its
deterministic rules exactly as it does for any model failure.

Every decision is written to the toolkit's own audit trail -- AGT's
GovernanceAuditLogger with its OpenTelemetry backend (OTelLogsBackend), so
decisions reach any OTLP collector as log records under AGT's agt.* names,
correlated with the call's trace -- and every refusal counts on AGT's
agent.policy.violations metric (src/telemetry.py). A model call's decision is
also on its chat span and in its model_calls row.

This module adds three guarantees the toolkit leaves to its host:

* Every context field is always present. The toolkit's matcher treats a
  missing field as "no match", so a deny rule on an absent field would
  silently never fire.
* A policy that cannot be loaded refuses every call. A missing or malformed
  policy file must not mean an ungoverned model -- including one the toolkit
  itself would load as allow-everything (see _load()).
* A policy cannot rely on a field the evaluator ignores: tool_allowlist is
  documented as enforced by PolicyEvaluator and is not, so a policy that sets
  it is refused rather than trusted.

Why agent-os-kernel and not the toolkit's newer native runtime: its package
(agent-control-specification) ships prebuilt wheels for x86_64 Linux only,
and this stack's Docker host is arm64.
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from src import telemetry
from src.config import settings
from src.logging_config import get_logger

with warnings.catch_warnings():
    # agent_os's own pydantic models use the deprecated json_encoders option;
    # the warning concerns the library, not anything this module controls.
    warnings.simplefilter("ignore", DeprecationWarning)
    from agent_os.audit_logger import GovernanceAuditLogger
    from agent_os.credential_redactor import CredentialRedactor
    from agent_os.otel_audit_backend import OTelLogsBackend
    from agent_os.policies import PolicyDocument, PolicyEvaluator

_log = get_logger(__name__)

# Roles that do not identify themselves are evaluated as this, which no rule
# allows.
UNREGISTERED_ROLE = "unregistered"


@dataclass(frozen=True)
class GovernanceDecision:
    allowed: bool
    action: str            # audit | allow | deny | block
    rule: str | None       # the rule that decided; None for the default or a load failure
    reason: str
    policy: str | None     # policy document name; None when it could not be loaded


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[PolicyEvaluator | None, str | None, str | None]:
    """
    (evaluator, policy name, load error). Cached per path; see reload_policy().

    The toolkit's PolicyDocument ignores unknown keys and defaults to allow
    when `defaults` is absent, so a file with a typo (`rule:` for `rules:`)
    or no defaults loads as allow-everything. Here an unknown key is an error
    and a policy that is not deny-by-default is refused. So is one that sets
    tool_allowlist: PolicyEvaluator never reads it, so it restricts nothing.
    (tests/test_agt_known_bugs.py shows all three.)
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ValueError("the policy file is not a mapping")
        unknown = sorted(set(raw) - set(PolicyDocument.model_fields))
        if unknown:
            raise ValueError(f"unknown policy key(s) {unknown}")
        document = PolicyDocument.model_validate(raw)
        if document.defaults.action.value != "deny":
            raise ValueError(
                f"the default action is {document.defaults.action.value!r}; the policy must be deny-by-default"
            )
        if document.tool_allowlist:
            raise ValueError(
                "tool_allowlist is not enforced by the toolkit's PolicyEvaluator; express tool access as rules"
            )
    except Exception as exc:  # noqa: BLE001 -- any failure to load means refuse
        return None, None, f"{type(exc).__name__}: {exc}"
    return PolicyEvaluator(policies=[document]), document.name, None


@lru_cache(maxsize=1)
def audit_logger() -> GovernanceAuditLogger:
    """AGT's audit logger, writing OpenTelemetry log records through the installed logger provider."""
    telemetry.setup()   # OTelLogsBackend binds to the logger provider when it is built
    audit = GovernanceAuditLogger()
    audit.add_backend(OTelLogsBackend(service_name=telemetry.SERVICE_NAME))
    return audit


def reload_policy() -> None:
    """Drop the cached policy; the next call reads the file again."""
    _load.cache_clear()


def build_context(
    *, role: str | None, model: str | None, prompt: str, max_tokens: int, redacted: bool,
) -> dict[str, Any]:
    """The evaluation context. Every field is always present and never None."""
    return {
        "action": "model_call",
        "role": role or UNREGISTERED_ROLE,
        "model": model or "",
        "prompt_chars": len(prompt),
        "max_tokens": int(max_tokens),
        "redacted": bool(redacted),
        "credentials_found": len(CredentialRedactor.find_matches(prompt)),
    }


def evaluate_model_call(
    *, role: str | None, model: str | None, prompt: str, max_tokens: int, redacted: bool,
) -> GovernanceDecision:
    """Ask the model-use policy whether this call may be sent."""
    context = build_context(
        role=role, model=model, prompt=prompt, max_tokens=max_tokens, redacted=redacted,
    )
    decision = _evaluate(settings.MODEL_POLICY_PATH, context, what="Model-use")
    if not decision.allowed:
        # The context carries sizes and flags only -- never the prompt.
        _log.warning(
            "model_call_refused_by_policy",
            extra={"role": context["role"], "rule": decision.rule, "reason": decision.reason,
                   "model": context["model"], "prompt_chars": context["prompt_chars"],
                   "credentials_found": context["credentials_found"]},
        )
    return decision


def build_tool_context(*, role: str | None, tool: str, access: str) -> dict[str, Any]:
    """The tool-use evaluation context. Every field is always present and never None."""
    role = role or UNREGISTERED_ROLE
    return {"action": "tool_call", "role": role, "tool": tool, "access": access, "grant": f"{role}:{tool}"}


def evaluate_tool_call(*, role: str | None, tool: str, access: str) -> GovernanceDecision:
    """Ask the tool-use policy whether this agent may invoke this tool."""
    context = build_tool_context(role=role, tool=tool, access=access)
    decision = _evaluate(settings.TOOL_POLICY_PATH, context, what="Tool-use")
    if not decision.allowed:
        _log.warning("tool_call_refused_by_policy", extra={
            "role": context["role"], "tool": tool, "access": access,
            "rule": decision.rule, "reason": decision.reason,
        })
    return decision


def _evaluate(path_setting: Any, context: dict[str, Any], *, what: str) -> GovernanceDecision:
    path = str(path_setting)
    started = time.perf_counter()
    evaluator, policy_name, load_error = _load(path)
    if evaluator is None:
        decision = GovernanceDecision(
            allowed=False, action="deny", rule=None, policy=None,
            reason=f"{what} policy unavailable, so the call is refused: {load_error}",
        )
        _log.error("policy_unavailable", extra={"policy": what, "path": path, "error": load_error})
    else:
        result = evaluator.evaluate(context)
        decision = GovernanceDecision(
            allowed=result.allowed, action=result.action, rule=result.matched_rule,
            reason=result.reason, policy=policy_name,
        )
    _audit(decision, context, latency_ms=(time.perf_counter() - started) * 1000)
    return decision


def _audit(decision: GovernanceDecision, context: dict[str, Any], *, latency_ms: float) -> None:
    """Write the decision to AGT's audit trail and count it. Never changes the decision."""
    details = {k: v for k, v in context.items() if k not in ("action", "role")}
    try:
        telemetry.record_policy_decision(context["role"], decision.action, decision.rule,
                                         allowed=decision.allowed, action=context["action"])
        audit_logger().log_decision(
            agent_id=context["role"], action=context["action"], decision=decision.action,
            reason=decision.reason, latency_ms=round(latency_ms, 3),
            rule=decision.rule or "(default)", policy=decision.policy or "(unavailable)", **details,
        )
    except Exception as exc:  # noqa: BLE001 -- the durable record is the model_calls row
        _log.warning("policy_audit_failed", extra={"error": str(exc)})
