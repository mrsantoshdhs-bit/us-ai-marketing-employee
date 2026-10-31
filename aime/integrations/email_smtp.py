"""SMTP sender for approved replies to inbound inquiries. Disabled unless explicitly enabled and configured."""

from __future__ import annotations

import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from aime.config import Settings


POSTAL_PLACEHOLDER = "[postal address required before sending]"
MAIL_TIMEOUT = 60  # seconds for SMTP and IMAP connections; a hung server must not hold a job for an hour
TEST_MARKER = re.compile(r"\[TEST for ([^\]\s]+)\]\s*", re.I)


def marked_subject(subject: str, to: str) -> str:
    """The subject of a test-mode email for `to`: exactly one "[TEST for <to>]" marker. A reply keeps its subject
    (and its marker where it stands) so it stays threaded; markers for other addresses or repeated ones, carried
    over from another thread, are removed."""
    found = [m.group(1).lower() for m in TEST_MARKER.finditer(subject)]
    if found == [to.lower()]:
        return subject
    return f"[TEST for {to}] {' '.join(TEST_MARKER.sub('', subject).split())}".strip()


def tls_context() -> ssl.SSLContext:
    """Verified TLS for SMTP and IMAP: the server's certificate and host name are checked.

    smtplib's starttls() and imaplib's IMAP4_SSL default to a context that checks neither, so anyone on the
    network path could read the app password."""
    return ssl.create_default_context()


class EmailNotReady(Exception):
    pass


def compliance_problem(settings: Settings, body: str) -> str | None:
    """What a sales email lacks under CAN-SPAM: the opt-out line, or the configured postal address.
    No approval or quality override can waive this; an edited draft that lost its footer is not sent."""
    text = " ".join(body.split()).lower()
    if "unsubscribe" not in text:
        return "the opt-out line (reply \"unsubscribe\") is missing"
    address = " ".join((settings.company_postal_address or "").split()).lower()
    if address and address not in text:
        return "the postal address is missing"
    return None


def compliance_footer(settings: Settings, outreach: bool = False, why: str | None = None) -> str:
    """CAN-SPAM footer: why the person is receiving this, how to opt out, and our postal address.
    Outreach (B2B first touch) says it was sent to a business contact the company published, unless `why` gives
    the true reason (for an address that was given to us rather than published)."""
    unsub = settings.unsubscribe_email or settings.smtp_from or "this address"
    address = settings.company_postal_address or POSTAL_PLACEHOLDER
    why = why or ("We emailed you because your company publishes this address as a business contact."
                  if outreach else f"You are receiving this because you contacted {settings.company_name}.")
    # A postal address often starts with the company's name: the name is then written once.
    sender = address if address.lower().startswith(settings.company_name.lower()) else f"{settings.company_name} · {address}"
    return (
        "\n\n---\n"
        f"{why} "
        f"To stop receiving emails from us, reply with \"unsubscribe\" or write to {unsub}.\n"
        f"{sender}"
    )


def new_message_id(settings: Settings) -> str:
    return make_msgid(domain=(settings.smtp_from or "localhost").split("@")[-1])


class EmailSender:
    def __init__(self, settings: Settings, smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
                 token: Callable[[], str] | None = None):
        self.settings = settings
        self.smtp_factory = smtp_factory
        self.token = token  # MAIL_AUTH=oauth: a fresh access token for the connected account (aime/integrations/oauth)

    def status(self) -> tuple[bool, str]:
        s = self.settings
        if not s.outbound_email_enabled:
            return False, "DISABLED: sending is switched off"
        if not s.smtp_host or not s.smtp_from:
            return False, "NOT CONFIGURED: SMTP_HOST / SMTP_FROM missing"
        if not s.company_postal_address:
            return False, "NOT CONFIGURED: COMPANY_POSTAL_ADDRESS required (CAN-SPAM)"
        if s.mail_auth == "oauth":
            client = s.google_client_id if s.mail_oauth_provider == "google" else s.microsoft_client_id
            if not client:
                return False, "NOT CONFIGURED: MAIL_AUTH=oauth needs the OAuth client in .env and the account connected"
        elif s.smtp_username and not s.smtp_password:
            return False, "NOT CONFIGURED: SMTP_PASSWORD missing (the mailbox's app password); every send would fail at sign-in"
        if s.smtp_username and not s.smtp_use_tls:
            return False, "NOT SAFE: SMTP_USE_TLS=false would send the mailbox password unencrypted; set it to true"
        if s.test_recipients:
            return True, "Ready in TEST MODE: every email is delivered only to " + ", ".join(sorted(s.test_recipients))
        return True, "Ready"

    def recipient_for(self, to: str) -> tuple[str, bool]:
        """Test mode delivers anything addressed outside OUTBOUND_TEST_RECIPIENTS to the first test address."""
        tests = self.settings.test_recipients
        if not tests or to.strip().lower() in tests:
            return to, False
        return sorted(tests)[0], True

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None,
             message_id: str | None = None, references: str | None = None) -> str:
        """Returns the Message-ID. Pass `message_id` (stored before sending) so it is known even if the send fails
        midway, and replies to a delivered-but-unconfirmed email still match by In-Reply-To."""
        ready, reason = self.status()
        if not ready:
            raise EmailNotReady(reason)
        s = self.settings
        if any(ch in to for ch in "\r\n,;") or "@" not in to:
            raise EmailNotReady(f"invalid recipient address {to!r}")
        subject = " ".join(subject.split())  # a header is one line: no folding, no injected headers
        actual, redirected = self.recipient_for(to)
        if redirected:  # controlled test: nobody outside the test list is ever emailed
            subject = marked_subject(subject, to)
            body = (f"TEST MODE: this email would have gone to {to}. Only addresses in OUTBOUND_TEST_RECIPIENTS "
                    f"receive email.\n\n{body}")
        msg = EmailMessage()
        msg["From"] = formataddr((s.sender_name or s.company_name, s.smtp_from))
        msg["To"] = actual
        msg["Subject"] = subject
        msg_id = message_id or new_message_id(s)
        msg["Message-ID"] = msg_id
        unsub = s.unsubscribe_email or s.smtp_from
        msg["List-Unsubscribe"] = f"<mailto:{unsub}?subject=unsubscribe>"
        if in_reply_to:  # keep the reply in the thread: References carries the whole chain, ending with its parent
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(body)
        bearer = ""
        if s.mail_auth == "oauth":  # before connecting: without a token nothing is sent, and it is safe to retry
            from aime.integrations.oauth import OAuthError

            try:
                bearer = self.token() if self.token else ""
            except OAuthError as exc:
                raise EmailNotReady(f"OAuth sign-in unavailable: {exc}") from exc
            if not bearer:
                raise EmailNotReady("MAIL_AUTH=oauth needs the account connected on the Integrations page")
        with self.smtp_factory(s.smtp_host, s.smtp_port, timeout=MAIL_TIMEOUT) as smtp:
            if s.smtp_use_tls:
                smtp.starttls(context=tls_context())
            if s.mail_auth == "oauth":
                from aime.integrations.oauth import xoauth2

                smtp.ehlo()
                smtp.auth("XOAUTH2", lambda challenge=None: xoauth2(s.smtp_username, bearer))
            elif s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
        return msg_id
