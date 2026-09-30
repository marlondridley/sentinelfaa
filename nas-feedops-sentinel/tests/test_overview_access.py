"""
The Overview page runs paid model calls and writes to the database from three places: the first brief on load,
Refresh brief / Deterministic only, and the Planner's Ask. It used to discard the signed-in identity and check no
role, so an account with no realm role, or an auditor (who reads and decides nothing), could spend the model
budget and fill the tables. It now follows the Ingest page: an operator does these, and each of them at most
once every MODEL_ACTION_COOLDOWN_SECONDS.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import config, rate_limit, storage

pytest.importorskip("streamlit", reason="the Overview renders through Streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"


class _FakePlanner:
    asked: list[str] = []

    async def query(self, question):
        from src.models import PlannerResult

        _FakePlanner.asked.append(question)
        return PlannerResult(question=question, answer="All quiet.", confidence=None, recommended_actions=[],
                             sub_queries=[], citations=[])


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    storage.override_db_path(":memory:")
    _FakePlanner.asked = []
    monkeypatch.setattr("src.planner_agent.PlannerAgent", _FakePlanner)
    monkeypatch.setattr(rate_limit, "_model_actions", None)
    monkeypatch.setattr(config.settings, "MODEL_ACTION_COOLDOWN_SECONDS", 3600.0)
    yield
    storage.override_db_path(":memory:")
    monkeypatch.setattr(rate_limit, "_model_actions", None)


def _run() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _as_operator(monkeypatch) -> None:
    """A self-declared operator may act only where ALLOW_UNVERIFIED_DECISIONS says a lab run may."""
    monkeypatch.setattr(config.settings, "ALLOW_UNVERIFIED_DECISIONS", True)


async def test_a_visitor_sees_the_actions_disabled_and_a_visit_generates_nothing():
    at = _run()

    assert _button(at, "Refresh brief").disabled
    assert _button(at, "Deterministic only").disabled
    assert _button(at, "Ask").disabled
    assert at.text_input(key="planner_input").disabled
    assert await storage.get_latest_watch_officer_brief() is None, "loading the page spent nothing and wrote nothing"
    assert any("No brief has been generated yet" in i.value for i in at.info)


async def test_an_operators_visit_generates_the_first_brief(monkeypatch):
    _as_operator(monkeypatch)

    at = _run()

    assert not _button(at, "Refresh brief").disabled
    assert await storage.get_latest_watch_officer_brief() is not None


async def test_refresh_brief_is_limited_to_one_start_per_cooldown(monkeypatch):
    _as_operator(monkeypatch)
    at = _run()                                          # the first brief consumed this operator's start
    before = await storage.get_recent_watch_officer_briefs(limit=10)

    at = _button(at, "Refresh brief").click().run()

    assert any("Wait" in w.value for w in at.warning), "it says why nothing happened"
    assert len(await storage.get_recent_watch_officer_briefs(limit=10)) == len(before)


async def test_a_refresh_after_the_cooldown_is_allowed(monkeypatch):
    _as_operator(monkeypatch)
    monkeypatch.setattr(config.settings, "MODEL_ACTION_COOLDOWN_SECONDS", 0.0)
    at = _run()
    before = len(await storage.get_recent_watch_officer_briefs(limit=10))

    _button(at, "Refresh brief").click().run()

    assert len(await storage.get_recent_watch_officer_briefs(limit=10)) == before + 1


async def test_the_planner_question_is_capped_and_asking_is_limited(monkeypatch):
    _as_operator(monkeypatch)
    at = _run()
    assert at.text_input(key="planner_input").max_chars == config.settings.PLANNER_QUESTION_MAX_CHARS

    at.text_input(key="planner_input").set_value("What is happening at KIAD?")
    at = _button(at, "Ask").click().run()
    assert _FakePlanner.asked == ["What is happening at KIAD?"]

    at = _button(at, "Ask").click().run()               # straight away again
    assert _FakePlanner.asked == ["What is happening at KIAD?"], "the second one waited"
    assert any("Wait" in w.value for w in at.warning)


async def test_a_visitor_cannot_ask_the_planner():
    at = _run()

    assert _button(at, "Ask").disabled
    assert _FakePlanner.asked == []
