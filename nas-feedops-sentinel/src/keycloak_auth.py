"""
Keycloak identity for the dashboard: what Sentinel decides after OIDC.

Keycloak is the identity provider; Streamlit runs the protocol and keeps the
authenticated session (dashboard/lib/login.py). st.login("keycloak") sends
the browser to the realm's own login page -- the authorization code flow with
PKCE (S256) -- and Streamlit, through Authlib, exchanges the code server to
server, validates the ID token (signature against the realm's JWKS, issuer,
audience, expiry, nonce), and keeps the claims in its signed, HttpOnly
identity cookie. The password is typed into Keycloak, never into Sentinel; the
realm's password policy, brute-force lockout, required actions and any second
factor apply as Keycloak defines them; and the session survives page loads.

The TLS path, traced against the running stack:
  browser -> https://localhost:8443          the edge proxy -> dashboard:8501
  browser -> https://localhost:8081/.../auth  Keycloak's login page (KC_HOSTNAME)
  dashboard -> https://keycloak:8443/.../token and /certs, server to server
    (KC_HOSTNAME_BACKCHANNEL_DYNAMIC), verified against the offline root by
    [auth.keycloak.client_kwargs] verify = "/run/tls/root-ca.crt"
  Keycloak -> browser -> https://localhost:8443/oauth2callback (redirect_uri)

What stays Sentinel's, and lives here without importing Streamlit:
- which realm roles count (only this application's, KNOWN_ROLES);
- how long a sign-in lasts. Streamlit's cookie is valid for 30 days; a
  sign-in here is valid for SIGN_IN_MAX_SECONDS (at most the realm's maximum
  SSO session) from the moment the operator authenticated (auth_time), no
  longer. Nothing asks Keycloak again in between: roles and the guest expiry
  are the ones in the ID token, so a removed role or a disabled account keeps
  acting until the session ends;
- that identity URLs are HTTPS, because the realm's sign-in carries the
  operator's password;
- when a login's access ends. Keycloak has no account expiry, so a login
  made with an end date (scripts/Add-Guest.ps1 -Days) carries it in the
  admin-only user attribute sentinel_expires_on, which the realm's "access
  expiry in ID token" mapper puts in the ID token.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

from src.auth import KNOWN_ROLES, Principal, PrincipalSource
from src.config import settings

# The provider's name under [auth] in .streamlit/secrets.toml.
PROVIDER = "keycloak"

# The realm's ssoSessionMaxLifespan (keycloak/realm-nas-sentinel.json): the
# longest a sign-in lasts, from authentication, however active the operator.
SESSION_MAX_SECONDS = 36_000


class InsecureIdentityEndpoint(ValueError):
    """An identity URL that is not HTTPS."""


def require_https(url: str, what: str) -> str:
    if urlsplit(url).scheme.lower() != "https":
        raise InsecureIdentityEndpoint(f"{what} is not HTTPS: {url}")
    return url


def roles_from_claims(claims: Mapping[str, Any]) -> frozenset[str]:
    """
    This application's realm roles from verified claims' realm_access.roles.

    The realm's "realm roles in ID token" mapper puts them there
    (keycloak/realm-nas-sentinel.json). Roles this application does not
    define are ignored.
    """
    realm_access = claims.get("realm_access") or {}
    roles = realm_access.get("roles") if isinstance(realm_access, Mapping) else None
    return frozenset(str(r) for r in (roles or []) if str(r) in KNOWN_ROLES)


def session_max_seconds() -> int:
    """
    The longest a sign-in lasts: SIGN_IN_MAX_SECONDS, never more than the realm's own maximum. Roles and the
    guest expiry are read from the ID token when the operator signs in and not again, so this is also the
    longest a removed role or a disabled account can keep acting.
    """
    return max(0, min(SESSION_MAX_SECONDS, int(settings.SIGN_IN_MAX_SECONDS)))


def session_expires_at(claims: Mapping[str, Any], max_seconds: int | None = None) -> float:
    """When this sign-in ends: authentication time plus the session maximum. 0 if unknown."""
    if max_seconds is None:
        max_seconds = session_max_seconds()
    authenticated = claims.get("auth_time") or claims.get("iat")
    try:
        return float(authenticated) + max_seconds if authenticated else 0.0
    except (TypeError, ValueError):
        return 0.0


def session_expired(claims: Mapping[str, Any], now: float | None = None) -> bool:
    """True once the sign-in has outlived the realm's maximum session, or its start is unknown."""
    return (now if now is not None else time.time()) >= session_expires_at(claims)


# The ID-token claim carrying a login's last day, YYYY-MM-DD in UTC
# (scripts/keycloak-rbac.sh declares the attribute and its mapper).
EXPIRY_CLAIM = "sentinel_expires_on"


def access_ends_on(claims: Mapping[str, Any]) -> date | None:
    """
    The login's last day, or None if it does not expire (operators). A value
    that is present but not a date reads as already over: an end date that
    cannot be read must not become no end date.
    """
    raw = claims.get(EXPIRY_CLAIM)
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, list):                        # a mapper set to multivalued
        raw = raw[0] if len(raw) == 1 else None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return date.min


def access_ended(claims: Mapping[str, Any], today: date | None = None) -> bool:
    """True once today (UTC) is past the login's last day."""
    ends = access_ends_on(claims)
    return ends is not None and (today or datetime.now(UTC).date()) > ends


def principal_from_claims(claims: Mapping[str, Any], role: str) -> Principal:
    """
    The verified operator. `role` is the station they sit at, a label only;
    authority comes from `roles`, taken from the realm.
    """
    principal_id = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not principal_id:
        raise ValueError("verified claims carry no user identifier")
    return Principal(
        principal_id=str(principal_id),
        role=role,
        source=PrincipalSource.OIDC,
        roles=roles_from_claims(claims),
    )
