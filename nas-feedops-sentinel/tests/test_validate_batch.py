"""validate_batch(): the one place a ValidationResult becomes the fields the pipeline reads."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import src.agent_evaluator as agent_evaluator
import src.pipeline as pipeline
from src.models import NASEvent
from src.validators import validate_batch


def _event(**overrides) -> NASEvent:
    now = datetime.now(timezone.utc)
    fields = dict(
        airport_code="KDCA", tracon="PCT", service_name="SMES", message_type="SURFACE_MOVEMENT",
        event_time_utc=now - timedelta(seconds=1), received_time_utc=now, latency_ms=1_000.0,
        track_id="TRK1",
    )
    fields.update(overrides)
    return NASEvent(**fields)


def test_stamps_status_and_errors_on_each_event_in_order():
    good, bad = _event(), _event(track_id=None)
    results = validate_batch([good, bad])
    assert [r.passed for r in results] == [True, False]
    assert good.validation_status == "PASSED" and not good.validation_errors
    assert bad.validation_status == "FAILED"
    assert bad.validation_errors == results[1].errors and bad.validation_errors


def test_empty_batch_is_a_no_op():
    assert validate_batch([]) == []


def test_callers_do_not_carry_their_own_copy_of_the_loop():
    """The loop existed three times (pipeline once, agent_evaluator twice); one copy was missing validation_errors."""
    for module in (pipeline, agent_evaluator):
        source = inspect.getsource(module)
        assert "validate_event(" not in source, module.__name__
        assert "validate_batch(" in source, module.__name__
