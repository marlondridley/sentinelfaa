"""
The Temporal worker and client helpers (src/temporal_worker.py): one client per loop, the workflow starters,
the heartbeat and watchdog, and the worker's start-up and shutdown. Temporal itself is replaced.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src import temporal_worker as tw


class _Handle:
    def __init__(self, workflow_id: str) -> None:
        self.id = workflow_id


class _Client:
    """A Temporal client that records what it was asked to start."""

    def __init__(self) -> None:
        self.started: list[dict] = []

    async def start_workflow(self, run, *, args, id, task_queue):
        self.started.append({"run": run, "args": args, "id": id, "task_queue": task_queue})
        return _Handle(id)


@pytest.fixture
def client(monkeypatch):
    fake = _Client()

    async def _get():
        return fake

    monkeypatch.setattr(tw, "get_or_create_client", _get)
    return fake


# ── The client is made once per event loop ───────────────────────────────────────────────────────────

async def test_the_client_is_connected_once_and_then_reused(monkeypatch):
    connects: list[int] = []

    async def _connect():
        connects.append(1)
        return object()

    monkeypatch.setattr(tw, "connect_client", _connect)
    loop = asyncio.get_running_loop()
    tw._CLIENTS.pop(loop, None)

    first = await tw.get_or_create_client()
    second = await tw.get_or_create_client()

    assert first is second
    assert connects == [1]
    tw._CLIENTS.pop(loop, None)


async def test_concurrent_callers_share_one_connection(monkeypatch):
    connects: list[int] = []

    async def _connect():
        connects.append(1)
        await asyncio.sleep(0.01)
        return object()

    monkeypatch.setattr(tw, "connect_client", _connect)
    loop = asyncio.get_running_loop()
    tw._CLIENTS.pop(loop, None)
    tw._CLIENT_LOCKS.pop(loop, None)

    clients = await asyncio.gather(*(tw.get_or_create_client() for _ in range(5)))

    assert len({id(c) for c in clients}) == 1
    assert connects == [1], "the second caller found the first one's client behind the lock"
    tw._CLIENTS.pop(loop, None)


async def test_connect_client_uses_the_configured_host_and_namespace(monkeypatch):
    seen: dict = {}

    async def _connect(host, namespace):
        seen.update(host=host, namespace=namespace)
        return "client"

    monkeypatch.setattr(tw.Client, "connect", _connect)
    monkeypatch.setattr(tw.settings, "TEMPORAL_HOST", "temporal:7233")
    monkeypatch.setattr(tw.settings, "TEMPORAL_NAMESPACE", "sentinel")

    assert await tw.connect_client() == "client"
    assert seen == {"host": "temporal:7233", "namespace": "sentinel"}


# ── Starting workflows ───────────────────────────────────────────────────────────────────────────────

def test_the_pipeline_workflow_id_carries_the_correlation_id():
    assert tw.pipeline_workflow_id("abc") == "sentinel-pipeline-abc"


async def test_a_pipeline_workflow_starts_under_the_given_correlation_id(client):
    workflow_id = await tw.start_pipeline_workflow([{"event_id": "e1"}], label="TEST", correlation_id="cid-1")

    assert workflow_id == "sentinel-pipeline-cid-1"
    [call] = client.started
    assert call["args"] == [[{"event_id": "e1"}], "TEST", "cid-1"]
    assert call["task_queue"] == tw.settings.TEMPORAL_TASK_QUEUE


@pytest.mark.parametrize("starter, prefix", [
    (lambda: tw.start_pipeline_workflow([]), "sentinel-pipeline-"),
    (lambda: tw.start_ingest_workflow(None), "sentinel-ingest-"),
])
async def test_a_workflow_started_with_no_correlation_id_gets_a_fresh_one(client, monkeypatch, starter, prefix):
    monkeypatch.setattr(tw, "get_correlation_id", lambda: "-")
    monkeypatch.setattr(tw, "new_correlation_id", lambda: "fresh")

    assert await starter() == f"{prefix}fresh"


async def test_a_workflow_started_inside_a_request_uses_that_requests_correlation_id(client, monkeypatch):
    monkeypatch.setattr(tw, "get_correlation_id", lambda: "from-request")

    assert await tw.start_ingest_workflow("/data/raw/x") == "sentinel-ingest-from-request"
    assert client.started[0]["args"] == ["/data/raw/x", "from-request"]


# ── The heartbeat and the watchdog ───────────────────────────────────────────────────────────────────

async def test_the_heartbeat_beats_until_it_is_cancelled(monkeypatch):
    beats: list[int] = []
    monkeypatch.setattr(tw, "beat", lambda: beats.append(1))

    task = asyncio.create_task(tw._beat_forever(interval_seconds=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(beats) >= 2


def test_the_watchdog_checks_for_a_stall_on_every_interval(monkeypatch):
    threads: list = []

    class _Thread:
        def __init__(self, target, daemon, name):
            self.target, self.daemon, self.name = target, daemon, name
            threads.append(self)

        def start(self):
            pass

    checks: list[int] = []
    sleeps: list[float] = []

    def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise SystemExit      # the real loop never ends; the test ends it

    monkeypatch.setattr(tw.threading, "Thread", _Thread)
    monkeypatch.setattr(tw.time, "sleep", _sleep)
    monkeypatch.setattr(tw, "exit_if_stalled", lambda: checks.append(1))

    tw._start_watchdog(check_seconds=3.0)
    [thread] = threads
    assert (thread.daemon, thread.name) == (True, "loop-watchdog")
    with pytest.raises(SystemExit):
        thread.target()

    assert sleeps[:2] == [3.0, 3.0] and len(checks) == 2


# ── The worker's life ────────────────────────────────────────────────────────────────────────────────

async def test_the_worker_polls_until_interrupted_and_then_drains(monkeypatch):
    events: list[str] = []

    class _Worker:
        def __init__(self, client, **kwargs):
            events.append("built")
            self.kwargs = kwargs
            built.append(self)

        async def __aenter__(self):
            events.append("polling")
            return self

        async def __aexit__(self, *exc):
            events.append("drained")

    built: list[_Worker] = []
    connected = SimpleNamespace()

    async def _connect():
        return connected

    monkeypatch.setattr(tw, "Worker", _Worker)
    monkeypatch.setattr(tw, "connect_client", _connect)
    monkeypatch.setattr(tw, "configure_logging", lambda: events.append("logging"))
    monkeypatch.setattr(tw, "start_metrics_server", lambda port: events.append(f"metrics:{port}"))
    monkeypatch.setattr(tw, "_start_watchdog", lambda: events.append("watchdog"))
    monkeypatch.setattr(tw, "new_correlation_id", lambda: "cid")
    interrupt = asyncio.Event()
    interrupt.set()

    await tw.run_worker(interrupt)

    assert events == ["logging", f"metrics:{tw.settings.METRICS_PORT}", "watchdog", "built", "polling", "drained"]
    [worker] = built
    assert worker.kwargs["task_queue"] == tw.settings.TEMPORAL_TASK_QUEUE
    assert worker.kwargs["graceful_shutdown_timeout"].total_seconds() == 20
    assert len(worker.kwargs["workflows"]) == 2 and len(worker.kwargs["activities"]) == 2


def test_main_runs_the_worker_on_its_own_loop_and_closes_it(monkeypatch):
    ran: list[asyncio.Event] = []
    handlers: list = []

    async def _run_worker(event):
        ran.append(event)

    class _Loop:
        def __init__(self):
            self.closed = False
            self._real = asyncio.new_event_loop()

        def add_signal_handler(self, sig, callback):
            handlers.append((sig, callback))

        def run_until_complete(self, coro):
            return self._real.run_until_complete(coro)

        def shutdown_asyncgens(self):
            return self._real.shutdown_asyncgens()

        def close(self):
            self.closed = True
            self._real.close()

    loop = _Loop()
    monkeypatch.setattr(tw, "run_worker", _run_worker)
    monkeypatch.setattr(tw.asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(tw.asyncio, "set_event_loop", lambda _loop: None)

    tw.main()

    assert loop.closed and len(ran) == 1
    assert {sig.name for sig, _ in handlers} == {"SIGINT", "SIGTERM"}
    handlers[0][1]()
    assert ran[0].is_set(), "a signal sets the event the worker is waiting on"
