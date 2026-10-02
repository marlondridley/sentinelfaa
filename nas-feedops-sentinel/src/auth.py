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

import hmac
from dataclasses import dataclass, field
from enum import Enum


class PrincipalSource(str, Enum):
    """How a Principal's identity was established, in descending order of assurance."""

    OIDC = "oidc"  # Signed in on Keycloak's own page (authorization code flow with PKCE).
    API_KEY = "api_key"  # Presented a configured API key (service-to-service).
    SELF_DECLARED = "self_declared"  # Operator typed a callsign; not independently verified.
    SYSTEM = "system"  # The application itself (e.g. auto-staging a CRITICAL alert).
    ANONYMOUS = "anonymous"  # No auth configured at all; fail-open, but recorded as such.


@dataclass(frozen=True)
class Principal:
    """An identified actor: a person, service, or the system itself."""

    principal_id: str
    role: str                      # station label the operator sits at; display only
    source: PrincipalSource
    # Authorization roles, taken from the verified Keycloak token
    # (realm_access.roles). Never from anything the operator types or picks.
    roles: frozenset[str] = field(default_factory=frozenset)

    @property
    def verified(self) -> bool:
        """True when this identity was independently established by an
        authority other than the operator's own assertion -- Keycloak (OIDC)
        or a configured API key."""
        return self.source in (PrincipalSource.OIDC, PrincipalSource.API_KEY)


SYSTEM_PRINCIPAL = Principal(principal_id="system", role="automation", source=PrincipalSource.SYSTEM)


# ── Authorization ─────────────────────────────────────────────────────────────
#
# Identity says who acted; these roles say what they were allowed to do.
# Before this, any signed-in user -- or, without Keycloak, anyone who typed a
# callsign -- could approve, override or reject a staged action, and the
# "role" in the audit trail was a dropdown the operator picked.

ROLE_WATCH_OFFICER = "watch_officer"
ROLE_SUPERVISOR = "supervisor"
ROLE_AUDITOR = "auditor"
KNOWN_ROLES = frozenset({ROLE_WATCH_OFFICER, ROLE_SUPERVISOR, ROLE_AUDITOR})


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MONITOR = "monitor"
    OVERRIDE = "override"      # edit the recommendation/severity, then sign off
    HALT_MODELS = "halt_models"          # stop every model call (src/model_halt.py)
    RESUME_MODELS = "resume_models"      # let model calls run again


# Override replaces the AI's recommendation or severity, so it takes the
# senior role; the ordinary dispositions take either operational role.
# Auditors can read everything and decide nothing.
DECISION_ROLES: dict[Decision, frozenset[str]] = {
    Decision.APPROVE: frozenset({ROLE_WATCH_OFFICER, ROLE_SUPERVISOR}),
    Decision.REJECT: frozenset({ROLE_WATCH_OFFICER, ROLE_SUPERVISOR}),
    Decision.MONITOR: frozenset({ROLE_WATCH_OFFICER, ROLE_SUPERVISOR}),
    Decision.OVERRIDE: frozenset({ROLE_SUPERVISOR}),
    # Stopping or restarting the AI layer for everyone is a supervisor's call, not a watch officer's.
    Decision.HALT_MODELS: frozenset({ROLE_SUPERVISOR}),
    Decision.RESUME_MODELS: frozenset({ROLE_SUPERVISOR}),
}


class AuthorizationError(PermissionError):
    """The principal may not make this decision."""


def authorize(principal: Principal, decision: Decision, *, allow_unverified: bool = False) -> None:
    """
    Raise AuthorizationError unless `principal` may make `decision`.

    Requires a verified human identity holding one of DECISION_ROLES[decision].
    `allow_unverified` (Settings.ALLOW_UNVERIFIED_DECISIONS, off by default)
    lets a self-declared operator decide on a single-user lab box with no
    identity provider; the audit trail still stamps them [self-declared].
    """
    if principal.source == PrincipalSource.SELF_DECLARED and allow_unverified:
        return
    if principal.source != PrincipalSource.OIDC:
        raise AuthorizationError(
            f"{decision.value} requires a verified operator sign-in; "
            f"{principal.principal_id} is {principal.source.value}."
        )
    required = DECISION_ROLES[decision]
    if not principal.roles & required:
        raise AuthorizationError(
            f"{decision.value} requires role {' or '.join(sorted(required))}; "
            f"{principal.principal_id} has {', '.join(sorted(principal.roles)) or 'no roles'}."
        )


def can_decide(principal: Principal, decision: Decision, *, allow_unverified: bool = False) -> bool:
    try:
        authorize(principal, decision, allow_unverified=allow_unverified)
    except AuthorizationError:
        return False
    return True


def format_actor(principal: Principal) -> str:
    """
    Render a Principal into the flat string stored in the `actor` column.

    Self-declared identities are suffixed so the audit trail never implies more
    assurance than was actually obtained -- a reviewer reading the log can tell
    "jsmith (Watch Officer)" (self-declared) apart from "jsmith (Watch Officer)
    [oidc]" (verified SSO login) at a glance.
    """
    base = f"{principal.principal_id} ({principal.role})"
    if principal.roles:
        # The roles held at the moment of the decision, so the audit trail
        # shows the authority it was made under, not just who clicked.
        base += f" roles={','.join(sorted(principal.roles))}"
    if principal.source == PrincipalSource.SELF_DECLARED:
        return f"{base} [self-declared]"
    if principal.source == PrincipalSource.API_KEY:
        return f"{base} [api_key]"
    if principal.source == PrincipalSource.OIDC:
        return f"{base} [oidc]"
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
    # hmac.compare_digest, not == or a dict lookup: both leak, through
    # timing, how much of a guessed key matched.
    if not provided_key:
        return None
    for secret, name in named_keys.items():
        if hmac.compare_digest(provided_key.encode(), secret.encode()):
            return Principal(principal_id=name, role="service", source=PrincipalSource.API_KEY)
    if legacy_key and hmac.compare_digest(provided_key.encode(), legacy_key.encode()):
        return Principal(
            principal_id="legacy_api_key",
            role="service",
            source=PrincipalSource.API_KEY,
        )
    return None
