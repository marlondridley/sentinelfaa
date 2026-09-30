"""
Seeded, varied inputs for src/evaluation.py: investigations of every shape the grounding gate, the audit log and
the gateway can produce, and the calls, actions, verdicts and budgets around them.

Not a test module. tests/test_evaluation_golden.py runs the evaluation over these and compares the result with a
snapshot taken before evaluation.py was split into smaller functions, so the split is proved to change nothing:
not a gate's status or wording, not a metric's value or its source.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from src import agt_metrics as agt
from src.evaluation import GateConfig
from src.investigation.record import (
    Brief, Challenge, Claim, Critique, Disagreement, Evidence, Finding, Investigation, Provenance, RejectedClaim,
    StepRecord, VerificationStep,
)

T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
_AGENTS = ("feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer")
_OUTCOMES = ("ok", "ok", "ok", "repaired", "incomplete", "invalid_output", "timeout", "rate_limited",
             "provider_error", "policy_denied")
_STATUSES = (None, "PENDING", "APPROVED", "APPROVED", "REJECTED", "REJECTED", "OVERRIDDEN", "EXPIRED", "MERGED")
_ACTORS = ("jsmith [oidc]", "ann [keycloak-password]", "bot [api_key]", "typed callsign", "system", "")
_VERIFY_STEP_REJECTION = "verification step without valid evidence"


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _provenance(rng: random.Random, method: str | None = None) -> Provenance:
    method = method or rng.choice(("llm", "llm", "deterministic"))
    if method == "deterministic":
        return Provenance(method="deterministic", fallback_reason=rng.choice((None, "model unavailable")))
    complete = rng.random() < 0.7
    return Provenance(method="llm", model="deepseek-chat" if complete or rng.random() < 0.5 else None,
                      prompt_name="p", prompt_version="1" if complete else None,
                      prompt_sha256="a" * 64 if complete else None, input_sha256="b" * 64 if complete else None,
                      latency_ms=rng.uniform(100, 5000))


def _claims(rng: random.Random, prefix: str, evidence_ids: list[str], n: int) -> list[Claim]:
    claims = []
    for k in range(n):
        cited = [rng.choice(evidence_ids)] if rng.random() < 0.9 else rng.choice(([], ["never-collected"]))
        claims.append(Claim(claim_id=f"{prefix}-{k}", statement=f"{prefix} statement {k}", evidence_ids=cited))
    return claims


def _rejected(rng: random.Random, n: int, verify_step: bool = False) -> list[RejectedClaim]:
    return [RejectedClaim(statement=f"rejected {k}", cited=["x"],
                          reason=_VERIFY_STEP_REJECTION if verify_step and rng.random() < 0.5 else "no valid evidence")
            for k in range(n)]


def make_investigation(rng: random.Random, index: int) -> Investigation:
    status = rng.choice(("COMPLETE", "COMPLETE", "COMPLETE", "COMPLETE", "FAILED", "OPEN"))
    opened = rng.uniform(0, 600)
    evidence = [Evidence(evidence_id=f"e{k}", kind="feed_heartbeat", subject="KDCA / ISMC", source="db",
                         observed_at=_at(opened), data={"k": k}) for k in range(rng.randint(1, 4))]
    ids = [e.evidence_id for e in evidence]

    steps, cursor = [], opened
    for name in ("collect_evidence", *rng.sample(_AGENTS, rng.randint(0, 4)), "seal"):
        if name == "seal" and rng.random() < 0.2:
            continue
        agent = name if name in _AGENTS else None
        started = cursor + rng.uniform(0, 3) if name in ("feed_diagnostician", "correlation_analyst") and steps \
            else cursor
        finished = started + rng.uniform(0.1, 30)
        cursor = finished if name not in ("feed_diagnostician",) else cursor
        steps.append(StepRecord(
            step=name, agent=agent, started_at=_at(started), finished_at=_at(finished),
            outcome=rng.choice(("ok", "ok", "ok", "fallback", "error")),
            detail=rng.choice(("", "deterministic", "deterministic", "timed out", "model unavailable"))))
    present = {s.step for s in steps}

    findings = [Finding(finding_id=f"f-{a}", agent=a, question="q", conclusion="C", summary="s",
                        claims=_claims(rng, a, ids, rng.randint(0, 3)), rejected_claims=_rejected(rng, rng.randint(0, 2)),
                        provenance=_provenance(rng))
                for a in ("feed_diagnostician", "correlation_analyst") if a in present and rng.random() < 0.85]

    critique = None
    challenges: list[Challenge] = []
    if "red_team_critic" in present and rng.random() < 0.85:
        for k in range(rng.randint(0, 4)):
            challenges.append(Challenge(
                challenge_id=f"c{k}", kind=rng.choice(("UNSUPPORTED_CLAIM", "CONTRADICTING_EVIDENCE",
                                                       "ALTERNATIVE_EXPLANATION", "MISSING_EVIDENCE")),
                target=None, statement=f"challenge {k}", evidence_ids=[], origin=rng.choice(("rule", "llm"))))
        alt = _claims(rng, "alt", ids, 1)[0] if rng.random() < 0.6 else None
        if alt is not None and findings and rng.random() < 0.3:
            alt = alt.model_copy(update={"statement": findings[0].claims[0].statement if findings[0].claims else alt.statement})
        critique = Critique(strongest_alternative=alt, challenges=challenges,
                            rejected_claims=_rejected(rng, rng.randint(0, 2)), provenance=_provenance(rng))

    brief = None
    if "watch_officer" in present and rng.random() < 0.85:
        established = _claims(rng, "est", ids, rng.randint(0, 3))
        finding_claims = [c for f in findings for c in f.claims]
        if finding_claims and rng.random() < 0.5:
            established.append(rng.choice(finding_claims))            # a verbatim copy of a finding's claim
        open_ids = [c.challenge_id for c in challenges]
        if open_ids and rng.random() < 0.25:
            open_ids.pop()                                            # a challenge dropped from the brief
        brief = Brief(situation="s", bottom_line="b", established=established,
                      disagreements=[Disagreement(statement="d", positions={}, evidence_ids=[])
                                     for _ in range(rng.randint(0, 2))],
                      open_challenges=open_ids,
                      verify_next=[VerificationStep(action="a", why="w", evidence_ids=[])],
                      limitations=[], rejected_claims=_rejected(rng, rng.randint(0, 3), verify_step=True),
                      provenance=_provenance(rng))

    completed = None if status == "OPEN" else _at(max(cursor, opened) + rng.uniform(0, 5))
    trigger: dict[str, Any] = rng.choice((
        {}, {"alert_type": "ALERT_LOW_RVR", "airport_code": "KBWI",
             "triggered_at": _at(opened - rng.uniform(0, 90)).isoformat()},
        {"alert_type": "ALERT_FEED_DISCONNECTED", "airport_code": "KDCA",
         "triggered_at": _at(opened - rng.uniform(0, 90)).isoformat().replace("+00:00", "Z")},
        {"alert_type": "ALERT_ISMC_LINK_DEGRADED", "triggered_at": "not a time"}))
    inv = Investigation(
        investigation_id=str(uuid.UUID(int=rng.getrandbits(128))), action_id=f"act-{index}",
        alert_id=None if rng.random() < 0.15 else f"alert-{index}", trigger=trigger, status=status,
        opened_at=_at(opened), completed_at=completed, evidence=evidence, findings=findings, critique=critique,
        brief=brief, steps=steps)
    inv.record_sha256 = inv.compute_sha256() if rng.random() < 0.9 else "0" * 64
    return inv


def make_calls(rng: random.Random, subject: str | None, n: int | None = None) -> list[dict]:
    rows = []
    for k in range(rng.randint(0, 5) if n is None else n):
        outcome = rng.choice(_OUTCOMES)
        rows.append({
            "call_id": f"{subject}-{k}-{rng.getrandbits(24)}", "subject": subject,
            "role": rng.choice(("critic", "planner", "watch_officer", "feed_diagnostician", "red_team_critic")),
            "outcome": outcome, "attempts": rng.randint(0, 4), "repaired": rng.random() < 0.2,
            "prompt_tokens": rng.choice((None, 0, rng.randint(100, 4000))),
            "completion_tokens": rng.choice((None, 0, rng.randint(10, 800))),
            "cost_usd": rng.choice((None, 0.0, round(rng.uniform(0.0001, 0.02), 6))),
            "served_model": rng.choice((None, "deepseek-chat", "gemini-2.5-flash")),
            "provider": rng.choice((None, "deepseek", "google")),
            "policy_action": rng.choice((None, "audit", "audit", "allow", "unevaluated", "deny")),
            "latency_ms": rng.uniform(50, 30000),
        })
    return rows


def make_action(rng: random.Random):
    status = rng.choice(_STATUSES)
    if status is None:
        return None
    decided = rng.choice((None, _at(rng.uniform(600, 3000)), _at(rng.uniform(600, 3000)).isoformat(), "bad"))
    return SimpleNamespace(status=status, decided_by=rng.choice(_ACTORS), decided_at=decided,
                           critic_confidence=rng.choice((None, 0.4, 0.9)))


def make_audit(rng: random.Random, action) -> list[dict]:
    if rng.random() < 0.1:
        return []
    rows = [{"actor": rng.choice(("system", "system", "jsmith [oidc]")),
             "new_status": rng.choice(("PENDING", "PENDING", "EXPIRED", "APPROVED"))}]
    for _ in range(rng.randint(0, 3)):
        rows.append({"actor": rng.choice(("system", "jsmith [oidc]", "")),
                     "new_status": rng.choice(("MERGED", "APPROVED", "REJECTED", "PENDING", "OVERRIDDEN"))})
    if action is not None and rng.random() < 0.7:
        rows.append({"actor": "jsmith [oidc]", "new_status": action.status})
    return rows


def make_verdict(rng: random.Random) -> dict | None:
    if rng.random() < 0.25:
        return None
    return {"confidence": rng.choice((None, 0.3, 0.6, 0.95)), "escalate": rng.choice((True, False, None)),
            "action_status": rng.choice(_STATUSES[1:]), "alert_id": "x"}


def make_config(rng: random.Random) -> GateConfig:
    return GateConfig(
        budget=agt.Budget(max_cost_usd=rng.choice((None, 0.001, 0.05)), max_tokens=rng.choice((None, 2000, 100000)),
                          max_tool_calls=rng.choice((None, 2, 10)), max_duration_seconds=rng.choice((None, 5.0, 300.0))),
        max_retries=rng.choice((0, 1, 3)), max_depth=rng.choice((1, 2, 3)))


def make_case(seed: int) -> dict[str, Any]:
    """One investigation with everything `evaluate_investigation` is given about it."""
    rng = random.Random(seed)
    inv = make_investigation(rng, seed)
    action = make_action(rng)
    return {
        "inv": inv, "calls": make_calls(rng, inv.investigation_id),
        "critic_calls": make_calls(rng, inv.alert_id) if inv.alert_id and rng.random() < 0.7 else [],
        "action": action, "audit": make_audit(rng, action),
        "verdict": make_verdict(rng) if inv.alert_id else None, "config": make_config(rng),
    }
