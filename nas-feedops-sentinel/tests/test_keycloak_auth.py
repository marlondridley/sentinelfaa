"""
What Sentinel decides once Keycloak has signed an operator in, and the
configuration the OIDC flow depends on agreeing wherever it is written.

Streamlit and Authlib run the protocol and validate the ID token
(src/keycloak_auth.py). These pin what stays Sentinel's: which realm roles
count, how long a sign-in lasts, that identity URLs are HTTPS -- and that the
Keycloak client, secrets.toml.example, the reconcile script and
docker-compose.yml agree on the redirect, origin, PKCE, no password grant and
the certificate authority. They once disagreed: the client and the secrets
still named http://localhost:8501 after the dashboard moved behind the HTTPS
edge proxy, and nothing noticed.
"""
from __future__ import annotations

import json
import re
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from src import keycloak_auth as ka
from src.auth import PrincipalSource, format_actor

APP = Path(__file__).resolve().parent.parent
REALM = json.loads((APP / "keycloak" / "realm-nas-sentinel.json").read_text(encoding="utf-8"))
CLIENT = next(c for c in REALM["clients"] if c["clientId"] == "nas-feedops-sentinel")
AUTH = tomllib.loads((APP / ".streamlit" / "secrets.toml.example").read_text(encoding="utf-8"))["auth"]
DASHBOARD = AUTH["redirect_uri"].removesuffix("/oauth2callback")
SIGNED_IN_AT = 1_000_000


def _claims(**overrides) -> dict:
    base = {"sub": "3f2a", "preferred_username": "jsmith", "email": "jsmith@example.test",
            "auth_time": SIGNED_IN_AT,
            "realm_access": {"roles": ["watch_officer", "offline_access", "uma_authorization"]}}
    base.update(overrides)
    return base


# ── The operator ──────────────────────────────────────────────────────────────

def test_a_signed_in_operator_is_a_verified_oidc_principal_with_only_this_applications_roles():
    principal = ka.principal_from_claims(_claims(), role="Watch Officer")
    assert (principal.principal_id, principal.source, principal.verified) == ("jsmith", PrincipalSource.OIDC, True)
    assert principal.roles == frozenset({"watch_officer"}), "realm roles this application does not define are dropped"
    assert format_actor(principal).endswith("[oidc]")


@pytest.mark.parametrize("claims, expected", [
    (_claims(), "jsmith"),
    (_claims(preferred_username=None), "jsmith@example.test"),
    (_claims(preferred_username=None, email=None), "3f2a"),
])
def test_the_operator_is_named_by_username_then_email_then_subject(claims, expected):
    assert ka.principal_from_claims(claims, role="Watch Officer").principal_id == expected


def test_claims_without_any_identifier_are_refused():
    with pytest.raises(ValueError, match="identifier"):
        ka.principal_from_claims(_claims(preferred_username=None, email=None, sub=None), role="Watch Officer")


def test_a_malformed_realm_access_grants_no_roles():
    assert ka.roles_from_claims({"realm_access": ["watch_officer"]}) == frozenset()


# ── How long a sign-in lasts ──────────────────────────────────────────────────

def test_a_sign_in_lasts_the_session_maximum_from_authentication():
    ends = SIGNED_IN_AT + ka.session_max_seconds()
    assert ka.session_expires_at(_claims()) == ends
    assert not ka.session_expired(_claims(), now=ends - 1)
    assert ka.session_expired(_claims(), now=ends)


def test_without_auth_time_the_token_issue_time_starts_the_session():
    assert ka.session_expires_at(_claims(auth_time=None, iat=SIGNED_IN_AT)) == SIGNED_IN_AT + ka.session_max_seconds()


@pytest.mark.parametrize("start", [None, "not-a-time"])
def test_a_sign_in_whose_start_is_unknown_has_expired(start):
    assert ka.session_expired(_claims(auth_time=start))


def test_the_session_bound_is_the_realms_maximum_sso_session():
    """Streamlit's identity cookie lasts 30 days; a sign-in here lasts what the realm allows."""
    assert ka.SESSION_MAX_SECONDS == REALM["ssoSessionMaxLifespan"]


# ── When a login's access ends (Add-Guest.ps1 -Days) ──────────────────────────

LAST_DAY = date(2026, 10, 13)


def test_a_login_without_an_end_date_never_ends():
    assert ka.access_ends_on(_claims()) is None
    assert not ka.access_ended(_claims(), today=date(2099, 1, 1))


def test_access_lasts_through_its_last_day_utc_and_ends_the_day_after():
    claims = _claims(sentinel_expires_on=LAST_DAY.isoformat())
    assert ka.access_ends_on(claims) == LAST_DAY
    assert not ka.access_ended(claims, today=LAST_DAY)
    assert ka.access_ended(claims, today=LAST_DAY + timedelta(days=1))


@pytest.mark.parametrize("value", ["13/10/2026", "someday", "2026-13-45", ["2026-10-13", "2027-01-01"]])
def test_an_end_date_that_cannot_be_read_has_already_passed(value):
    """An unreadable end date must not become no end date."""
    assert ka.access_ended(_claims(sentinel_expires_on=value), today=date(2000, 1, 1))


def test_a_single_valued_list_claim_is_read():
    assert ka.access_ends_on(_claims(sentinel_expires_on=["2026-10-13"])) == LAST_DAY


def test_the_realm_puts_the_end_date_in_the_id_token_and_records_sign_ins():
    mapper = next(m for m in CLIENT["protocolMappers"] if m["name"] == "access expiry in ID token")
    assert mapper["protocolMapper"] == "oidc-usermodel-attribute-mapper"
    assert mapper["config"]["user.attribute"] == mapper["config"]["claim.name"] == ka.EXPIRY_CLAIM
    assert mapper["config"]["id.token.claim"] == "true"
    assert REALM["eventsEnabled"] and REALM["adminEventsEnabled"]
    assert REALM["eventsExpiration"] == 90 * 24 * 3600
    assert REALM["adminEventsDetailsEnabled"] is False, "details would copy request bodies into the event store"


def test_the_reconcile_script_brings_a_live_realm_to_the_same_expiry_and_audit():
    script = (APP / "scripts" / "keycloak-rbac.sh").read_text(encoding="utf-8")
    assert f"EXPIRY_ATTR={ka.EXPIRY_CLAIM}" in script
    assert '"permissions" : { "view" : [ "admin" ], "edit" : [ "admin" ] }' in script, "the user cannot see or change it"
    assert "-s eventsEnabled=true -s eventsExpiration=7776000" in script
    assert "-s adminEventsEnabled=true -s adminEventsDetailsEnabled=false" in script
    operator = (APP / "scripts" / "keycloak-operator.sh").read_text(encoding="utf-8")
    assert "Keycloak did not keep the expiry date" in operator, "a dropped attribute fails loudly"
    # Read back from the whole record: kcadm's `--fields attributes` prints an
    # empty map, which failed every expiring login on the live stack.
    assert 'grep -Eq "\\"sentinel_expires_on\\"[[:space:]]*:[[:space:]]*\\[[[:space:]]*\\"$APP_EXPIRES_ON\\""' in operator
    assert not re.search(r"kc get [^\n]*--fields attributes", operator)


def test_an_http_identity_url_is_refused():
    with pytest.raises(ka.InsecureIdentityEndpoint, match="server_metadata_url"):
        ka.require_https("http://keycloak:8080/realms/x/.well-known/openid-configuration", "server_metadata_url")
    assert ka.require_https(DASHBOARD, "redirect_uri") == DASHBOARD


# ── One configuration, written in four places ─────────────────────────────────

def test_the_keycloak_client_signs_in_only_by_the_code_flow_with_pkce():
    assert CLIENT["standardFlowEnabled"] and not CLIENT["publicClient"]
    assert not CLIENT["directAccessGrantsEnabled"], "the password must never pass through the dashboard"
    assert not CLIENT["implicitFlowEnabled"]
    assert CLIENT["attributes"]["pkce.code.challenge.method"] == "S256"


def test_the_client_redirect_origin_and_logout_are_the_dashboard_the_browser_loads():
    assert DASHBOARD == "https://localhost:8443", "the edge proxy's dashboard port (docker/edge/nginx.conf)"
    assert CLIENT["redirectUris"] == [AUTH["redirect_uri"]]
    assert CLIENT["webOrigins"] == [DASHBOARD]
    # After the realm's logout, Streamlit has Keycloak return the browser to redirect_uri.
    assert CLIENT["attributes"]["post.logout.redirect.uris"] == AUTH["redirect_uri"]


def test_the_reconcile_script_brings_a_live_realm_to_the_same_client():
    script = (APP / "scripts" / "keycloak-rbac.sh").read_text(encoding="utf-8")
    assert re.search(r'DASHBOARD_URL="\$\{DASHBOARD_URL:-([^}]+)\}"', script).group(1) == DASHBOARD
    for setting in ("standardFlowEnabled=true", "directAccessGrantsEnabled=false",
                    '"pkce.code.challenge.method"=S256'):
        assert setting in script


def test_keycloak_is_reached_over_https_and_verified_against_the_mounted_authority():
    ka.require_https(AUTH["keycloak"]["server_metadata_url"], "server_metadata_url")
    verify = AUTH["keycloak"]["client_kwargs"]["verify"]
    assert "ca_file" not in AUTH["keycloak"], "one CA setting: the one Streamlit's client reads"
    compose = yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))
    targets = [v.split(":")[1] for v in compose["services"]["dashboard"]["volumes"] if isinstance(v, str)]
    assert str(Path(verify).parent.as_posix()) in targets, (verify, targets)
