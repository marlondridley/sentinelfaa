"""The metrics /health endpoint reports a hung event loop, not just a live thread."""
from __future__ import annotations

import time

import pytest

from src import metrics


def _health() -> str:
    statuses = []
    body = metrics._metrics_app({"PATH_INFO": "/health"}, lambda status, headers: statuses.append(status))
    return f"{statuses[0]} {b''.join(body).decode()}"


def test_health_is_plain_liveness_when_nothing_beats(monkeypatch):
    monkeypatch.setattr(metrics, "_last_beat", None)
    assert _health() == "200 OK ok"


def test_health_is_ok_while_the_loop_beats(monkeypatch):
    monkeypatch.setattr(metrics, "_last_beat", time.monotonic())
    assert _health() == "200 OK ok"


def test_health_fails_once_the_loop_stops_beating(monkeypatch):
    monkeypatch.setattr(metrics, "_last_beat", time.monotonic() - metrics.LOOP_STALL_SECONDS - 1)
    assert _health() == "503 Service Unavailable event loop stalled"


@pytest.mark.parametrize("seconds_since_beat, expected_exits", [
    (0.0, []),
    (metrics.LOOP_STALL_SECONDS + 1, [1]),
])
def test_the_worker_exits_only_once_its_loop_has_stalled(monkeypatch, seconds_since_beat, expected_exits):
    from src import temporal_worker

    exits = []
    monkeypatch.setattr(temporal_worker.os, "_exit", exits.append)
    monkeypatch.setattr(metrics, "_last_beat", time.monotonic() - seconds_since_beat)
    temporal_worker.exit_if_stalled()
    assert exits == expected_exits
