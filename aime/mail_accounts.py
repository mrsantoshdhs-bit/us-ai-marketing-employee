"""Sales mailboxes configured from the dashboard by a Super Admin, instead of .env (aime/web: /admin/mailboxes).

Two kinds:
- Gmail or Google Workspace, with IMAP and SMTP and an app password;
- Microsoft 365, with OAuth (XOAUTH2), signed in from the panel with the organisation's own app registration.

Secrets (the app password, the OAuth client secret) are stored encrypted with Windows DPAPI
(aime/secure_store.py). They are never shown, logged or returned by the API: an edit keeps the stored secret unless
a new one is typed.

**Safe by default.** A new mailbox is disabled, reads nothing and sends nothing.
- A disabled mailbox is never used.
- An enabled mailbox is read for replies only with "read replies" on.
- Only the one primary mailbox sends, and only when OUTBOUND_EMAIL_ENABLED and every existing approval allow it.

**Test connection.** It signs in to IMAP (read-only) and SMTP and sends nothing: no MAIL FROM, no recipient, no
message. It runs only when a Super Admin clicks it.

When no mailbox is configured here, everything works from .env as before.
"""

from __future__ import annotations

import dataclasses
import imaplib
import re
import smtplib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime import automation as am
from aime.integrations.email_smtp import MAIL_TIMEOUT, tls_context
from aime.models import Alert, MailAccount, OAuthToken
from aime.redact import redact
from aime.secure_store import SecretUnreadable, protect, unprotect
from aime.timeutil import utcnow

PRESETS = {
    "gmail": {"label": "Gmail / Google Workspace (app password)", "auth": "app_password",
              "imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 587},
    "microsoft365": {"label": "Microsoft 365 (OAuth)", "auth": "oauth",
                     "imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com",
                     "smtp_port": 587},
}
EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
HOST = re.compile(r"^[A-Za-z0-9.-]{3,255}$")
UNREADABLE_ALERT = "mail:secret"


def oauth_key(account_id: int) -> str:
    return f"mailbox:{account_id}"


def _stored_password(a: MailAccount) -> str | None:
    """The app password, or None when it cannot be read here (restored on another PC, Windows password reset)."""
    try:
        return unprotect(a.secret)
    except (OSError, ValueError, RuntimeError):  # on another PC, or damaged
        return None


def secret_problem(a: MailAccount) -> str | None:
    """Plain words when this mailbox's stored password cannot be read. Never says anything about the password itself."""
    if a.auth == "oauth" or not a.secret or _stored_password(a) is not None:
        return None
    return (f"The password of mailbox {a.email} cannot be read on this PC (it was stored on another PC or under another "
            "Windows password): enter it again on the Mailboxes page")


def unreadable_mailboxes(session: Session) -> list[MailAccount]:
    return [a for a in session.execute(select(MailAccount).where(MailAccount.active.is_(True)).order_by(MailAccount.id)).scalars()
            if secret_problem(a)]


def alert_unreadable(session: Session, settings) -> None:
    """One alert, updated rather than repeated, closed once the password has been entered again."""
    from aime.monitoring import alert_once, resolve_alerts

    boxes = unreadable_mailboxes(session)
    if boxes:
        alert_once(session, "error", "mailbox", " ".join(secret_problem(a) for a in boxes)[:500], settings, key=UNREADABLE_ALERT)
    elif session.execute(select(Alert.id).where(Alert.key == UNREADABLE_ALERT, Alert.resolved_at.is_(None))).first():
        resolve_alerts(session, (UNREADABLE_ALERT,), set())


def imap_factory_for(a: MailAccount, factory):
    """What the inbox job connects with: no sign-in attempt without the password (a mailbox can lock after refused
    sign-ins), and the mailbox fails its part of the run in plain words, so the others are still read."""
    why = secret_problem(a)
    if not why:
        return factory

    def refuse(*_args, **_kwargs):
        raise SecretUnreadable(why)
    return refuse


def public(a: MailAccount) -> dict:
    """What the panel and the API may show: everything except secrets (only whether one is stored)."""
    why = secret_problem(a)
    return {"id": a.id, "label": a.label, "provider": a.provider, "email": a.email, "auth": a.auth,
            "username": a.username, "imap_host": a.imap_host, "imap_port": a.imap_port, "imap_folder": a.imap_folder,
            "smtp_host": a.smtp_host, "smtp_port": a.smtp_port, "active": a.active, "read_replies": a.read_replies,
            "is_primary": a.is_primary, "has_secret": bool(a.secret), "oauth_client_id": a.oauth_client_id,
            "has_oauth_secret": bool(a.oauth_client_secret), "oauth_tenant": a.oauth_tenant,
            "last_test_at": a.last_test_at.isoformat() if a.last_test_at else (utcnow().isoformat() if why else None),
            "last_test_ok": False if why else a.last_test_ok, "last_test_detail": why or a.last_test_detail}


def _clean(data: dict, existing: MailAccount | None) -> dict:
    """Validate the form or API fields; raises ValueError with words for the Super Admin."""
    provider = (data.get("provider") or (existing.provider if existing else "")).strip()
    if provider not in PRESETS:
        raise ValueError("Choose Gmail / Google Workspace or Microsoft 365")
    preset = PRESETS[provider]
    email = (data.get("email") or (existing.email if existing else "")).strip().lower()
    if not EMAIL.match(email):
        raise ValueError("Give the mailbox's email address")
    out = {"provider": provider, "auth": preset["auth"], "email": email,
           "label": (data.get("label") or (existing.label if existing else "") or email).strip()[:100],
           "username": (data.get("username") or (existing.username if existing else "") or email).strip()[:200],
           "imap_folder": (data.get("imap_folder") or (existing.imap_folder if existing else "") or "INBOX").strip()[:100]}
    for key in ("imap_host", "smtp_host"):
        value = (data.get(key) or (getattr(existing, key) if existing else "") or preset[key]).strip().lower()
        if not HOST.match(value):
            raise ValueError(f"{key.replace('_', ' ').upper()} is not a host name")
        out[key] = value
    for key in ("imap_port", "smtp_port"):
        raw = data.get(key) or (getattr(existing, key) if existing else None) or preset[key]
        try:
            port = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key.replace('_', ' ').upper()} must be a number") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{key.replace('_', ' ').upper()} must be between 1 and 65535")
        out[key] = port
    if preset["auth"] == "oauth":
        out["oauth_client_id"] = (data.get("oauth_client_id") or (existing.oauth_client_id if existing else "")).strip()[:200]
        out["oauth_tenant"] = (data.get("oauth_tenant") or (existing.oauth_tenant if existing else "") or "common").strip()[:100]
        if not out["oauth_client_id"]:
            raise ValueError("Microsoft 365 needs the app registration's client ID")
    return out


def save(session: Session, actor: str, data: dict, account_id: int | None = None) -> MailAccount:
    """Create or update a mailbox. New mailboxes are disabled, read nothing and are not primary. A secret is
    replaced only when a new one is given; it is stored encrypted."""
    existing = session.get(MailAccount, account_id) if account_id else None
    if account_id and existing is None:
        raise ValueError("No such mailbox")
    fields = _clean(data, existing)
    clash = session.execute(select(MailAccount).where(MailAccount.email == fields["email"])).scalars().first()
    if clash is not None and clash is not existing:
        raise ValueError(f"{fields['email']} is already configured")
    a = existing or MailAccount(active=False, read_replies=False, is_primary=False, created_by=actor)
    for key, value in fields.items():
        setattr(a, key, value)
    if (data.get("secret") or "").strip():
        if a.auth != "app_password":
            raise ValueError("Microsoft 365 signs in with OAuth: no app password is stored")
        a.secret = protect(data["secret"].strip())
    if (data.get("oauth_client_secret") or "").strip():
        a.oauth_client_secret = protect(data["oauth_client_secret"].strip())
    if a.auth == "app_password" and not a.secret:
        raise ValueError("Gmail / Google Workspace needs the app password (it is stored encrypted)")
    a.updated_by = actor
    if existing is None:
        session.add(a)
    session.flush()
    am.audit(session, actor, "mailbox_created" if existing is None else "mailbox_updated", f"mailbox #{a.id}",
             f"{a.email} ({a.provider}, {a.auth}); secret {'changed' if (data.get('secret') or '').strip() else 'unchanged'}")
    return a


def change(session: Session, actor: str, account_id: int, action: str) -> MailAccount:
    """enable | disable | read_on | read_off | primary | delete. A disabled mailbox cannot be primary or read."""
    a = session.get(MailAccount, account_id)
    if a is None:
        raise ValueError("No such mailbox")
    if action == "enable":
        a.active = True
    elif action == "disable":
        a.active, a.read_replies, a.is_primary = False, False, False
    elif action in ("read_on", "read_off"):
        if action == "read_on" and not a.active:
            raise ValueError("Enable the mailbox before reading it")
        a.read_replies = action == "read_on"
    elif action == "primary":
        if not a.active:
            raise ValueError("Enable the mailbox before making it the sending mailbox")
        for other in session.execute(select(MailAccount).where(MailAccount.is_primary.is_(True))).scalars():
            other.is_primary = False
        a.is_primary = True
    elif action == "delete":
        for tok in session.execute(select(OAuthToken).where(OAuthToken.provider == oauth_key(a.id))).scalars():
            session.delete(tok)
        am.audit(session, actor, "mailbox_deleted", f"mailbox #{a.id}", f"{a.email}; stored secrets and tokens deleted")
        session.delete(a)
        session.flush()
        return a
    else:
        raise ValueError(f"Unknown action {action!r}")
    a.updated_by = actor
    am.audit(session, actor, f"mailbox_{action}", f"mailbox #{a.id}", a.email)
    return a


def primary(session: Session) -> MailAccount | None:
    return session.execute(select(MailAccount).where(MailAccount.is_primary.is_(True),
                                                     MailAccount.active.is_(True))).scalars().first()


def readable(session: Session) -> list[MailAccount]:
    return session.execute(select(MailAccount).where(MailAccount.active.is_(True), MailAccount.read_replies.is_(True))
                           .order_by(MailAccount.id)).scalars().all()


def sending_settings(session: Session, settings):
    """The settings sending uses now: the primary panel mailbox when there is one, else .env."""
    a = primary(session)
    return settings_for(settings, a) if a is not None else settings


def sending_status(session: Session, settings) -> tuple[bool, str]:
    """Whether email can go out now, and why not, judged as sending judges it: the primary panel mailbox when there
    is one, else .env. The pages and `aime status` show this, so a panel mailbox is never reported as missing."""
    from aime.integrations.email_smtp import EmailSender

    from aime.send_journal import hold, held_message

    held = hold(settings.data_dir)
    if held:  # after a restore or a damaged database: every other answer would be wrong for now
        return False, held_message(held)
    a = primary(session)
    why = secret_problem(a) if a is not None else None
    if why and settings.outbound_email_enabled:
        return False, why
    ready, reason = EmailSender(sending_settings(session, settings)).status()
    if a is not None:
        return ready, f"{reason}; sends from {a.email} (Mailboxes panel)" if ready else reason
    if not ready and reason.startswith("NOT CONFIGURED: SMTP_HOST") and session.execute(
            select(MailAccount.id).where(MailAccount.active.is_(True))).first():
        reason = "NOT CONFIGURED: no sending mailbox; make one Primary on the Mailboxes page"
    return ready, reason


def reading_status(session: Session, settings) -> tuple[bool, str]:
    """Which mailboxes are read for replies: the .env one and every enabled panel mailbox with Read replies on."""
    read = ([f"{settings.imap_host} / {settings.imap_folder}"] if settings.imap_configured else []) + [
        f"{a.email} / {a.imap_folder or 'INBOX'} (Mailboxes panel)" for a in readable(session)]
    if read:
        return True, "; ".join(read) + ", read-only"
    return False, "No mailbox is read: on the Mailboxes page enable one and turn on Read replies (or set IMAP_HOST in .env)"


def settings_for(settings, a: MailAccount):
    """The app's settings with this mailbox's server, user and sign-in in place of the .env mail settings. Every
    switch (OUTBOUND_EMAIL_ENABLED, REQUIRE_HUMAN_APPROVAL, ...) stays as it is."""
    oauth = a.auth == "oauth"
    # Unreadable: as if none were stored. The sender then reports that it is not ready and signs in to nothing.
    password = "" if oauth or not a.secret else _stored_password(a) or ""
    return dataclasses.replace(
        settings, smtp_host=a.smtp_host, smtp_port=a.smtp_port, smtp_username=a.username, smtp_password=password,
        smtp_from=a.email, smtp_use_tls=True, imap_host=a.imap_host, imap_port=a.imap_port, imap_username=a.username,
        imap_password=password, imap_folder=a.imap_folder or "INBOX", mail_auth="oauth" if oauth else "password",
        mail_oauth_provider="microsoft" if oauth else settings.mail_oauth_provider,
        microsoft_client_id=a.oauth_client_id or "" if oauth else settings.microsoft_client_id,
        microsoft_tenant=a.oauth_tenant or "common" if oauth else settings.microsoft_tenant)


def token_for(session: Session, settings, a: MailAccount, http):
    """For a Microsoft 365 mailbox: a callable giving a fresh access token (None for app-password mailboxes)."""
    if a.auth != "oauth":
        return None
    from aime.integrations import oauth

    return lambda: oauth.access_token(session, settings, http, "microsoft", "mail", account=a.id)


def test_connection(session: Session, settings, a: MailAccount, http, actor: str,
                    imap_factory=imaplib.IMAP4_SSL, smtp_factory=smtplib.SMTP, now: datetime | None = None) -> tuple[bool, str]:
    """Sign in to IMAP (read-only) and SMTP, then sign out. Sends nothing. Records and audits the result."""
    if not settings.network_enabled:
        return False, "NETWORK_ENABLED=false: no connection was made"
    token = token_for(session, settings, a, http)
    steps: list[str] = []
    ok = True
    try:
        bearer = token() if token else None
        from aime.integrations.oauth import xoauth2

        conn = imap_factory(a.imap_host, a.imap_port, ssl_context=tls_context(), timeout=MAIL_TIMEOUT)
        try:
            if bearer:
                conn.authenticate("XOAUTH2", lambda _c: xoauth2(a.username, bearer).encode())
            else:
                conn.login(a.username, unprotect(a.secret))
            typ, _ = conn.select(f'"{a.imap_folder or "INBOX"}"', readonly=True)
            if typ != "OK":
                raise RuntimeError(f"folder {a.imap_folder} cannot be opened")
            steps.append("IMAP sign-in and read-only folder: OK")
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: S110 - closing a broken connection: nothing else to do
                pass
        with smtp_factory(a.smtp_host, a.smtp_port, timeout=MAIL_TIMEOUT) as smtp:
            smtp.starttls(context=tls_context())
            if bearer:
                smtp.ehlo()
                smtp.auth("XOAUTH2", lambda challenge=None: xoauth2(a.username, bearer))
            else:
                smtp.login(a.username, unprotect(a.secret))
            steps.append("SMTP sign-in: OK (no email was sent)")
    except Exception as exc:  # the answer is the point: record it, without secrets
        ok = False
        steps.append(redact(f"failed: {type(exc).__name__}: {exc}")[:300])
    detail = "; ".join(steps)
    a.last_test_at, a.last_test_ok, a.last_test_detail = now or utcnow(), ok, detail[:500]
    am.audit(session, actor, "mailbox_tested", f"mailbox #{a.id}", f"{a.email}: {'passed' if ok else 'failed'}")
    if ok and a.is_primary and a.active:  # it signs in again: the mailbox that sends is no longer "refused". Sends nothing.
        from aime.agents.sales_workflow import SIGN_IN_ALERT
        from aime.monitoring import resolve_alerts

        resolve_alerts(session, (SIGN_IN_ALERT,), set())
    return ok, detail
