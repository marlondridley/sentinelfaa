"""
src/async_bridge.py: the synchronous callers' way into the async storage layer.

It used to run every coroutine to completion under one process-wide lock, so a
single operator's long call (a model question, a replay) froze every other
session's page load, the review queue included. Callers now wait only for their
own coroutine.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.async_bridge import run_async


def test_it_returns_a_result_and_raises_an_exception():
    async def value():
        return 42

    async def boom():
        raise ValueError("nope")

    assert run_async(value()) == 42
    with pytest.raises(ValueError, match="nope"):
        run_async(boom())


def test_a_long_call_does_not_block_another_caller():
    release = threading.Event()
    started = threading.Event()

    async def slow():
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "slow"

    async def quick():
        return "quick"

    slow_result: list[str] = []
    worker = threading.Thread(target=lambda: slow_result.append(run_async(slow())))
    worker.start()
    try:
        assert started.wait(5), "the slow call is running on the bridge"
        began = time.monotonic()
        assert run_async(quick()) == "quick"
        assert time.monotonic() - began < 1.0, "the second caller did not wait for the first"
    finally:
        release.set()
        worker.join(5)
    assert slow_result == ["slow"]


def test_every_caller_runs_on_the_same_loop_so_a_pool_stays_usable():
    async def loop_id():
        return id(asyncio.get_running_loop())

    ids = []
    threads = [threading.Thread(target=lambda: ids.append(run_async(loop_id()))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(ids) == 4 and len(set(ids)) == 1, "one long-lived loop, whichever thread calls"


async def test_it_refuses_to_run_inside_a_running_loop_instead_of_blocking_it():
    async def never_started():
        return 1

    coro = never_started()
    with pytest.raises(RuntimeError, match="running event loop"):
        run_async(coro)
    assert coro.cr_frame is None, "the coroutine was closed, not left un-awaited"


def test_a_coroutine_on_the_bridge_that_calls_run_async_fails_instead_of_deadlocking():
    async def inner():
        return 1

    async def outer():
        return run_async(inner())

    with pytest.raises(RuntimeError, match="running event loop"):
        run_async(outer())
