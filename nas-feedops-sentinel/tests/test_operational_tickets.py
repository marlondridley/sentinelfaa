"""
Tests for OperationalTicket — the internal escalation package.

Verifies:
  - Ticket is created when a CRITICAL alert is staged
  - Duplicate staging of the same alert returns the existing ticket (idempotent)
  - get_draft_operational_tickets() lists DRAFT only
  - decide_staged_action moves the ticket to APPROVED_FOR_ESCALATION / REJECTED /
    OVERRIDDEN in the same transaction as its staged_action, never one without the
    other (the split-brain approve_operational_ticket etc. used to allow)
  - MONITORING is non-terminal (can later be re-decided); the other three are sticky
  - All ticket transitions write an action_audit_log row
  - The raw approve/reject/override doors are gone from the public storage API
"""
from __future__ import annotations

import pytest

from src import storage
from src.auth import Decision, Principal, PrincipalSource
from src.execution_gate import DecisionNotRecorded, decide_staged_action, stage_critical_alert
from src.models import AlertEvent
from src.storage.pool import get_db_pool
from src.storage.repositories.tickets_repo import TicketsRepository


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _critical_alert(**kwargs) -> AlertEvent:
    base = dict(
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 700 ft at KDCA runway 19.",
    )
    base.update(kwargs)
    return AlertEvent(**base)


async def _persist_and_stage(alert: AlertEvent, critic_result=None):
    """Save the alert, then stage it -- the same order src/pipeline.py uses
    (staged_actions.alert_id is a foreign key to alert_events)."""
    await storage.save_alert(alert)
    return await stage_critical_alert(alert, critic_result)


SUPERVISOR = Principal("smith", "Supervisor", PrincipalSource.OIDC, roles=frozenset({"supervisor"}))


# ── Ticket creation tied to staging ──────────────────────────────────────────

class TestTicketCreation:
    async def test_critical_alert_creates_ticket(self):
        alert = _critical_alert()
        action = await _persist_and_stage(alert)
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        assert ticket is not None
        assert ticket.action_id == action.action_id
        assert ticket.alert_id == alert.alert_id
        assert ticket.status == "DRAFT"
        assert ticket.brief_id  # brief was generated
        assert ticket.created_by_agent == "watch_officer"

    async def test_duplicate_alert_returns_existing_ticket(self):
        alert = _critical_alert()
        await _persist_and_stage(alert)
        ticket_a = await storage.get_ticket_by_action_id(
            (await storage.get_pending_staged_actions())[0].action_id
        )
        # Re-stage same alert
        await _persist_and_stage(alert)
        ticket_b = await storage.get_ticket_by_action_id(
            (await storage.get_pending_staged_actions())[0].action_id
        )
        assert ticket_a.ticket_id == ticket_b.ticket_id, "duplicate staging must be idempotent"
        assert len(await storage.get_draft_operational_tickets()) == 1

    async def test_non_critical_alert_does_not_create_ticket(self):
        warn = _critical_alert(severity="WARNING")
        await _persist_and_stage(warn)  # returns None, no ticket
        assert await storage.get_draft_operational_tickets() == []

    async def test_draft_query_returns_only_drafts(self):
        a1 = await _persist_and_stage(_critical_alert(alert_type="ALERT_LOW_RVR"))
        await _persist_and_stage(_critical_alert(alert_type="ALERT_ISMC_LINK_DEGRADED",
                                                 service_name="ISMC"))
        await decide_staged_action(a1.action_id, Decision.APPROVE, SUPERVISOR, "")

        drafts = await storage.get_draft_operational_tickets()
        draft_ids = {t.ticket_id for t in drafts}
        t1 = await storage.get_ticket_by_action_id(a1.action_id)
        # The approved one should be excluded
        assert t1.ticket_id not in draft_ids


# ── The ticket moves with its staged_action, never without it ─────────────────
#
# approve_operational_ticket, reject_operational_ticket and
# mark_operational_ticket_overridden used to let any code that imports src.storage
# move a ticket alone, with no principal, no role check, and no write to the
# staged_action underneath it -- a ticket could read APPROVED_FOR_ESCALATION while
# its review sat PENDING forever. decide_staged_action is now the only door for
# those three outcomes, and it moves both rows in one transaction.

class TestTicketFollowsItsAction:
    async def test_approve_moves_the_action_and_its_ticket_together(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)

        decided = await decide_staged_action(action.action_id, Decision.APPROVE, SUPERVISOR, "cleared by tower")

        assert decided.status == "APPROVED"
        updated_ticket = await storage.get_ticket_by_action_id(action.action_id)
        assert updated_ticket.status == "APPROVED_FOR_ESCALATION"
        assert updated_ticket.decided_by == "smith (Supervisor) roles=supervisor [oidc]"
        assert updated_ticket.decision_note == "cleared by tower"
        assert ticket.ticket_id == updated_ticket.ticket_id, "the same ticket, moved, not a new one"

    async def test_reject_moves_the_action_and_its_ticket_together(self):
        action = await _persist_and_stage(_critical_alert())
        decided = await decide_staged_action(action.action_id, Decision.REJECT, SUPERVISOR, "false alarm")

        assert decided.status == "REJECTED"
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        assert ticket.status == "REJECTED"

    async def test_override_moves_the_action_and_its_ticket_together(self):
        action = await _persist_and_stage(_critical_alert())
        decided = await decide_staged_action(
            action.action_id, Decision.OVERRIDE, SUPERVISOR, "escalating with edits",
            overridden_recommendation="Notify tower directly", overridden_severity="CRITICAL",
        )

        assert decided.status == "OVERRIDDEN"
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        assert ticket.status == "OVERRIDDEN"

    async def test_a_second_decision_neither_moves_the_ticket_nor_reports_success(self):
        """The exact split-brain the raw doors allowed: this is the regression test for it."""
        action = await _persist_and_stage(_critical_alert())
        await decide_staged_action(action.action_id, Decision.APPROVE, SUPERVISOR, "first")

        with pytest.raises(DecisionNotRecorded, match="already APPROVED"):
            await decide_staged_action(action.action_id, Decision.REJECT, SUPERVISOR, "second, too late")

        # Neither row moved a second time: the ticket cannot end up disagreeing with its action.
        action_now = [a for a in await storage.get_recent_staged_actions(limit=5)
                      if a.action_id == action.action_id][0]
        ticket_now = await storage.get_ticket_by_action_id(action.action_id)
        assert action_now.status == "APPROVED" and ticket_now.status == "APPROVED_FOR_ESCALATION"

    async def test_monitoring_leaves_the_action_pending_and_marks_only_the_ticket(self):
        action = await _persist_and_stage(_critical_alert())
        await decide_staged_action(action.action_id, Decision.MONITOR, SUPERVISOR, "watching weather")

        ticket = await storage.get_ticket_by_action_id(action.action_id)
        assert ticket.status == "MONITORING"
        action_now = [a for a in await storage.get_recent_staged_actions(limit=5)
                      if a.action_id == action.action_id][0]
        assert action_now.status == "PENDING", "MONITOR is advisory; the review still awaits a real decision"

    async def test_monitoring_is_not_terminal_a_real_decision_still_lands(self):
        action = await _persist_and_stage(_critical_alert())
        await decide_staged_action(action.action_id, Decision.MONITOR, SUPERVISOR, "watching")

        decided = await decide_staged_action(action.action_id, Decision.APPROVE, SUPERVISOR, "escalating now")

        assert decided.status == "APPROVED"
        assert (await storage.get_ticket_by_action_id(action.action_id)).status == "APPROVED_FOR_ESCALATION"


# ── TicketsRepository.transition_in(): the primitive itself ────────────────────
#
# White-box, the same way tests/test_swim_file_ingest.py reaches underscored parsing
# helpers directly: decide_staged_action_with_ticket is the only production caller,
# but the sticky/non-terminal state machine belongs to this class and is tested here
# rather than only indirectly through the gate above.

class TestTransitionInStateMachine:
    async def _repo(self) -> TicketsRepository:
        return TicketsRepository(await get_db_pool())

    async def test_approved_is_sticky(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        repo = await self._repo()

        await repo._transition(ticket.ticket_id, "APPROVED_FOR_ESCALATION", "a", "first")
        again = await repo._transition(ticket.ticket_id, "REJECTED", "b", "overrule?")

        assert again.status == "APPROVED_FOR_ESCALATION" and again.decided_by == "a"

    async def test_rejected_is_sticky(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        repo = await self._repo()

        await repo._transition(ticket.ticket_id, "REJECTED", "a", "ignore")
        again = await repo._transition(ticket.ticket_id, "APPROVED_FOR_ESCALATION", "b", "overrule?")

        assert again.status == "REJECTED" and again.decided_by == "a"

    async def test_monitoring_writes_audit_row(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)

        await storage.mark_operational_ticket_monitoring(ticket.ticket_id, actor="x", note="watching")

        audit = await storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(r["new_status"] == "MONITORING" for r in audit)


# ── Audit trail integration ───────────────────────────────────────────────────

class TestAuditIntegration:
    async def test_ticket_creation_writes_audit_row(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        audit = await storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(r["new_status"] == "DRAFT" for r in audit)

    async def test_approve_writes_audit_row(self):
        action = await _persist_and_stage(_critical_alert())
        ticket = await storage.get_ticket_by_action_id(action.action_id)
        await decide_staged_action(action.action_id, Decision.APPROVE, SUPERVISOR, "ok")
        audit = await storage.get_action_audit_log(action_id=f"ticket:{ticket.ticket_id}")
        assert any(
            r["previous_status"] == "DRAFT" and r["new_status"] == "APPROVED_FOR_ESCALATION"
            for r in audit
        )


# ── The removed doors stay removed ─────────────────────────────────────────────

def test_the_raw_ticket_decision_doors_are_gone():
    """
    approve_operational_ticket, reject_operational_ticket and
    mark_operational_ticket_overridden bypassed guard_human_gate and authorize()
    entirely and could move a ticket without its staged_action. If one of these
    names reappears here, it is that same hole again.
    """
    for name in ("approve_operational_ticket", "reject_operational_ticket",
                 "mark_operational_ticket_overridden"):
        assert name not in storage.__all__
        assert not hasattr(storage, name)
    for name in ("approve", "reject", "mark_overridden"):
        assert not hasattr(TicketsRepository, name)
