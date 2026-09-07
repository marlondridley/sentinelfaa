"""
Shared synchronous-to-asynchronous bridge for NAS FeedOps Sentinel.

storage.py — and anything built on top of it (planner/watch-officer
context building, the news ticker) — is fully async, sitting on an
asyncpg pool that binds itself to whichever event loop first created it
(storage.get_db_pool() caches that pool in a module-level global).

Callers that are otherwise synchronous (the Streamlit dashboard script,
CrewAI's synchronous Crew.kickoff() flow) need a way to run those
coroutines to completion. Don't reach for asyncio.run() for this: it
spins up a brand-new event loop on every call, so the *second* call
would try to reuse a pool bound to a loop asyncio.run() already closed
and tore down — that's the original crash this module exists to avoid.

Instead this module keeps exactly one event loop alive for the life of
the process and runs every coroutine on it, serialized with a lock (one
event loop isn't safe to drive concurrently from more than one OS
thread, and Streamlit can run each session's script on its own worker
thread). Every module that needs to call into storage.py from sync code
should import `run_async` from here rather than rolling its own loop —
using two different bridges in the same process would recreate the
exact cross-loop bug this is meant to prevent.
"""
from __future__ import annotations

import asyncio
import threading

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def run_async(coro):
    """Run an async coroutine to completion on the shared process-wide loop."""
    with _LOCK:
        loop = _get_loop()
        return loop.run_until_complete(coro)