"""
Stable identities: the same input, processed twice, is the same data.

Events and alerts used to get random uuid4 identities. A re-dropped SWIM
file, a double scan, or a Temporal activity retry therefore produced
duplicate events, duplicate alerts, and -- because staging is idempotent per
alert -- a duplicate staged action (and Critic call) for every CRITICAL alert.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src import storage
from src.alert_engine import check_alerts
from src.swim_file_ingest import _normalise_record, _parse_file

CAPTURE = Path(__file__).parent / "fixtures" / "swim_kit" / "capture_2026-08-27"


def _event_ids_for(path: Path) -> list[str]:
    records, error = _parse_file(path)
    assert error is None
    ids = []
    for index, raw in enumerate(records):
        event, err = _normalise_record(raw, str(path), index=index)
        assert event is not None, err
        ids.append(event.event_id)
    return ids


def test_the_same_swim_message_gets_the_same_event_ids(tmp_path: Path):
    original = CAPTURE / "1787863346242"               # APDS: six runway records
    copy = tmp_path / "renamed_copy"
    shutil.copy(original, copy)
    first, again = _event_ids_for(original), _event_ids_for(copy)
    assert first == again, "identity comes from the message headers, not the file name"
    assert len(set(first)) == len(first) == 6, "records within one message stay distinct"


def test_different_messages_get_different_event_ids():
    a = set(_event_ids_for(CAPTURE / "1787863346242"))
    b = set(_event_ids_for(CAPTURE / "1787863346109"))
    assert a and b and not (a & b)


def test_alerts_have_the_same_identity_when_the_same_events_are_evaluated_again():
    from src.demo_scenario import generate_cascade_failure_scenario

    events = generate_cascade_failure_scenario()
    first = [a.alert_id for a in check_alerts(events)]
    again = [a.alert_id for a in check_alerts(events)]
    assert first == again
    assert len(set(first)) == len(first), "one event can raise distinct alerts (e.g. touchdown vs rollout)"


def test_replaying_the_same_file_twice_gives_the_same_events(tmp_path: Path):
    from src.replay import replay_from_directory

    sample = {"airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC", "message_type": "link",
              "event_time_utc": "2026-09-25T12:00:00+00:00", "received_time_utc": "2026-09-25T12:00:01+00:00",
              "feed_status": "operational"}
    (tmp_path / "replay.json").write_text(json.dumps([sample, dict(sample, airport_code="KIAD")]))
    first, _ = replay_from_directory(tmp_path)
    again, _ = replay_from_directory(tmp_path)
    assert [e.event_id for e in first] == [e.event_id for e in again]
    assert len({e.event_id for e in first}) == 2


# ── Against the database: a retried pass changes nothing ─────────────────────

@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield storage
    storage.override_db_path(":memory:")


async def test_processing_the_same_batch_twice_duplicates_nothing(db, monkeypatch):
    """What a Temporal activity retry or a double scan does to the pipeline."""
    from src.config import settings
    from src.demo_scenario import generate_cascade_failure_scenario
    from src.pipeline import run_pipeline
    from src.storage.pool import get_db_pool

    monkeypatch.setattr(settings, "CRITIC_ENABLED", False)
    events = generate_cascade_failure_scenario()
    await run_pipeline(events, label="FIRST")
    pool = await get_db_pool()

    async def counts():
        return {t: await pool.fetchval(f"SELECT count(*) FROM {t}")
                for t in ("nas_events", "alert_events", "staged_actions", "operational_tickets", "investigations")}

    before = await counts()
    await run_pipeline([e.model_copy() for e in events], label="RETRY")
    assert await counts() == before
    assert before["staged_actions"] > 0
