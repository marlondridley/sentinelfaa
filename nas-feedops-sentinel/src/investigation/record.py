"""
The Investigation record: the one piece of shared, structured state an
investigation produces.

Specialists never talk to each other. Each receives a read-only view of this
record, returns a typed result, and the orchestrator (workflow.py) is the only
code that writes into it. There are no conversational messages anywhere in
the flow -- only evidence, claims that cite evidence, challenges to those
claims, and a brief that cites both.

Traceability is the rule the whole model is built around:

  * Every Evidence item has a stable evidence_id (e.g. "heartbeat:SMES",
    "health:KDCA:ISMC") and records where it came from and when.
  * Every Claim cites at least one evidence_id. A claim whose citations are
    missing or point at evidence that was never collected does not enter the
    record as a claim: the grounding gate moves it to rejected_claims, where
    the Red-Team Critic turns it into an UNSUPPORTED_CLAIM challenge.
  * Investigation.trace() resolves any claim back to the evidence behind it.
  * The completed record is hashed (record_sha256) and stored append-only.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AgentName = Literal[
    "feed_diagnostician",
    "correlation_analyst",
    "red_team_critic",
    "watch_officer",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Evidence(BaseModel):
    """One observation, collected deterministically before any model runs."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    kind: str                      # feed_heartbeat, ingest_latency, related_alerts, ...
    subject: str                   # human label: "KDCA / ISMC"
    source: str                    # where it came from, precisely enough to re-query
    observed_at: datetime
    data: dict[str, Any]


class Claim(BaseModel):
    """A statement a specialist makes. Only valid if it cites collected evidence."""

    claim_id: str
    statement: str
    evidence_ids: list[str]


class RejectedClaim(BaseModel):
    """A claim the grounding gate refused, kept for audit rather than discarded."""

    statement: str
    cited: list[str]
    reason: str


class Provenance(BaseModel):
    """How a specialist's output was produced -- enough to reconstruct the call."""

    method: Literal["llm", "deterministic"]
    model: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    input_sha256: str | None = None
    latency_ms: float | None = None
    # Why the deterministic path was used when an LLM run was attempted.
    fallback_reason: str | None = None


class Finding(BaseModel):
    """What one evidence specialist concluded, and on what evidence."""

    finding_id: str
    agent: AgentName
    question: str
    conclusion: str
    summary: str
    claims: list[Claim]
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    provenance: Provenance


ChallengeKind = Literal[
    "UNSUPPORTED_CLAIM",        # a claim with no (valid) evidence behind it
    "CONTRADICTING_EVIDENCE",   # collected evidence points the other way
    "ALTERNATIVE_EXPLANATION",  # the same evidence fits a different story
    "MISSING_EVIDENCE",         # a conclusion rests on data that was not available
]


class Challenge(BaseModel):
    challenge_id: str
    kind: ChallengeKind
    target: str | None             # claim_id or finding_id challenged, if any
    statement: str
    # Empty is legitimate for UNSUPPORTED_CLAIM and MISSING_EVIDENCE: the
    # absence of evidence is the point.
    evidence_ids: list[str] = Field(default_factory=list)
    origin: Literal["rule", "llm"]


class Critique(BaseModel):
    """The Red-Team Critic's output. Not a score: alternatives and gaps."""

    agent: AgentName = "red_team_critic"
    strongest_alternative: Claim | None
    challenges: list[Challenge]
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    provenance: Provenance


class Disagreement(BaseModel):
    """Where specialists' conclusions conflict. Surfaced, never resolved by vote."""

    statement: str
    positions: dict[str, str]      # agent -> conclusion
    evidence_ids: list[str]


class VerificationStep(BaseModel):
    action: str
    why: str
    evidence_ids: list[str]


class Brief(BaseModel):
    """
    The Watch Officer's human-oriented synthesis. It does not restate the
    specialists; it tells the reviewer what happened, what the evidence
    establishes, what is contested, and what to check before deciding.
    """

    agent: AgentName = "watch_officer"
    situation: str                           # deterministic: what triggered this
    bottom_line: str
    established: list[Claim]                 # what the evidence supports
    disagreements: list[Disagreement]        # deterministic: computed, not chosen
    open_challenges: list[str]               # deterministic: every challenge_id, always
    verify_next: list[VerificationStep]
    limitations: list[str]
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    provenance: Provenance


class StepRecord(BaseModel):
    """One entry in the execution log: which step ran, when, and how it ended."""

    step: str
    agent: AgentName | None = None
    started_at: datetime
    finished_at: datetime
    outcome: Literal["ok", "fallback", "error"]
    detail: str = ""


class Investigation(BaseModel):
    investigation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str
    alert_id: str | None
    trigger: dict[str, Any]                  # snapshot of the alert that opened it
    status: Literal["OPEN", "COMPLETE", "FAILED"] = "OPEN"
    opened_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None

    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    critique: Critique | None = None
    brief: Brief | None = None
    steps: list[StepRecord] = Field(default_factory=list)

    record_sha256: str | None = None

    # ── Traceability ──────────────────────────────────────────────────────

    def evidence_index(self) -> dict[str, Evidence]:
        return {e.evidence_id: e for e in self.evidence}

    def all_claims(self) -> list[Claim]:
        claims = [c for f in self.findings for c in f.claims]
        if self.critique and self.critique.strongest_alternative:
            claims.append(self.critique.strongest_alternative)
        if self.brief:
            claims.extend(self.brief.established)
        return claims

    def trace(self, claim_id: str) -> list[Evidence]:
        """Resolve a claim to the evidence it cites. KeyError if unknown."""
        index = self.evidence_index()
        for claim in self.all_claims():
            if claim.claim_id == claim_id:
                return [index[eid] for eid in claim.evidence_ids]
        raise KeyError(claim_id)

    def untraceable_claims(self) -> list[str]:
        """claim_ids that cite nothing, or cite evidence not in this record. Must be empty."""
        index = self.evidence_index()
        return [
            c.claim_id for c in self.all_claims()
            if not c.evidence_ids or any(eid not in index for eid in c.evidence_ids)
        ]

    # ── Integrity ─────────────────────────────────────────────────────────

    def canonical_json(self) -> str:
        body = self.model_dump(mode="json", exclude={"record_sha256"})
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def compute_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def verify(self) -> bool:
        return self.record_sha256 is not None and self.record_sha256 == self.compute_sha256()
