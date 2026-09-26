"""
The investigation orchestrator. Deterministic, and the only writer of the
Investigation record.

    1. collect evidence            (no model; evidence.py)
    2. Feed Diagnostician  ─┐      in parallel, each on its own evidence scope,
       Correlation Analyst ─┘      neither sees the other's evidence or finding
    3. Red-Team Critic             sees all evidence and both findings
    4. Watch Officer               sees everything; writes the human brief
    5. seal                        check traceability, hash, mark COMPLETE

The order never depends on model output. Specialists get deep copies of the
record ("views"), so nothing a specialist does can change what another one
sees; they return typed results and this module appends them. A specialist
whose model call fails falls back to its rules, and the step log says so.
"""
from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from src.config import settings
from src.investigation.evidence import CORRELATION_KINDS, FEED_KINDS, Snapshot, collect, gather_snapshot
from src.investigation.record import Investigation, StepRecord
from src.investigation.specialists import CorrelationAnalyst, FeedDiagnostician, RedTeamCritic, WatchOfficer
from src.logging_config import get_logger
from src.models import AlertEvent, StagedAction

_log = get_logger(__name__)

FEED = FeedDiagnostician()
CORRELATION = CorrelationAnalyst()
CRITIC = RedTeamCritic()
WATCH = WatchOfficer()


class InvestigationError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def view_for(inv: Investigation, *, kinds: frozenset[str] | None, findings: bool, critique: bool) -> Investigation:
    """A deep copy of the record restricted to what one specialist may see."""
    return inv.model_copy(deep=True, update={
        "evidence": [e for e in inv.evidence if kinds is None or e.kind in kinds],
        "findings": [f.model_copy(deep=True) for f in inv.findings] if findings else [],
        "critique": inv.critique.model_copy(deep=True) if (critique and inv.critique) else None,
        "brief": None,
        "steps": [],
    })


async def _run(spec, view: Investigation, use_llm: bool) -> tuple[Any, StepRecord]:
    started = _now()
    result = await spec.run(view, use_llm)
    fallback = result.provenance.fallback_reason
    return result, StepRecord(
        step=spec.name, agent=spec.name, started_at=started, finished_at=_now(),
        outcome="fallback" if fallback else "ok",
        detail=fallback or result.provenance.method,
    )


@asynccontextmanager
async def _step(inv: Investigation, name: str):
    started = _now()
    try:
        yield
    except Exception as exc:
        inv.steps.append(StepRecord(step=name, started_at=started, finished_at=_now(),
                                    outcome="error", detail=f"{type(exc).__name__}: {exc}"))
        raise
    inv.steps.append(StepRecord(step=name, started_at=started, finished_at=_now(), outcome="ok"))


async def run_investigation(
    action: StagedAction,
    alert: AlertEvent,
    *,
    use_llm: bool,
    snapshot: Snapshot | None = None,
) -> Investigation:
    """Run the fixed sequence and return a sealed record (COMPLETE or FAILED)."""
    inv = Investigation(action_id=action.action_id, alert_id=alert.alert_id,
                        trigger=alert.model_dump(mode="json"))
    try:
        async with _step(inv, "collect_evidence"):
            snap = snapshot or await gather_snapshot(alert, action.action_id)
            inv.evidence = collect(snap)

        # Parallel, isolated: each sees only its own evidence scope and no findings.
        (feed, feed_step), (corr, corr_step) = await asyncio.gather(
            _run(FEED, view_for(inv, kinds=FEED_KINDS, findings=False, critique=False), use_llm),
            _run(CORRELATION, view_for(inv, kinds=CORRELATION_KINDS, findings=False, critique=False), use_llm),
        )
        inv.findings = [feed, corr]
        inv.steps += [feed_step, corr_step]            # fixed order, whichever finished first

        inv.critique, step = await _run(CRITIC, view_for(inv, kinds=None, findings=True, critique=False), use_llm)
        inv.steps.append(step)

        inv.brief, step = await _run(WATCH, view_for(inv, kinds=None, findings=True, critique=True), use_llm)
        inv.steps.append(step)

        async with _step(inv, "seal"):
            untraceable = inv.untraceable_claims()
            if untraceable:
                raise InvestigationError(f"claims without evidence reached the record: {untraceable}")
            inv.status = "COMPLETE"
    except Exception as exc:  # noqa: BLE001 -- recorded, never raised into the pipeline
        _log.error("investigation_failed", extra={"action_id": action.action_id, "error": str(exc)})
        inv.status = "FAILED"

    inv.completed_at = _now()
    inv.record_sha256 = inv.compute_sha256()
    return inv


async def investigate_staged(
    pairs: list[tuple[StagedAction, AlertEvent]],
    *,
    llm_budget: int | None = None,
) -> list[Investigation]:
    """
    Investigate newly staged actions that do not have a COMPLETE investigation.

    The snapshot's expensive reads are done once and shared across the batch
    (each investigation swaps in its own trigger). The first `llm_budget`
    investigations use the model; the rest run the same flow on rules alone.
    """
    from src.storage.pool import get_db_pool
    from src.storage.repositories.investigations_repo import InvestigationsRepository

    if not pairs:
        return []
    repo = InvestigationsRepository(await get_db_pool())
    budget = settings.MAX_LLM_INVESTIGATIONS_PER_PASS if llm_budget is None else llm_budget
    base: Snapshot | None = None
    done: list[Investigation] = []
    llm_runs = 0

    for action, alert in pairs:
        if await repo.get_by_action_id(action.action_id):
            continue
        if base is None:
            base = await gather_snapshot(alert, action.action_id)
        use_llm = settings.CRITIC_ENABLED and llm_runs < budget
        inv = await run_investigation(
            action, alert, use_llm=use_llm,
            snapshot=dataclasses.replace(base, alert=alert, action_id=action.action_id),
        )
        llm_runs += use_llm
        await repo.save(inv)
        done.append(inv)
        _log.info("investigation_stored", extra={
            "action_id": action.action_id, "investigation_id": inv.investigation_id,
            "status": inv.status, "used_llm": use_llm,
            "conclusions": {f.agent: f.conclusion for f in inv.findings},
        })
    return done
