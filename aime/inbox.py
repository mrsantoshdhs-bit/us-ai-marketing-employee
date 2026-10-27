"""The monitored sales mailbox: read-only IMAP fetching, parsing, and quoted-text removal.

The mailbox is opened with EXAMINE (read-only) and messages are fetched with BODY.PEEK, so nothing is
marked read, moved or deleted. Progress is tracked by UID (with UIDVALIDITY); the first run only looks
back `first_run_days`. Delivery-status reports (bounces) are recognised and their failed recipients and
original Message-ID extracted.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html import unescape

from aime.integrations.email_smtp import MAIL_TIMEOUT, tls_context
from aime.timeutil import utcnow

BOUNCE_SENDERS = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|mailerdaemon)@", re.I)
BOUNCE_SUBJECT = re.compile(r"(delivery status notification \(failure\)|undeliver(able|ed)|mail delivery (failed|subsystem)|"
                            r"returned mail|delivery failure|failure notice|could not be delivered)", re.I)
AUTO_SUBJECT = re.compile(r"^(auto(matic)?[- ]?reply|out of (the )?office|autoreply|away from|on vacation|ooo\b)", re.I)
QUOTE_START = re.compile(r"^(on .{4,200} wrote:|-{2,} ?original message ?-{2,}|from: .+|sent from my \w+|"
                         r"_{5,}|>.*)$", re.I)


@dataclass
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    references: str
    from_email: str
    from_name: str | None
    to_email: str | None
    subject: str
    received_at: datetime
    body_text: str
    auto_submitted: bool = False
    is_bounce: bool = False
    bounced_addresses: list[str] = field(default_factory=list)
    bounced_message_id: str | None = None


def _text_of(msg: Message) -> str:
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        except (AttributeError, LookupError):
            continue
        (plain if ctype == "text/plain" else html).append(text)
    if plain:
        return "\n".join(plain)
    joined = "\n".join(html)
    joined = re.sub(r"(?is)<(script|style).*?</\1>", " ", joined)
    joined = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", joined)
    return unescape(re.sub(r"<[^>]+>", " ", joined))


def _dsn_info(msg: Message) -> tuple[list[str], str | None]:
    recipients, original = [], None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            for block in part.get_payload() if part.is_multipart() else [part]:
                for key in ("Final-Recipient", "Original-Recipient"):
                    for value in block.get_all(key, []) if hasattr(block, "get_all") else []:
                        addr = value.split(";", 1)[-1].strip().lower()
                        if "@" in addr and addr not in recipients:
                            recipients.append(addr)
        elif ctype in ("message/rfc822", "text/rfc822-headers") and original is None:
            inner = part.get_payload(0) if part.is_multipart() else email.message_from_string(part.get_payload())
            original = (inner.get("Message-ID") or "").strip() or None
    if not recipients:
        text = _text_of(msg)
        recipients = list(dict.fromkeys(m.lower() for m in re.findall(
            r"(?:Final-Recipient: rfc822;|to\s+|address:?\s+)<?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)>?", text, re.I)))
    return recipients, original


def parse_message(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(msg.get("From", ""))
    to = [a for _, a in getaddresses(msg.get_all("To", []))]
    try:
        received = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        received = utcnow()
    subject = " ".join(str(make_header(decode_header(msg.get("Subject", "")))).split())  # unfold long headers
    auto = (msg.get("Auto-Submitted", "no").lower() not in ("no", "") or msg.get("X-Autoreply") is not None
            or msg.get("Precedence", "").lower() in ("auto_reply", "bulk", "junk") or bool(AUTO_SUBJECT.search(subject)))
    is_bounce = bool(BOUNCE_SENDERS.search(addr or "")) or msg.get_content_type() == "multipart/report" \
        or bool(BOUNCE_SUBJECT.search(subject) and (auto or BOUNCE_SENDERS.search(addr or "")))
    bounced, original = _dsn_info(msg) if is_bounce else ([], None)
    return ParsedEmail(
        message_id=(msg.get("Message-ID") or "").strip() or None, in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        references=" ".join((msg.get("References") or "").split()), from_email=(addr or "").lower(),
        from_name=name or None, to_email=(to[0].lower() if to else None), subject=subject[:300], received_at=received,
        body_text=_text_of(msg)[:20000], auto_submitted=auto, is_bounce=is_bounce, bounced_addresses=bounced,
        bounced_message_id=original)


def new_text(body: str) -> str:
    """The reply itself: everything before the quoted earlier message or the signature delimiter."""
    lines = []
    for line in body.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if QUOTE_START.match(stripped) or stripped == "--":
            if stripped.lower().startswith("sent from my") and not any(x.strip() for x in lines):
                continue  # a device signature above the reply, not the end of it
            break
        lines.append(line)
    return "\n".join(lines).strip()[:5000]


QUOTE_LINE = re.compile(r"^(>.*|on .{4,200} wrote:|-{2,} ?original message ?-{2,}|sent from my \w+.*|_{5,}|-{3})$",
                        re.I)
OUR_FOOTER = re.compile(r"(to stop receiving emails from us|reply with \"?unsubscribe\"?|we emailed you because|"
                        r"you are receiving this because)", re.I)


def own_text(body: str) -> str:
    """The sender's own words when nothing was written above the quote (a reply below or inside the quote, or
    after "Sent from my iPhone"): quoted lines, quote headers and our own compliance footer are dropped, so
    our "reply unsubscribe" line is never read as theirs."""
    keep = [line for line in body.replace("\r\n", "\n").split("\n")
            if not QUOTE_LINE.match(line.strip()) and not OUR_FOOTER.search(line)]
    return "\n".join(keep).strip()[:5000]


class Mailbox:
    """Read-only IMAP access. `fetch_new(state)` returns ([(uid_key, raw bytes)], new_state)."""

    def __init__(self, settings, imap_factory=imaplib.IMAP4_SSL, first_run_days: int = 14, batch: int = 50):
        self.settings = settings
        self.imap_factory = imap_factory
        self.first_run_days = first_run_days
        self.batch = batch

    def fetch_new(self, state: dict | None) -> tuple[list[tuple[str, bytes]], dict]:
        s = self.settings
        user, password = s.imap_credentials
        conn = self.imap_factory(s.imap_host, s.imap_port, ssl_context=tls_context(), timeout=MAIL_TIMEOUT)
        try:
            conn.login(user, password)
            typ, data = conn.select(f'"{s.imap_folder}"', readonly=True)  # EXAMINE: never changes the mailbox
            if typ != "OK":
                raise RuntimeError(f"cannot open mailbox folder {s.imap_folder}")
            validity = self._uidvalidity(conn)
            state = dict(state or {})
            if state.get("uidvalidity") != validity:
                state = {"uidvalidity": validity, "last_uid": 0}
                since = (utcnow() - timedelta(days=self.first_run_days)).strftime("%d-%b-%Y")
                typ, data = conn.uid("SEARCH", None, "SINCE", since)
            else:
                typ, data = conn.uid("SEARCH", None, f"UID {int(state['last_uid']) + 1}:*")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > int(state.get("last_uid", 0)))[: self.batch]
            out = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None)
                if typ == "OK" and raw:
                    out.append((f"{validity}:{uid}", raw))
                state["last_uid"] = uid
            return out, state
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: S110 -- closing a broken connection: nothing else to do
                pass

    @staticmethod
    def _uidvalidity(conn) -> str:
        typ, data = conn.response("UIDVALIDITY")
        return (data[0].decode() if data and data[0] else "0") if typ else "0"
