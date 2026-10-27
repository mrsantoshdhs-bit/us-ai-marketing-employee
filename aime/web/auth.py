"""Sales Manager login for the dashboard.

- Accounts are created only from the command line (`aime user create`), where the owner types the password;
  there is no web sign-up, so nobody can claim the first account through the browser.
- Passwords are stored as scrypt hashes. Five wrong attempts lock the account for 15 minutes.
- The session is an HMAC-signed cookie (HttpOnly, SameSite=Lax, 12 hours). Its signing key is random,
  stored in data/session.key and protected with Windows DPAPI where available. Changing a password bumps
  the user's session version, which signs out every existing session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import timedelta
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from aime.models import User
from aime.secure_store import protect, unprotect
from aime.timeutil import utcnow

COOKIE = "aime_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 12
PUBLIC_PREFIXES = ("/login", "/static/")
PUBLIC_PATHS = ("/api/inbound",)  # protected by its own token and rate limit
_SECRETS: dict[str, bytes] = {}
# One sign-in check at a time (the dashboard is one process): parallel wrong passwords each count towards the lock.
LOGIN_LOCK = threading.Lock()


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    salt = os.urandom(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- session cookie
def session_secret(data_dir) -> bytes:
    key = str(data_dir)
    if key not in _SECRETS:
        path = data_dir / "session.key"
        if path.exists():
            raw = base64.b64decode(unprotect(path.read_text(encoding="utf-8").strip()))
        else:
            raw = os.urandom(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(protect(base64.b64encode(raw).decode()), encoding="utf-8")
        _SECRETS[key] = raw
    return _SECRETS[key]


def make_cookie(secret: bytes, user: User, now: float | None = None) -> str:
    payload = {"u": user.id, "v": user.session_version, "exp": int((now or time.time()) + SESSION_SECONDS)}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_cookie(secret: bytes, value: str | None, now: float | None = None) -> dict | None:
    if not value or "." not in value:
        return None
    body, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if payload.get("exp", 0) < (now or time.time()):
        return None
    return payload


# ---------------------------------------------------------------- accounts
def user_count(session: Session) -> int:
    return session.execute(select(func.count(User.id)).where(User.active.is_(True))).scalar_one()


def authenticate(session: Session, username: str, password: str) -> tuple[User | None, str]:
    """Check a login. Same message for unknown user and wrong password; lock after repeated failures."""
    user = session.execute(select(User).where(User.username == username.strip().lower())).scalars().first()
    if user is None or not user.active:
        verify_password(password, "scrypt$16384$8$AAAAAAAAAAAAAAAAAAAAAA==$AAAA")  # similar timing for unknown users
        return None, "Wrong username or password"
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        return None, f"Account locked after repeated failed sign-ins; try again after {user.locked_until:%H:%M} UTC"
    if not verify_password(password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until, user.failed_logins = now + timedelta(minutes=LOCK_MINUTES), 0
        return None, "Wrong username or password"
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    return user, "Signed in"


def safe_next(target: str | None) -> str:
    """Only same-site relative paths, never another host."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user["username"] if user else "dashboard user"


# Operations that change how the system runs, not the sales work itself: admin accounts only.
# Sales Managers approve, edit, reply, log meetings and manage leads.
ADMIN_ONLY_PREFIXES = ("/jobs/", "/integrations/linkedin/connect", "/integrations/linkedin/disconnect",
                       "/inbound/process")


def admin_only(method: str, path: str) -> bool:
    if path.startswith("/integrations/linkedin/connect"):
        return True  # starting OAuth changes the connected account, even though it is a GET
    return method == "POST" and path.startswith(ADMIN_ONLY_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Every page and API needs a signed-in user (when DASHBOARD_AUTH is on); operational changes need an admin."""

    def __init__(self, app, get_ctx):
        super().__init__(app)
        self.get_ctx = get_ctx

    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        ctx = self.get_ctx()
        path = request.url.path
        if not ctx.settings.dashboard_auth or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        payload = read_cookie(session_secret(ctx.settings.data_dir), request.cookies.get(COOKIE))
        if payload:
            with ctx.db.session() as s:
                user = s.get(User, payload.get("u"))
                if user and user.active and user.session_version == payload.get("v"):
                    request.state.user = {"id": user.id, "username": user.username, "role": user.role}
        if request.state.user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        if admin_only(request.method, path) and request.state.user["role"] != "admin":
            return JSONResponse({"detail": "Admin only: ask an admin to do this"}, status_code=403)
        return await call_next(request)
