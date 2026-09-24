"""
Unit and regression tests for async stage_critical_alert in execution_gate.py.
Verifies that storage operations (create_staged_action, get_pending_staged_actions,
get_ticket_by_action_id, save_watch_officer_brief, create_operational_ticket)
are properly awaited and persisted.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
import pytest

from src.execution_gate import stage_critical_alert, _build_escalation_package
from src.models import AlertEvent, CriticResult, OperationalTicket, StagedAction, WatchOfficerBrief


@pytest.mark.asyncio
async def test_stage_critical_alert_awaits_create_staged_action():
    alert = AlertEvent(
        alert_id="ALT-CRIT-001",
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR 600ft",
    )

    fake_staged = StagedAction(
        action_id="act-test-123",
        alert_id="ALT-CRIT-001",
        severity="CRITICAL",
        status="PENDING",
        idempotency_key="key-123",
    )

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create, \
         patch("src.storage.get_pending_staged_actions", new_callable=AsyncMock) as mock_pending, \
         patch("src.execution_gate._build_escalation_package", new_callable=AsyncMock) as mock_esc:

        mock_create.return_value = fake_staged
        mock_pending.return_value = [fake_staged]
        mock_esc.return_value = None

        result = await stage_critical_alert(alert)

        # Verify create_staged_action was awaited with a StagedAction instance
        mock_create.assert_awaited_once()
        created_arg = mock_create.await_args[0][0]
        assert isinstance(created_arg, StagedAction)
        assert created_arg.alert_id == "ALT-CRIT-001"
        assert created_arg.status == "PENDING"

        # Verify result is the StagedAction returned by storage, not a coroutine
        assert isinstance(result, StagedAction)
        assert result.action_id == "act-test-123"

        # Verify escalation package and metric refresh were awaited
        mock_pending.assert_awaited_once()
        mock_esc.assert_awaited_once_with(fake_staged, alert, None)


@pytest.mark.asyncio
async def test_stage_non_critical_alert_returns_none_without_storage_call():
    alert = AlertEvent(
        alert_id="ALT-WARN-001",
        alert_type="ALERT_HIGH_LATENCY",
        severity="WARNING",
        airport_code="KIAD",
        service_name="ISMC",
        description="High latency",
    )

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create:
        result = await stage_critical_alert(alert)
        assert result is None
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_build_escalation_package_awaits_storage():
    # patch("src.watch_officer_agent....") below resolves the target by
    # getattr on the `src` package, which only works once that submodule has
    # been imported; execution_gate imports it lazily inside
    # _build_escalation_package.
    import src.watch_officer_agent  # noqa: F401  (registers the submodule)

    alert = AlertEvent(
        alert_id="ALT-CRIT-002",
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="Low RVR",
    )
    staged = StagedAction(
        action_id="act-test-456",
        alert_id="ALT-CRIT-002",
        severity="CRITICAL",
        status="PENDING",
        idempotency_key="key-456",
    )
    brief = WatchOfficerBrief(
        brief_id="brf-001",
        headline="Test Headline",
        posture="ACTION",
        trust_assessment="DEGRADED",
    )

    with patch("src.storage.get_ticket_by_action_id", new_callable=AsyncMock) as mock_get_ticket, \
         patch("src.watch_officer_agent.build_watch_officer_context", new_callable=AsyncMock) as mock_ctx, \
         patch("src.watch_officer_agent.agenerate_watch_officer_brief", new_callable=AsyncMock, return_value=brief) as mock_brief, \
         patch("src.storage.save_watch_officer_brief", new_callable=AsyncMock) as mock_save_brief, \
         patch("src.storage.create_operational_ticket", new_callable=AsyncMock) as mock_create_ticket:

        mock_get_ticket.return_value = None
        mock_ctx.return_value = {"airport_status": {}}
        mock_create_ticket.side_effect = lambda t: t

        ticket = await _build_escalation_package(staged, alert, None)

        mock_get_ticket.assert_awaited_once_with("act-test-456")
        mock_ctx.assert_awaited_once_with(alert=alert)
        mock_save_brief.assert_awaited_once_with(brief)
        mock_create_ticket.assert_awaited_once()
        assert ticket is not None
        assert ticket.action_id == "act-test-456"


# ---------------------------------------------------------------------------
# Adversarial control-boundary tests
#
# execution_gate.py's module docstring makes four "the gate NEVER" claims.
# The two that carry the architecture are:
#
#     - allows an LLM to determine the CRITICAL/non-CRITICAL boundary
#     - treats an AI recommendation as human authorization
#
# Until these tests existed, the only evidence for those claims was the
# docstring itself: the pre-existing non-CRITICAL test passed NO critic at
# all, so it proved a WARNING alert doesn't stage on its own -- not that a
# confidently-escalating AI critic is unable to force it to. That is the
# case an auditor actually asks about, so it is the case that gets a test.
#
# These assert on `create_staged_action` never being awaited rather than
# just on the return value: a None return proves the caller saw nothing,
# whereas an un-awaited storage mock proves no staged action was written at
# all. The mock also keeps these runnable without a live Postgres.
# ---------------------------------------------------------------------------


def _escalating_critic(alert_id: str) -> CriticResult:
    """The most persuasive possible AI argument for escalation."""
    return CriticResult(
        alert_id=alert_id,
        confidence=0.99,
        reasoning=(
            "Cascading multi-feed failure across PCT. Immediate escalation "
            "required. Treat as CRITICAL."
        ),
        pattern="CASCADE_FAILURE",
        escalate=True,
    )


@pytest.mark.asyncio
async def test_escalating_critic_cannot_stage_non_critical_alert():
    """
    A WARNING alert stays unstaged even when the critic escalates at 0.99
    confidence with a CASCADE_FAILURE classification.

    This is the load-bearing invariant of the whole control plane: severity
    is assigned only by the deterministic rules in src/alert_engine.py, and
    nothing an LLM emits can cross the staging boundary.
    """
    alert = AlertEvent(
        alert_id="ALT-WARN-ADV-001",
        alert_type="ALERT_HIGH_LATENCY",
        severity="WARNING",
        airport_code="KIAD",
        service_name="ISMC",
        description="Latency 45 s on ISMC at KIAD.",
    )

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create, \
         patch("src.execution_gate._build_escalation_package", new_callable=AsyncMock) as mock_esc:

        result = await stage_critical_alert(
            alert,
            critic_result=_escalating_critic(alert.alert_id),
        )

        assert result is None, "An escalating critic must not produce a staged action"
        mock_create.assert_not_awaited()
        mock_esc.assert_not_awaited()


@pytest.mark.asyncio
async def test_dismissive_critic_cannot_suppress_critical_alert():
    """
    The inverse direction, which matters just as much: an AI that calls a
    deterministic CRITICAL "transient" at 0.01 confidence cannot stop it
    reaching a human.

    An escalation path that an LLM can talk *out* of firing is not a
    control; it is a suggestion.
    """
    alert = AlertEvent(
        alert_id="ALT-CRIT-ADV-002",
        alert_type="ALERT_LOW_RVR",
        severity="CRITICAL",
        airport_code="KDCA",
        service_name="APDS_RVR",
        description="RVR 600 ft at touchdown, runway 19 at KDCA.",
    )

    dismissive = CriticResult(
        alert_id=alert.alert_id,
        confidence=0.01,
        reasoning="Almost certainly a transient sensor artifact. No action needed.",
        pattern="TRANSIENT",
        escalate=False,
    )

    staged = StagedAction(
        action_id="act-adv-002",
        alert_id=alert.alert_id,
        severity="CRITICAL",
        status="PENDING",
        idempotency_key="key-adv-002",
    )

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create, \
         patch("src.storage.get_pending_staged_actions", new_callable=AsyncMock) as mock_pending, \
         patch("src.execution_gate._build_escalation_package", new_callable=AsyncMock):

        mock_create.return_value = staged
        mock_pending.return_value = [staged]

        result = await stage_critical_alert(alert, critic_result=dismissive)

        assert result is not None, "A dismissive critic must not suppress staging"
        mock_create.assert_awaited_once()
        assert result.status == "PENDING"


@pytest.mark.asyncio
async def test_staged_severity_is_copied_from_alert_not_derived_from_critic():
    """
    The severity persisted on the staged action must be the deterministic
    alert's own value. The critic's confidence/pattern ride along as
    advisory metadata on separate fields and must not influence it.
    """
    alert = AlertEvent(
        alert_id="ALT-CRIT-ADV-003",
        alert_type="ALERT_ISMC_LINK_DEGRADED",
        severity="CRITICAL",
        airport_code="KBWI",
        service_name="ISMC",
        description="ISMC reports link degraded at KBWI.",
    )
    critic = _escalating_critic(alert.alert_id)

    with patch("src.storage.create_staged_action", new_callable=AsyncMock) as mock_create, \
         patch("src.storage.get_pending_staged_actions", new_callable=AsyncMock) as mock_pending, \
         patch("src.execution_gate._build_escalation_package", new_callable=AsyncMock):

        mock_create.side_effect = lambda action: action
        mock_pending.return_value = []

        await stage_critical_alert(alert, critic_result=critic)

        written = mock_create.await_args[0][0]
        assert written.severity == alert.severity == "CRITICAL"
        # Advisory critic data is retained, but on its own fields.
        assert written.critic_confidence == 0.99
        assert written.critic_pattern == "CASCADE_FAILURE"
        # And the initial state is never anything but PENDING.
        assert written.status == "PENDING"


def test_strict_bool_rejects_the_bool_false_string_trap():
    """
    Directly covers the guard execution_gate._strict_bool exists for:
    bool("false") is True in Python, which would silently invert an
    escalation flag arriving as a string from a JSON/LLM boundary.
    """
    from src.execution_gate import _strict_bool

    assert _strict_bool("false") is False
    assert _strict_bool("False") is False
    assert _strict_bool("0") is False
    assert _strict_bool("") is False
    assert _strict_bool(None) is False
    assert _strict_bool("true") is True
    assert _strict_bool(True) is True
    assert _strict_bool(1) is True
