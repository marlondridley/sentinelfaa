"""
The four specialists.

Each specialist answers one question from a read-only view of the
Investigation record and returns a typed result. None of them can call
another, see state it was not given, or write to the record -- the
orchestrator (workflow.py) decides what each sees and writes what each
returns.

Every specialist has two paths:

  * deterministic -- explicit rules over the evidence. Always computed. It is
    the fallback when the model is unavailable, over budget, or returns
    something that fails validation, so an investigation always completes.
  * model -- one structured completion through the shared LiteLLM client
    (redacted, error-classified), whose output must pass the grounding gate:
    every claim must cite evidence_ids present in the specialist's view.

Some fields are never the model's to decide: the Critic's rule-based
challenges cannot be removed by the model, and the Watch Officer's
situation, disagreements and open challenges are computed, not written.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from src.config import settings
from src.investigation.evidence import TRIGGER_ID
from src.investigation.record import (
    Brief,
    Challenge,
    Claim,
    Critique,
    Disagreement,
    Evidence,
    Finding,
    Investigation,
    Provenance,
    RejectedClaim,
    VerificationStep,
)
from src.llm_client import build_llm_client
from src.logging_config import get_logger
from src.metrics import LLM_CALL_LATENCY, LLM_CALLS
from src.prompts_loader import load_prompt, system_prompt, tool_schema
from src.redaction import redact_structured

_log = get_logger(__name__)

FEED_ALERT_TYPES = frozenset({"ALERT_FEED_STALE", "ALERT_FEED_DISCONNECTED", "ALERT_ISMC_LINK_DEGRADED"})
OPERATIONAL_ALERT_TYPES = frozenset({"ALERT_LOW_RVR", "ALERT_SURFACE_EVENT_SPIKE"})
_UNHEALTHY = frozenset({"STALE", "DEGRADED", "UNAVAILABLE", "DISCONNECTED"})


# ── Shared machinery ──────────────────────────────────────────────────────────

class ModelUnavailable(RuntimeError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _by_kind(view: Investigation, kind: str) -> list[Evidence]:
    return [e for e in view.evidence if e.kind == kind]


def _get(view: Investigation, evidence_id: str) -> Evidence | None:
    return view.evidence_index().get(evidence_id)


def ground(raw_claims: Any, allowed: set[str], prefix: str) -> tuple[list[Claim], list[RejectedClaim]]:
    """
    The grounding gate. A claim enters the record only if it cites at least
    one evidence_id and every id it cites exists in the specialist's view.
    Everything else is kept as a RejectedClaim with the reason.
    """
    kept: list[Claim] = []
    rejected: list[RejectedClaim] = []
    for item in raw_claims if isinstance(raw_claims, list) else []:
        statement = str((item or {}).get("statement") or "").strip()
        cited = [str(x) for x in ((item or {}).get("evidence_ids") or []) if str(x).strip()]
        unknown = [eid for eid in cited if eid not in allowed]
        if not statement:
            continue
        if not cited:
            rejected.append(RejectedClaim(statement=statement, cited=[], reason="cites no evidence"))
        elif unknown:
            rejected.append(RejectedClaim(statement=statement, cited=cited,
                                          reason=f"cites evidence not in the record: {unknown}"))
        else:
            kept.append(Claim(claim_id=f"{prefix}:C{len(kept) + 1}", statement=statement, evidence_ids=cited))
    return kept, rejected


def _view_payload(view: Investigation) -> dict[str, Any]:
    """What a model sees: evidence, and whatever findings/critique the view carries."""
    return {
        "evidence": [e.model_dump(mode="json") for e in view.evidence],
        "findings": [
            {"finding_id": f.finding_id, "agent": f.agent, "conclusion": f.conclusion,
             "claims": [c.model_dump() for c in f.claims]}
            for f in view.findings
        ],
        "critique": view.critique.model_dump(mode="json", exclude={"provenance"}) if view.critique else None,
    }


async def ask_model(prompt_name: str, agent: str, payload: dict[str, Any]) -> tuple[dict[str, Any], Provenance]:
    client = build_llm_client(model=settings.INVESTIGATION_MODEL)
    if client is None:
        raise ModelUnavailable("no LLM client configured")

    system = system_prompt(prompt_name)
    tool = tool_schema(prompt_name)
    user = json.dumps(redact_structured(payload), default=str, sort_keys=True)
    provenance = Provenance(
        method="llm",
        model=settings.INVESTIGATION_MODEL,
        prompt_name=prompt_name,
        prompt_version=str(load_prompt(prompt_name).get("version", "")),
        prompt_sha256=_sha(system + json.dumps(tool, sort_keys=True)),
        input_sha256=_sha(user),
    )
    start = time.perf_counter()
    try:
        raw = await client.acall_with_tool(system_prompt=system, tool=tool, user_message=user, max_tokens=1200)
        LLM_CALLS.labels(agent=agent, outcome="success").inc()
        return raw, provenance.model_copy(update={"latency_ms": round((time.perf_counter() - start) * 1000, 1)})
    except Exception:
        LLM_CALLS.labels(agent=agent, outcome="error").inc()
        raise
    finally:
        LLM_CALL_LATENCY.labels(agent=agent).observe(time.perf_counter() - start)


def _fallback(result, reason: str):
    """Mark a deterministic result as the fallback for a failed model attempt."""
    return result.model_copy(update={"provenance": Provenance(method="deterministic", fallback_reason=reason)})


# ── 1. Feed Diagnostician ─────────────────────────────────────────────────────

class FeedDiagnostician:
    """Is the infrastructure/feed itself behaving abnormally?"""

    name = "feed_diagnostician"
    question = "Is the infrastructure/feed itself behaving abnormally?"
    conclusions = ("FEED_ABNORMAL", "FEED_NORMAL", "INSUFFICIENT_EVIDENCE")
    prompt = "feed_diagnostician"

    def deterministic(self, view: Investigation) -> Finding:
        abnormal: list[tuple[str, list[str]]] = []
        for e in _by_kind(view, "service_health"):
            if e.data["status"] in _UNHEALTHY:
                reported = ", ".join(e.data["reported_feed_status"]) or "none"
                abnormal.append((f"{e.subject} is {e.data['status']} (feed_status reported: {reported}).", [e.evidence_id]))
        for e in _by_kind(view, "last_message"):
            if e.data["stale"]:
                abnormal.append((f"{e.subject} last delivered {e.data['seconds_since_last']:.0f} s ago, "
                                 f"past the {e.data['stale_threshold_seconds']} s staleness threshold.", [e.evidence_id]))
        for e in _by_kind(view, "ingest_latency"):
            if e.data["over_threshold"]:
                abnormal.append((f"{e.subject} latency p95 {e.data['p95_ms']:.0f} ms; {e.data['over_threshold']} of "
                                 f"{e.data['count']} messages exceeded {e.data['threshold_ms']} ms.", [e.evidence_id]))
        for e in _by_kind(view, "feed_heartbeat"):
            if e.data["last_received"] and e.data["messages_in_window"] == 0:
                abnormal.append((f"{e.subject} has gone silent: no messages in the last "
                                 f"{e.data['window_minutes']} min.", [e.evidence_id]))
        failed = _get(view, "failed_files")
        if failed and failed.data.get("readable") and failed.data["last_hour"]:
            abnormal.append((f"{failed.data['last_hour']} file(s) were quarantined to data/failed/ in the last hour.",
                             ["failed_files"]))
        parser = _get(view, "parser_failures")
        if parser and parser.data["failed_in_window"]:
            abnormal.append((f"{parser.data['failed_in_window']} of {parser.data['files_in_window']} ingested files "
                             "failed to parse in the window.", ["parser_failures"]))

        heartbeats = _by_kind(view, "feed_heartbeat")
        active = [e for e in heartbeats if e.data["messages_in_window"]]
        open_questions = []
        if failed and not failed.data.get("readable"):
            open_questions.append("data/failed/ could not be read; quarantined files were not checked.")
        never = [e.subject for e in heartbeats if not e.data["last_received"]]
        if never:
            open_questions.append(f"No messages at all from {', '.join(never)} in the snapshot -- not subscribed, or down?")

        if abnormal:
            conclusion, summary = "FEED_ABNORMAL", f"{len(abnormal)} feed/infrastructure abnormalit{'y' if len(abnormal) == 1 else 'ies'} observed."
            raw = abnormal[:8]
        elif active:
            conclusion, summary = "FEED_NORMAL", "Feeds delivering within thresholds; no quarantined or failed files."
            raw = [(f"{e.subject} delivered {e.data['messages_in_window']} messages in the window, the last "
                    f"{e.data['seconds_since_last']:.0f} s ago.", [e.evidence_id]) for e in active]
            raw += [(f"{e.subject} latency p95 {e.data['p95_ms']:.0f} ms, within {e.data['threshold_ms']} ms.", [e.evidence_id])
                    for e in _by_kind(view, "ingest_latency")]
            if parser:
                raw.append(("No ingest parse failures in the window.", ["parser_failures"]))
        else:
            conclusion, summary = "INSUFFICIENT_EVIDENCE", "No messages from any service in the window."
            raw = [("No supported service delivered a message in the window.", [e.evidence_id for e in heartbeats])]

        claims, _ = ground([{"statement": s, "evidence_ids": ids} for s, ids in raw],
                           set(view.evidence_index()), self.name)
        return Finding(finding_id=f"{self.name}:{uuid.uuid4().hex[:8]}", agent=self.name, question=self.question,
                       conclusion=conclusion, summary=summary, claims=claims, open_questions=open_questions,
                       provenance=Provenance(method="deterministic"))

    async def run(self, view: Investigation, use_llm: bool) -> Finding:
        return await _run_finding(self, view, use_llm)


# ── 2. Correlation Analyst ────────────────────────────────────────────────────

class CorrelationAnalyst:
    """Is this event isolated or part of a broader operational pattern?"""

    name = "correlation_analyst"
    question = "Is this event isolated or part of a broader operational pattern?"
    conclusions = ("WEATHER_CORRELATED", "CROSS_AIRPORT_PATTERN", "CROSS_SERVICE_PATTERN",
                   "RECURRING", "ISOLATED", "INSUFFICIENT_EVIDENCE")
    prompt = "correlation_analyst"

    def deterministic(self, view: Investigation) -> Finding:
        trigger = _get(view, TRIGGER_ID).data
        airport, service = trigger["airport"], trigger["service"]
        related = _by_kind(view, "related_alert")
        patterns: dict[str, list[tuple[str, list[str]]]] = {}

        low_rvr = [e for e in _by_kind(view, "rvr_reading")
                   if e.data["below_threshold"] and e.subject.startswith(str(airport))
                   and (e.data["age_minutes"] is None or e.data["age_minutes"] <= 60)]
        if low_rvr:
            patterns["WEATHER_CORRELATED"] = [(
                f"RVR below {low_rvr[0].data['low_threshold_ft']} ft at {airport} on "
                f"{', '.join(e.subject.split('runway ')[-1] for e in low_rvr)}.",
                [e.evidence_id for e in low_rvr])]

        other_airports = [e for e in related if e.data["airport"] != airport and e.data["severity"] == "CRITICAL"]
        if other_airports:
            names = sorted({e.data["airport"] or "PCT" for e in other_airports})
            patterns["CROSS_AIRPORT_PATTERN"] = [(
                f"CRITICAL alerts also fired at {', '.join(names)} in the same window.",
                ["alerts_by_airport"] + [e.evidence_id for e in other_airports[:3]])]

        other_services = [e for e in related if e.data["airport"] == airport and e.data["service"] != service]
        if other_services:
            names = sorted({e.data["service"] or "unknown" for e in other_services})
            patterns["CROSS_SERVICE_PATTERN"] = [(
                f"Other services at {airport} also alerted: {', '.join(names)}.",
                ["alerts_by_service"] + [e.evidence_id for e in other_services[:3]])]

        rate = _get(view, "alert_rate").data
        priors = _by_kind(view, "prior_incident")
        if rate["same_type_same_airport_24h"] or priors:
            cites = ["alert_rate"] + [e.evidence_id for e in priors[:3]]
            patterns["RECURRING"] = [(
                f"{trigger['alert_type']} fired {rate['same_type_same_airport_24h']} other time(s) at {airport} in "
                f"24 h; {len(priors)} prior staged action(s) at {airport}.", cites)]

        for conclusion in self.conclusions:
            if conclusion in patterns:
                break
        else:
            conclusion = "ISOLATED"
            patterns["ISOLATED"] = [(
                f"No other alerts within the window at any airport or service; "
                f"{rate['same_airport'][60]} at {airport} in the prior hour.",
                ["alerts_by_airport", "alerts_by_service", "alert_rate"])]

        raw = [claim for c in self.conclusions if c in patterns for claim in patterns[c]]
        claims, _ = ground([{"statement": s, "evidence_ids": ids} for s, ids in raw],
                           set(view.evidence_index()), self.name)
        summary = (f"Primary pattern: {conclusion}."
                   + (f" Also: {', '.join(c for c in patterns if c != conclusion)}." if len(patterns) > 1 else ""))
        return Finding(finding_id=f"{self.name}:{uuid.uuid4().hex[:8]}", agent=self.name, question=self.question,
                       conclusion=conclusion, summary=summary, claims=claims,
                       open_questions=[] if _by_kind(view, "rvr_reading") else ["No RVR readings available to test weather."],
                       provenance=Provenance(method="deterministic"))

    async def run(self, view: Investigation, use_llm: bool) -> Finding:
        return await _run_finding(self, view, use_llm)


async def _run_finding(spec, view: Investigation, use_llm: bool) -> Finding:
    baseline = spec.deterministic(view)
    if not use_llm:
        return baseline
    try:
        raw, provenance = await ask_model(spec.prompt, spec.name, {"question": spec.question, **_view_payload(view)})
    except Exception as exc:  # noqa: BLE001 -- advisory layer; fall back
        _log.warning("investigation_model_failed", extra={"agent": spec.name, "error": str(exc)})
        return _fallback(baseline, f"model call failed: {exc}")

    conclusion = str(raw.get("conclusion") or "")
    if conclusion not in spec.conclusions:
        return _fallback(baseline, f"model conclusion {conclusion!r} not one of {spec.conclusions}")
    claims, rejected = ground(raw.get("claims"), set(view.evidence_index()), spec.name)
    if not claims:
        return _fallback(baseline, "model produced no claim grounded in the evidence").model_copy(
            update={"rejected_claims": rejected})
    return baseline.model_copy(update={
        "conclusion": conclusion,
        "summary": str(raw.get("summary") or baseline.summary),
        "claims": claims,
        "rejected_claims": rejected,
        "open_questions": [str(q) for q in raw.get("open_questions") or []] or baseline.open_questions,
        "provenance": provenance,
    })


# ── 3. Red-Team Critic ────────────────────────────────────────────────────────

class RedTeamCritic:
    """
    What is the strongest explanation that contradicts the investigation, and
    which claims have no evidence? Produces challenges, not a score.
    """

    name = "red_team_critic"
    question = ("What is the strongest explanation that contradicts the current investigation, "
                "and which claims don't have evidence?")
    prompt = "red_team_critic"

    def rule_challenges(self, view: Investigation) -> list[Challenge]:
        out: list[Challenge] = []

        def add(kind, target, statement, evidence_ids=()):
            out.append(Challenge(challenge_id=f"X{len(out) + 1}", kind=kind, target=target,
                                 statement=statement, evidence_ids=list(evidence_ids), origin="rule"))

        findings = {f.agent: f for f in view.findings}
        feed, corr = findings.get("feed_diagnostician"), findings.get("correlation_analyst")
        trigger = _get(view, TRIGGER_ID).data

        # Unsupported claims: refused by the grounding gate, or resting only on
        # the alert they are meant to explain.
        for f in view.findings:
            for r in f.rejected_claims:
                add("UNSUPPORTED_CLAIM", f.finding_id, f"{f.agent} asserted \"{r.statement}\" with no valid evidence ({r.reason}).")
            for c in f.claims:
                if c.evidence_ids == [TRIGGER_ID]:
                    add("UNSUPPORTED_CLAIM", c.claim_id,
                        f"\"{c.statement}\" rests only on the alert it is meant to explain.")

        # Contradicting evidence: a feed-outage alert while that feed is delivering.
        heartbeat = _get(view, f"heartbeat:{trigger['service']}")
        if trigger["alert_type"] in FEED_ALERT_TYPES and heartbeat and heartbeat.data["messages_last_5m"]:
            add("CONTRADICTING_EVIDENCE", TRIGGER_ID,
                f"The alert reports {trigger['service']} down or degraded, but {trigger['service']} delivered "
                f"{heartbeat.data['messages_last_5m']} message(s) in the last 5 min.",
                [TRIGGER_ID, heartbeat.evidence_id])

        # Alternative explanations built from the findings' own evidence.
        if feed and feed.conclusion == "FEED_ABNORMAL":
            feed_ids = sorted({eid for c in feed.claims for eid in c.evidence_ids})
            if trigger["alert_type"] in OPERATIONAL_ALERT_TYPES:
                add("ALTERNATIVE_EXPLANATION", feed.finding_id,
                    "The alert may be an artifact of degraded feed data rather than a real operational condition.",
                    [TRIGGER_ID] + feed_ids)
            if corr and corr.conclusion == "CROSS_AIRPORT_PATTERN":
                corr_ids = sorted({eid for c in corr.claims for eid in c.evidence_ids})
                add("ALTERNATIVE_EXPLANATION", corr.finding_id,
                    "The multi-airport pattern may have one upstream cause (SWIM subscription, broker or ingest "
                    "host) rather than several independent events.", feed_ids + corr_ids)
            if corr and corr.conclusion == "ISOLATED":
                add("ALTERNATIVE_EXPLANATION", corr.finding_id,
                    "The event may only look isolated: degraded feeds can hide the correlated alerts that would "
                    "show a pattern.", feed_ids)

        # Missing evidence behind a conclusion.
        failed = _get(view, "failed_files")
        if feed and failed and not failed.data.get("readable"):
            add("MISSING_EVIDENCE", feed.finding_id, "The feed conclusion was reached without the data/failed/ listing.")
        if heartbeat and not heartbeat.data["last_received"]:
            add("MISSING_EVIDENCE", feed.finding_id if feed else None,
                f"No {trigger['service']} messages exist in the snapshot, so the feed behind this alert was never observed.")
        if trigger["service"] == "APDS_RVR" and not _by_kind(view, "rvr_reading"):
            add("MISSING_EVIDENCE", corr.finding_id if corr else None, "An RVR alert with no RVR readings in evidence.")
        return out

    def deterministic(self, view: Investigation) -> Critique:
        challenges = self.rule_challenges(view)
        return Critique(strongest_alternative=_strongest(challenges), challenges=challenges,
                        provenance=Provenance(method="deterministic"))

    async def run(self, view: Investigation, use_llm: bool) -> Critique:
        baseline = self.deterministic(view)
        if not use_llm:
            return baseline
        try:
            raw, provenance = await ask_model(self.prompt, self.name, {"question": self.question, **_view_payload(view)})
        except Exception as exc:  # noqa: BLE001
            _log.warning("investigation_model_failed", extra={"agent": self.name, "error": str(exc)})
            return _fallback(baseline, f"model call failed: {exc}")

        allowed = set(view.evidence_index())
        claim_ids = {c.claim_id for f in view.findings for c in f.claims} | {f.finding_id for f in view.findings}
        challenges = list(baseline.challenges)      # rule challenges are never removable
        rejected: list[RejectedClaim] = []
        seen = {c.statement for c in challenges}
        for item in raw.get("challenges") or []:
            kind = str((item or {}).get("kind") or "")
            statement = str((item or {}).get("statement") or "").strip()
            cited = [str(x) for x in (item or {}).get("evidence_ids") or []]
            target = (item or {}).get("target")
            if not statement or statement in seen:
                continue
            if kind not in ("UNSUPPORTED_CLAIM", "CONTRADICTING_EVIDENCE", "ALTERNATIVE_EXPLANATION", "MISSING_EVIDENCE"):
                rejected.append(RejectedClaim(statement=statement, cited=cited, reason=f"unknown challenge kind {kind!r}"))
                continue
            if kind in ("CONTRADICTING_EVIDENCE", "ALTERNATIVE_EXPLANATION") and (
                    not cited or any(eid not in allowed for eid in cited)):
                rejected.append(RejectedClaim(statement=statement, cited=cited,
                                              reason="an alternative or contradiction must cite collected evidence"))
                continue
            if kind == "UNSUPPORTED_CLAIM" and target not in claim_ids:
                rejected.append(RejectedClaim(statement=statement, cited=cited,
                                              reason=f"targets unknown claim {target!r}"))
                continue
            seen.add(statement)
            challenges.append(Challenge(challenge_id=f"X{len(challenges) + 1}", kind=kind, target=target,
                                        statement=statement, evidence_ids=[e for e in cited if e in allowed],
                                        origin="llm"))

        alt, alt_rejected = ground([raw.get("strongest_alternative")] if raw.get("strongest_alternative") else [],
                                   allowed, self.name)
        rejected.extend(alt_rejected)
        strongest = (alt[0].model_copy(update={"claim_id": f"{self.name}:ALT"}) if alt else _strongest(challenges))
        return Critique(strongest_alternative=strongest, challenges=challenges,
                        rejected_claims=rejected, provenance=provenance)


def _strongest(challenges: list[Challenge]) -> Claim | None:
    for kind in ("CONTRADICTING_EVIDENCE", "ALTERNATIVE_EXPLANATION"):
        for c in challenges:
            if c.kind == kind and c.evidence_ids:
                return Claim(claim_id="red_team_critic:ALT", statement=c.statement, evidence_ids=c.evidence_ids)
    return None


# ── 4. Watch Officer (synthesis) ──────────────────────────────────────────────

_VERIFY = {
    "FEED_ABNORMAL": "Check the SWIM subscription, the Jumpstart kit log and data/failed/ for the affected service before treating the alert as operational.",
    "FEED_NORMAL": "Feeds are delivering; verify the alerted condition itself against the facility.",
    "WEATHER_CORRELATED": "Confirm RVR with the tower or ASOS before disposition.",
    "CROSS_AIRPORT_PATTERN": "Look for a common upstream cause across airports before handling each alert separately.",
    "CROSS_SERVICE_PATTERN": "Review the airport's other alerting services together, not this alert alone.",
    "RECURRING": "Review how the earlier occurrences were dispositioned.",
    "ISOLATED": "Verify this single alert against the authoritative operational source.",
}


class WatchOfficer:
    """
    Synthesis for the human reviewer. It does not repeat the specialists:
    it says what happened, what the evidence establishes, what is contested,
    and what to check before deciding.
    """

    name = "watch_officer"
    prompt = "investigation_brief"

    def fixed_fields(self, view: Investigation) -> dict[str, Any]:
        """Computed, never written by the model."""
        t = _get(view, TRIGGER_ID).data
        findings = {f.agent: f.conclusion for f in view.findings}
        challenges = view.critique.challenges if view.critique else []
        disagreements = [
            Disagreement(statement=c.statement,
                         positions={**findings, "red_team_critic": c.kind},
                         evidence_ids=c.evidence_ids)
            for c in challenges if c.kind in ("CONTRADICTING_EVIDENCE", "ALTERNATIVE_EXPLANATION")
        ]
        return {
            "situation": f"{t['severity']} {t['alert_type']} at {t['airport'] or 'PCT'} ({t['service'] or 'unknown service'}), "
                         f"staged for human review as action {t['action_id']}.",
            "disagreements": disagreements,
            "open_challenges": [c.challenge_id for c in challenges],
        }

    def deterministic(self, view: Investigation) -> Brief:
        fixed = self.fixed_fields(view)
        challenges = view.critique.challenges if view.critique else []
        challenged = {c.target for c in challenges}
        established_raw = [
            {"statement": c.statement, "evidence_ids": c.evidence_ids}
            for f in view.findings if f.finding_id not in challenged
            for c in f.claims if c.claim_id not in challenged
        ][:5]
        established, _ = ground(established_raw, set(view.evidence_index()), self.name)

        verify: list[VerificationStep] = []
        for f in view.findings:
            if f.conclusion in _VERIFY:
                verify.append(VerificationStep(action=_VERIFY[f.conclusion], why=f"{f.agent}: {f.conclusion}",
                                               evidence_ids=sorted({e for c in f.claims for e in c.evidence_ids})[:6]))
        for c in challenges:
            if c.kind in ("CONTRADICTING_EVIDENCE", "MISSING_EVIDENCE"):
                verify.append(VerificationStep(action=f"Resolve challenge {c.challenge_id}: {c.statement}",
                                               why=c.kind, evidence_ids=c.evidence_ids))

        conclusions = " / ".join(f"{f.agent.replace('_', ' ')}: {f.conclusion}" for f in view.findings)
        n = len(fixed["open_challenges"])
        return Brief(
            bottom_line=f"{conclusions}. {n} open challenge{'s' if n != 1 else ''} to resolve before disposition."
                        if view.findings else "No specialist findings were produced.",
            established=established, verify_next=verify,
            limitations=self._limitations(view), provenance=Provenance(method="deterministic"), **fixed,
        )

    def _limitations(self, view: Investigation) -> list[str]:
        notes = ["Advisory only: this investigation approves, rejects and executes nothing."]
        rule_based = [f.agent for f in view.findings if f.provenance.method == "deterministic"]
        if view.critique and view.critique.provenance.method == "deterministic":
            rule_based.append("red_team_critic")
        if rule_based:
            notes.append(f"Rule-based, no model reasoning: {', '.join(rule_based)}.")
        return notes

    async def run(self, view: Investigation, use_llm: bool) -> Brief:
        baseline = self.deterministic(view)
        if not use_llm:
            return baseline
        try:
            raw, provenance = await ask_model(self.prompt, self.name, _view_payload(view))
        except Exception as exc:  # noqa: BLE001
            _log.warning("investigation_model_failed", extra={"agent": self.name, "error": str(exc)})
            return _fallback(baseline, f"model call failed: {exc}")

        allowed = set(view.evidence_index())
        established, rejected = ground(raw.get("established"), allowed, self.name)
        verify: list[VerificationStep] = []
        for item in raw.get("verify_next") or []:
            cited = [str(x) for x in (item or {}).get("evidence_ids") or []]
            action = str((item or {}).get("action") or "").strip()
            if action and cited and all(e in allowed for e in cited):
                verify.append(VerificationStep(action=action, why=str(item.get("why") or ""), evidence_ids=cited))
            elif action:
                rejected.append(RejectedClaim(statement=action, cited=cited, reason="verification step without valid evidence"))
        return baseline.model_copy(update={
            "bottom_line": str(raw.get("bottom_line") or baseline.bottom_line),
            "established": established or baseline.established,
            "verify_next": verify or baseline.verify_next,
            "limitations": baseline.limitations + [str(x) for x in raw.get("limitations") or []],
            "rejected_claims": rejected,
            "provenance": provenance,
        })
