"""
Sign-in: Keycloak is the identity provider; Streamlit keeps the session.

"Sign in with Keycloak" calls st.login(), which sends the browser to the
realm's own login page (authorization code flow with PKCE). Streamlit
exchanges the code server to server, validates the ID token, and keeps the
identity in its signed, HttpOnly cookie, so the operator stays signed in
across page loads, drill-downs and new tabs, and this application never sees
a password. The flow, and what Sentinel still decides itself (which realm
roles count, how long a sign-in lasts), are in src/keycloak_auth.py.

Configuration is [auth] and [auth.keycloak] in .streamlit/secrets.toml (see
secrets.toml.example). Without it the dashboard falls back to a self-declared
callsign, labelled UNVERIFIED (lib/page_setup.py).
"""
from __future__ import annotations

import time
from typing import Any, NoReturn

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from src.auth import Principal
from src.keycloak_auth import (
    PROVIDER,
    InsecureIdentityEndpoint,
    principal_from_claims,
    require_https,
    session_expired,
    session_expires_at,
)
from src.logging_config import get_logger

from lib import theme

_log = get_logger(__name__)

ROLES = [
    "Watch Officer",
    "Supervisory Traffic Management Coordinator (STMC)",
    "Operations Manager in Charge (OMIC)",
    "Systems Engineer",
]


def _load_auth() -> tuple[dict[str, Any] | None, str]:
    """
    ([auth] from secrets.toml when a Keycloak provider is configured, else None,
    why secrets.toml could not be read, else "").

    No secrets file at all means sign-in was never set up: the lab fallback, a
    self-declared callsign that cannot decide anything. A file that exists but
    cannot be read is a broken setup, and it must not look like the lab
    fallback: a typo would otherwise open the console to anyone.
    """
    try:
        auth = st.secrets.get("auth")
        return (auth.to_dict() if auth and auth.get(PROVIDER) else None), ""
    except StreamlitSecretNotFoundError as exc:
        if exc.__cause__ is None:                    # no secrets file: not configured
            return None, ""
        reason = f"secrets.toml could not be parsed ({type(exc.__cause__).__name__})"
    except Exception as exc:  # noqa: BLE001 -- never crash every page
        reason = f"secrets.toml could not be read ({type(exc).__name__})"
    _log.warning("auth_config_unreadable", extra={"error": reason})
    return None, reason


def _auth_config() -> dict[str, Any] | None:
    return _load_auth()[0]


def oidc_configured() -> bool:
    """Sign-in is wanted: a Keycloak provider is configured, or the file that would say so is broken."""
    auth, unreadable = _load_auth()
    return auth is not None or bool(unreadable)


# The example file ships these; a value that was never replaced is not a secret.
_PLACEHOLDERS = ("REPLACE_WITH", "change-me", "changeme")
_MIN_COOKIE_SECRET = 16


def _config_problem(auth: dict[str, Any]) -> str:
    """
    Why sign-in cannot start safely, or "" if it can. Identity URLs must be HTTPS,
    and the secrets that make a sign-in trustworthy must have been set: the roles
    the dashboard acts on are read from a cookie signed with cookie_secret, so
    anyone who knows a placeholder secret can forge a supervisor.
    """
    try:
        require_https(str(auth.get("redirect_uri", "")), "redirect_uri")
        require_https(str(auth[PROVIDER].get("server_metadata_url", "")), "server_metadata_url")
    except InsecureIdentityEndpoint as exc:
        return f"{exc}. See .streamlit/secrets.toml.example."
    for where, value, minimum in (
        ("[auth] cookie_secret", auth.get("cookie_secret"), _MIN_COOKIE_SECRET),
        ("[auth.keycloak] client_secret", auth[PROVIDER].get("client_secret"), 1),
    ):
        text = str(value or "").strip()
        if len(text) < minimum or any(text.lower().startswith(p.lower()) for p in _PLACEHOLDERS):
            return (f"{where} is missing, too short or still the example placeholder. "
                    "See .streamlit/secrets.toml.example.")
    return ""


def current_principal() -> Principal | None:
    """The signed-in operator, or None. A sign-in past the realm's maximum session is ended here."""
    # st.user has no is_logged_in at all without an [auth] section. With an unusable
    # configuration nobody is signed in, whatever cookie the browser presents: it may
    # have been signed with a secret that is public.
    auth, unreadable = _load_auth()
    if auth is None or unreadable or _config_problem(auth) or not st.user.is_logged_in:
        return None
    claims = st.user.to_dict()
    if session_expired(claims):
        _log.info("oidc_session_expired", extra={"username": claims.get("preferred_username")})
        st.logout()                      # ends the realm's session too, then returns here signed out
        return None
    return principal_from_claims(claims, st.session_state.get("operator_role", ROLES[0]))


def _login_styles() -> None:
    """The sign-in screen only: no sidebar or header, a narrow centred card."""
    st.markdown(
        f"""
        <style>
        section[data-testid="stSidebar"], header[data-testid="stHeader"] {{ display: none !important; }}
        .block-container {{ padding-top: 4vh !important; max-width: 100% !important; }}
        .lg-wrap {{ text-align: center; margin-bottom: 18px; }}
        .lg-mark {{ font-size: 30px; line-height: 1; color: {theme.BLUE5}; margin-bottom: 10px; }}
        .lg-app {{ font-size: 19px; font-weight: 700; color: {theme.LIGHT_GRAY5}; letter-spacing: 0.01em; }}
        .lg-sub {{
            font-size: 12px; color: {theme.GRAY1}; margin-top: 4px;
            letter-spacing: 0.10em; text-transform: uppercase; font-weight: 600;
        }}
        .lg-card-title {{
            font-size: 16px; font-weight: 650; color: {theme.LIGHT_GRAY5};
            text-align: center; padding-bottom: 12px; margin-bottom: 12px;
            border-bottom: 1px solid {theme.DARK_GRAY3};
        }}
        .lg-alert {{
            border: 1px solid #5E2326; border-left-width: 3px;
            background: rgba(205,66,70,0.10); border-radius: 3px;
            padding: 10px 13px; color: {theme.RED5}; font-size: 13px; font-weight: 600;
        }}
        .lg-foot {{ text-align: center; font-size: 11px; color: {theme.GRAY1}; margin-top: 16px; line-height: 1.7; }}
        .lg-foot code {{ color: {theme.GRAY3}; font-size: 10.5px; }}
        .stButton button {{ font-weight: 700 !important; letter-spacing: 0.03em; padding: 9px 0 !important; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_login() -> NoReturn:
    """Draw the sign-in screen and stop the page: nothing below renders for a signed-out visitor."""
    auth, unreadable = _load_auth()
    problem = unreadable or (_config_problem(auth) if auth else "Sign-in is not configured.")
    auth = auth or {}
    _login_styles()
    _, mid, _ = st.columns([1, 1.15, 1])
    with mid:
        st.markdown(
            '<div class="lg-wrap"><div class="lg-mark">&#9992;</div>'
            '<div class="lg-app">NAS FeedOps Sentinel</div>'
            '<div class="lg-sub">PCT TRACON Operations Console</div></div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            st.markdown('<div class="lg-card-title">Sign in to your account</div>', unsafe_allow_html=True)
            if problem:
                st.markdown(f'<div class="lg-alert">{theme._esc(problem)}</div>', unsafe_allow_html=True)
            elif st.button("Sign in with Keycloak", type="primary", use_container_width=True):
                st.login(PROVIDER)
        realm = str(auth.get(PROVIDER, {}).get("server_metadata_url", "")).split("/.well-known")[0]
        st.markdown(
            '<div class="lg-foot">You sign in on the realm\'s own page; this console never sees your password.'
            + (f"<br/>Realm: <code>{theme._esc(realm)}</code>" if realm else "") + "</div>",
            unsafe_allow_html=True,
        )
    st.stop()


def render_session_sidebar(principal: Principal) -> None:
    """The signed-in operator's block in the sidebar, with a sign-out that ends the realm session."""
    claims = st.user.to_dict()
    until = time.strftime("%H:%M", time.localtime(session_expires_at(claims)))
    email = claims.get("email") or ""
    st.markdown(
        f'<div style="border:1px solid {theme.DARK_GRAY3};border-left:3px solid {theme.GREEN5};'
        f'background:{theme.DARK_GRAY2};border-radius:3px;padding:9px 11px;margin-bottom:8px;">'
        f'<div style="font-size:13px;font-weight:650;color:{theme.LIGHT_GRAY5};">'
        f"{theme._esc(claims.get('name') or principal.principal_id)}</div>"
        + (f'<div style="font-size:11px;color:{theme.GRAY1};">{theme._esc(email)}</div>' if email else "")
        + f'<div style="font-size:10.5px;color:{theme.GREEN5};letter-spacing:0.06em;'
          f'font-weight:700;margin-top:5px;">VERIFIED &middot; KEYCLOAK</div>'
        + f'<div style="font-size:10.5px;color:{theme.GRAY1};margin-top:3px;">Signed in until {until}</div>'
        + "</div>",
        unsafe_allow_html=True,
    )
    if st.button("Sign out", use_container_width=True, key="auth_sign_out"):
        st.logout()
