"""
Tests for the Principal identity layer (src/auth.py).

Covers the three things this module exists to get right: named API keys resolve to
named principals (not just "someone with a key"), the legacy single-key fallback still
works but is recorded as generic, and format_actor() never claims more assurance than
the source actually provides.
"""
from __future__ import annotations

from src.auth import (
    Principal,
    PrincipalSource,
    SYSTEM_PRINCIPAL,
    format_actor,
    principal_from_api_key,
)


def test_named_key_resolves_to_named_principal():
    principal = principal_from_api_key(
        "secret-a",
        named_keys={"secret-a": "ingest-service", "secret-b": "ops-cron"},
        legacy_key="legacy-secret",
    )
    assert principal is not None
    assert principal.principal_id == "ingest-service"
    assert principal.source == PrincipalSource.API_KEY
    assert principal.verified is True


def test_legacy_key_resolves_to_generic_principal():
    principal = principal_from_api_key(
        "legacy-secret",
        named_keys={"secret-a": "ingest-service"},
        legacy_key="legacy-secret",
    )
    assert principal is not None
    assert principal.principal_id == "legacy_api_key"
    assert principal.source == PrincipalSource.API_KEY


def test_unknown_key_resolves_to_none():
    principal = principal_from_api_key(
        "not-a-real-key",
        named_keys={"secret-a": "ingest-service"},
        legacy_key="legacy-secret",
    )
    assert principal is None


def test_empty_key_resolves_to_none():
    principal = principal_from_api_key("", named_keys={}, legacy_key="legacy-secret")
    assert principal is None


def test_format_actor_marks_self_declared():
    p = Principal(principal_id="jsmith", role="Watch Officer", source=PrincipalSource.SELF_DECLARED)
    rendered = format_actor(p)
    assert "jsmith" in rendered
    assert "[self-declared]" in rendered


def test_format_actor_marks_oidc_as_distinct_from_self_declared():
    self_declared = format_actor(
        Principal(principal_id="jsmith", role="Watch Officer", source=PrincipalSource.SELF_DECLARED)
    )
    oidc = format_actor(Principal(principal_id="jsmith", role="Watch Officer", source=PrincipalSource.OIDC))
    assert self_declared != oidc
    assert "[oidc]" in oidc


def test_system_principal_is_not_verified():
    assert SYSTEM_PRINCIPAL.verified is False
    assert SYSTEM_PRINCIPAL.source == PrincipalSource.SYSTEM
