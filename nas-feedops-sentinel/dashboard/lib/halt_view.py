"""
The halt of the AI layer, as the console shows it: a banner on every page while it is on, and a panel on the
Agent Evaluation page where a supervisor halts and resumes it (src/model_halt.py).

banner_text() and status_line() are plain functions of the state, tested without Streamlit. Everything a person
typed (the reason) or the system wrote about one (the actor) goes through safe_text.plain, so it renders as text.
"""
from __future__ import annotations

import streamlit as st

from lib import safe_text, theme
from src import model_halt
from src.async_bridge import run_async
from src.auth import AuthorizationError, Decision, Principal, can_decide
from src import config

SUPERVISORS_ONLY = "Requires the supervisor role and a verified sign-in."

_WHAT_STILL_WORKS = ("Every model call is refused: alerts are still staged and the deterministic rules still run, "
                     "but there are no model verdicts, briefs or answers.")


def _when(state: model_halt.HaltState) -> str:
    return state.changed_at.strftime("%Y-%m-%d %H:%M UTC") if state.changed_at else "an unknown time"


def banner_text(state: model_halt.HaltState) -> str:
    """The sentence shown on every page while the AI layer is halted. Empty when it is running."""
    if not state.halted:
        return ""
    if state.source == "unreadable":
        return (f"**The AI layer is held halted.** The halt state could not be read, so no model call is sent until it "
                f"can be. {_WHAT_STILL_WORKS}")
    how = "tripped automatically" if state.source == "auto" else "was halted"
    return (f"**The AI layer is halted.** It {how} by {safe_text.plain(state.actor)} at {_when(state)}: "
            f"{safe_text.plain(state.reason)} {_WHAT_STILL_WORKS} A supervisor resumes it on the Agent Evaluation page.")


def status_line(state: model_halt.HaltState) -> str:
    if not state.halted:
        if state.source == "supervisor":
            return (f"Running. Last resumed by {safe_text.plain(state.actor)} at {_when(state)}: "
                    f"{safe_text.plain(state.reason)}")
        return "Running. Nothing has halted it."
    return banner_text(state).replace("**", "")


def render_banner() -> None:
    """The banner. Never breaks a page: a failure to read the state shows nothing here (the calls are held anyway)."""
    try:
        state = run_async(model_halt.get_state(fresh=True))
    except Exception:  # noqa: BLE001
        return
    text = banner_text(state)
    if text:
        st.error(text, icon="⏸️")


def render_panel(principal: Principal) -> None:
    """The control: the state, and for a supervisor a reason and a button to halt or resume."""
    state = run_async(model_halt.get_state(fresh=True))
    # Read from the module when the page runs, not bound at import: this module outlives a page run.
    allowed = can_decide(principal, Decision.HALT_MODELS, allow_unverified=config.settings.ALLOW_UNVERIFIED_DECISIONS)
    theme.section("Model halt", "One switch for every model call. Alerts are still staged and the rules still run.")
    st.markdown(status_line(state))
    reason = st.text_input("Reason (recorded in the audit log with your name)", key="model_halt_reason",
                           max_chars=model_halt.REASON_MAX_CHARS, disabled=not allowed,
                           help=None if allowed else SUPERVISORS_ONLY)
    label = "Resume the AI layer" if state.halted else "Halt the AI layer"
    # The disabled state is the browser's; the check below is the lock.
    if st.button(label, key="model_halt_toggle", disabled=not allowed, help=None if allowed else SUPERVISORS_ONLY,
                 type="secondary" if state.halted else "primary") and allowed:
        try:
            run_async(model_halt.resume(principal, reason) if state.halted else model_halt.halt(principal, reason))
        except (model_halt.HaltError, AuthorizationError) as exc:
            st.error(safe_text.plain(exc))
        else:
            st.rerun()
