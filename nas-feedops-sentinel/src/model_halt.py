"""
The runtime halt of the AI layer: one switch that makes every model call refuse, without a restart.

    A supervisor halts it, or resumes it, from the Agent Evaluation page; both need a reason.
    An optional automatic trip (MODEL_AUTO_HALT_ENABLED, off by default) halts it after repeated refusals that
    signal a real fault.

While it is halted every model call is refused by the model-use policy (rule `model-halted`): nothing is sent,
and each caller falls back to its deterministic rules exactly as it does when a model fails. Alerts are still
staged and the human gate is untouched, because the mission never depended on a model. That is why halting is
cheap here.

The state is one row in the database (`model_halt`), so a halt reaches every service. Each process keeps what it
read for MODEL_HALT_CACHE_SECONDS, so a change reaches the others within that long. Every change is audited by a
trigger in the database, not by this module (src/storage/schema.py), so no path can change it unrecorded.

Three decisions about failure:
  - A state that cannot be read halts: an unknown state is not "running". (A database the migrate job has not
    reached has no table at all, and that is not a halt, so a rolling deploy keeps working.)
  - The automatic trip counts only refusals that signal a fault, not a misconfigured role or an oversized prompt.
  - The trip can be provoked on purpose to switch the AI layer off, so it is optional, it is as visible as a
    supervisor's halt (the banner, the audit row, the alert), and only a supervisor resumes.
"""
from __future__ import annotations

import asyncio
import time
import weakref
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from src import config, metrics, storage
from src.auth import Decision, Principal, authorize, format_actor
from src.logging_config import get_logger

_log = get_logger(__name__)

# The policy rule that refuses a call while the AI layer is halted (policies/model-use.yaml).
HALT_RULE = "model-halted"
# Refusals that mean something is wrong, as opposed to a caller that was configured badly: a credential or a
# value that must not be sent was found in a prompt. Only these count toward the automatic trip.
FAULT_RULES = frozenset({"no-credentials-in-prompts", "no-protected-values-in-prompts"})
REASON_MAX_CHARS = 500
AUTO_ACTOR = "system (automatic trip)"


@dataclass(frozen=True)
class HaltState:
    halted: bool
    reason: str = ""
    source: str = "none"          # none | supervisor | auto | unreadable
    actor: str = ""
    changed_at: datetime | None = None


class HaltError(ValueError):
    """A halt or resume that cannot be made as asked (no reason given)."""


_cache: tuple[float, HaltState] | None = None
_violations: deque[float] = deque()
# One read at a time per event loop: a pipeline's concurrent calls all arrive on a cold cache together, and
# without this each would read the database (and queue for a pooled connection) before any could send.
_read_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def reset() -> None:
    """Forget what this process has read and counted. For tests and for a process that wants a fresh read."""
    global _cache
    _cache = None
    _violations.clear()


def _from_row(row: dict[str, Any]) -> HaltState:
    return HaltState(halted=bool(row["halted"]), reason=row["reason"], source=row["source"],
                     actor=row["actor"], changed_at=row["changed_at"])


def _remember(state: HaltState) -> HaltState:
    global _cache
    _cache = (time.monotonic(), state)
    metrics.MODEL_HALTED.set(1 if state.halted else 0)
    metrics.MODEL_HALT_CHECKED.set(time.time())
    return state


async def get_state(*, fresh: bool = False) -> HaltState:
    """
    The halt state: from this process's last read if it is younger than MODEL_HALT_CACHE_SECONDS, else from the
    database. `fresh` always reads. A state that cannot be read is a halt.
    """
    if not fresh and _is_current():
        return _cache[1]  # type: ignore[index]
    lock = _read_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        if not fresh and _is_current():            # another caller read it while this one waited
            return _cache[1]  # type: ignore[index]
        return await _read()


def _is_current() -> bool:
    return _cache is not None and time.monotonic() - _cache[0] < config.settings.MODEL_HALT_CACHE_SECONDS


async def _read() -> HaltState:
    try:
        row = await storage.get_model_halt()
    except asyncpg.UndefinedTableError:
        _log.warning("model_halt_table_missing_run_migrations")
        return _remember(HaltState(halted=False))
    except Exception as exc:  # noqa: BLE001 -- any failure to read means the state is unknown
        _log.error("model_halt_unreadable", extra={"error": f"{type(exc).__name__}: {exc}"})
        return _remember(HaltState(
            halted=True, source="unreadable",
            reason="The halt state could not be read, so the AI layer is held halted until it can be."))
    if row is None:
        _log.error("model_halt_row_missing_run_migrations")
        return _remember(HaltState(
            halted=True, source="unreadable", reason="The halt state has no row, so the AI layer is held halted."))
    return _remember(_from_row(row))


async def _write(*, halted: bool, reason: str, source: str, actor: str) -> HaltState:
    state = _remember(_from_row(await storage.set_model_halt(halted=halted, reason=reason, source=source, actor=actor)))
    metrics.MODEL_HALT_CHANGES.labels(to="halted" if halted else "running", source=source).inc()
    _log.warning("model_halt_changed", extra={"halted": halted, "source": source, "actor": actor, "reason": reason})
    return state


def _reason(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise HaltError("a reason is required: say why, so the audit trail can")
    return cleaned[:REASON_MAX_CHARS]


async def halt(principal: Principal, reason: str) -> HaltState:
    """Halt the AI layer. A supervisor's call; raises AuthorizationError otherwise, and HaltError without a reason."""
    authorize(principal, Decision.HALT_MODELS, allow_unverified=config.settings.ALLOW_UNVERIFIED_DECISIONS)
    return await _write(halted=True, reason=_reason(reason), source="supervisor", actor=format_actor(principal))


async def resume(principal: Principal, reason: str) -> HaltState:
    """Let model calls run again. A supervisor's call, with a reason, whether the halt was theirs or automatic."""
    authorize(principal, Decision.RESUME_MODELS, allow_unverified=config.settings.ALLOW_UNVERIFIED_DECISIONS)
    return await _write(halted=False, reason=_reason(reason), source="supervisor", actor=format_actor(principal))


async def note_violation(rule: str | None) -> bool:
    """
    Count one refusal toward the automatic trip, and trip it when the threshold is reached. True when this call
    halted the AI layer. Does nothing unless MODEL_AUTO_HALT_ENABLED, and counts only FAULT_RULES. Never raises:
    the refusal being counted has already been decided.

    The count is this process's: a service counts the refusals it saw itself.
    """
    if not config.settings.MODEL_AUTO_HALT_ENABLED or rule not in FAULT_RULES:
        return False
    now = time.monotonic()
    _violations.append(now)
    while _violations and _violations[0] < now - config.settings.MODEL_AUTO_HALT_WINDOW_SECONDS:
        _violations.popleft()
    if len(_violations) < config.settings.MODEL_AUTO_HALT_THRESHOLD:
        return False
    count = len(_violations)
    _violations.clear()
    try:
        if (await get_state(fresh=True)).halted:
            return False
        await _write(
            halted=True, source="auto", actor=AUTO_ACTOR,
            reason=(f"{count} model calls were refused for '{rule}' within {config.settings.MODEL_AUTO_HALT_WINDOW_SECONDS:g} s: "
                    "a credential or a protected value was found in a prompt. Find where it came from, then resume."))
    except Exception as exc:  # noqa: BLE001 -- the refusal is decided; a failed trip is logged, not raised
        _log.error("model_halt_trip_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return False
    _log.critical("model_halt_tripped_automatically", extra={"refusals": count, "rule": rule})
    return True
