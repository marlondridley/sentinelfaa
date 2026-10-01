"""
A sign-in is bounded (results.md S-05) and deleting Kit files is attributed to a person (S-11).

S-05  the dashboard reads roles and the guest expiry from the ID token when the operator signs in and nothing asks
      Keycloak again, so a disabled account or a removed role kept its authority for the realm's whole 10 h session.
      SIGN_IN_MAX_SECONDS bounds that lag.
S-11  "Clean Kit log now" deleted messages nobody had ingested and recorded no actor.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src import keycloak_auth as ka
from src import storage
from src.async_bridge import run_async as run
from src.config import settings

_ROOT = Path(__file__).resolve().parent.parent


def _claims(hours_ago: float) -> dict:
    return {"preferred_username": "mallory", "auth_time": time.time() - hours_ago * 3600,
            "realm_access": {"roles": ["supervisor"]}}


# ── S-05 ───────────────────────────────────────────────────────────────────────

def test_the_session_is_shorter_than_the_realms_by_default():
    assert ka.session_max_seconds() == settings.SIGN_IN_MAX_SECONDS == 14_400 < ka.SESSION_MAX_SECONDS


def test_a_sign_in_older_than_the_bound_has_ended_though_the_realm_would_still_allow_it():
    assert not ka.session_expired(_claims(hours_ago=3))
    assert ka.session_expired(_claims(hours_ago=5)), "5 h is inside the realm's 10 h but past this console's bound"


def test_the_bound_cannot_be_set_past_the_realms_maximum(monkeypatch):
    monkeypatch.setattr(settings, "SIGN_IN_MAX_SECONDS", 10 * ka.SESSION_MAX_SECONDS)
    assert ka.session_max_seconds() == ka.SESSION_MAX_SECONDS
    monkeypatch.setattr(settings, "SIGN_IN_MAX_SECONDS", -5)
    assert ka.session_max_seconds() == 0, "a nonsense value ends every session rather than lengthening it"


def test_the_sidebars_sign_out_time_is_the_bounded_one(monkeypatch):
    monkeypatch.setattr(settings, "SIGN_IN_MAX_SECONDS", 3600)
    claims = {"auth_time": 1_000_000}
    assert ka.session_expires_at(claims) == 1_000_000 + 3600


def test_the_compose_file_passes_the_bound_to_the_dashboard():
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "SIGN_IN_MAX_SECONDS: ${SIGN_IN_MAX_SECONDS:-14400}" in compose


# ── S-11 ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    storage.override_db_path(":memory:")
    yield
    storage.override_db_path(":memory:")


def _kit(tmp_path: Path, *, old=("a.xml", "b.xml"), fresh=("c.xml",)) -> Path:
    for name in old:
        path = tmp_path / name
        path.write_text("<message/>")
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    for name in fresh:
        (tmp_path / name).write_text("<message/>")
    return tmp_path


def _trail() -> list[dict]:
    return [r for r in run(storage.get_action_audit_log(limit=50)) if str(r["action_id"]).startswith("kit_purge:")]


def test_a_purge_by_a_person_is_on_the_audit_trail_with_their_name_and_the_counts(db, tmp_path):
    from src.maintenance import purge_old_kit_files

    kit = _kit(tmp_path)
    assert purge_old_kit_files(kit, max_age_minutes=5, actor="ada@supervisor [oidc]") == (2, 1)

    rows = sorted(_trail(), key=lambda r: r["created_at"])
    assert [(r["previous_status"], r["new_status"]) for r in rows] == [
        ("WAITING", "DELETE_REQUESTED"), ("DELETE_REQUESTED", "DELETED")]
    assert {r["actor"] for r in rows} == {"ada@supervisor [oidc]"}
    assert len({r["action_id"] for r in rows}) == 1, "one purge, one id: the outcome follows the request"
    assert "deleted 2 file(s), 2 of them messages never ingested; 1 remain" in rows[1]["note"]


def test_the_request_is_recorded_before_anything_is_deleted(db, tmp_path, monkeypatch):
    from src import maintenance

    kit = _kit(tmp_path)
    seen_when_recorded = []

    def record(actor, previous, new, note, action_id=None):
        seen_when_recorded.append((new, sorted(p.name for p in kit.iterdir())))
        return "kit_purge:x"

    monkeypatch.setattr(maintenance, "_audit", record)
    maintenance.purge_old_kit_files(kit, max_age_minutes=5, actor="ada")

    assert seen_when_recorded[0] == ("DELETE_REQUESTED", ["a.xml", "b.xml", "c.xml"]), "nothing gone yet"
    assert seen_when_recorded[1][0] == "DELETED"


def test_if_the_request_cannot_be_recorded_nothing_is_deleted(db, tmp_path, monkeypatch):
    from src import maintenance

    kit = _kit(tmp_path)

    def down(*a, **k):
        raise ConnectionError("audit store unavailable")

    monkeypatch.setattr(maintenance, "_audit", down)
    with pytest.raises(ConnectionError):
        maintenance.purge_old_kit_files(kit, max_age_minutes=5, actor="ada")
    assert sorted(p.name for p in kit.iterdir()) == ["a.xml", "b.xml", "c.xml"]


def test_a_purge_with_no_actor_is_unchanged_and_writes_no_audit(db, tmp_path):
    from src.maintenance import purge_old_kit_files

    assert purge_old_kit_files(_kit(tmp_path), max_age_minutes=5) == (2, 1)
    assert _trail() == []


def test_the_dashboard_passes_the_signed_in_identity():
    page = (_ROOT / "dashboard" / "pages" / "5_Ingest_and_Replay.py").read_text(encoding="utf-8")
    assert "actor=format_actor(_principal)" in page


def test_the_janitors_log_line_names_who_asked(db, tmp_path, caplog):
    from src.maintenance import purge_old_kit_files

    with caplog.at_level("WARNING"):
        purge_old_kit_files(_kit(tmp_path), max_age_minutes=5, actor="ada")
    assert any(getattr(r, "actor", None) == "ada" for r in caplog.records)
