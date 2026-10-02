"""
The halt as the console shows it (dashboard/lib/halt_view.py): a banner on every page while the AI layer is halted,
and a panel on the Agent Evaluation page where only a supervisor halts and resumes it, with a reason.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import config, model_halt, storage
from src.auth import Principal, PrincipalSource

pytest.importorskip("streamlit", reason="the console renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from lib import halt_view, safe_text  # noqa: E402
EVALUATION = DASHBOARD / "pages" / "6_Agent_Evaluation.py"
FEED_HEALTH = DASHBOARD / "pages" / "2_Feed_Health.py"
SUPERVISOR = Principal(principal_id="sam", role="Supervisor", source=PrincipalSource.OIDC,
                       roles=frozenset({"supervisor"}))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    storage.override_db_path(":memory:")
    model_halt.reset()
    yield
    model_halt.reset()
    storage.override_db_path(":memory:")


def _run(page: Path) -> AppTest:
    at = AppTest.from_file(str(page), default_timeout=90).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _toggle(at: AppTest):
    return next(b for b in at.button if b.key == "model_halt_toggle")


def _reason(at: AppTest):
    return next(t for t in at.text_input if t.key == "model_halt_reason")


def _banners(at: AppTest) -> list[str]:
    return [e.value for e in at.error if "AI layer" in e.value]


# ── The wording ──────────────────────────────────────────────────────────────────────────────────────

def _state(**kw) -> model_halt.HaltState:
    base = dict(halted=True, reason="the Critic is quoting callsigns", source="supervisor", actor="sam (Supervisor)",
                changed_at=datetime(2026, 10, 2, 14, 5, tzinfo=timezone.utc))
    base.update(kw)
    return model_halt.HaltState(**base)


def test_the_banner_says_who_why_when_and_what_still_works():
    text = halt_view.banner_text(_state())

    assert "The AI layer is halted" in text and safe_text.plain("sam (Supervisor)") in text
    assert "2026-10-02 14:05 UTC" in text and "quoting callsigns" in text
    assert "alerts are still staged and the deterministic rules still run" in text
    assert "A supervisor resumes it on the Agent Evaluation page" in text


def test_an_automatic_trip_says_it_tripped_and_a_state_that_cannot_be_read_says_so():
    assert "tripped automatically by" in halt_view.banner_text(_state(source="auto", actor=model_halt.AUTO_ACTOR))
    unreadable = halt_view.banner_text(_state(source="unreadable", actor="", reason="x"))
    assert "held halted" in unreadable and "could not be read" in unreadable


def test_a_running_layer_has_no_banner_and_the_status_says_so():
    assert halt_view.banner_text(model_halt.HaltState(halted=False)) == ""
    assert halt_view.status_line(model_halt.HaltState(halted=False)) == "Running. Nothing has halted it."
    resumed = halt_view.status_line(_state(halted=False, reason="fixed"))
    assert resumed.startswith("Running. Last resumed by") and "fixed" in resumed


def test_what_a_person_typed_is_shown_as_text_not_as_markdown():
    text = halt_view.banner_text(_state(reason="![x](https://evil.example/p?d=1) **click**"))

    assert "![" not in text and "\\!\\[x\\]" in text
    assert "(https://evil.example" not in text.replace("\\(", "(").replace("https\\:", "https:") or "\\(" in text


# ── The banner ───────────────────────────────────────────────────────────────────────────────────────

async def test_a_halt_shows_on_a_page_that_has_nothing_to_do_with_it():
    await model_halt.halt(SUPERVISOR, "stop everything")

    banners = _banners(_run(FEED_HEALTH))

    assert len(banners) == 1 and "stop everything" in banners[0]


def test_a_running_layer_shows_no_banner_anywhere():
    assert _banners(_run(FEED_HEALTH)) == [] and _banners(_run(EVALUATION)) == []


# ── The panel ────────────────────────────────────────────────────────────────────────────────────────

def test_a_visitor_without_a_verified_supervisor_sees_the_control_disabled_and_nothing_changes():
    at = _run(EVALUATION)

    assert _toggle(at).label == "Halt the AI layer" and _toggle(at).disabled
    assert _reason(at).disabled
    assert not model_halt._cache or not model_halt._cache[1].halted


def test_a_supervisor_halts_it_with_a_reason_and_the_page_then_says_so(monkeypatch):
    monkeypatch.setattr(config.settings, "ALLOW_UNVERIFIED_DECISIONS", True)
    at = _run(EVALUATION)
    assert not _toggle(at).disabled

    _reason(at).set_value("investigating a leak")
    _toggle(at).click()
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert model_halt._cache is not None
    assert _toggle(at).label == "Resume the AI layer"
    assert any("investigating a leak" in b for b in _banners(at))
    [halted] = [r for r in model_halt_audit_rows() if r["new_status"] == "HALTED"]
    assert "investigating a leak" in halted["note"]


def model_halt_audit_rows() -> list[dict]:
    from src.async_bridge import run_async

    return [r for r in run_async(storage.get_action_audit_log(limit=50)) if r["action_id"].startswith("model_halt:")]


def test_a_halt_without_a_reason_is_refused_with_a_message_and_changes_nothing(monkeypatch):
    monkeypatch.setattr(config.settings, "ALLOW_UNVERIFIED_DECISIONS", True)
    at = _run(EVALUATION)

    _toggle(at).click()
    at.run()

    assert any("reason is required" in e.value for e in at.error)
    assert model_halt_audit_rows() == []


def test_a_supervisor_resumes_it(monkeypatch):
    from src.async_bridge import run_async

    monkeypatch.setattr(config.settings, "ALLOW_UNVERIFIED_DECISIONS", True)
    run_async(model_halt.halt(SUPERVISOR, "stop"))
    model_halt.reset()
    at = _run(EVALUATION)
    assert _toggle(at).label == "Resume the AI layer"

    _reason(at).set_value("fixed in the gateway config")
    _toggle(at).click()
    at.run()

    assert _toggle(at).label == "Halt the AI layer" and _banners(at) == []
    assert [r["new_status"] for r in reversed(model_halt_audit_rows())] == ["HALTED", "RUNNING"]
