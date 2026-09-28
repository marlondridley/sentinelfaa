"""
The sign-in gate must fail closed.

The roles the dashboard acts on come from a Streamlit identity cookie signed
with [auth] cookie_secret, so a placeholder secret lets anyone who knows it
forge a supervisor. And a secrets.toml that exists but cannot be read used to
read as "no sign-in configured", which is the lab fallback: a typo opened the
console.
"""
from __future__ import annotations

import copy
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("streamlit", reason="the sign-in gate is the dashboard's")
DASHBOARD = str(Path(__file__).resolve().parent.parent / "dashboard")
if DASHBOARD not in sys.path:
    sys.path.insert(0, DASHBOARD)

from streamlit.errors import StreamlitSecretNotFoundError  # noqa: E402

from lib import login  # noqa: E402

GOOD = {
    "redirect_uri": "https://localhost:8443/oauth2callback",
    "cookie_secret": secrets.token_hex(32),
    "keycloak": {
        "server_metadata_url": "https://keycloak:8443/realms/nas-sentinel/.well-known/openid-configuration",
        "client_id": "nas-feedops-sentinel",
        "client_secret": secrets.token_hex(16),
    },
}


def _auth(**overrides) -> dict:
    auth = copy.deepcopy(GOOD)
    auth.update(overrides)
    return auth


def _streamlit(monkeypatch, *, secrets_get, logged_in=False):
    """Stand in for st.secrets and st.user; login.py reads nothing else here."""
    claims = {"preferred_username": "mallory", "auth_time": 4_000_000_000,
              "realm_access": {"roles": ["supervisor"]}}
    monkeypatch.setattr(login, "st", SimpleNamespace(
        secrets=SimpleNamespace(get=secrets_get),
        user=SimpleNamespace(is_logged_in=logged_in, to_dict=lambda: claims),
        session_state={},
    ))


def test_a_complete_configuration_is_accepted():
    assert login._config_problem(GOOD) == ""


@pytest.mark.parametrize("cookie_secret", ["REPLACE_WITH_A_LONG_RANDOM_STRING", "", "short", None])
def test_a_missing_short_or_placeholder_cookie_secret_is_refused(cookie_secret):
    auth = _auth(cookie_secret=cookie_secret)
    assert "cookie_secret" in login._config_problem(auth)


def test_a_placeholder_client_secret_is_refused():
    auth = _auth()
    auth["keycloak"]["client_secret"] = "REPLACE_WITH_CLIENT_SECRET_FROM_KEYCLOAK"
    assert "client_secret" in login._config_problem(auth)


def test_no_secrets_file_is_the_lab_fallback_not_a_broken_setup(monkeypatch):
    def missing(key):
        raise StreamlitSecretNotFoundError("no secrets.toml")

    _streamlit(monkeypatch, secrets_get=missing)
    assert login._load_auth() == (None, "")
    assert login.oidc_configured() is False


def test_a_secrets_file_that_cannot_be_parsed_is_not_the_lab_fallback(monkeypatch):
    try:
        try:
            raise ValueError("bad toml")
        except ValueError as cause:
            raise StreamlitSecretNotFoundError("cannot parse") from cause
    except StreamlitSecretNotFoundError as broken:
        failure = broken

    def parse_error(key):
        raise failure

    _streamlit(monkeypatch, secrets_get=parse_error)
    auth, reason = login._load_auth()
    assert auth is None and "could not be parsed" in reason
    assert login.oidc_configured() is True, "so the sign-in gate stays up and shows the reason"


def test_any_other_failure_reading_the_secrets_also_fails_closed(monkeypatch):
    def denied(key):
        raise PermissionError("secrets.toml")

    _streamlit(monkeypatch, secrets_get=denied)
    assert "could not be read" in login._load_auth()[1]
    assert login.oidc_configured() is True


def test_a_forged_cookie_is_not_a_sign_in_while_the_secret_is_a_placeholder(monkeypatch):
    """The cookie says supervisor and is 'logged in'; it may have been signed with a public secret."""
    weak = _auth(cookie_secret="REPLACE_WITH_A_LONG_RANDOM_STRING")

    class Auth(dict):
        def to_dict(self):
            return dict(self)

    _streamlit(monkeypatch, secrets_get=lambda key: Auth(weak), logged_in=True)
    assert login.oidc_configured() is True
    assert login.current_principal() is None


def test_a_properly_configured_sign_in_is_still_honoured(monkeypatch):
    class Auth(dict):
        def to_dict(self):
            return dict(self)

    _streamlit(monkeypatch, secrets_get=lambda key: Auth(GOOD), logged_in=True)
    principal = login.current_principal()
    assert principal is not None and principal.principal_id == "mallory"
