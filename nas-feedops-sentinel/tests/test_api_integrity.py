"""
The ingest API: server-assigned identity, no cross-origin overwrite, a retry that attaches,
no placeholder key, a bounded label, a body-size cap and a per-principal rate limit
(results.md S-04, S-06, S-07, S-08 and the two API limits).
"""
from __future__ import annotations

import logging
import re
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


# ── the correlation id a caller sends ───────────────────────────────────────────
#
# X-Correlation-Id is the caller's text, and a folder-scan ingest with no Idempotency-Key is named after it
# (sentinel-ingest-<id>). It is also logged and forwarded to the model gateway. So it is checked where it enters.

_CID = "X-Correlation-Id"
_MINTED = re.compile(r"[0-9a-f]{12}")        # what new_correlation_id() makes


@pytest.mark.parametrize("sent", [
    "abc123",
    "3f2b8c1e-5d4a-4e7b-9a60-1c2d3e4f5a6b",                       # a UUID
    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",    # a W3C traceparent
    "a" * 64,                                                     # the longest allowed
])
def test_a_well_formed_correlation_id_is_kept_and_names_the_workflow(api, sent):
    client, started = api
    response = _post(client, [], headers={_CID: sent})

    assert response.status_code == 200
    assert response.headers[_CID] == sent
    assert response.json()["correlation_id"] == sent
    assert started[0]["correlation_id"] == sent, "a folder scan is keyed by the request's own id"


@pytest.mark.parametrize("sent, seen_length", [
    ("a" * 65, 65),            # one past the longest allowed
    ("has space", 9),
    ("a/b", 3),
    ("a;b", 3),
    ("café", 5),          # not ASCII. The test client re-encodes the raw byte as UTF-8, so the server sees 5
    ("line\nbreak", 10),       # the server's own parser refuses this first; the rule must not depend on that
])
def test_a_malformed_correlation_id_is_replaced_and_never_reaches_the_workflow(api, sent, seen_length, caplog):
    client, started = api
    with caplog.at_level(logging.WARNING, logger="src.api"):
        # Raw bytes: the test client itself refuses a non-ASCII str header, a real client need not.
        response = _post(client, [], headers={_CID: sent.encode("latin-1")})

    assert response.status_code == 200, "a bad trace header is not worth refusing the call"
    minted = response.headers[_CID]
    assert _MINTED.fullmatch(minted)
    assert response.json()["correlation_id"] == minted
    assert started[0]["correlation_id"] == minted, "so nothing the caller typed becomes part of a workflow id"
    replaced = [r for r in caplog.records if r.getMessage() == "correlation_id_replaced"]
    assert [r.supplied_length for r in replaced] == [seen_length], "the replacement is logged, by length"
    assert not any(sent in str(vars(r)) for r in caplog.records), "the caller's value is not"


def test_no_correlation_id_still_gets_a_minted_one_without_a_warning(api, caplog):
    client, started = api
    with caplog.at_level(logging.WARNING, logger="src.api"):
        response = _post(client, [])

    assert _MINTED.fullmatch(response.headers[_CID]) and started[0]["correlation_id"] == response.headers[_CID]
    assert not [r for r in caplog.records if r.getMessage() == "correlation_id_replaced"]


def test_the_ids_this_service_mints_pass_the_inbound_rule():
    from src.logging_config import inbound_correlation_id, new_correlation_id

    minted = new_correlation_id()
    assert inbound_correlation_id(minted) == minted, "a proxy that echoes our id back must not have it replaced"


@pytest.mark.parametrize("raw, accepted", [
    (None, None), ("", None), ("   ", None),
    ("  abc-1  ", "abc-1"),                 # surrounding whitespace is not part of an id
    ("abc\n", "abc"),
    ("a" * 64, "a" * 64), ("a" * 65, None),
    ("a b", None), ("a/b", None), ("a\x00b", None), ("café", None),
    ("a.b_c:d-e", "a.b_c:d-e"),
])
def test_the_inbound_correlation_id_rule(raw, accepted):
    from src.logging_config import inbound_correlation_id

    assert inbound_correlation_id(raw) == accepted


# ── the Idempotency-Key a caller sends ──────────────────────────────────────────
#
# It names the workflow too (sentinel-pipeline-<caller>-<key>), so it is bounded the same way. A bad key is refused,
# not replaced: the key is the caller's own handle for the batch, and a different one would break its retry.

@pytest.mark.parametrize("key", ["batch-1", "batch-2026-09-16-0001", "3f2b8c1e-5d4a-4e7b-9a60-1c2d3e4f5a6b",
                                 "a.b_c:d-e", "k" * 128])
def test_a_well_formed_idempotency_key_is_accepted(api, key):
    client, started = api
    assert _post(client, headers={"Idempotency-Key": key}).status_code == 200
    assert started, "and the workflow started"


@pytest.mark.parametrize("key", ["k" * 129, "has space", "a/b", "a;b", "line\nbreak"])
def test_a_malformed_idempotency_key_is_refused_before_anything_starts(api, key):
    client, started = api
    response = _post(client, headers={"Idempotency-Key": key})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_idempotency_key"
    assert started == [], "nothing was started under a key the caller must correct"


def test_no_idempotency_key_is_still_fine(api):
    client, started = api
    assert _post(client).status_code == 200 and started


def test_a_workflow_key_the_service_derived_is_not_held_to_the_inbound_rule():
    """The pipeline and the activities bind the workflow key: an Idempotency-Key joined to the caller's name, or a
    payload hash. It is ours, and longer than a caller's id may be; rewriting it would cut the log's tie to the run."""
    from src.logging_config import bind_correlation_id

    derived = "caller-a-" + "k" * 120
    assert bind_correlation_id(derived) == derived
