"""
The investigation layer: one shared structured record, four specialists,
a deterministic flow, and every conclusion traceable to evidence.

Most tests build a Snapshot by hand and never touch the database -- evidence
is a pure function of the snapshot, which is the point. The last section runs
the real pipeline against the test database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.investigation import specialists as sp
from src.investigation import workflow as wf
from src.investigation.evidence import CORRELATION_KINDS, FEED_KINDS, Snapshot, collect
from src.investigation.record import Investigation
from src.models import AlertEvent, NASEvent, StagedAction

NOW = datetime.now(timezone.utc).replace(microsecond=0)


# ── builders ──────────────────────────────────────────────────────────────────

def _event(airport="KDCA", service="SMES", minutes_ago=1.0, **kw) -> NASEvent:
    t = NOW - timedelta(minutes=minutes_ago)
    fields = dict(airport_code=airport, tracon="PCT", service_name=service, message_type="X",
                  event_time_utc=t - timedelta(seconds=1), received_time_utc=t, latency_ms=1000.0,
                  track_id="1", feed_status="operational")
    fields.update(kw)
    return NASEvent(**fields)


def _alert(alert_type="ALERT_ISMC_LINK_DEGRADED", airport="KDCA", service="ISMC", **kw) -> AlertEvent:
    return AlertEvent(alert_id=kw.pop("alert_id", "A-TRIGGER"), alert_type=alert_type, severity="CRITICAL",
                      airport_code=airport, service_name=service, description="test trigger",
                      triggered_at=NOW - timedelta(minutes=1), **kw)


def _alert_row(alert_id, airport, service, alert_type="ALERT_FEED_DISCONNECTED", severity="CRITICAL", minutes_ago=2):
    return {"alert_id": alert_id, "alert_type": alert_type, "severity": severity, "airport_code": airport,
            "service_name": service, "description": "related", "triggered_at": NOW - timedelta(minutes=minutes_ago)}


def _snapshot(events=None, alerts=(), ingest_log=(), failed=(), rvr=(), staged=(), alert=None) -> Snapshot:
    return Snapshot(
        taken_at=NOW, alert=alert or _alert(), action_id="ACT-1",
        events=list(events if events is not None else [_event(service=s) for s in sorted(settings.SUPPORTED_SERVICES)]),
        alerts=list(alerts), ingest_log=list(ingest_log),
        failed_files=None if failed is None else list(failed), rvr=list(rvr), staged_actions=list(staged),
        window=timedelta(minutes=60),
    )


def _record(snap: Snapshot) -> Investigation:
    return Investigation(action_id=snap.action_id, alert_id=snap.alert.alert_id,
                         trigger=snap.alert.model_dump(mode="json"), evidence=collect(snap))


def _feed_view(snap):
    return wf.view_for(_record(snap), kinds=FEED_KINDS, findings=False, critique=False)


def _corr_view(snap):
    return wf.view_for(_record(snap), kinds=CORRELATION_KINDS, findings=False, critique=False)


# ── Evidence ──────────────────────────────────────────────────────────────────

def test_evidence_is_a_pure_function_of_the_snapshot():
    snap = _snapshot(alerts=[_alert_row("A2", "KIAD", "SMES")])
    first, second = collect(snap), collect(snap)
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]
    ids = [e.evidence_id for e in first]
    assert len(ids) == len(set(ids)), "evidence ids must be unique"


def test_every_supported_service_gets_a_heartbeat_even_when_silent():
    snap = _snapshot(events=[_event(service="SMES")])
    heartbeats = {e.evidence_id: e for e in collect(snap) if e.kind == "feed_heartbeat"}
    assert set(heartbeats) == {f"heartbeat:{s}" for s in settings.SUPPORTED_SERVICES}
    assert heartbeats["heartbeat:ISMC"].data["messages_in_window"] == 0


def test_feed_diagnostician_view_excludes_correlation_evidence():
    kinds = {e.kind for e in _feed_view(_snapshot(alerts=[_alert_row("A2", "KIAD", "SMES")])).evidence}
    assert kinds <= FEED_KINDS
    assert "related_alert" not in kinds and "alerts_by_airport" not in kinds


# ── Grounding gate ────────────────────────────────────────────────────────────

def test_grounding_gate_keeps_only_claims_citing_collected_evidence():
    kept, rejected = sp.ground([
        {"statement": "grounded", "evidence_ids": ["trigger"]},
        {"statement": "cites nothing", "evidence_ids": []},
        {"statement": "cites a ghost", "evidence_ids": ["trigger", "heartbeat:NOPE"]},
    ], allowed={"trigger"}, prefix="x")
    assert [c.statement for c in kept] == ["grounded"]
    assert kept[0].claim_id == "x:C1"
    assert {r.statement for r in rejected} == {"cites nothing", "cites a ghost"}


# ── Feed Diagnostician ────────────────────────────────────────────────────────

def test_feed_diagnostician_flags_a_degraded_feed_with_its_evidence():
    events = [_event(service=s) for s in sorted(settings.SUPPORTED_SERVICES)] + [
        _event(service="ISMC", feed_status="degraded", minutes_ago=0.5)]
    finding = sp.FeedDiagnostician().deterministic(_feed_view(_snapshot(events=events)))
    assert finding.conclusion == "FEED_ABNORMAL"
    assert any("health:KDCA:ISMC" in c.evidence_ids for c in finding.claims)


def test_feed_diagnostician_reports_quarantined_files_and_parse_failures():
    snap = _snapshot(
        failed=[("1787863348274", NOW - timedelta(minutes=10))],
        ingest_log=[{"file": "f1", "status": "failed", "error": "bad", "timestamp": (NOW - timedelta(minutes=5)).isoformat()}],
    )
    finding = sp.FeedDiagnostician().deterministic(_feed_view(snap))
    cited = {eid for c in finding.claims for eid in c.evidence_ids}
    assert finding.conclusion == "FEED_ABNORMAL"
    assert {"failed_files", "parser_failures"} <= cited


def test_feed_diagnostician_healthy_feed_is_normal():
    finding = sp.FeedDiagnostician().deterministic(_feed_view(_snapshot()))
    assert finding.conclusion == "FEED_NORMAL"
    assert finding.claims


def test_feed_diagnostician_with_no_messages_is_insufficient_evidence():
    finding = sp.FeedDiagnostician().deterministic(_feed_view(_snapshot(events=[])))
    assert finding.conclusion == "INSUFFICIENT_EVIDENCE"


# ── Correlation Analyst ───────────────────────────────────────────────────────

def test_correlation_analyst_sees_a_cross_airport_pattern():
    snap = _snapshot(alerts=[_alert_row("A2", "KIAD", "ISMC"), _alert_row("A3", "KBWI", "ISMC")])
    finding = sp.CorrelationAnalyst().deterministic(_corr_view(snap))
    assert finding.conclusion == "CROSS_AIRPORT_PATTERN"
    cited = {eid for c in finding.claims for eid in c.evidence_ids}
    assert "alerts_by_airport" in cited and "alert:A2" in cited


def test_correlation_analyst_ties_low_rvr_to_weather():
    rvr = [{"airport_code": "KDCA", "runway": "01", "rvr_touchdown": 600, "rvr_midpoint": 800,
            "rvr_rollout": 900, "event_time_utc": NOW - timedelta(minutes=3)}]
    snap = _snapshot(rvr=rvr, alert=_alert("ALERT_LOW_RVR", service="APDS_RVR"))
    finding = sp.CorrelationAnalyst().deterministic(_corr_view(snap))
    assert finding.conclusion == "WEATHER_CORRELATED"
    assert finding.claims[0].evidence_ids == ["rvr:KDCA:01"]


def test_correlation_analyst_notices_prior_incidents():
    prior = StagedAction(action_id="ACT-0", airport_code="KDCA", alert_type="ALERT_ISMC_LINK_DEGRADED",
                         status="REJECTED", idempotency_key="k0", created_at=NOW - timedelta(hours=3))
    finding = sp.CorrelationAnalyst().deterministic(_corr_view(_snapshot(staged=[prior])))
    assert finding.conclusion == "RECURRING"
    assert any("prior:ACT-0" in c.evidence_ids for c in finding.claims)


def test_correlation_analyst_says_isolated_only_with_evidence_for_it():
    finding = sp.CorrelationAnalyst().deterministic(_corr_view(_snapshot()))
    assert finding.conclusion == "ISOLATED"
    assert set(finding.claims[0].evidence_ids) == {"alerts_by_airport", "alerts_by_service", "alert_rate"}


# ── Red-Team Critic ───────────────────────────────────────────────────────────

async def _findings_view(snap) -> Investigation:
    inv = _record(snap)
    inv.findings = [
        sp.FeedDiagnostician().deterministic(wf.view_for(inv, kinds=FEED_KINDS, findings=False, critique=False)),
        sp.CorrelationAnalyst().deterministic(wf.view_for(inv, kinds=CORRELATION_KINDS, findings=False, critique=False)),
    ]
    return inv


async def test_critic_finds_evidence_contradicting_a_feed_outage_alert():
    # The alert says ISMC is disconnected; ISMC delivered a message a minute ago.
    snap = _snapshot(alert=_alert("ALERT_FEED_DISCONNECTED", service="ISMC"))
    critique = sp.RedTeamCritic().deterministic(await _findings_view(snap))
    contra = [c for c in critique.challenges if c.kind == "CONTRADICTING_EVIDENCE"]
    assert contra and set(contra[0].evidence_ids) == {"trigger", "heartbeat:ISMC"}
    assert critique.strongest_alternative is not None
    assert critique.strongest_alternative.evidence_ids == contra[0].evidence_ids


async def test_critic_proposes_a_common_upstream_cause_for_multi_airport_feed_trouble():
    events = [_event(service=s) for s in sorted(settings.SUPPORTED_SERVICES)] + [
        _event(service="ISMC", feed_status="degraded", minutes_ago=0.5)]
    snap = _snapshot(events=events, alerts=[_alert_row("A2", "KIAD", "ISMC")])
    critique = sp.RedTeamCritic().deterministic(await _findings_view(snap))
    alts = [c.statement for c in critique.challenges if c.kind == "ALTERNATIVE_EXPLANATION"]
    assert any("upstream cause" in s for s in alts)


async def test_critic_turns_rejected_model_claims_into_unsupported_claim_challenges(monkeypatch):
    class Model:
        async def acall_with_tool(self, **kw):
            return {"conclusion": "FEED_ABNORMAL", "summary": "s", "open_questions": [],
                    "claims": [{"statement": "grounded", "evidence_ids": ["heartbeat:SMES"]},
                               {"statement": "the broker is overloaded", "evidence_ids": ["broker:cpu"]}]}

    monkeypatch.setattr(sp, "build_llm_client", lambda *a, **k: Model())
    snap = _snapshot()
    inv = _record(snap)
    finding = await sp.FeedDiagnostician().run(wf.view_for(inv, kinds=FEED_KINDS, findings=False, critique=False), True)
    assert finding.provenance.method == "llm"
    assert [r.statement for r in finding.rejected_claims] == ["the broker is overloaded"]
    inv.findings = [finding]
    critique = sp.RedTeamCritic().deterministic(inv)
    assert any(c.kind == "UNSUPPORTED_CLAIM" and "broker is overloaded" in c.statement for c in critique.challenges)


async def test_model_cannot_remove_rule_challenges_or_slip_in_ungrounded_alternatives(monkeypatch):
    class Model:
        async def acall_with_tool(self, **kw):
            return {"strongest_alternative": {"statement": "solar flare", "evidence_ids": ["sun:1"]},
                    "challenges": [
                        {"kind": "ALTERNATIVE_EXPLANATION", "target": None, "statement": "ungrounded alt",
                         "evidence_ids": []},
                        {"kind": "ALTERNATIVE_EXPLANATION", "target": None, "statement": "grounded alt",
                         "evidence_ids": ["alert_rate"]},
                    ]}

    monkeypatch.setattr(sp, "build_llm_client", lambda *a, **k: Model())
    view = await _findings_view(_snapshot(alert=_alert("ALERT_FEED_DISCONNECTED", service="ISMC")))
    rules = sp.RedTeamCritic().deterministic(view).challenges
    critique = await sp.RedTeamCritic().run(view, True)

    assert [c.statement for c in critique.challenges[:len(rules)]] == [c.statement for c in rules]
    statements = [c.statement for c in critique.challenges]
    assert "grounded alt" in statements and "ungrounded alt" not in statements
    assert critique.strongest_alternative.statement != "solar flare"
    assert {r.statement for r in critique.rejected_claims} >= {"ungrounded alt", "solar flare"}


# ── Watch Officer ─────────────────────────────────────────────────────────────

async def test_brief_surfaces_every_challenge_and_computes_disagreements(monkeypatch):
    class Model:
        async def acall_with_tool(self, **kw):
            return {"bottom_line": "Probably fine.",
                    "established": [{"statement": "ISMC delivered recently", "evidence_ids": ["heartbeat:ISMC"]}],
                    "verify_next": [{"action": "Call the facility", "why": "confirm", "evidence_ids": ["trigger"]}],
                    "limitations": []}

    monkeypatch.setattr(sp, "build_llm_client", lambda *a, **k: Model())
    inv = await _findings_view(_snapshot(alert=_alert("ALERT_FEED_DISCONNECTED", service="ISMC")))
    inv.critique = sp.RedTeamCritic().deterministic(inv)
    brief = await sp.WatchOfficer().run(inv, True)

    assert brief.open_challenges == [c.challenge_id for c in inv.critique.challenges]
    assert any(d.positions.get("red_team_critic") == "CONTRADICTING_EVIDENCE" for d in brief.disagreements)
    assert brief.situation.startswith("CRITICAL ALERT_FEED_DISCONNECTED at KDCA")
    assert brief.bottom_line == "Probably fine."
    assert all(c.evidence_ids for c in brief.established)


# ── The flow ──────────────────────────────────────────────────────────────────

async def test_flow_is_fixed_and_specialists_see_only_their_scope(monkeypatch):
    seen: dict[str, Investigation] = {}
    for spec in (wf.FEED, wf.CORRELATION, wf.CRITIC, wf.WATCH):
        original = spec.run

        async def spy(view, use_llm, _name=spec.name, _orig=original):
            seen[_name] = view
            return await _orig(view, use_llm)
        monkeypatch.setattr(spec, "run", spy)

    snap = _snapshot(alerts=[_alert_row("A2", "KIAD", "ISMC")])
    inv = await wf.run_investigation(StagedAction(action_id="ACT-1", idempotency_key="k"), snap.alert,
                                     use_llm=False, snapshot=snap)

    assert inv.status == "COMPLETE"
    assert [s.step for s in inv.steps] == [
        "collect_evidence", "feed_diagnostician", "correlation_analyst", "red_team_critic", "watch_officer", "seal"]
    assert not seen["feed_diagnostician"].findings and not seen["correlation_analyst"].findings
    assert {e.kind for e in seen["feed_diagnostician"].evidence} <= FEED_KINDS
    assert {e.kind for e in seen["correlation_analyst"].evidence} <= CORRELATION_KINDS
    assert len(seen["red_team_critic"].findings) == 2 and seen["red_team_critic"].critique is None
    assert seen["watch_officer"].critique is not None


async def test_every_conclusion_traces_back_to_evidence_and_the_record_is_sealed():
    snap = _snapshot(alerts=[_alert_row("A2", "KIAD", "ISMC")],
                     alert=_alert("ALERT_FEED_DISCONNECTED", service="ISMC"))
    inv = await wf.run_investigation(StagedAction(action_id="ACT-1", idempotency_key="k"), snap.alert,
                                     use_llm=False, snapshot=snap)
    assert inv.untraceable_claims() == []
    for claim in inv.all_claims():
        assert inv.trace(claim.claim_id), claim.claim_id
    assert inv.verify()
    tampered = inv.model_copy(deep=True)
    tampered.findings[0].conclusion = "FEED_NORMAL" if inv.findings[0].conclusion != "FEED_NORMAL" else "FEED_ABNORMAL"
    assert not tampered.verify()


async def test_model_outage_degrades_every_step_to_rules_and_still_completes(monkeypatch):
    class Down:
        async def acall_with_tool(self, **kw):
            raise ConnectionError("proxy unreachable")

    monkeypatch.setattr(sp, "build_llm_client", lambda *a, **k: Down())
    snap = _snapshot()
    inv = await wf.run_investigation(StagedAction(action_id="ACT-1", idempotency_key="k"), snap.alert,
                                     use_llm=True, snapshot=snap)
    assert inv.status == "COMPLETE"
    specialist_steps = [s for s in inv.steps if s.agent]
    assert [s.outcome for s in specialist_steps] == ["fallback"] * 4
    assert all("proxy unreachable" in s.detail for s in specialist_steps)


async def test_rule_based_investigation_is_reproducible():
    snap = _snapshot(alerts=[_alert_row("A2", "KIAD", "ISMC")])
    action = StagedAction(action_id="ACT-1", idempotency_key="k")
    a = await wf.run_investigation(action, snap.alert, use_llm=False, snapshot=snap)
    b = await wf.run_investigation(action, snap.alert, use_llm=False, snapshot=snap)

    def essence(inv):
        return ([(f.agent, f.conclusion, [(c.statement, c.evidence_ids) for c in f.claims]) for f in inv.findings],
                [(c.kind, c.statement, c.evidence_ids) for c in inv.critique.challenges],
                inv.brief.bottom_line, [d.statement for d in inv.brief.disagreements])
    assert essence(a) == essence(b)


# ── Against the database, through the real pipeline ──────────────────────────

@pytest.fixture
def db():
    from src import storage
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


async def test_pipeline_investigates_every_staged_action_within_the_llm_budget(db, monkeypatch):
    from src.demo_scenario import generate_cascade_failure_scenario
    from src.pipeline import run_pipeline
    from src.storage.pool import get_db_pool
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    calls = []

    class Model:
        async def acall_with_tool(self, **kw):
            calls.append(kw["tool"]["name"])
            raise ConnectionError("no model in tests")

    monkeypatch.setattr(settings, "CRITIC_ENABLED", True)
    monkeypatch.setattr(settings, "MAX_LLM_INVESTIGATIONS_PER_PASS", 2)
    monkeypatch.setattr(sp, "build_llm_client", lambda *a, **k: Model())
    import src.critic_agent as critic_agent
    import src.watch_officer_agent as watch_officer
    monkeypatch.setattr(critic_agent, "build_llm_client", lambda *a, **k: None)
    monkeypatch.setattr(watch_officer, "build_llm_client", lambda *a, **k: None)

    summary = await run_pipeline(generate_cascade_failure_scenario(), label="TEST")
    assert summary["staged_count"] > 2
    assert summary["investigated_count"] == summary["staged_count"]
    assert len(calls) == 2 * 4, "only the budgeted investigations may call the model, 4 specialists each"

    repo = InvestigationsRepository(await get_db_pool())
    for action in await db.get_pending_staged_actions():
        inv = await repo.get_by_action_id(action.action_id)
        assert inv is not None and inv.status == "COMPLETE"
        assert inv.verify(), "stored record must match its hash"
        assert inv.untraceable_claims() == []

    # Re-running does not duplicate investigations.
    again = await wf.investigate_staged([(a, _alert(alert_id=a.alert_id)) for a in await db.get_pending_staged_actions()])
    assert again == []


async def test_stored_investigations_are_append_only(db):
    import asyncpg

    from src.demo_scenario import generate_cascade_failure_scenario
    from src.pipeline import run_pipeline
    from src.storage.pool import get_db_pool

    settings_backup = settings.CRITIC_ENABLED
    settings.CRITIC_ENABLED = False
    try:
        await run_pipeline(generate_cascade_failure_scenario(), label="TEST")
    finally:
        settings.CRITIC_ENABLED = settings_backup
    pool = await get_db_pool()
    assert await pool.fetchval("SELECT count(*) FROM investigations") > 0
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pool.execute("UPDATE investigations SET status = 'FAILED'")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pool.execute("DELETE FROM investigations")
