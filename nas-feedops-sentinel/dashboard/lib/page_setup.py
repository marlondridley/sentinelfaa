"""
Per-page bootstrap: page config, theme, sign-in gate, and operator identity.

Every page in this app -- dashboard/app.py and every file under
dashboard/pages/ -- is its own independently reachable Streamlit script.
Streamlit does NOT run dashboard/app.py's top-level code when a user
navigates straight to a sub-page's URL, so the auth gate, the stylesheet,
and the operator identity block cannot live only in app.py the way they
did in the old single-file dashboard: any page that skipped calling these
directly would be reachable with no auth check at all, and would render
unstyled. bootstrap_page() is the one call every page makes, first thing,
to avoid that.

Identity has two tiers, and the difference is recorded rather than
smoothed over:

  * Keycloak configured (.streamlit/secrets.toml has [auth.keycloak]) --
    the operator must sign in on the dashboard's own login screen before
    any page renders. See lib/login.py and src/keycloak_auth.py.
  * Keycloak not configured -- the dashboard falls back to a
    self-declared callsign typed into the sidebar, and every action that
    operator takes is stamped `[self-declared]` in action_audit_log.

A deployment that wants verified attribution configures the first. A
deployment that has not configured it is not silently upgraded to look as
though it had.
"""
from __future__ import annotations

import os

import streamlit as st

from src.auth import Principal, PrincipalSource, format_actor
from src.logging_config import get_logger

from lib import login as _login
from lib import theme

_log = get_logger(__name__)

# Host-published ports from docker-compose.yml. Grafana runs with
# anonymous Viewer access enabled and Prometheus has no auth of its own,
# so both open without a second login -- which is the point: an operator
# chasing a latency spike should not hit a credentials wall between the
# dashboard and the metric behind it.
GRAFANA_URL = "http://localhost:3000"
PROMETHEUS_URL = "http://localhost:9090"


def require_oidc_auth() -> None:
    """
    Backwards-compatible shim.

    The gate now lives in bootstrap_page() so that the resolved Principal
    can be returned to the caller in one step. Kept as a name because
    external notes and the secrets.toml.example template still refer to
    it; calling it on its own remains a valid way to gate a page.
    """
    settings = _login.keycloak_settings()
    if settings is None:
        return
    if _login.current_principal() is None:
        _login.render_login(settings)


def _self_declared_identity() -> Principal:
    """
    The no-identity-provider path: a typed callsign, recorded as such.

    This is not authentication and the UI says so. It exists because a
    single-operator lab deployment with no Keycloak still needs *a* name
    on each audit row, and an empty actor column is worse than an
    honestly-labelled unverified one.
    """
    default = (
        os.getenv("OPERATOR_ID")
        or os.getenv("USERNAME")
        or os.getenv("USER")
        or "watch_officer_primary"
    )
    st.session_state.setdefault("operator_id", default)

    typed = st.text_input(
        "Operator ID / Callsign",
        value=st.session_state["operator_id"],
        help=(
            "Recorded in the immutable action_audit_log table on every "
            "approve/reject transition."
        ),
    )
    if typed:
        st.session_state["operator_id"] = typed.strip()

    role = st.selectbox("Role / Station", _login.ROLES, index=0)
    st.session_state["operator_role"] = role

    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {theme.ORANGE5};'
        f'background:rgba(200,118,25,0.08);border-radius:3px;padding:8px 10px;'
        f'font-size:11px;color:{theme.GRAY3};line-height:1.5;margin-top:6px;">'
        f'<b style="color:{theme.ORANGE5};letter-spacing:0.05em;">UNVERIFIED</b><br/>'
        "Self-declared identity. Configure <code>[auth.keycloak]</code> in "
        ".streamlit/secrets.toml for verified sign-in.</div>",
        unsafe_allow_html=True,
    )

    return Principal(
        principal_id=st.session_state["operator_id"],
        role=role,
        source=PrincipalSource.SELF_DECLARED,
    )


def _console_links() -> None:
    """
    Links to the two observability consoles behind this dashboard.

    Plain links, not iframe embeds. Grafana refuses to be framed unless
    GF_SECURITY_ALLOW_EMBEDDING is set, and an embed that renders a login
    wall inside the ops console is worse than an honest new tab. Both
    targets are configured for anonymous read so neither prompts.
    """
    st.markdown(
        f'<div style="font-size:10.5px;font-weight:700;letter-spacing:0.07em;'
        f'text-transform:uppercase;color:{theme.GRAY1};margin:2px 0 6px 0;">Consoles</div>'
        f'<a href="{GRAFANA_URL}" target="_blank" style="display:block;font-size:12.5px;'
        f'color:{theme.BLUE5};text-decoration:none;padding:3px 0;">Grafana metrics &#8599;</a>'
        f'<a href="{PROMETHEUS_URL}" target="_blank" style="display:block;font-size:12.5px;'
        f'color:{theme.BLUE5};text-decoration:none;padding:3px 0;">Prometheus queries &#8599;</a>',
        unsafe_allow_html=True,
    )


def render_operator_identity_sidebar() -> Principal:
    """
    Render the "Watch Officer Session" sidebar block and return the
    resulting Principal. Every page calls this (via bootstrap_page) so the
    same operator identity -- and the same verified-vs-self-declared
    distinction -- is attributed on every Execution Gate / Escalation
    decision, regardless of which page the operator happened to be on when
    they made it.
    """
    settings = _login.keycloak_settings()

    with st.sidebar:
        st.markdown("### Watch Officer Session")

        signed_in = _login.current_principal()
        if settings is not None and signed_in is not None:
            _login.render_session_sidebar(settings)
            # The verified identity is fixed by the token; only the station
            # the operator is sitting at can change mid-shift.
            role = st.selectbox(
                "Role / Station",
                _login.ROLES,
                index=(
                    _login.ROLES.index(st.session_state["operator_role"])
                    if st.session_state.get("operator_role") in _login.ROLES
                    else 0
                ),
            )
            st.session_state["operator_role"] = role
            principal = Principal(
                principal_id=signed_in.principal_id,
                role=role,
                source=signed_in.source,
                roles=signed_in.roles,
            )
            # Authority comes from the token's realm roles; the station above
            # is only a label.
            st.caption(
                "Authorized roles: " + (", ".join(sorted(principal.roles)) or
                                        "none -- read-only until an admin grants a realm role")
            )
        else:
            principal = _self_declared_identity()

        # format_actor() is what turns the Principal into the flat string
        # stored in action_audit_log.actor. Computing it once here means
        # every page's approve/reject path attributes identically.
        st.session_state["operator_principal_actor"] = format_actor(principal)

        st.divider()
        _console_links()

    return principal


def bootstrap_page(page_title: str, page_icon: str = "✈") -> Principal:
    """
    Standard page bootstrap, called as the first thing every page does:
    st.set_page_config() (must run before any other Streamlit command),
    the stylesheet, the sign-in gate, then the operator identity sidebar.

    Returns the resolved Principal so a page that needs it (the Review
    page's approve/reject actions) doesn't have to re-read it out of
    st.session_state itself.
    """
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout="wide")

    # The stylesheet goes in first even for a signed-out visitor: the login
    # card is an st.container(border=True), which is styled by the main
    # theme's panel rules. render_login() then layers its own rules on top
    # (hidden sidebar, narrower canvas) and wins by source order.
    theme.inject_theme()

    settings = _login.keycloak_settings()
    if settings is not None and _login.current_principal() is None:
        # render_login() ends in st.stop(), so nothing below this line runs
        # for a signed-out user -- including the sidebar, which would
        # otherwise leak the operator identity widgets onto the login screen.
        _login.render_login(settings)

    return render_operator_identity_sidebar()
