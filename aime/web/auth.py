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
import re
import threading
import time
from datetime import timedelta
from urllib.parse import quote, urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from aime.db import kv_set
from aime.models import User
from aime.permissions import BUILTIN_ROLES, permissions_of
from aime.visibility import EVERYTHING, org_page, record_visible, scope_for
from aime.secure_store import protect, unprotect
from aime.timeutil import utcnow

COOKIE = "aime_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILED = 5
LOCK_MINUTES = 15
SIGN_IN_FAILED = (f"Wrong username or password. After {MAX_FAILED} wrong attempts an account waits {LOCK_MINUTES} "
                  "minutes before it can sign in again")
MIN_PASSWORD = 12
SCRYPT_N = 2 ** 15  # new and changed passwords; older hashes (2**14) are upgraded at the next successful sign-in
SCRYPT_MAXMEM = 64 * 1024 * 1024
# a well-formed hash nobody's password matches: an unknown user or a locked account costs the same scrypt work as a
# wrong password, so timing does not reveal which accounts exist
DUMMY_HASH = f"scrypt${SCRYPT_N}$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
PUBLIC_PREFIXES = ("/login", "/static/", "/setup")  # /setup: a one-time account link
PUBLIC_PATHS = ("/api/inbound",)  # protected by its own token and rate limit
_SECRETS: dict[str, bytes] = {}
# One sign-in check at a time (the dashboard is one process): parallel wrong passwords each count towards the lock.
LOGIN_LOCK = threading.Lock()


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    salt = os.urandom(16)
    n, r, p = SCRYPT_N, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=SCRYPT_MAXMEM)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=32, maxmem=SCRYPT_MAXMEM)
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


def same_text(a: str, b: str) -> bool:
    """A constant-time comparison that takes any text: compare_digest raises TypeError on non-ASCII strings, and these
    come from a cookie or a query string, where anyone can put anything."""
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def read_cookie(secret: bytes, value: str | None, now: float | None = None) -> dict | None:
    if not value or "." not in value:
        return None
    body, sig = value.rsplit(".", 1)
    if not same_text(sig, hmac.new(secret, body.encode("utf-8", "replace"), hashlib.sha256).hexdigest()):
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
        verify_password(password, DUMMY_HASH)  # similar timing for unknown users
        return None, SIGN_IN_FAILED
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        verify_password(password, DUMMY_HASH)
        return None, SIGN_IN_FAILED  # the same words as a wrong password: nobody learns which accounts exist
    if not verify_password(password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until, user.failed_logins = now + timedelta(minutes=LOCK_MINUTES), 0
        return None, SIGN_IN_FAILED
    if user.last_login_at:  # for "since you last signed in" on Manager Today: this sign-in is about to replace it
        kv_set(session, f"signin.previous.{user.id}", user.last_login_at.isoformat())
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    if not user.password_hash.startswith(f"scrypt${SCRYPT_N}$"):
        user.password_hash = hash_password(password)  # same password, today's work factor; sessions stay valid
    return user, "Signed in"


def safe_next(target: str | None) -> str:
    """Only same-site relative paths, never another host."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def return_to(request: Request) -> str:
    """Where to come back after signing in: this page, or for a form sent with an expired session, the page the form
    was on. The address a form posts to answers only POST, so the browser's GET there would be a dead end."""
    if request.method in READ_METHODS:
        return request.url.path
    page = urlparse(request.headers.get("referer") or "")
    return page.path if page.netloc == request.headers.get("host") and page.path else "/"


def wants_page(request: Request) -> bool:
    """A person's browser, which asks for HTML: it gets a page with the reason and a way back. API routes and clients
    that ask for JSON keep JSON."""
    return "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/")


def current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user["username"] if user else "dashboard user"


# Operations that change how the system runs, not the sales work itself: admin accounts only.
# Sales Managers approve, edit, reply, log meetings and manage leads.
ADMIN_ONLY_PREFIXES = ("/jobs/", "/agents/", "/integrations/linkedin/connect", "/integrations/linkedin/disconnect",
                       "/integrations/oauth/",
                       "/inbound/process", "/automation/mode", "/automation/authorizations",
                       "/automation/state/start", "/automation/state/resume")  # pausing and stopping: anyone signed in


# Mailbox configuration (servers, passwords, which mailbox sends): `configure_system` (Super Admin), viewing too.
SUPER_ADMIN_PREFIXES = ("/admin/mailboxes", "/api/mailboxes", "/admin/settings", "/integrations/linkedin-leads")
# The sales organisation: accounts, teams, custom roles: `manage_users`, viewing too.
TEAM_ADMIN_PREFIXES = ("/admin/users", "/admin/teams", "/admin/roles")


def super_admin_only(path: str) -> bool:
    return path.startswith(SUPER_ADMIN_PREFIXES)


def admin_only(method: str, path: str) -> bool:
    if path.startswith(("/integrations/linkedin/connect", "/integrations/linkedin/callback", "/integrations/oauth/")):
        return True  # signing in changes the connected account, even though the start and callback are GETs
    return method == "POST" and path.startswith(ADMIN_ONLY_PREFIXES)


# Roles are sets of permissions (aime.permissions): the built-in sales roles from Read-Only Analyst to Super Admin,
# and custom roles an administrator defines. What each request needs:
# - configure_system: mailboxes, sales settings, LinkedIn Lead Sync;  admin_ops: jobs, automation, integrations;
# - manage_users: accounts, teams and roles;  assign_leads: moving a lead or deal to someone else;
# - approve_outbound: approving, confirming or resending what goes out to customers, and campaigns;
# - approve_outbound or reject_outbound: rejecting or cancelling an email (decided in the route: the same URL also
#   cancels a task, which edit_records is enough for);
# - edit_records: every other change. Without it (a Read-Only Analyst) a user can only look and sign out.
# - view_all: acknowledging an alert: alerts are about the whole organisation and only such a role sees them.
# Which records a user sees and works is limited separately (aime.visibility).
ROLES = tuple(BUILTIN_ROLES)
ROLE_LABELS = {k: v["label"] for k, v in BUILTIN_ROLES.items()}
ADMIN_ROLES = tuple(k for k, v in BUILTIN_ROLES.items() if "admin_ops" in v["permissions"])
READ_METHODS = ("GET", "HEAD")
_APPROVALS = re.compile(r"/(?:followups/\d+/(?:approve|reconfirm|mark_sent|requeue)"
                        r"|content/\d+/(?:approve|confirm_published|requeue)|campaigns(?:/\d+/(?:status|audience))?"
                        r"|meetings/proposals/\d+/book|meetings/\d+/move|inbox/\d+/(?:reclassify|apply_calendly))/?")
# one record in the URL: /leads/7, /opportunities/3, /followups/12/approve, /inbox/5/reviewed, /meetings/2/held,
# /meetings/proposals/4/book, /meetings/requests/9/propose, /companies/6/not_competitor
_RECORD = re.compile(r"/(?:meetings/)?(leads|opportunities|followups|inbox|meetings|proposals|requests|companies)/(\d+)(?:/|$)")
_ASSIGN = re.compile(r"/(?:leads|opportunities)/\d+/assign/?")


def read_only(perms, method: str, path: str) -> bool:
    """Whether this request changes something a user without `edit_records` may not change (anything but signing
    out)."""
    return "edit_records" not in perms and method not in READ_METHODS and path != "/logout"


def approver_only(method: str, path: str) -> bool:
    return method == "POST" and bool(_APPROVALS.fullmatch(path))


def assigning(method: str, path: str) -> bool:
    return method == "POST" and bool(_ASSIGN.fullmatch(path))


# FastAPI reads "+1", "1_0", " 1" (%20) or "1.0" in a path as the number 1, while the rules here match plain digits
# only: such a path could reach an approval under another name, so it is refused outright.
_LOOSE_NUMBER = re.compile(r"\s*[+-]?[\d_]+(?:\.0*)?\s*")
MAX_ID_DIGITS = 18


def odd_number(path: str) -> bool:
    return any(seg and not seg.isdigit() and _LOOSE_NUMBER.fullmatch(seg) for seg in path.split("/"))


def huge_number(path: str) -> bool:
    """A number no record can have: past 64 bits the database driver raises OverflowError, and past 4300 digits so
    does int(). Such a path is simply not found."""
    return any(seg.isdigit() and len(seg) > MAX_ID_DIGITS for seg in path.split("/"))


def form_id(text: str) -> int | None:
    """A record number sent in a form or a filter: None unless it is plain digits that fit a database id. isdigit()
    alone also accepts a superscript 2, which int() refuses."""
    text = text.strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= MAX_ID_DIGITS else None


def refusal(perms, method: str, path: str) -> str | None:
    """Why this signed-in user may not make this request, or None."""
    if super_admin_only(path) and "configure_system" not in perms:
        return "Super Admin only"
    if path.startswith(TEAM_ADMIN_PREFIXES) and "manage_users" not in perms:
        return "Only an administrator manages users, teams and roles"
    if admin_only(method, path) and "admin_ops" not in perms:
        return "Admin only: ask an admin to do this"
    if read_only(perms, method, path):
        return "Read-only access: ask a Sales Manager to do this"
    if approver_only(method, path) and "approve_outbound" not in perms:
        return "A Sales Manager approves what goes out to customers: ask one to do this"
    if assigning(method, path) and "assign_leads" not in perms:
        return "Only a manager assigns leads and deals: ask one to do this"
    if method == "POST" and path.startswith("/alerts/") and "view_all" not in perms:
        return "Alerts are about the whole organisation: ask a manager who sees them all to acknowledge this one"
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Every page and API needs a signed-in user (when DASHBOARD_AUTH is on); operational changes need an admin."""

    def __init__(self, app, get_ctx, error_page=None):
        super().__init__(app)
        self.get_ctx = get_ctx
        self.error_page = error_page  # (request, status, reason) -> the page a person's browser gets

    def deny(self, request: Request, status: int, reason: str):
        if self.error_page and wants_page(request):
            return self.error_page(request, status, reason)
        return JSONResponse({"detail": reason}, status_code=status)

    async def dispatch(self, request: Request, call_next):
        request.state.user = None
        request.state.scope = EVERYTHING  # which records this user may see (aime.visibility); all without sign-in
        ctx = self.get_ctx()
        path = request.url.path
        if not ctx.settings.dashboard_auth or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        payload = read_cookie(session_secret(ctx.settings.data_dir), request.cookies.get(COOKIE))
        if payload:
            with ctx.db.session() as s:
                user = s.get(User, payload.get("u"))
                if user and user.active and user.session_version == payload.get("v"):
                    request.state.user = {"id": user.id, "username": user.username, "role": user.role,
                                          "perms": permissions_of(s, user.role)}
        if request.state.user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            return RedirectResponse(f"/login?next={quote(return_to(request))}", status_code=303)
        with ctx.db.session() as s:
            request.state.scope = scope_for(s, request.state.user)
        if odd_number(path) or huge_number(path):
            return self.deny(request, 404, "Not found")
        if reason := refusal(request.state.user["perms"], request.method, path):
            return self.deny(request, 403, reason)
        if request.method in READ_METHODS and org_page(path) and "view_all" not in request.state.user["perms"]:
            # the whole organisation's figures and work: their own (or their team's) are on My work
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Organisation-wide figures need a role that sees all records"},
                                    status_code=403)
            return RedirectResponse("/my", status_code=303)
        with ctx.db.session() as s:
            if (m := _RECORD.match(path)) and not record_visible(s, request.state.scope, m.group(1), int(m.group(2))):
                # someone else's lead, deal, email, reply or meeting: as if it did not exist, for viewing and changing
                return self.deny(request, 404, "Not found")
        return await call_next(request)
