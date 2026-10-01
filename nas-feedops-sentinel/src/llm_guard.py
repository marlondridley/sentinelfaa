"""
Two limits on what the model layer may spend: a circuit breaker per model alias, and a token
budget per pipeline pass.

Both exist because every AI path here degrades to rules (the Critic's verdict is advisory, staging
never waits on a model). That is safe, and it hid the state the stack once ran in for weeks: a
gateway that answered nothing, with every alert still paying a timeout to find out. So:

  CircuitBreaker   after LLM_BREAKER_THRESHOLD consecutive "the gateway cannot serve this" outcomes the
                   alias is OPEN for LLM_BREAKER_COOLDOWN_SECONDS and calls are refused without being sent
                   (the Critic reports itself unavailable at once and Prometheus alerts). After the
                   cooldown one probe is let through; it closes the breaker or re-opens it.
  TokenBudget      one pipeline pass may spend LLM_TOKEN_BUDGET_PER_PASS tokens across the Critic and the
                   investigations. A call is reserved against the budget before it is sent and settled
                   with its real usage, so concurrent calls cannot overshoot it together.

Retries are a third limit and live in one place: the gateway (instraclustr/litellm-config.yaml,
router_settings) retries and falls back across providers. The client in src/llm_client.py retries only
a request that never reached the gateway, once. Temporal does not see model failures at all: the
Critic and the investigations catch them and fall back to rules.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from src.config import settings
from src.logging_config import get_logger
from src.metrics import LLM_BUDGET_REFUSALS, LLM_CIRCUIT_OPENED, LLM_CIRCUIT_STATE

_log = get_logger(__name__)

CLOSED, HALF_OPEN, OPEN = "closed", "half_open", "open"
_STATE_VALUE = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}

# Outcome classes (src/model_telemetry.py) that mean the gateway could not serve the call. A reply that
# was unusable (invalid_output), a request the gateway rejected (bad_request) or a refusal we made
# ourselves says nothing about availability, and does not count.
UNAVAILABLE_OUTCOMES = frozenset({"timeout", "rate_limited", "quota_exhausted", "auth_error", "provider_error"})


@dataclass
class CircuitBreaker:
    """Consecutive-failure breaker for one model alias. Thread-safe; the clock can be replaced in tests."""

    name: str
    clock: Callable[[], float] = time.monotonic
    state: str = CLOSED
    failures: int = 0
    opened_at: float = 0.0
    probing: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _set(self, state: str) -> None:
        self.state = state
        LLM_CIRCUIT_STATE.labels(model=self.name).set(_STATE_VALUE[state])

    def is_open(self) -> bool:
        """True while calls would be refused: open and still cooling down, or a probe is already out."""
        with self._lock:
            if self.state == CLOSED:
                return False
            if self.state == OPEN and self.clock() - self.opened_at >= settings.LLM_BREAKER_COOLDOWN_SECONDS:
                return False                # the next call becomes the probe
            return self.state == OPEN or self.probing

    def allow(self) -> bool:
        """May a call be sent now? In HALF_OPEN exactly one caller is told yes."""
        with self._lock:
            if self.state == CLOSED:
                return True
            if self.state == OPEN:
                if self.clock() - self.opened_at < settings.LLM_BREAKER_COOLDOWN_SECONDS:
                    return False
                self._set(HALF_OPEN)
                self.probing = False
            if self.probing:
                return False
            self.probing = True
            return True

    def release(self) -> None:
        """A call that was allowed was not sent after all (another limit refused it): free the probe slot."""
        with self._lock:
            self.probing = False

    def record(self, outcome: str) -> None:
        """Fold in how an allowed call ended."""
        with self._lock:
            self.probing = False
            if outcome in UNAVAILABLE_OUTCOMES:
                self.failures += 1
                if self.state == HALF_OPEN or (self.state == CLOSED and self.failures >= settings.LLM_BREAKER_THRESHOLD):
                    self._open()
                return
            if outcome in ("ok", "repaired", "incomplete", "invalid_output", "bad_request"):
                # The gateway answered, even if the answer was no use: it is reachable.
                if self.state != CLOSED:
                    _log.info("llm_circuit_closed", extra={"model": self.name})
                self.failures = 0
                self._set(CLOSED)

    def _open(self) -> None:
        self.opened_at = self.clock()
        self._set(OPEN)
        LLM_CIRCUIT_OPENED.labels(model=self.name).inc()
        _log.error(
            "llm_circuit_opened",
            extra={"model": self.name, "consecutive_failures": self.failures,
                   "cooldown_seconds": settings.LLM_BREAKER_COOLDOWN_SECONDS},
        )


_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def breaker_for(model_alias: str) -> CircuitBreaker:
    with _registry_lock:
        breaker = _breakers.get(model_alias)
        if breaker is None:
            breaker = _breakers[model_alias] = CircuitBreaker(model_alias)
            LLM_CIRCUIT_STATE.labels(model=model_alias).set(0)
        return breaker


def circuit_is_open(model_alias: str) -> bool:
    return breaker_for(model_alias).is_open()


def reset_breakers() -> None:
    """Forget every breaker (tests)."""
    with _registry_lock:
        for name in _breakers:
            LLM_CIRCUIT_STATE.labels(model=name).set(0)
        _breakers.clear()


# ── the per-pass token budget ─────────────────────────────────────────────────

@dataclass
class TokenBudget:
    """
    Tokens one pipeline pass may spend. Shared by every task the pass starts (it is reached through a
    context variable, and tasks inherit the context), so concurrent Critic calls draw on one budget.
    """

    limit: int
    spent: int = 0
    reserved: int = 0
    refusals: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, tokens: int) -> bool:
        with self._lock:
            if self.limit > 0 and self.spent + self.reserved + tokens > self.limit:
                self.refusals += 1
                if self.refusals == 1:
                    _log.warning("llm_pass_budget_exhausted", extra={
                        "limit": self.limit, "spent": self.spent, "reserved": self.reserved, "requested": tokens})
                LLM_BUDGET_REFUSALS.inc()
                return False
            self.reserved += tokens
            return True

    def settle(self, reserved: int, actual: int) -> None:
        with self._lock:
            self.reserved -= reserved
            self.spent += actual


_budget: contextvars.ContextVar[TokenBudget | None] = contextvars.ContextVar("llm_pass_budget", default=None)


def current_budget() -> TokenBudget | None:
    return _budget.get()


@contextlib.contextmanager
def pass_budget(limit: int | None = None) -> Iterator[TokenBudget]:
    """Everything the model layer does inside this block, in this task or one it starts, draws on one budget."""
    budget = TokenBudget(settings.LLM_TOKEN_BUDGET_PER_PASS if limit is None else limit)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)
