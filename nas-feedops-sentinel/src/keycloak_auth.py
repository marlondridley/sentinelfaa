"""
First-party sign-in against Keycloak, without handing the browser away.

Why this exists, and what it costs
----------------------------------
The dashboard originally used Streamlit's built-in OIDC support
(`st.login("keycloak")`), which is the authorization-code flow: click a
button, get redirected to Keycloak's own login page, sign in there, get
redirected back. That is the stronger design and it is still the one this
codebase would choose on a public network, because the application never
sees the operator's password and an identity provider can interpose MFA,
step-up authentication, or a federated IdP without the application
knowing or caring.

It was replaced here on an explicit operational request: the watch
officer signs in *to the Sentinel*, and bouncing through a second
product's login screen -- on a console that is published only on
loopback -- was not wanted. This module implements the alternative
honestly rather than by inventing a local password store.

What it does: the OAuth2 Resource Owner Password Credentials grant
(RFC 6749 s4.3, "direct access grant" in Keycloak's console). The
dashboard collects the username and password, posts them to the realm's
token endpoint over the compose network, and gets tokens back. Keycloak
remains the sole authority on identity:

  * the user store is the realm's, not ours -- there is no local
    credential table to keep in sync, leak, or forget to rotate;
  * the realm password policy still applies (see
    keycloak/realm-nas-sentinel.json: 12 characters, not the username,
    not the email);
  * brute-force detection still applies, and still locks the account,
    because the failure is counted by Keycloak;
  * disabling a user in the Keycloak console still locks them out of
    this dashboard immediately.

What it costs, stated plainly because the audit trail depends on it:

  * this process sees the plaintext password for the duration of the
    request. It is never logged, never stored, and never placed in
    session state -- see password_login() -- but "not retained" is a
    weaker guarantee than "never received", which is what the redirect
    flow gives you;
  * no second factor, no identity-provider federation, and no
    step-up authentication can be added without returning to the
    redirect flow;
  * a phishing page that imitates this dashboard harvests a working
    credential, where under the redirect flow it would only harvest a
    code it could not exchange.

Those three sentences are why `PrincipalSource.PASSWORD_GRANT` exists as
a separate value from `PrincipalSource.OIDC` in src/auth.py, and why
format_actor() renders it as `[keycloak-password]` rather than `[oidc]`.
An auditor reading action_audit_log can tell which flow actually
authenticated the operator who approved an escalation.

Token handling
--------------
The returned ID token is verified before it is trusted: signature
against the realm's published JWKS, then issuer, audience, authorised
party, and expiry. That verification is not redundant just because the
token arrived over a direct connection we opened -- it is what pins the
token to *this realm and this client*, and it is the difference between
"Keycloak said so" and "something answering on port 8080 said so".

This module deliberately does not import Streamlit. It is transport and
verification only; dashboard/lib/login.py owns the UI, reads settings out
of st.secrets, and is the only place session state is touched.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.auth import KNOWN_ROLES, Principal, PrincipalSource
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 8.0

# openid is required to get an ID token back at all; profile and email
# populate preferred_username / email / name, which is what the operator
# identity block and the audit actor string are built from.
_SCOPE = "openid profile email"

# Asymmetric algorithms only. Listing them explicitly rather than letting
# the library accept whatever the token header claims is what stops an
# "alg": "none" or an HMAC-signed forgery from being accepted using the
# public key as the shared secret.
_ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]

# Tolerance for clock drift between this container and Keycloak's.
_CLOCK_SKEW_S = 60


@dataclass(frozen=True)
class KeycloakSettings:
    """Everything needed to talk to the realm. Mirrors [auth.keycloak] in secrets.toml."""

    client_id: str
    client_secret: str
    metadata_url: str
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class AuthOutcome:
    """
    The result of a sign-in attempt.

    `message` is what the operator reads; `remediation` is the one concrete
    thing to do about it, and is what turns "Authentication error" -- the
    entirety of what the old redirect flow told anyone when it broke -- into
    something a person can act on without reading container logs.
    """

    ok: bool
    principal: Principal | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    refresh_token: str = ""
    error_code: str = ""
    message: str = ""
    remediation: str = ""


def _get_json(url: str, timeout_s: float) -> dict[str, Any]:
    resp = httpx.get(url, timeout=timeout_s, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def fetch_metadata(settings: KeycloakSettings) -> dict[str, Any]:
    """
    Fetch the realm's OIDC discovery document.

    Everything downstream -- token endpoint, JWKS location, the issuer
    string we pin tokens to, the logout endpoint -- comes from here rather
    than from hand-built URLs, because this deployment deliberately serves
    two different hostnames for the same realm: KC_HOSTNAME advertises
    browser-facing URLs on localhost:8081 while KC_HOSTNAME_BACKCHANNEL_DYNAMIC
    lets server-to-server URLs follow the request host (keycloak:8080 on the
    compose network). Guessing either one would work in exactly one of the
    two contexts this dashboard runs in.
    """
    doc = _get_json(settings.metadata_url, settings.timeout_s)
    missing = [k for k in ("token_endpoint", "jwks_uri", "issuer") if not doc.get(k)]
    if missing:
        raise ValueError(f"discovery document missing required keys: {', '.join(missing)}")
    return doc


def fetch_jwks(jwks_uri: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Fetch the realm's public signing keys."""
    return _get_json(jwks_uri, timeout_s)


def _decode_verified(id_token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """
    Verify the ID token's signature against the realm JWKS and return claims.

    joserfc is the primary implementation (it is the JOSE layer Authlib
    itself pulls in, so it is present wherever streamlit[auth] is), with
    Authlib's own JWT as the fallback. There is intentionally no third
    branch that skips verification: if neither library is importable this
    raises, and password_login() turns that into a failed sign-in rather
    than a successful one. Failing closed is the only safe direction here.
    """
    try:
        from joserfc import jwt as _joserfc_jwt
        from joserfc.jwk import KeySet

        key_set = KeySet.import_key_set(jwks)
        token = _joserfc_jwt.decode(id_token, key_set, algorithms=_ALLOWED_ALGS)
        return dict(token.claims)
    except ImportError:
        pass

    from authlib.jose import JsonWebKey, jwt as _authlib_jwt

    key_set = JsonWebKey.import_key_set(jwks)
    return dict(_authlib_jwt.decode(id_token, key_set))


def _check_claims(claims: dict[str, Any], *, issuer: str, client_id: str) -> str:
    """
    Validate the claims a signature alone does not cover.

    Returns "" when the token is acceptable, or a human-readable reason
    when it is not. Each check answers a specific question: `iss` -- did
    this come from the realm we configured, rather than another realm on
    the same Keycloak? `aud`/`azp` -- was it minted for this client,
    rather than replayed from a different client in the same realm?
    `exp` -- is it still current?
    """
    now = time.time()

    token_iss = str(claims.get("iss", ""))
    if token_iss != issuer:
        return f"token issuer {token_iss!r} does not match the configured realm {issuer!r}"

    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else list(aud or [])
    azp = claims.get("azp")
    if client_id not in audiences and azp != client_id:
        return f"token was not issued for client {client_id!r}"

    exp = claims.get("exp")
    if exp is None or float(exp) + _CLOCK_SKEW_S < now:
        return "token is expired"

    nbf = claims.get("nbf")
    if nbf is not None and float(nbf) - _CLOCK_SKEW_S > now:
        return "token is not yet valid (check clock skew between this host and Keycloak)"

    return ""


# Keycloak's token-endpoint errors, mapped to something an operator can act
# on. The bare error codes are OAuth2's; the descriptions are Keycloak's own
# strings, which are stable across 20.x-26.x. Anything unrecognised falls
# through to a generic message that still shows the raw description.
_ERROR_GUIDANCE: dict[str, tuple[str, str]] = {
    "invalid_user_credentials": (
        "Incorrect username or password.",
        "If several attempts have failed, the realm's brute-force protection may have "
        "temporarily locked the account -- wait, or clear the lock in the Keycloak "
        "console under Users -> the user -> Sessions.",
    ),
    "account_is_not_fully_set_up": (
        "Keycloak accepted the password but the account is incomplete.",
        "The user is missing a required profile field (usually email, first name, or "
        "last name) or has a pending required action such as 'Update Password'. "
        "Run scripts/Setup-Keycloak.ps1, which fills the profile and sets a "
        "permanent password in one pass.",
    ),
    "account_disabled": (
        "This account is disabled in Keycloak.",
        "Re-enable it in the Keycloak console under Users -> the user -> Enabled.",
    ),
    "client_not_allowed_for_direct_access_grants": (
        "The Keycloak client is not configured for in-app sign-in.",
        "Enable 'Direct access grants' on the nas-feedops-sentinel client "
        "(Clients -> nas-feedops-sentinel -> Settings -> Authentication flow), or run "
        "scripts/Setup-Keycloak.ps1, which sets it idempotently.",
    ),
    "invalid_client": (
        "The dashboard's client credentials were rejected by Keycloak.",
        "The client_secret in .streamlit/secrets.toml no longer matches the realm. "
        "Copy the current one from Clients -> nas-feedops-sentinel -> Credentials.",
    ),
    "invalid_client_credentials": (
        "The dashboard's client credentials were rejected by Keycloak.",
        "The client_secret in .streamlit/secrets.toml no longer matches the realm. "
        "Copy the current one from Clients -> nas-feedops-sentinel -> Credentials.",
    ),
}


def _classify(error: str, description: str) -> tuple[str, str, str]:
    """Map an OAuth2 error/description pair to (code, message, remediation)."""
    key = (description or error or "").strip().lower().replace(" ", "_").replace(".", "")
    if key in _ERROR_GUIDANCE:
        msg, fix = _ERROR_GUIDANCE[key]
        return key, msg, fix
    if error in _ERROR_GUIDANCE:
        msg, fix = _ERROR_GUIDANCE[error]
        return error, msg, fix
    return (
        error or "unknown_error",
        description or "Keycloak rejected the sign-in attempt.",
        "Check the nas-keycloak container logs for the matching request.",
    )


def roles_from_claims(claims: dict[str, Any]) -> frozenset[str]:
    """
    This application's realm roles from a verified token's realm_access.roles.

    The realm's "realm roles" protocol mapper on the dashboard client puts
    them in the ID token (keycloak/realm-nas-sentinel.json). Only roles this
    application defines are kept; anything else in the realm is ignored.
    """
    realm_access = claims.get("realm_access") or {}
    roles = realm_access.get("roles") if isinstance(realm_access, dict) else None
    return frozenset(str(r) for r in (roles or []) if str(r) in KNOWN_ROLES)


def password_login(
    settings: KeycloakSettings,
    username: str,
    password: str,
    *,
    role: str = "Watch Officer",
) -> AuthOutcome:
    """
    Authenticate one operator against the realm and return a verified Principal.

    `password` is used once, in the form body of the token request below,
    and is not logged, stored, returned, or placed in the AuthOutcome. The
    only thing that survives this call is the verified claim set and the
    refresh token needed to end the session on sign-out.
    """
    if not username or not password:
        return AuthOutcome(
            ok=False,
            error_code="missing_credentials",
            message="Enter both a username and a password.",
        )

    try:
        meta = fetch_metadata(settings)
    except Exception as exc:  # noqa: BLE001 -- any failure here is "realm unreachable"
        _log.warning("keycloak_discovery_failed", extra={"error": str(exc)})
        return AuthOutcome(
            ok=False,
            error_code="realm_unreachable",
            message="Cannot reach Keycloak to verify the sign-in.",
            remediation=(
                "Confirm the nas-keycloak container is running and healthy, and that "
                "server_metadata_url in .streamlit/secrets.toml resolves from inside "
                "the dashboard container (use http://keycloak:8080/... on the compose "
                "network, not localhost:8081)."
            ),
        )

    try:
        resp = httpx.post(
            meta["token_endpoint"],
            data={
                "grant_type": "password",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "username": username,
                "password": password,
                "scope": _SCOPE,
            },
            timeout=settings.timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("keycloak_token_request_failed", extra={"error": str(exc)})
        return AuthOutcome(
            ok=False,
            error_code="realm_unreachable",
            message="Keycloak did not answer the sign-in request.",
            remediation="Check that the nas-keycloak container is healthy.",
        )

    if resp.status_code != 200:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        code, message, remediation = _classify(
            str(body.get("error", "")), str(body.get("error_description", ""))
        )
        # Username is logged (it is not a secret and the audit value of
        # knowing which account was attempted is real); the password is not,
        # and neither is the response body, which can echo it back in some
        # misconfigurations.
        _log.info(
            "keycloak_login_rejected",
            extra={"username": username, "status": resp.status_code, "error_code": code},
        )
        return AuthOutcome(ok=False, error_code=code, message=message, remediation=remediation)

    payload = resp.json()
    id_token = payload.get("id_token")
    if not id_token:
        return AuthOutcome(
            ok=False,
            error_code="no_id_token",
            message="Keycloak returned tokens but no ID token.",
            remediation=(
                "The client is missing the 'openid' scope. Check that the "
                "nas-feedops-sentinel client has the default openid scope assigned."
            ),
        )

    try:
        jwks = fetch_jwks(meta["jwks_uri"], settings.timeout_s)
        claims = _decode_verified(id_token, jwks)
    except Exception as exc:  # noqa: BLE001
        _log.warning("keycloak_token_verify_failed", extra={"error": str(exc)})
        return AuthOutcome(
            ok=False,
            error_code="token_unverifiable",
            message="The token Keycloak returned could not be verified.",
            remediation=(
                "This is a signing-key or library problem, not a password problem. "
                "Check the nas-dashboard logs for 'keycloak_token_verify_failed'."
            ),
        )

    problem = _check_claims(claims, issuer=str(meta["issuer"]), client_id=settings.client_id)
    if problem:
        _log.warning("keycloak_token_rejected", extra={"reason": problem, "username": username})
        return AuthOutcome(
            ok=False,
            error_code="token_rejected",
            message=f"The token Keycloak returned was rejected: {problem}.",
            remediation="Confirm secrets.toml points at the realm you intend to authenticate against.",
        )

    principal_id = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("sub")
        or username
    )
    _log.info("keycloak_login_ok", extra={"username": str(principal_id)})

    return AuthOutcome(
        ok=True,
        principal=Principal(
            principal_id=str(principal_id),
            role=role,
            source=PrincipalSource.PASSWORD_GRANT,
            roles=roles_from_claims(claims),
        ),
        claims=claims,
        refresh_token=str(payload.get("refresh_token") or ""),
    )


def end_session(settings: KeycloakSettings, refresh_token: str) -> bool:
    """
    Revoke the operator's Keycloak session on sign-out.

    Without this, "Sign out" would only clear this app's session state
    while the Keycloak-side session stayed live until it expired on its
    own -- which would make the sign-out button a lie. Returns True when
    Keycloak acknowledged the logout. Best-effort by design: a failure
    here must not trap the operator in a signed-in state, so the caller
    clears local state either way.
    """
    if not refresh_token:
        return False
    try:
        meta = fetch_metadata(settings)
        endpoint = meta.get("end_session_endpoint")
        if not endpoint:
            return False
        resp = httpx.post(
            endpoint,
            data={
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "refresh_token": refresh_token,
            },
            timeout=settings.timeout_s,
        )
        return resp.status_code in (200, 204)
    except Exception as exc:  # noqa: BLE001
        _log.info("keycloak_logout_failed", extra={"error": str(exc)})
        return False
