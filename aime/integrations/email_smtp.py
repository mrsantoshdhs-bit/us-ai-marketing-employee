"""SMTP sender for approved replies to inbound inquiries. Disabled unless explicitly enabled and configured."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from aime.config import Settings


class EmailNotReady(Exception):
    pass


def compliance_footer(settings: Settings) -> str:
    unsub = settings.unsubscribe_email or settings.smtp_from or "this address"
    address = settings.company_postal_address or "[postal address required before sending]"
    return (
        "\n\n---\n"
        f"You are receiving this because you contacted {settings.company_name}. "
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
        return True, "Ready"

    def send(self, to: str, subject: str, body: str) -> str:
        ready, reason = self.status()
        if not ready:
            raise EmailNotReady(reason)
        s = self.settings
        msg = EmailMessage()
        msg["From"] = formataddr((s.sender_name or s.company_name, s.smtp_from))
        msg["To"] = to
        msg["Subject"] = subject
        msg_id = make_msgid(domain=s.smtp_from.split("@")[-1])
        msg["Message-ID"] = msg_id
        unsub = s.unsubscribe_email or s.smtp_from
        msg["List-Unsubscribe"] = f"<mailto:{unsub}?subject=unsubscribe>"
        msg.set_content(body)
        with self.smtp_factory(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            if s.smtp_use_tls:
                smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
        return msg_id
