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
    the operator signs in on the realm's own page (OIDC) before any page
    renders, and Streamlit keeps the session. See lib/login.py and
    src/keycloak_auth.py.
  * Keycloak not configured -- the dashboard falls back to a
    self-declared callsign typed into the sidebar, and every action that
    operator takes is stamped `[self-declared]` in action_audit_log.

A deployment that wants verified attribution configures the first. A
deployment that has not configured it is not silently upgraded to look as
though it had.
"""
from __future__ import annotations

import dataclasses
import os
from urllib.parse import urlencode

import streamlit as st

from src.auth import Principal, PrincipalSource, format_actor
from src.config import settings
from src.logging_config import get_logger
from src.metrics import start_metrics_server

from lib import login as _login
from lib import theme

_log = get_logger(__name__)

# Host-published ports from docker-compose.yml. Grafana runs with
# anonymous Viewer access enabled and Prometheus has no auth of its own,
# so both open without a second login -- which is the point: an operator
# chasing a latency spike should not hit a credentials wall between the
# dashboard and the metric behind it.
# Public (edge TLS) URLs, set in docker-compose.yml; defaults for a local run.
GRAFANA_URL = os.getenv("GRAFANA_PUBLIC_URL", "http://localhost:3000")
PROMETHEUS_URL = os.getenv("PROMETHEUS_PUBLIC_URL", "http://localhost:9090")

# What an operator asks Prometheus first, preloaded when the console opens
# (Prometheus 3 query page: one panel per g<n>.expr). The gauges are exported
# by every app process, so max() takes the one that sets them.
PROMETHEUS_QUERIES = (
    "sum by (service) (rate(nas_events_ingested_total[5m])) * 60",                      # events/min, by service
    'sum by (alert_type) (increase(nas_alerts_triggered_total{severity="CRITICAL"}[1h]))',  # CRITICAL alerts, last hour
    "max(nas_hitl_queue_depth)",                                                          # actions awaiting review
    "max(nas_hitl_oldest_pending_age_seconds) / 3600",                                    # oldest review wait, hours
    "max by (airport) (nas_airport_overall_status)",  # 0 OK, 1 STALE, 2 DEGRADED, 3 UNAVAILABLE, 4 DISCONNECTED
)


def prometheus_queries_url(base: str = PROMETHEUS_URL) -> str:
    """The Prometheus query page with PROMETHEUS_QUERIES open, each graphed over the last 6 hours."""
    params = {}
    for i, expr in enumerate(PROMETHEUS_QUERIES):
        params.update({f"g{i}.expr": expr, f"g{i}.tab": "graph", f"g{i}.range_input": "6h"})
    return f"{base.rstrip('/')}/query?{urlencode(params)}"


def require_oidc_auth() -> None:
    """
    The sign-in gate. With Keycloak configured, a signed-out visitor gets the
    sign-in screen and nothing else: render_login() ends in st.stop(), so no
    page content -- and no sidebar identity widgets -- render below it.
    """
    if _login.oidc_configured() and _login.current_principal() is None:
        _login.render_login()


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
        f'font-size:13px;color:{theme.GRAY3};line-height:1.5;margin-top:6px;">'
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
        f'<div style="font-size:13px;font-weight:700;letter-spacing:0.07em;'
        f'text-transform:uppercase;color:{theme.GRAY1};margin:2px 0 6px 0;">Consoles</div>'
        f'<a href="{GRAFANA_URL}" target="_blank" style="display:block;font-size:14px;'
        f'color:{theme.BLUE5};text-decoration:none;padding:3px 0;">Grafana metrics &#8599;</a>'
        f'<a href="{theme._esc(prometheus_queries_url())}" target="_blank" style="display:block;font-size:14px;'
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
    with st.sidebar:
        st.markdown("### Watch Officer Session")

        signed_in = _login.current_principal()
        if signed_in is not None:
            _login.render_session_sidebar(signed_in)
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
            principal = dataclasses.replace(signed_in, role=role)
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
    _expose_metrics()
    st.set_page_config(page_title=page_title, page_icon=page_icon, layout="wide")

    # The stylesheet goes in first even for a signed-out visitor: the login
    # card is an st.container(border=True), which is styled by the main
    # theme's panel rules. render_login() then layers its own rules on top
    # (hidden sidebar, narrower canvas) and wins by source order.
    theme.inject_theme()

    require_oidc_auth()
    _provenance_banner()
    return render_operator_identity_sidebar()


def _expose_metrics() -> None:
    """
    Serve this process's Prometheus metrics on METRICS_PORT, once however many pages and reruns there are
    (start_metrics_server is idempotent). Prometheus scrapes it as the nas-dashboard job, so "the dashboard
    stopped answering" is an `up == 0` like every other service, and the model calls and CRITICAL-path counters
    the dashboard's own actions raise (the planner, a refreshed brief) are visible.
    """
    try:
        start_metrics_server(port=settings.METRICS_PORT)
    except Exception as exc:  # noqa: BLE001 -- observability must never stop a page from rendering
        _log.warning("dashboard_metrics_server_failed", extra={"error": f"{type(exc).__name__}: {exc}"})


def _provenance_banner() -> None:
    """
    Say so whenever data on screen did not come from the live FAA feed.

    Sample generation, replays and API posts land in the same tables as the
    live feed; each event carries data_origin, and this banner keeps an
    operator from reading a demo or a replay as the current picture.
    """
    try:
        from src import storage
        from src.async_bridge import run_async

        counts = run_async(storage.get_origin_counts(window_minutes=60))
    except Exception:  # noqa: BLE001 -- provenance banner must never break a page
        return
    not_live = {k: v for k, v in counts.items() if k != "live" and v}
    if not not_live:
        return
    detail = ", ".join(f"{n} {origin}" for origin, n in sorted(not_live.items()))
    st.warning(
        f"Not all data here is live FAA feed data: {detail} event(s) in the last hour. "
        "Alerts and actions derived from them carry the same origin tag.",
        icon="⚠️",
    )
