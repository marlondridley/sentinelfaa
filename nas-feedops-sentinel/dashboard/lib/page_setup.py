"""
Per-page bootstrap: page config, OIDC auth gate, and operator identity.

Every page in this app -- dashboard/app.py and every file under
dashboard/pages/ -- is its own independently reachable Streamlit script.
Streamlit does NOT run dashboard/app.py's top-level code when a user
navigates straight to a sub-page's URL, so the auth gate and the operator
identity block cannot live only in app.py the way they did in the old
single-file dashboard: any page that skipped calling these directly would
be reachable with no auth check at all. bootstrap_page() is the one call
every page makes, first thing, to avoid that.
"""
from __future__ import annotations

import os

import streamlit as st

from src.auth import Principal, PrincipalSource, format_actor
from src.logging_config import get_logger

_log = get_logger(__name__)


def require_oidc_auth() -> None:
    """
    Gate the whole page behind Keycloak OIDC login when `st.secrets["auth"]`
    is configured. When it isn't configured, this is a no-op -- the
    dashboard falls back to the self-declared operator identity captured by
    render_operator_identity_sidebar() below, exactly as it always has.
    """
    oidc_configured = False
    try:
        if hasattr(st, "secrets") and "auth" in st.secrets:
            oidc_configured = True
    except Exception:
        oidc_configured = False

    if not oidc_configured:
        return

    try:
        if hasattr(st, "user") and hasattr(st.user, "is_logged_in") and not st.user.is_logged_in:
            st.title("✈ NAS FeedOps Sentinel")
            st.info("🔐 **Authentication required** to access the operations dashboard and human review queue.")
            c_auth1, _ = st.columns([1, 3])
            with c_auth1:
                st.button(
                    "Sign in with Keycloak OIDC",
                    on_click=st.login,
                    args=["keycloak"],
                    type="primary",
                    use_container_width=True,
                )
            st.stop()
    except Exception as _auth_exc:
        _log.warning("oidc_auth_check_error", extra={"error": str(_auth_exc)})


def render_operator_identity_sidebar() -> Principal:
    """
    Render the "Watch Officer Session" sidebar block and return the
    resulting Principal. Every page calls this so the same operator
    identity -- and the same OIDC-verified-vs-self-declared distinction --
    is attributed on every Execution Gate / Escalation decision, regardless
    of which page the operator happened to be on when they made it.
    """
    with st.sidebar:
        st.markdown("### 👤 Watch Officer Session")
        st.caption("Attributed operator identity for all Execution Gate & Escalation decisions.")

        _oidc_user = None
        if hasattr(st, "user") and getattr(st.user, "is_logged_in", False):
            _oidc_user = (
                getattr(st.user, "email", None)
                or getattr(st.user, "preferred_username", None)
                or getattr(st.user, "name", None)
            )

        _default_op = _oidc_user or os.getenv("OPERATOR_ID") or os.getenv("USERNAME") or os.getenv("USER") or "watch_officer_primary"
        if "operator_id" not in st.session_state:
            st.session_state["operator_id"] = _default_op

        _op_input = st.text_input(
            "Operator ID / Callsign",
            value=st.session_state["operator_id"],
            help="This callsign/ID is recorded in the immutable action_audit_log table on every approve/reject transition.",
        )
        if _op_input:
            st.session_state["operator_id"] = _op_input.strip()

        _op_role = st.selectbox(
            "Role / Station",
            [
                "Watch Officer",
                "Supervisory Traffic Management Coordinator (STMC)",
                "Operations Manager in Charge (OMIC)",
                "Systems Engineer",
            ],
            index=0,
        )
        st.session_state["operator_role"] = _op_role

        # The Principal's `source` records *how* this identity was established, not just
        # what it is: a Keycloak/OIDC login is independently verified, while a typed
        # callsign is self-declared and trusted only as far as physical/network access to
        # this dashboard is controlled. format_actor() below makes that distinction visible
        # in the audit trail rather than letting both look identical.
        _operator_principal = Principal(
            principal_id=st.session_state["operator_id"],
            role=_op_role,
            source=PrincipalSource.OIDC if _oidc_user else PrincipalSource.SELF_DECLARED,
        )
        st.session_state["operator_principal_actor"] = format_actor(_operator_principal)

        if _oidc_user:
            st.success(f"Authenticated (OIDC): **{st.session_state['operator_id']}** ({_op_role})")
        else:
            st.warning(
                f"Self-declared identity: **{st.session_state['operator_id']}** ({_op_role}) — "
                "not independently verified. Configure Keycloak OIDC (`st.secrets['auth']`) "
                "for verified sign-in."
            )
        if _oidc_user and hasattr(st, "logout"):
            st.button("Sign out", on_click=st.logout, use_container_width=True)
        st.divider()

        # Points at the host-mapped Grafana port (docker-compose.yml's
        # grafana service, provisioned against monitoring/grafana-provisioning/
        # -- its one dashboard, nas-sentinel.json, already queries the same
        # nas_* Prometheus metrics sentinel exposes on :9108). This is a
        # plain link, not an embed: Grafana requires its own login, and an
        # iframe embed needs GF_SECURITY_ALLOW_EMBEDDING plus an auth-proxy
        # setup that isn't configured here, so a new tab is the honest option
        # rather than an iframe that would just show a login wall.
        st.link_button(
            "📊 Open full metrics dashboard (Grafana)",
            "http://localhost:3000",
            use_container_width=True,
            help="Requires the prometheus and grafana services to be running (docker compose up).",
        )

    return _operator_principal


def bootstrap_page(page_title: str, page_icon: str = "✈") -> Principal:
    """
    Standard page bootstrap, called as the first thing every page does:
    st.set_page_config() (must run before any other Streamlit command),
    the OIDC auth gate, then the operator identity sidebar. Returns the
    resolved Principal so a page that needs it (the Review page's
    approve/reject actions) doesn't have to re-read it out of
    st.session_state itself.
    """
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout="wide")
    require_oidc_auth()
    return render_operator_identity_sidebar()
