"""
The dashboard's own sign-in screen.

This replaces the "Sign in with Keycloak OIDC" button that handed the
browser to Keycloak's hosted login page and hoped it came back. The
operator now signs in to the Sentinel, on a page that belongs to the
Sentinel, and the credential is checked by Keycloak behind the scenes via
src/keycloak_auth.py's direct access grant. Read that module's docstring
before changing anything here -- it states precisely what this design
gains, what it gives up relative to the redirect flow, and why the audit
trail records `[keycloak-password]` rather than `[oidc]`.

Division of labour: this file owns the UI and the session, and is the only
place st.session_state is written for authentication. src/keycloak_auth.py
owns the protocol and imports no Streamlit at all, so the token exchange
and its verification can be exercised by tests without a browser session.

Session model
-------------
A successful sign-in puts the verified Principal, the ID-token claims, and
the refresh token into st.session_state. Streamlit's session state lives
for as long as the browser tab holds its websocket, which means navigating
between pages keeps the operator signed in and a full page reload signs
them out again. That is a deliberate non-decision rather than a feature:
persisting a session across reloads means writing a signed cookie, and a
hand-rolled cookie is a much easier thing to get wrong than a login form.
The session is additionally bounded by the ID token's own `exp` claim, so
a tab left open overnight re-prompts rather than silently trusting a
day-old token.
"""
from __future__ import annotations

import os
import time
from typing import Any

import streamlit as st

from src.auth import Principal
from src.keycloak_auth import KeycloakSettings, end_session, password_login
from src.logging_config import get_logger

from lib import theme

_log = get_logger(__name__)

_S_PRINCIPAL = "auth_principal"
_S_CLAIMS = "auth_claims"
_S_REFRESH = "auth_refresh_token"
_S_EXPIRES = "auth_expires_at"
_S_ERROR = "auth_last_error"

ROLES = [
    "Watch Officer",
    "Supervisory Traffic Management Coordinator (STMC)",
    "Operations Manager in Charge (OMIC)",
    "Systems Engineer",
]


def keycloak_settings() -> KeycloakSettings | None:
    """
    Read [auth.keycloak] out of .streamlit/secrets.toml.

    Returns None when the section is absent, which is the signal that this
    deployment has no identity provider configured at all. The dashboard
    then falls back to the self-declared operator callsign exactly as it
    did before Keycloak existed -- refusing to start would make a missing
    optional config file into an outage.
    """
    try:
        if not hasattr(st, "secrets") or "auth" not in st.secrets:
            return None
        section = st.secrets["auth"].get("keycloak")
        if not section:
            return None
        client_id = str(section.get("client_id", "")).strip()
        client_secret = str(section.get("client_secret", "")).strip()
        metadata_url = str(section.get("server_metadata_url", "")).strip()
        if not (client_id and client_secret and metadata_url):
            return None
        # The authority that signed Keycloak's certificate: ca_file in the
        # section, else KEYCLOAK_CA_FILE (set in docker-compose.yml).
        ca_file = str(section.get("ca_file", "") or os.environ.get("KEYCLOAK_CA_FILE", "")).strip() or None
        return KeycloakSettings(
            client_id=client_id,
            client_secret=client_secret,
            metadata_url=metadata_url,
            ca_file=ca_file,
        )
    except Exception as exc:  # noqa: BLE001 -- a malformed secrets file must not crash every page
        _log.warning("keycloak_settings_unreadable", extra={"error": str(exc)})
        return None


def current_principal() -> Principal | None:
    """Return the signed-in Principal, or None once the token has expired."""
    principal = st.session_state.get(_S_PRINCIPAL)
    if principal is None:
        return None
    expires_at = st.session_state.get(_S_EXPIRES) or 0
    if expires_at and time.time() > float(expires_at):
        _clear_session()
        st.session_state[_S_ERROR] = {
            "message": "Your session expired.",
            "remediation": "Sign in again to continue.",
        }
        return None
    return principal


def _clear_session() -> None:
    for key in (_S_PRINCIPAL, _S_CLAIMS, _S_REFRESH, _S_EXPIRES):
        st.session_state.pop(key, None)


def sign_out(settings: KeycloakSettings | None) -> None:
    """
    End the Keycloak session, then the local one.

    Order matters: the refresh token has to still be in session state when
    end_session() is called, or the Keycloak-side session outlives the
    button that claimed to end it.
    """
    if settings is not None:
        end_session(settings, str(st.session_state.get(_S_REFRESH) or ""))
    _clear_session()


def _login_styles() -> None:
    """
    Styling for the sign-in screen only.

    Deliberately close to Keycloak's own login layout -- a narrow centred
    card, the realm name above it, stacked labelled fields, one full-width
    primary action -- because that layout is well-judged and because the
    operator asked to keep it. What changes is whose page it is: the
    colours are the Sentinel's, and there is no redirect.
    """
    st.markdown(
        f"""
        <style>
        section[data-testid="stSidebar"], header[data-testid="stHeader"] {{ display: none !important; }}
        .block-container {{ padding-top: 4vh !important; max-width: 100% !important; }}
        .lg-wrap {{ text-align: center; margin-bottom: 18px; }}
        .lg-mark {{
            font-size: 30px; line-height: 1;
            color: {theme.BLUE5}; margin-bottom: 10px;
        }}
        .lg-app {{
            font-size: 19px; font-weight: 700; color: {theme.LIGHT_GRAY5};
            letter-spacing: 0.01em;
        }}
        .lg-sub {{
            font-size: 12px; color: {theme.GRAY1}; margin-top: 4px;
            letter-spacing: 0.10em; text-transform: uppercase; font-weight: 600;
        }}
        .lg-card-title {{
            font-size: 16px; font-weight: 650; color: {theme.LIGHT_GRAY5};
            text-align: center; padding-bottom: 12px; margin-bottom: 4px;
            border-bottom: 1px solid {theme.DARK_GRAY3};
        }}
        .lg-alert {{
            border: 1px solid #5E2326; border-left-width: 3px;
            background: rgba(205,66,70,0.10); border-radius: 3px;
            padding: 10px 13px; margin-bottom: 14px;
        }}
        .lg-alert-msg {{ color: {theme.RED5}; font-size: 13px; font-weight: 600; }}
        .lg-alert-fix {{ color: {theme.GRAY3}; font-size: 12px; margin-top: 5px; line-height: 1.5; }}
        .lg-foot {{
            text-align: center; font-size: 11px; color: {theme.GRAY1};
            margin-top: 16px; line-height: 1.7;
        }}
        .lg-foot code {{ color: {theme.GRAY3}; font-size: 10.5px; }}
        /* Field labels: Keycloak stacks a small label directly on the input.
           These selectors are page-global rather than scoped to a wrapper,
           because st.markdown() output is a sibling of the widgets that
           follow it, never their parent -- a scoped selector would match
           nothing. Safe here: this stylesheet is emitted only on the login
           screen, which has no other widgets on it. */
        label p {{ font-size: 12.5px !important; font-weight: 600 !important;
                   color: {theme.GRAY5} !important; }}
        .stTextInput input {{
            background: {theme.DARK_GRAY2} !important;
            border: 1px solid {theme.DARK_GRAY4} !important;
            font-size: 14px !important; padding: 9px 11px !important;
        }}
        .stTextInput input:focus {{ border-color: {theme.BLUE3} !important; }}
        .stForm [data-testid="stFormSubmitButton"] button {{
            width: 100%; font-weight: 700 !important; letter-spacing: 0.03em;
            padding: 9px 0 !important; margin-top: 4px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_error() -> None:
    err = st.session_state.get(_S_ERROR)
    if not err:
        return
    st.markdown(
        f'<div class="lg-alert">'
        f'<div class="lg-alert-msg">{theme._esc(err.get("message", ""))}</div>'
        + (
            f'<div class="lg-alert-fix">{theme._esc(err["remediation"])}</div>'
            if err.get("remediation") else ""
        )
        + "</div>",
        unsafe_allow_html=True,
    )


def render_login(settings: KeycloakSettings) -> None:
    """
    Draw the sign-in screen and stop the page.

    Never returns: a page that called this is not authorised to render, so
    it ends here rather than continuing with a half-built identity.
    """
    _login_styles()

    left, mid, right = st.columns([1, 1.15, 1])
    with mid:
        st.markdown(
            '<div class="lg-wrap">'
            '<div class="lg-mark">&#9992;</div>'
            '<div class="lg-app">NAS FeedOps Sentinel</div>'
            '<div class="lg-sub">PCT TRACON Operations Console</div>'
            "</div>",
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            st.markdown('<div class="lg-card-title">Sign in to your account</div>',
                        unsafe_allow_html=True)
            _render_error()

            with st.form("sentinel_login", clear_on_submit=False, border=False):
                username = st.text_input(
                    "Username or email",
                    key="login_username",
                    autocomplete="username",
                    placeholder="watch officer account",
                )
                password = st.text_input(
                    "Password",
                    type="password",
                    key="login_password",
                    autocomplete="current-password",
                )
                role = st.selectbox(
                    "Role / station for this shift",
                    ROLES,
                    index=0,
                    help=(
                        "Recorded alongside your verified identity on every Execution "
                        "Gate approve/reject decision in action_audit_log."
                    ),
                )
                submitted = st.form_submit_button("Sign in", type="primary")

            if submitted:
                with st.spinner("Verifying with Keycloak ..."):
                    outcome = password_login(
                        settings, (username or "").strip(), password or "", role=role
                    )
                if outcome.ok and outcome.principal is not None:
                    st.session_state[_S_PRINCIPAL] = outcome.principal
                    st.session_state[_S_CLAIMS] = outcome.claims
                    st.session_state[_S_REFRESH] = outcome.refresh_token
                    st.session_state[_S_EXPIRES] = outcome.claims.get("exp")
                    st.session_state["operator_id"] = outcome.principal.principal_id
                    st.session_state["operator_role"] = role
                    st.session_state.pop(_S_ERROR, None)
                    # The password lives in the widget's session-state entry
                    # until something removes it. Nothing reads it after this
                    # point, so it is dropped here rather than left resident
                    # for the life of the tab.
                    st.session_state.pop("login_password", None)
                    st.rerun()
                else:
                    st.session_state[_S_ERROR] = {
                        "message": outcome.message,
                        "remediation": outcome.remediation,
                    }
                    st.session_state.pop("login_password", None)
                    st.rerun()

        realm_hint = settings.metadata_url.split("/.well-known")[0]
        st.markdown(
            f'<div class="lg-foot">'
            f"Credentials are verified by Keycloak; this console does not store passwords.<br/>"
            f"Realm: <code>{theme._esc(realm_hint)}</code>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.stop()


def render_session_sidebar(settings: KeycloakSettings) -> None:
    """The signed-in operator's block in the sidebar, with a working sign-out."""
    principal = st.session_state.get(_S_PRINCIPAL)
    claims: dict[str, Any] = st.session_state.get(_S_CLAIMS) or {}
    if principal is None:
        return

    display = claims.get("name") or principal.principal_id
    email = claims.get("email") or ""
    expires_at = st.session_state.get(_S_EXPIRES)
    expiry_note = ""
    if expires_at:
        mins = max(0, int((float(expires_at) - time.time()) // 60))
        expiry_note = f"Session valid {mins} more min"

    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {theme.GREEN5};'
        f'background:{theme.DARK_GRAY2};border-radius:3px;padding:9px 11px;margin-bottom:8px;">'
        f'<div style="font-size:13px;font-weight:650;color:{theme.LIGHT_GRAY5};">'
        f"{theme._esc(display)}</div>"
        + (f'<div style="font-size:11px;color:{theme.GRAY1};">{theme._esc(email)}</div>' if email else "")
        + f'<div style="font-size:10.5px;color:{theme.GREEN5};letter-spacing:0.06em;'
          f'font-weight:700;margin-top:5px;">VERIFIED &middot; KEYCLOAK</div>'
        + (f'<div style="font-size:10.5px;color:{theme.GRAY1};margin-top:3px;">'
           f"{theme._esc(expiry_note)}</div>" if expiry_note else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    if st.button("Sign out", use_container_width=True, key="auth_sign_out"):
        sign_out(settings)
        st.rerun()
