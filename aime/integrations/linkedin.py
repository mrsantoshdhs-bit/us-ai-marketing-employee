"""LinkedIn official API adapter: OAuth 2.0 (3-legged) + Posts API + Images API.

Only documented, authorized endpoints are used. No scraping, browser automation,
cookies, connection requests, or messaging.

Member posting needs the "Share on LinkedIn" product (scope w_member_social).
Company-page posting needs Community Management API approval (w_organization_social).
Reading posts back for reconciliation needs r_member_social / r_organization_social; without it,
uncertain publish outcomes are resolved by a person in the dashboard.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.models import OAuthToken
from aime.redact import redact
from aime.secure_store import protect, unprotect
from aime.timeutil import utcnow

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"  # noqa: S105 (endpoint URL, not a secret)
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_URL = "https://api.linkedin.com/rest/images"

_RESERVED = set("\\|{}@[]()<>#*_~")
_POST_URN_RE = re.compile(r"urn:li:(?:share|ugcPost|activity):\d+")
POST_REF_RE = re.compile(r"^(?:urn:li:(?:share|ugcPost|activity):\d+|https://www\.linkedin\.com/\S+)$")
AUTHOR_URN_RE = re.compile(r"^urn:li:(?:person|organization):[A-Za-z0-9_-]{1,64}$")
INVALID_AUTHOR_REASON = (
    "LINKEDIN_AUTHOR_URN in .env is not a valid LinkedIn author URN, so it is ignored and publishing is "
    "blocked. Leave it empty to post as your profile, or set urn:li:organization:<numeric id> for a page.")


def configured_author_urn(settings: Settings) -> tuple[str | None, bool]:
    """Return (author_urn, is_valid) for the LINKEDIN_AUTHOR_URN override.

    An invalid value is never returned, so it can never be displayed, logged, or sent to LinkedIn
    (a client secret pasted on the wrong line must not leak through the dashboard).
    """
    raw = (settings.linkedin_author_urn or "").strip()
    if not raw:
        return None, True
    if AUTHOR_URN_RE.match(raw):
        return raw, True
    return None, False


class LinkedInAPIError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"LinkedIn API {status}: {redact(body[:300])}")
        self.status = status
        self.body = body

    @property
    def duplicate_of(self) -> str | None:
        """LinkedIn rejects identical re-posts (422) and names the existing post: proof it was published."""
        if self.status == 422 and "duplicate" in self.body.lower():
            m = _POST_URN_RE.search(self.body)
            return m.group(0) if m else None
        return None


def escape_little_text(text: str) -> str:
    """Escape reserved characters for the Posts API 'little text' commentary format."""
    return "".join("\\" + ch if ch in _RESERVED else ch for ch in text)


def format_commentary(body: str, hashtags: list[str]) -> str:
    tags = []
    for tag in hashtags:
        clean = tag.lstrip("#").strip()
        if clean.isalnum():
            tags.append("{hashtag|\\#|" + clean + "}")
    # Plain line breaks only: a post saved before the dashboard stored them may still hold CR LF.
    text = escape_little_text(body.replace("\r\n", "\n").replace("\r", "\n").strip())
    return f"{text}\n\n{' '.join(tags)}" if tags else text


def normalize_commentary(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:400]


@dataclass
class LinkedInAuthStatus:
    configured: bool
    connected: bool
    can_publish: bool
    reason: str
    author_urn: str | None = None
    expires_at: object | None = None
    scopes: str = ""
    member_name: str | None = None
    can_read_posts: bool = False


def get_token(session: Session) -> OAuthToken | None:
    return session.execute(select(OAuthToken).where(OAuthToken.provider == "linkedin")).scalars().first()


def access_token(tok: OAuthToken) -> str:
    return unprotect(tok.access_token)


def get_auth_status(settings: Settings, session: Session) -> LinkedInAuthStatus:
    if not settings.linkedin_configured:
        return LinkedInAuthStatus(False, False, False,
                                  "NOT CONFIGURED: LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET are not set")
    tok = get_token(session)
    override, override_valid = configured_author_urn(settings)
    if not override_valid:
        return LinkedInAuthStatus(True, tok is not None, False, INVALID_AUTHOR_REASON,
                                  expires_at=tok.expires_at if tok else None, scopes=tok.scope if tok else "",
                                  member_name=tok.subject_name if tok else None)
    if tok is None:
        return LinkedInAuthStatus(True, False, False,
                                  "Not connected: open Integrations and click 'Connect LinkedIn' to authorize")
    if tok.expires_at and tok.expires_at <= utcnow():
        return LinkedInAuthStatus(True, False, False, "Access token expired: reconnect LinkedIn",
                                  expires_at=tok.expires_at, scopes=tok.scope)
    author = override or (f"urn:li:person:{tok.subject}" if tok.subject else None)
    is_org = bool(author and author.startswith("urn:li:organization:"))
    needed = "w_organization_social" if is_org else "w_member_social"
    scopes = set(tok.scope.replace(",", " ").split())
    can_read = ("r_organization_social" if is_org else "r_member_social") in scopes
    common = dict(author_urn=author, expires_at=tok.expires_at, scopes=tok.scope, member_name=tok.subject_name,
                  can_read_posts=can_read)
    if needed not in scopes:
        return LinkedInAuthStatus(True, True, False, f"Token lacks the {needed} permission", **common)
    if not author:
        return LinkedInAuthStatus(True, True, False, "Author URN unknown (userinfo missing 'sub')", **common)
    if not settings.publishing_enabled:
        return LinkedInAuthStatus(True, True, False,
                                  "Authorized, but PUBLISHING_ENABLED=false (master switch off)", **common)
    return LinkedInAuthStatus(True, True, True, "Authorized", **common)


def new_state() -> str:
    return secrets.token_urlsafe(24)


class LinkedInClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, timeout: float = 30):
        self.settings = settings
        self.transport = transport
        self.timeout = timeout

    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def _rest_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": self.settings.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    # ---- OAuth -------------------------------------------------------------
    def authorization_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.settings.linkedin_client_id,
            "redirect_uri": self.settings.linkedin_redirect_uri,
            "state": state,
            "scope": self.settings.linkedin_scopes,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict:
        with self._http() as client:
            resp = client.post(TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.linkedin_redirect_uri,
                "client_id": self.settings.linkedin_client_id,
                "client_secret": self.settings.linkedin_client_secret,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code != 200:
            raise LinkedInAPIError(resp.status_code, resp.text)
        return resp.json()

    def userinfo(self, token: str) -> dict:
        with self._http() as client:
            resp = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            raise LinkedInAPIError(resp.status_code, resp.text)
        return resp.json()

    def store_token(self, session: Session, token_resp: dict, info: dict | None) -> OAuthToken:
        tok = get_token(session) or OAuthToken(provider="linkedin", access_token="")
        tok.access_token = protect(token_resp["access_token"])  # encrypted at rest
        expires_in = int(token_resp.get("expires_in") or 0)
        tok.expires_at = utcnow() + timedelta(seconds=expires_in) if expires_in else None
        tok.scope = token_resp.get("scope") or self.settings.linkedin_scopes
        if info:
            tok.subject = info.get("sub")
            tok.subject_name = info.get("name")
        session.add(tok)
        return tok

    # ---- Publishing --------------------------------------------------------
    def upload_image(self, token: str, owner_urn: str, path: Path) -> str:
        with self._http() as client:
            init = client.post(f"{IMAGES_URL}?action=initializeUpload", headers=self._rest_headers(token),
                               json={"initializeUploadRequest": {"owner": owner_urn}})
            if init.status_code != 200:
                raise LinkedInAPIError(init.status_code, init.text)
            try:
                value = init.json()["value"]
                upload_url, image = value["uploadUrl"], value["image"]
            except (ValueError, KeyError, TypeError) as exc:  # a 200 that is not LinkedIn's answer (a gateway page)
                raise LinkedInAPIError(init.status_code, f"Unexpected image-upload answer ({type(exc).__name__})") from exc
            upload = client.put(upload_url, content=path.read_bytes(),
                                headers={"Authorization": f"Bearer {token}"})
            if upload.status_code not in (200, 201):
                raise LinkedInAPIError(upload.status_code, upload.text)
            return image

    def create_post(self, token: str, author_urn: str, commentary: str,
                    image_urn: str | None = None, image_title: str | None = None, alt_text: str | None = None) -> str:
        body: dict = {
            "author": author_urn,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image_urn:
            media: dict = {"id": image_urn}
            if image_title:
                media["title"] = image_title[:200]
            if alt_text:
                media["altText"] = alt_text[:300]
            body["content"] = {"media": media}
        with self._http() as client:
            resp = client.post(POSTS_URL, headers=self._rest_headers(token), json=body)
        if resp.status_code != 201:
            raise LinkedInAPIError(resp.status_code, resp.text)
        post_urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")
        if not post_urn:
            raise LinkedInAPIError(resp.status_code, "201 without a post id header; not marking as published")
        return post_urn

    def recent_posts(self, token: str, author_urn: str, count: int = 20) -> list[dict]:
        """Posts by the author, newest first (needs r_member_social / r_organization_social)."""
        url = f"{POSTS_URL}?q=author&author={quote(author_urn, safe='')}&count={count}&sortBy=CREATED"
        with self._http() as client:
            resp = client.get(url, headers=self._rest_headers(token))
        if resp.status_code != 200:
            raise LinkedInAPIError(resp.status_code, resp.text)
        try:
            return resp.json().get("elements", [])
        except (ValueError, AttributeError) as exc:  # a 200 that is not LinkedIn's answer (a gateway page)
            raise LinkedInAPIError(resp.status_code, f"Unexpected posts answer ({type(exc).__name__})") from exc


def post_url(post_urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_urn}/"
