"""
Principal identity layer.

Every action that mutates operational state -- starting an ingest workflow,
approving or rejecting a staged Execution Gate action, transitioning an
operational ticket -- is recorded against an `actor` string in
`action_audit_log`/`operational_tickets`. Before this module, that string was
built ad hoc in three different places (api.py's bare `None` auth result,
dashboard/app.py's f-string, and storage.py's `"dashboard_operator"` default),
so the audit trail could not reliably say *who* did something or *how* their
identity was established (a verified SSO login vs. a self-typed callsign vs. a
shared API key).

`Principal` is the single representation of "who is acting" from here on.
`format_actor()` is the single place that turns a Principal into the string
that lands in storage's `actor` column, so the format only needs to change in
one place if the audit schema ever grows a structured actor field instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PrincipalSource(str, Enum):
    """How a Principal's identity was established, in descending order of assurance."""

    OIDC = "oidc"  # Verified via Keycloak/OIDC browser redirect (authorization code flow).
    # Verified by Keycloak, but via the OAuth2 direct access grant: the
    # dashboard collected the password itself and exchanged it at the realm's
    # token endpoint, rather than handing the browser to Keycloak's own login
    # page. Keycloak still authenticated the credential -- same user store,
    # same password policy, same brute-force lockout -- so this is a verified
    # identity. It is NOT folded into OIDC above because the two differ in one
    # way an auditor would care about: in a direct grant the dashboard process
    # sees the plaintext password, and no second factor or identity-provider
    # redirect can be interposed. Recording them under separate names keeps
    # action_audit_log honest about which of the two actually happened.
    PASSWORD_GRANT = "password_grant"
    API_KEY = "api_key"  # Presented a configured API key (service-to-service).
    SELF_DECLARED = "self_declared"  # Operator typed a callsign; not independently verified.
    SYSTEM = "system"  # The application itself (e.g. auto-staging a CRITICAL alert).
    ANONYMOUS = "anonymous"  # No auth configured at all; fail-open, but recorded as such.


@dataclass(frozen=True)
class Principal:
    """An identified actor: a person, service, or the system itself."""

    principal_id: str
    role: str
    source: PrincipalSource

    @property
    def verified(self) -> bool:
        """True when this identity was independently established by an
        authority other than the operator's own assertion -- Keycloak (either
        OIDC flow) or a configured API key."""
        return self.source in (
            PrincipalSource.OIDC,
            PrincipalSource.PASSWORD_GRANT,
            PrincipalSource.API_KEY,
        )


SYSTEM_PRINCIPAL = Principal(principal_id="system", role="automation", source=PrincipalSource.SYSTEM)


def format_actor(principal: Principal) -> str:
    """
    Render a Principal into the flat string stored in the `actor` column.

    Self-declared identities are suffixed so the audit trail never implies more
    assurance than was actually obtained -- a reviewer reading the log can tell
    "jsmith (Watch Officer)" (self-declared) apart from "jsmith (Watch Officer)
    [oidc]" (verified SSO login) at a glance.
    """
    base = f"{principal.principal_id} ({principal.role})"
    if principal.source == PrincipalSource.SELF_DECLARED:
        return f"{base} [self-declared]"
    if principal.source == PrincipalSource.API_KEY:
        return f"{base} [api_key]"
    if principal.source == PrincipalSource.OIDC:
        return f"{base} [oidc]"
    if principal.source == PrincipalSource.PASSWORD_GRANT:
        return f"{base} [keycloak-password]"
    return base


def principal_from_api_key(
    provided_key: str,
    *,
    named_keys: dict[str, str],
    legacy_key: str | None,
) -> Principal | None:
    """
    Resolve a Principal from an API key presented on an inbound request.

    `named_keys` (Settings.API_KEYS) is checked first so distinct callers get
    distinct, meaningful identities in the audit trail (e.g. "ingest-service"
    rather than an opaque "someone with a key"). `legacy_key` (Settings.API_KEY)
    is the backwards-compatible single shared secret; a caller presenting it
    is recorded as the generic "legacy_api_key" principal, which is a signal on
    its own -- if that name starts showing up in audits, it means callers still
    need to be migrated to individually-named keys.

    Returns None if `provided_key` matches neither, i.e. authentication failed.
    """
    if provided_key and provided_key in named_keys:
        return Principal(
            principal_id=named_keys[provided_key],
            role="service",
            source=PrincipalSource.API_KEY,
        )
    if provided_key and legacy_key and provided_key == legacy_key:
        return Principal(
            principal_id="legacy_api_key",
            role="service",
            source=PrincipalSource.API_KEY,
        )
    return None
