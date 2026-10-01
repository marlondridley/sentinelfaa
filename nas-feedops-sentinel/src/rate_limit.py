"""
A cooldown: one key (one person's one kind of action) may start at most once every `seconds`.

The dashboard's model-backed buttons (Ask the planner, Refresh brief) each spend from one shared gateway key.
Role checks say who may start them; this says how often, so one signed-in account cannot use all of it. State
is in this process, which is where Streamlit serves every session from; a second dashboard process would keep
its own, and the gateway's key-level ceiling is the limit behind it.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from src import config


class Cooldown:
    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 4096) -> None:
        self._seconds = seconds
        self._clock = clock
        self._max_keys = max_keys
        self._started: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_start(self, key: str) -> float:
        """0.0 if `key` may start now (and the start is recorded), else the seconds left to wait."""
        with self._lock:
            now = self._clock()
            last = self._started.get(key)
            if last is not None and now - last < self._seconds:
                return self._seconds - (now - last)
            if len(self._started) >= self._max_keys:
                self._started = {k: t for k, t in self._started.items() if now - t < self._seconds}
            self._started[key] = now
            return 0.0


class TokenBucket:
    """
    A token bucket per key: a burst of `per_minute` requests, refilled evenly over the minute.

    `try_acquire` returns 0.0 when the request may proceed, else the seconds until a token is back. State
    is in this process; the API runs as one process per container, and the edge proxy is the limit in front.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 4096) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}    # key -> (tokens, last refill)
        self._lock = threading.Lock()

    def try_acquire(self, key: str, per_minute: int) -> float:
        if per_minute <= 0:
            return 0.0
        rate = per_minute / 60.0
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return (1.0 - tokens) / rate
            if key not in self._buckets and len(self._buckets) >= self._max_keys:
                # Forget keys that have refilled completely: they are indistinguishable from new ones.
                self._buckets = {k: v for k, v in self._buckets.items()
                                 if v[0] + (now - v[1]) * rate < per_minute}
            self._buckets[key] = (tokens - 1.0, now)
            return 0.0


_model_actions: Cooldown | None = None


def model_action_cooldown() -> Cooldown:
    """The dashboard's shared cooldown for model-backed actions (MODEL_ACTION_COOLDOWN_SECONDS)."""
    global _model_actions
    if _model_actions is None:
        _model_actions = Cooldown(config.settings.MODEL_ACTION_COOLDOWN_SECONDS)
    return _model_actions
