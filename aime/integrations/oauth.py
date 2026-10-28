"""OAuth 2.0 sign-in to the owner's Google or Microsoft 365 account, for the sales mailbox (IMAP and SMTP with
XOAUTH2, A2/A3) and the calendar (A4).

Off until the owner registers an OAuth client (GOOGLE_CLIENT_ID / MICROSOFT_CLIENT_ID, and the secret, in .env)
and connects the account on the Integrations page. The refresh and access tokens are then stored encrypted with
Windows DPAPI (aime/secure_store.py), like the LinkedIn token. Sign-in uses PKCE (S256) and a one-time state that
expires after 15 minutes. Tokens and codes are never logged or shown.

This module only hands out access tokens. Reading mail, sending mail and booking meetings keep their own switches
and approvals; holding a token turns none of them on.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.db import kv_get, kv_set
from aime.models import OAuthToken
from aime.secure_store import protect, unprotect
from aime.timeutil import utcnow

PROVIDERS = {
    "google": {
        "label": "Google (Gmail, Google Calendar)",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": {"mail": "https://mail.google.com/",
                   "calendar": "https://www.googleapis.com/auth/calendar.events "
                               "https://www.googleapis.com/auth/calendar.freebusy"},
        "extra": {"access_type": "offline", "prompt": "consent"},  # a refresh token every time
    },
    "microsoft": {
        "label": "Microsoft 365 (Outlook mail, Outlook calendar)",
        "auth": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "scopes": {"mail": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All "
                           "https://outlook.office.com/SMTP.Send",
                   "calendar": "offline_access https://graph.microsoft.com/Calendars.ReadWrite"},
        "extra": {},
    },
}
PURPOSES = ("mail", "calendar")
STATE_TTL = timedelta(minutes=15)
REFRESH_MARGIN = timedelta(minutes=2)


class OAuthError(RuntimeError):
    """Sign-in or token problem, in words for the owner (never contains a token)."""


def token_key(provider: str, purpose: str, account: int | None = None) -> str:
    return f"{provider}:{purpose}" if account is None else f"mailbox:{account}"


def credentials(settings, provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.google_client_id, settings.google_client_secret
    if provider == "microsoft":
        return settings.microsoft_client_id, settings.microsoft_client_secret
    raise OAuthError(f"Unknown sign-in provider {provider!r}")


def _client(session: Session, settings, provider: str, account: int | None) -> tuple[str, str, str]:
    """(client id, client secret, tenant): from .env, or from a mailbox configured in the Super Admin panel, which
    carries its organisation's own app registration (the secret stored encrypted)."""
    if account is None:
        client_id, secret = credentials(settings, provider)
        return client_id, secret, settings.microsoft_tenant or "common"
    from aime.models import MailAccount

    row = session.get(MailAccount, account)
    if row is None:
        raise OAuthError("That mailbox no longer exists")
    return (row.oauth_client_id or "", unprotect(row.oauth_client_secret) if row.oauth_client_secret else "",
            row.oauth_tenant or "common")


def configured(settings, provider: str) -> bool:
    return bool(credentials(settings, provider)[0])


def redirect_uri(settings) -> str:
    """Loopback redirect: Google and Microsoft both allow http://127.0.0.1 for an app on the owner's own PC."""
    return f"http://127.0.0.1:{settings.dashboard_port}/integrations/oauth/callback"


def _endpoint(tenant: str, provider: str, name: str) -> str:
    return PROVIDERS[provider][name].format(tenant=tenant or "common")


def start(session: Session, settings, provider: str, purpose: str, account: int | None = None) -> str:
    """The provider's sign-in page for this purpose; remembers a one-time state and the PKCE verifier."""
    if provider not in PROVIDERS or purpose not in PURPOSES:
        raise OAuthError("Unknown provider or purpose")
    client_id, _secret, tenant = _client(session, settings, provider, account)
    if not client_id:
        raise OAuthError(f"Set {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET in .env first"
                         if account is None else "Give the mailbox's app registration client ID first")
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    kv_set(session, "oauth.pending", {"state": state, "verifier": protect(verifier), "provider": provider,
                                      "purpose": purpose, "account": account, "at": utcnow().isoformat()})
    params = {"client_id": client_id, "redirect_uri": redirect_uri(settings), "response_type": "code",
              "scope": PROVIDERS[provider]["scopes"][purpose], "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256", **PROVIDERS[provider]["extra"]}
    return f"{_endpoint(tenant, provider, 'auth')}?{urlencode(params)}"


def finish(session: Session, settings, http: httpx.Client, state: str, code: str, actor: str = "admin") -> OAuthToken:
    """Exchange the code from the sign-in page for tokens and store them encrypted. The state is used once."""
    pending = kv_get(session, "oauth.pending") or {}
    kv_set(session, "oauth.pending", None)
    fresh = pending.get("at") and utcnow() - datetime.fromisoformat(pending["at"]) < STATE_TTL
    if not state or not code or state != pending.get("state") or not fresh:
        raise OAuthError("Invalid or expired sign-in; please start again")
    provider, purpose, account = pending["provider"], pending["purpose"], pending.get("account")
    client_id, secret, tenant = _client(session, settings, provider, account)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(settings),
            "client_id": client_id, "code_verifier": unprotect(pending["verifier"])}
    if secret:
        data["client_secret"] = secret
    row = store(session, provider, purpose, _token_request(http, _endpoint(tenant, provider, "token"), data), account)
    from aime.automation import audit
    audit(session, actor, "account_connected", row.provider, "OAuth sign-in completed; tokens stored encrypted")
    return row


def _token_request(http: httpx.Client, url: str, data: dict) -> dict:
    try:
        resp = http.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not reach the sign-in service ({type(exc).__name__})") from exc
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "access_token" not in body:
        raise OAuthError(f"The sign-in service refused: {body.get('error_description') or body.get('error') or resp.status_code}")
    return body


def store(session: Session, provider: str, purpose: str, body: dict, account: int | None = None) -> OAuthToken:
    key = token_key(provider, purpose, account)
    row = session.execute(select(OAuthToken).where(OAuthToken.provider == key)).scalars().first()
    if row is None:
        row = OAuthToken(provider=key, access_token="")
        session.add(row)
    row.access_token = protect(body["access_token"])
    if body.get("refresh_token"):  # a refresh may not return a new one: keep the old
        row.refresh_token = protect(body["refresh_token"])
    row.expires_at = utcnow() + timedelta(seconds=int(body.get("expires_in") or 3600))
    row.scope = (body.get("scope") or PROVIDERS[provider]["scopes"][purpose])[:300]
    row.created_at = row.created_at or utcnow()
    session.flush()
    return row


def connected(session: Session, provider: str, purpose: str, account: int | None = None) -> OAuthToken | None:
    key = token_key(provider, purpose, account)
    return session.execute(select(OAuthToken).where(OAuthToken.provider == key)).scalars().first()


def access_token(session: Session, settings, http: httpx.Client, provider: str, purpose: str,
                 now: datetime | None = None, account: int | None = None) -> str:
    """A valid access token, refreshed shortly before it expires. Raises OAuthError when not connected."""
    row = connected(session, provider, purpose, account)
    if row is None:
        raise OAuthError(f"The {PROVIDERS[provider]['label']} account is not connected for {purpose} "
                         + ("(Integrations page)" if account is None else "(Mailboxes page: Connect)"))
    now = now or utcnow()
    if row.expires_at and row.expires_at - REFRESH_MARGIN > now:
        return unprotect(row.access_token)
    if not row.refresh_token:
        raise OAuthError("The sign-in has expired and cannot be renewed: connect the account again")
    client_id, secret, tenant = _client(session, settings, provider, account)
    data = {"grant_type": "refresh_token", "refresh_token": unprotect(row.refresh_token), "client_id": client_id}
    if secret:
        data["client_secret"] = secret
    if provider == "microsoft":
        data["scope"] = PROVIDERS[provider]["scopes"][purpose]
    return unprotect(store(session, provider, purpose, _token_request(http, _endpoint(tenant, provider, "token"), data),
                           account).access_token)


def disconnect(session: Session, provider: str, purpose: str, account: int | None = None) -> bool:
    row = connected(session, provider, purpose, account)
    if row is not None:
        session.delete(row)
    return row is not None


def xoauth2(user: str, token: str) -> str:
    """The SASL XOAUTH2 string for IMAP AUTHENTICATE and SMTP AUTH (Gmail and Outlook)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"
