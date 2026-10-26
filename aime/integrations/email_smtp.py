"""SMTP sender for approved replies to inbound inquiries. Disabled unless explicitly enabled and configured."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from aime.config import Settings


POSTAL_PLACEHOLDER = "[postal address required before sending]"


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


def compliance_footer(settings: Settings, outreach: bool = False) -> str:
    """CAN-SPAM footer: why the person is receiving this, how to opt out, and our postal address.
    Outreach (B2B first touch) says it was sent to a business contact the company published."""
    unsub = settings.unsubscribe_email or settings.smtp_from or "this address"
    address = settings.company_postal_address or POSTAL_PLACEHOLDER
    why = ("We emailed you because your company publishes this address as a business contact."
           if outreach else f"You are receiving this because you contacted {settings.company_name}.")
    return (
        "\n\n---\n"
        f"{why} "
        f"To stop receiving emails from us, reply with \"unsubscribe\" or write to {unsub}.\n"
        f"{settings.company_name} · {address}"
    )


class EmailSender:
    def __init__(self, settings: Settings, smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP):
        self.settings = settings
        self.smtp_factory = smtp_factory

    def status(self) -> tuple[bool, str]:
        s = self.settings
        if not s.outbound_email_enabled:
            return False, "DISABLED: OUTBOUND_EMAIL_ENABLED=false"
        if not s.smtp_host or not s.smtp_from:
            return False, "NOT CONFIGURED: SMTP_HOST / SMTP_FROM missing"
        if not s.company_postal_address:
            return False, "NOT CONFIGURED: COMPANY_POSTAL_ADDRESS required (CAN-SPAM)"
        if s.test_recipients:
            return True, "Ready in TEST MODE: every email is delivered only to " + ", ".join(sorted(s.test_recipients))
        return True, "Ready"

    def recipient_for(self, to: str) -> tuple[str, bool]:
        """Test mode delivers anything addressed outside OUTBOUND_TEST_RECIPIENTS to the first test address."""
        tests = self.settings.test_recipients
        if not tests or to.strip().lower() in tests:
            return to, False
        return sorted(tests)[0], True

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
        ready, reason = self.status()
        if not ready:
            raise EmailNotReady(reason)
        s = self.settings
        if any(ch in to for ch in "\r\n,;") or "@" not in to:
            raise EmailNotReady(f"invalid recipient address {to!r}")
        subject = " ".join(subject.split())  # a header is one line: no folding, no injected headers
        actual, redirected = self.recipient_for(to)
        if redirected:  # controlled test: nobody outside the test list is ever emailed
            subject = f"[TEST for {to}] {subject}"
            body = (f"TEST MODE: this email would have gone to {to}. Only addresses in OUTBOUND_TEST_RECIPIENTS "
                    f"receive email.\n\n{body}")
        msg = EmailMessage()
        msg["From"] = formataddr((s.sender_name or s.company_name, s.smtp_from))
        msg["To"] = actual
        msg["Subject"] = subject
        msg_id = make_msgid(domain=s.smtp_from.split("@")[-1])
        msg["Message-ID"] = msg_id
        unsub = s.unsubscribe_email or s.smtp_from
        msg["List-Unsubscribe"] = f"<mailto:{unsub}?subject=unsubscribe>"
        if in_reply_to:  # keep the reply in the prospect's thread
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        with self.smtp_factory(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if s.smtp_use_tls:
                smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
        return msg_id
