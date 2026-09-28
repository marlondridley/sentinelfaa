"""
The Ingest & Replay page changes things: Replay and Scan stage reviews, and
Clean deletes Kit messages by age alone, which after an outage means messages
the watcher has not ingested yet. It used to discard the signed-in identity and
check no role, so an auditor (who reads and decides nothing), or anyone when
sign-in was unconfigured, could delete them from a routine Refresh button.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import storage

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "pages" / "5_Ingest_and_Replay.py"


@pytest.fixture(autouse=True)
def isolated_db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _button(at, label: str):
    return next(b for b in at.sidebar.button if b.label == label)


def test_a_visitor_without_a_verified_operator_role_cannot_delete_kit_files(monkeypatch):
    pytest.importorskip("streamlit", reason="the page renders through Streamlit")
    from streamlit.testing.v1 import AppTest

    purged = []
    monkeypatch.setattr("src.maintenance.purge_old_kit_files", lambda *a, **k: purged.append((a, k)) or (0, 0))

    at = AppTest.from_file(str(PAGE), default_timeout=90).run()
    assert not at.exception, [e.value for e in at.exception]
    clean = _button(at, "Clean Kit log now")
    assert clean.disabled, "the button says why it is off"

    # There is no click to simulate: AppTest refuses to click a disabled button, as a browser would, and a
    # trigger forced onto one is ignored by Streamlit itself. The page's own `and _MAY_OPERATE` behind the
    # button is a second lock that no test here reaches.
    assert purged == [], "and rendering it deletes nothing"


def test_refreshing_the_page_never_deletes_files(monkeypatch):
    pytest.importorskip("streamlit", reason="the page renders through Streamlit")
    from streamlit.testing.v1 import AppTest

    purged = []
    monkeypatch.setattr("src.maintenance.purge_old_kit_files", lambda *a, **k: purged.append((a, k)) or (0, 0))
    at = AppTest.from_file(str(PAGE), default_timeout=90).run()
    _button(at, "Refresh dashboard").click().run()
    assert purged == []
