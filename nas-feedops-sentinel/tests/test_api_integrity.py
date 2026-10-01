"""
The ingest API: server-assigned identity, no cross-origin overwrite, a retry that attaches,
no placeholder key, a bounded label, a body-size cap and a per-principal rate limit
(results.md S-04, S-06, S-07, S-08 and the two API limits).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from src import storage
from src.config import is_placeholder_secret, settings
from src.models import NASEvent
from src.rate_limit import TokenBucket

_PAYLOAD = {"airport_code": "KDCA", "tracon": "PCT", "service_name": "ISMC", "message_type": "m",
            "event_time_utc": "2026-09-25T12:00:00Z", "received_time_utc": "2026-09-25T12:00:01Z",
            "feed_status": "operational"}


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


@pytest.fixture
def api(monkeypatch):
    import src.api as api_module

    started: list[dict] = []

    async def fake_pipeline(events, **kw):
        started.append({"events": events, **kw})
        return f"sentinel-pipeline-{kw['correlation_id']}"

    async def fake_ingest(**kw):
        started.append(kw)
        return "wf-ingest"

    monkeypatch.setattr(api_module, "start_pipeline_workflow", fake_pipeline)
    monkeypatch.setattr(api_module, "start_ingest_workflow", fake_ingest)
    monkeypatch.setattr(settings, "API_KEY", "k")
    monkeypatch.setattr(settings, "API_KEYS", {})
    monkeypatch.setattr(settings, "API_INGEST_RATE_PER_MINUTE", 0)
    monkeypatch.setattr(api_module, "_ingest_limiter", TokenBucket())
    return TestClient(api_module.app), started


def _post(client, events=None, *, key="k", headers=None, **body):
    return client.post("/v1/ingest", json={"events": events if events is not None else [_PAYLOAD], **body},
                       headers={"X-API-Key": key, **(headers or {})})


# ── S-04: identity is the server's ─────────────────────────────────────────────

def test_a_client_chosen_event_id_is_dropped_and_replaced_by_a_stable_one(api):
    client, started = api
    chosen = {**_PAYLOAD, "event_id": "some-live-event-id"}

    assert _post(client, [chosen, _PAYLOAD]).status_code == 200
    ids = [e["event_id"] for e in started[0]["events"]]
    assert "some-live-event-id" not in ids and len(set(ids)) == 2, "one id per event, none of them the client's"

    assert _post(client, [chosen, _PAYLOAD]).status_code == 200
    assert [e["event_id"] for e in started[1]["events"]] == ids, "the same request gets the same ids: a retry lands on the same rows"


def test_the_ids_do_not_depend_on_what_the_client_put_in_event_id(api):
    client, started = api
    _post(client, [{**_PAYLOAD, "event_id": "a"}])
    _post(client, [{**_PAYLOAD, "event_id": "b"}])
    assert started[0]["events"][0]["event_id"] == started[1]["events"][0]["event_id"]


def test_two_callers_with_the_same_idempotency_key_do_not_share_ids_or_a_workflow(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_KEYS", {"key-a": "caller-a", "key-b": "caller-b"})
    monkeypatch.setattr(settings, "API_KEY", None)
    headers = {"Idempotency-Key": "batch-1"}

    assert _post(client, key="key-a", headers=headers).status_code == 200
    assert _post(client, key="key-b", headers=headers).status_code == 200

    a, b = started
    assert a["correlation_id"] != b["correlation_id"], "a key is the caller's own"
    assert a["events"][0]["event_id"] != b["events"][0]["event_id"]


def test_a_duplicate_workflow_start_attaches_instead_of_failing(api, monkeypatch):
    import src.api as api_module

    client, _ = api

    async def already_running(events, **kw):
        raise WorkflowAlreadyStartedError(f"sentinel-pipeline-{kw['correlation_id']}", "SentinelPipelineWorkflow")

    monkeypatch.setattr(api_module, "start_pipeline_workflow", already_running)
    response = _post(client, headers={"Idempotency-Key": "batch-1"})

    assert response.status_code == 200
    body = response.json()
    assert body["duplicate"] is True
    assert body["workflow_id"] == f"sentinel-pipeline-{body['workflow_key']}"


async def test_an_event_never_rewrites_a_row_of_another_origin():
    t = datetime.now(timezone.utc)
    live = NASEvent(event_id="e1", airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="m",
                    event_time_utc=t, received_time_utc=t, latency_ms=100.0, feed_status="operational", data_origin="live")
    await storage.save_events([live])

    forged = live.model_copy(update={"data_origin": "api", "feed_status": "disconnected", "latency_ms": 99999.0})
    await storage.save_events([forged])

    (row,) = [r for r in await storage.get_recent_events(limit=50) if r["event_id"] == "e1"]
    assert (row["data_origin"], row["feed_status"], row["latency_ms"]) == ("live", "operational", 100.0)

    same_origin = live.model_copy(update={"feed_status": "degraded"})
    await storage.save_events([same_origin])
    (row,) = [r for r in await storage.get_recent_events(limit=50) if r["event_id"] == "e1"]
    assert row["feed_status"] == "degraded", "the feed's own re-drop still updates its row"


async def test_a_row_migrated_with_an_unknown_origin_is_still_replaced_by_the_feed():
    from src.storage.pool import get_db_pool

    t = datetime.now(timezone.utc)
    event = NASEvent(event_id="e2", airport_code="KDCA", tracon="PCT", service_name="ISMC", message_type="m",
                     event_time_utc=t, received_time_utc=t, latency_ms=100.0, feed_status="operational", data_origin="unknown")
    await storage.save_events([event])
    await storage.save_events([event.model_copy(update={"data_origin": "live"})])
    pool = await get_db_pool()
    assert await pool.fetchval("SELECT data_origin FROM nas_events WHERE event_id = 'e2'") == "live"


# ── S-06: a placeholder key is not a key ───────────────────────────────────────

@pytest.mark.parametrize("value", ["change-me", "Change-Me", "changeme", "replace-me-please", "your-api-key", "<key>", "", None, "password"])
def test_placeholder_secrets_are_recognised(value):
    assert is_placeholder_secret(value)


@pytest.mark.parametrize("value", ["k", "7f3a9c1e5b2d48609a1c", "ingest-service"])
def test_a_real_secret_is_not_a_placeholder(value):
    assert not is_placeholder_secret(value)


def test_the_api_refuses_to_run_on_a_placeholder_key(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_KEY", "change-me")
    assert _post(client, key="change-me").status_code == 503
    monkeypatch.setattr(settings, "API_KEY", None)
    monkeypatch.setattr(settings, "API_KEYS", {"changeme": "ingest"})
    assert _post(client, key="changeme").status_code == 503
    assert started == []


# ── S-07: the label is bounded ─────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["x" * 33, "has space", "semi;colon", ""])
def test_an_unbounded_or_odd_label_is_refused(api, label):
    client, started = api
    assert _post(client, label=label).status_code == 422
    assert started == []


def test_a_normal_label_is_accepted(api):
    client, started = api
    assert _post(client, label="nightly-replay.2").status_code == 200
    assert started[0]["label"] == "nightly-replay.2"


def test_the_metric_source_label_is_drawn_from_a_fixed_set():
    from src.pipeline import _source_label

    assert _source_label("SWIM_SCAN") == "swim_scan" and _source_label("API") == "api"
    assert _source_label("whatever-a-client-sent") == "other"


# ── S-08: no ungated ticket writer ─────────────────────────────────────────────

def test_the_ungated_ticket_writer_is_not_exported():
    assert not hasattr(storage, "save_operational_ticket")
    assert "save_operational_ticket" not in storage.__all__
    assert hasattr(storage, "create_operational_ticket"), "the gated path stays"


# ── the body-size cap ──────────────────────────────────────────────────────────

def test_a_body_over_the_cap_is_refused_before_anything_starts(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_MAX_BODY_BYTES", 300)
    response = _post(client, [{**_PAYLOAD, "runway": "x" * 2000}])
    assert response.status_code == 413 and response.json()["error"] == "request_body_too_large"
    assert started == []
    assert _post(client).status_code == 200, "a normal request is unaffected"


def test_a_chunked_body_over_the_cap_is_refused_as_it_arrives(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_MAX_BODY_BYTES", 300)

    def chunks():
        yield b'{"events": [{"runway": "'
        for _ in range(50):
            yield b"x" * 100
        yield b'"}]}'

    response = client.post("/v1/ingest", content=chunks(), headers={"X-API-Key": "k", "Content-Type": "application/json"})
    assert response.status_code == 413 and started == []


# ── the per-principal rate limit ───────────────────────────────────────────────

def test_a_principal_past_its_rate_is_refused_with_retry_after(api, monkeypatch):
    client, started = api
    monkeypatch.setattr(settings, "API_INGEST_RATE_PER_MINUTE", 2)
    assert [_post(client, [{**_PAYLOAD, "runway": str(i)}]).status_code for i in range(3)] == [200, 200, 429]
    limited = _post(client)
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1
    assert len(started) == 2


def test_one_principals_rate_does_not_spend_anothers(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(settings, "API_KEY", None)
    monkeypatch.setattr(settings, "API_KEYS", {"key-a": "caller-a", "key-b": "caller-b"})
    monkeypatch.setattr(settings, "API_INGEST_RATE_PER_MINUTE", 1)
    assert _post(client, key="key-a").status_code == 200
    assert _post(client, key="key-a", label="second").status_code == 429
    assert _post(client, key="key-b").status_code == 200


def test_an_unauthenticated_call_does_not_spend_a_principals_rate(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(settings, "API_INGEST_RATE_PER_MINUTE", 1)
    for _ in range(3):
        assert _post(client, key="wrong").status_code == 401
    assert _post(client).status_code == 200


def test_the_token_bucket_refills_evenly():
    now = [0.0]
    bucket = TokenBucket(clock=lambda: now[0])
    assert [bucket.try_acquire("p", 60) for _ in range(60)] == [0.0] * 60, "a burst of one minute's worth"
    wait = bucket.try_acquire("p", 60)
    assert 0.9 < wait <= 1.0
    now[0] += 1.0
    assert bucket.try_acquire("p", 60) == 0.0 and bucket.try_acquire("p", 60) > 0
    assert bucket.try_acquire("other", 60) == 0.0
    assert bucket.try_acquire("p", 0) == 0.0, "0 turns the limit off"
