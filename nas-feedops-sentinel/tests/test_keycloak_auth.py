"""
Tests for the dashboard's in-app sign-in against Keycloak.

These cover the parts of src/keycloak_auth.py that decide whether an
operator gets in, and what the audit trail then says about them. They do
not touch the network: every test here exercises either pure claim
validation or a guard that must refuse before a request is ever made.

The claim tests matter more than they look. The dashboard obtains its
token over a connection it opened itself, which makes it tempting to skip
verification entirely -- and skipping it would mean anything answering on
port 8080 could mint an operator identity that lands in action_audit_log
next to a real one. These assertions are what pin an accepted token to
this realm and this client.
"""
from __future__ import annotations

import dataclasses
import time

from src import keycloak_auth as ka
from src.auth import Principal, PrincipalSource, format_actor

ISSUER = "http://localhost:8081/realms/nas-sentinel"
CLIENT_ID = "nas-feedops-sentinel"


def _claims(**overrides) -> dict:
    base = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "exp": time.time() + 300,
        "preferred_username": "sentinel",
    }
    base.update(overrides)
    return base


# ── Claim validation ──────────────────────────────────────────────────────────

def test_well_formed_token_is_accepted():
    assert ka._check_claims(_claims(), issuer=ISSUER, client_id=CLIENT_ID) == ""


def test_token_from_a_different_realm_is_rejected():
    """A token signed by the same Keycloak but issued by another realm (master,
    say) must not authenticate an operator here."""
    other = _claims(iss="http://localhost:8081/realms/master")
    assert "issuer" in ka._check_claims(other, issuer=ISSUER, client_id=CLIENT_ID)


def test_token_issued_for_a_different_client_is_rejected():
    other = _claims(aud="account", azp="some-other-client")
    assert "client" in ka._check_claims(other, issuer=ISSUER, client_id=CLIENT_ID)


def test_azp_alone_satisfies_the_audience_check():
    """Keycloak access tokens routinely carry aud=["account"]; azp is what
    actually identifies the requesting client, so a token that names this
    client only in azp is legitimate and must not be refused."""
    claims = _claims(aud=["account"])
    assert ka._check_claims(claims, issuer=ISSUER, client_id=CLIENT_ID) == ""


def test_expired_token_is_rejected():
    assert "expired" in ka._check_claims(
        _claims(exp=time.time() - 3600), issuer=ISSUER, client_id=CLIENT_ID
    )


def test_expiry_inside_the_clock_skew_tolerance_is_accepted():
    """The dashboard container and Keycloak keep their own clocks; a token that
    expired a heartbeat ago is drift, not an attack."""
    assert ka._check_claims(
        _claims(exp=time.time() - 10), issuer=ISSUER, client_id=CLIENT_ID
    ) == ""


def test_token_without_an_expiry_is_rejected_not_treated_as_eternal():
    claims = _claims()
    del claims["exp"]
    assert ka._check_claims(claims, issuer=ISSUER, client_id=CLIENT_ID) != ""


def test_not_yet_valid_token_is_rejected():
    result = ka._check_claims(
        _claims(nbf=time.time() + 3600), issuer=ISSUER, client_id=CLIENT_ID
    )
    assert "not yet valid" in result


# ── Error guidance ────────────────────────────────────────────────────────────
# Every branch below corresponds to a failure that actually happened while
# standing this deployment up. "Authentication error" with no further detail
# is what the redirect flow produced for all of them.

def test_bad_password_explains_the_lockout_possibility():
    _code, message, remediation = ka._classify("invalid_grant", "Invalid user credentials")
    assert message == "Incorrect username or password."
    assert "brute-force" in remediation


def test_incomplete_account_is_diagnosed_and_points_at_the_repair_script():
    _code, message, remediation = ka._classify("invalid_grant", "Account is not fully set up")
    assert "incomplete" in message.lower()
    assert "Setup-Keycloak.ps1" in remediation


def test_client_without_direct_grants_names_the_console_setting():
    _code, message, remediation = ka._classify(
        "unauthorized_client", "Client not allowed for direct access grants"
    )
    assert "in-app sign-in" in message
    assert "Direct access grants" in remediation


def test_stale_client_secret_is_diagnosed():
    _code, message, _fix = ka._classify("invalid_client", "")
    assert "client credentials" in message.lower()


def test_unrecognised_error_still_produces_actionable_text():
    _code, message, remediation = ka._classify("", "")
    assert message and remediation


# ── Guards that must refuse before any network call ───────────────────────────

def _unreachable_settings() -> ka.KeycloakSettings:
    # Deliberately unroutable: if either test below ever reaches the network,
    # it fails slowly instead of passing quietly.
    return ka.KeycloakSettings(
        client_id=CLIENT_ID,
        client_secret="not-used",
        metadata_url="http://127.0.0.1:1/realms/x/.well-known/openid-configuration",
    )


def test_empty_username_is_refused_without_contacting_keycloak():
    outcome = ka.password_login(_unreachable_settings(), "", "somepassword")
    assert outcome.ok is False
    assert outcome.error_code == "missing_credentials"


def test_empty_password_is_refused_without_contacting_keycloak():
    outcome = ka.password_login(_unreachable_settings(), "sentinel", "")
    assert outcome.ok is False
    assert outcome.error_code == "missing_credentials"


def test_auth_outcome_has_no_field_that_could_carry_a_password_back():
    """Structural guarantee, not a behavioural one: there is nowhere for the
    credential to be returned to the UI even by accident."""
    names = {f.name for f in dataclasses.fields(ka.AuthOutcome)}
    assert "password" not in names
    assert not any("password" in n for n in names)


# ── What the audit trail says afterwards ──────────────────────────────────────

def test_password_grant_is_verified_but_distinguishable_from_the_redirect_flow():
    pw = Principal("sentinel", "Watch Officer", PrincipalSource.PASSWORD_GRANT)
    oidc = Principal("sentinel", "Watch Officer", PrincipalSource.OIDC)
    self_declared = Principal("sentinel", "Watch Officer", PrincipalSource.SELF_DECLARED)

    assert pw.verified is True
    assert self_declared.verified is False
    # The whole point of the separate enum value: an auditor can tell which
    # flow authenticated the operator who approved an escalation.
    assert format_actor(pw) != format_actor(oidc)
    assert format_actor(pw).endswith("[keycloak-password]")
    assert format_actor(oidc).endswith("[oidc]")
