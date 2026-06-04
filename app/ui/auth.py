"""app/ui/auth.py — Google OAuth SSO for the CLIP Flask UI."""
from __future__ import annotations

import functools
import os

from flask import Blueprint, redirect, render_template, request, session
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

auth_bp = Blueprint("auth", __name__)

_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")
_ALLOWED_DOMAIN = "themindtrust.org"
_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


def _make_flow(state: str | None = None) -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
                "redirect_uris": [_REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=_SCOPES,
        redirect_uri=_REDIRECT_URI,
        state=state,
    )


@auth_bp.route("/login")
def login():
    error = request.args.get("error")
    if error:
        return render_template("login.html", error=error)
    flow = _make_flow()
    auth_url, state = flow.authorization_url(prompt="select_account")
    session["oauth_state"] = state
    return redirect(auth_url)


@auth_bp.route("/oauth2callback")
def oauth2callback():
    if request.args.get("state") != session.get("oauth_state"):
        return redirect("/login?error=unauthorized")
    flow = _make_flow(state=session.get("oauth_state"))
    try:
        flow.fetch_token(authorization_response=request.url)
    except Exception:
        return redirect("/login?error=unauthorized")
    credentials = flow.credentials
    try:
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            _CLIENT_ID,
        )
    except Exception:
        return redirect("/login?error=unauthorized")
    if id_info.get("hd") != _ALLOWED_DOMAIN:
        return redirect("/login?error=unauthorized")
    session["user_email"] = id_info["email"]
    session["user_name"] = id_info.get("name", "")
    return redirect("/")


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def require_login(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if "user_email" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated
