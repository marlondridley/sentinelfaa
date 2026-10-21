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
from src import config  # noqa: E402

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


class _Metadata:
    def __init__(self, document: dict | None = None, error: Exception | None = None):
        self.document, self.error, self.calls = document, error, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.document)


def test_the_realm_shown_is_the_issuer_keycloak_publishes():
    get = _Metadata({"issuer": "https://auth.example.com/realms/nas-sentinel"})
    url = GOOD["keycloak"]["server_metadata_url"]
    assert login._issuer(url, "/run/tls/root-ca.crt", get) == "https://auth.example.com/realms/nas-sentinel"
    assert get.calls == [(url, {"verify": "/run/tls/root-ca.crt", "timeout": 3.0})], "fetched trusting the root"


@pytest.mark.parametrize("get", [_Metadata({"issuer": "http://auth.example.com/realms/nas-sentinel"}),
                                 _Metadata({}), _Metadata(error=OSError("connection refused"))])
def test_no_realm_is_shown_rather_than_a_wrong_or_insecure_one(get):
    assert login._issuer(GOOD["keycloak"]["server_metadata_url"], True, get) == ""


def _render(monkeypatch, realm: str, signed_in_claims: dict | None = None) -> str:
    """render_login with a stand-in Streamlit; returns every piece of HTML it drew, and [button: label]s."""
    class Auth(dict):
        def to_dict(self):
            return dict(self)

    class Block:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Stopped(Exception):
        pass

    def stop():
        raise Stopped

    drawn: list[str] = []

    def button(label, *args, **kwargs):
        drawn.append(f"[button: {label}]")
        return False

    monkeypatch.setattr(login, "st", SimpleNamespace(
        secrets=SimpleNamespace(get=lambda key: Auth(GOOD)),
        user=SimpleNamespace(is_logged_in=signed_in_claims is not None, to_dict=lambda: dict(signed_in_claims or {})),
        markdown=lambda html, **kwargs: drawn.append(html),
        columns=lambda spec: (Block(), Block(), Block()),
        container=lambda **kwargs: Block(),
        button=button,
        stop=stop,
    ))
    monkeypatch.setattr(login, "_realm_address", lambda url, verify: realm)
    with pytest.raises(Stopped):
        login.render_login()
    return "\n".join(drawn)


def test_the_sign_in_page_names_the_public_realm_never_the_internal_address(monkeypatch):
    page = _render(monkeypatch, "https://auth.example.com/realms/nas-sentinel")
    assert "Realm: <code>https://auth.example.com/realms/nas-sentinel</code>" in page
    assert "keycloak:8443" not in page, "the dashboard's server-to-server address stays off the page"


def test_the_sign_in_page_leaves_the_realm_out_when_it_is_unknown(monkeypatch):
    page = _render(monkeypatch, "")
    assert "Realm:" not in page and "keycloak:8443" not in page


def test_the_sign_in_page_shows_the_use_notice_before_anyone_signs_in(monkeypatch):
    page = _render(monkeypatch, "")
    assert login.USE_NOTICE in page and "[button: Sign in with Keycloak]" in page
    assert "invited users only" in login.USE_NOTICE.lower() and "recorded" in login.USE_NOTICE
    assert "government" not in login.USE_NOTICE.lower(), "a company demo must not claim to be a government system"


EXPIRED = {"preferred_username": "guest1", "auth_time": 4_000_000_000, "sentinel_expires_on": "2020-01-31",
           "realm_access": {"roles": ["watch_officer"]}}


def test_a_login_past_its_last_day_is_told_so_and_offered_only_sign_out(monkeypatch):
    page = _render(monkeypatch, "", signed_in_claims=EXPIRED)
    assert "Your access ended after 2020-01-31 (UTC)" in page
    assert "[button: Sign out]" in page and "[button: Sign in with Keycloak]" not in page


def test_a_login_past_its_last_day_is_nobody(monkeypatch):
    """Its cookie is valid and carries a decision role; it still decides nothing."""
    class Auth(dict):
        def to_dict(self):
            return dict(self)

    monkeypatch.setattr(login, "st", SimpleNamespace(
        secrets=SimpleNamespace(get=lambda key: Auth(GOOD)),
        user=SimpleNamespace(is_logged_in=True, to_dict=lambda: dict(EXPIRED)),
        session_state={},
    ))
    assert login.current_principal() is None


def test_a_properly_configured_sign_in_is_still_honoured(monkeypatch):
    class Auth(dict):
        def to_dict(self):
            return dict(self)

    _streamlit(monkeypatch, secrets_get=lambda key: Auth(GOOD), logged_in=True)
    principal = login.current_principal()
    assert principal is not None and principal.principal_id == "mallory"


# ── a deployment that requires sign-in ────────────────────────────────────────

def test_with_sign_in_required_a_missing_secrets_file_is_a_broken_setup_not_the_lab_fallback(monkeypatch):
    """The public deployment: a bad bind mount must not turn every page into a typed-callsign console."""
    def missing(key):
        raise StreamlitSecretNotFoundError("no secrets.toml")

    _streamlit(monkeypatch, secrets_get=missing)
    monkeypatch.setattr(config.settings, "REQUIRE_SIGN_IN", True)

    assert login.oidc_configured() is True
    assert login.current_principal() is None, "nobody is signed in, and there is nothing to sign in to"


def test_with_sign_in_required_the_overview_shows_only_the_not_configured_notice(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config.settings, "REQUIRE_SIGN_IN", True)      # and this test copy has no secrets.toml
    app = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"

    at = AppTest.from_file(str(app), default_timeout=90).run()

    assert not at.exception, [e.value for e in at.exception]
    html = " ".join(m.value for m in at.markdown)
    assert "Sign-in is not configured" in html
    assert not [b for b in at.button if b.label in ("Refresh brief", "Ask")], "no page content behind the notice"
    assert "Operator ID" not in " ".join(t.label for t in at.text_input), "and no typed-callsign fallback"


def test_without_it_the_lab_fallback_is_unchanged(monkeypatch):
    def missing(key):
        raise StreamlitSecretNotFoundError("no secrets.toml")

    _streamlit(monkeypatch, secrets_get=missing)
    monkeypatch.setattr(config.settings, "REQUIRE_SIGN_IN", False)

    assert login.oidc_configured() is False
