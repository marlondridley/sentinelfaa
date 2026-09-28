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
the process, running in a thread of its own, and hands every coroutine to
it with run_coroutine_threadsafe. The loop is only ever driven from that
thread, so it is safe however many threads call in (Streamlit runs each
session's script on its own worker thread), and coroutines from different
callers interleave on it: a caller waits for its own coroutine, never for
another caller's. (An earlier version ran every coroutine to completion
under one lock, so one operator's 90-second model call froze every other
session's page load, review queue included.) Every module that needs to
call into storage.py from sync code should import `run_async` from here
rather than rolling its own loop -- using two different bridges in the same
process would recreate the exact cross-loop bug this is meant to prevent.
"""
from __future__ import annotations

import asyncio
import threading

_START = threading.Lock()          # guards creating the loop thread, never held while running
_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None


def _serve(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _THREAD
    with _START:
        if _LOOP is None or _LOOP.is_closed() or _THREAD is None or not _THREAD.is_alive():
            loop = asyncio.new_event_loop()
            _THREAD = threading.Thread(target=_serve, args=(loop,), name="async-bridge", daemon=True)
            _THREAD.start()
            _LOOP = loop
        return _LOOP


def run_async(coro):
    """
    Run an async coroutine to completion on the shared process-wide loop and
    return its result (or raise its exception). Blocks only the calling thread.

    Not for use from inside a running event loop, the bridge's own included:
    that thread would wait on itself. Await the coroutine there instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError("run_async cannot be called from a running event loop; await the coroutine instead")
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result()