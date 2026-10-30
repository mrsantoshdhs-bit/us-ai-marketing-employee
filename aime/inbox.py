"""The monitored sales mailbox: read-only IMAP fetching, parsing, and quoted-text removal.

The mailbox is opened with EXAMINE (read-only) and messages are fetched with BODY.PEEK, so nothing is
marked read, moved or deleted. Progress is tracked by UID (with UIDVALIDITY); the first run only looks
back `first_run_days`. Delivery-status reports (bounces) are recognised and their failed recipients and
original Message-ID extracted.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.errors import HeaderParseError
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
# A quote looks different in every mail program and language: "On ... wrote:", "El ... escribió:", "Am ... schrieb ...:",
# or a copy of the earlier message's header lines ("De: ...", "Enviado: ...", "Von: ...").
_WROTE = r"(?:wrote|escribi[oó]|escreveu|a [eé]crit|schrieb|ha scritto|schreef|skrev)"
_QUOTED_BY = (rf"(?:on|el|em|le|am|il|op|den)\s.{{4,200}}\b{_WROTE}\b.{{0,100}}\s?:|"
              rf".{{0,200}}[@\d].{{0,200}}\b{_WROTE}\b.{{0,100}}\s?:")
_COPY_LINE = (r"-{2,} ?(?:original message|forwarded message|mensaje original|mensaje reenviado|mensagem original|"
              r"mensagem encaminhada|message d'origine|message transf[eé]r[eé]|urspr[uü]ngliche nachricht|"
              r"weitergeleitete nachricht|messaggio originale|messaggio inoltrato|oorspronkelijk bericht|"
              r"doorgestuurd bericht) ?-{2,}")
_HEADER_COPY = (rf"{_COPY_LINE}|from: .+|(?:de|von|da|van)\s?:\s.{{0,300}}@.{{0,300}}|"
                r"(?:enviado|gesendet|envoy[eé]|inviato|verzonden)\s?:\s.+|_{5,}")
QUOTE_START = re.compile(rf"^(?:{_QUOTED_BY}|{_HEADER_COPY}|sent from my \w+|>.*)$", re.I)
HEADER_COPY = re.compile(rf"^(?:{_HEADER_COPY})$", re.I)  # from here on it is all the earlier message


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
    auth_results: str | None = None  # the topmost Authentication-Results header: added by the receiving server


def _header(msg: Message, name: str, default: str = "") -> str:
    """A header as plain text: one with undecodable bytes arrives as a Header object, not a str."""
    return str(msg.get(name, default) or default)


def _decoded(value: str) -> str:
    """An RFC 2047 header as text. Anyone can send an unknown charset or broken base64: what is there is kept as it is."""
    try:
        return str(make_header(decode_header(value)))
    except (HeaderParseError, LookupError, ValueError):
        return value


def _part_text(part: Message) -> str:
    """A text part as str. An unknown charset is read as UTF-8, so the sender's words are still seen."""
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    try:
        return raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return raw.decode("utf-8", errors="replace")


BLOCKQUOTE = re.compile(r"(?is)<blockquote\b[^>]*>((?:(?!<blockquote\b).)*?)</blockquote>")


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html))


def _html_text(html: str) -> str:
    """The text of an HTML part. A <blockquote> becomes ">" lines, so a quote is recognised in any language."""
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    if "<blockquote" in html.lower():
        for _ in range(20):  # innermost first
            html, found = BLOCKQUOTE.subn(lambda m: "\n" + "\n".join(
                "> " + line.strip() for line in _strip_tags(m.group(1)).split("\n")) + "\n", html)
            if not found:
                break
    return unescape(_strip_tags(html))


def _text_of(msg: Message) -> str:
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or _header(part, "Content-Disposition").startswith("attachment"):
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        (plain if ctype == "text/plain" else html).append(_part_text(part))
    if plain:
        return "\n".join(plain)
    return _html_text("\n".join(html))


# Where a plain-text bounce starts copying the original message (our own footer and addresses are in it).
COPY_START = re.compile(r"(?im)^.*\b(copy of (the|your) (original )?message|original message|returned message|"
                        r"message headers follow|undelivered message)\b.*$")
SOFT_TEXT = re.compile(r"\b(delay(ed)?|will (retry|keep trying|try again)|temporar(y|ily)|deferred|4\.\d{1,3}\.\d{1,3})\b", re.I)
HARD_TEXT = re.compile(r"\b(permanent(ly)?|given up|5\.\d{1,3}\.\d{1,3}|no such|user unknown|does not exist|"
                       r"address not found|no mailbox)\b", re.I)
TEXT_RECIPIENT = re.compile(r"(?:Final-Recipient: rfc822;\s*|\bto\s+<?|\baddress:?\s+<?|^<)([\w.+-]+@[\w-]+(?:\.[\w-]+)+)",
                            re.I | re.M)


def _report_text(part: Message) -> str:
    """The delivery report's own text: attachments and attached copies of the original message left out."""
    if part.get_content_maintype() == "message":
        return ""
    if part.is_multipart():
        return "\n".join(_report_text(p) for p in part.get_payload())
    if part.get_content_type() != "text/plain" or _header(part, "Content-Disposition").startswith("attachment"):
        return ""
    return _part_text(part)


def _dsn_info(msg: Message, own: frozenset[str] = frozenset()) -> tuple[list[str], str | None]:
    """Permanently failed recipients and the original Message-ID. A delay or other temporary notice (Action
    delayed, Status 4.x.x) lists no recipient: the server is still trying, so nothing may be suppressed."""
    recipients, original, structured = [], None, False
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            structured = True
            for block in part.get_payload() if part.is_multipart() else [part]:
                if not hasattr(block, "get_all"):
                    continue
                action = _header(block, "Action").strip().lower()
                status = _header(block, "Status").strip()
                if action != "failed" and not status.startswith("5"):
                    continue  # delayed, delivered, relayed, or the per-message block
                for key in ("Final-Recipient", "Original-Recipient"):
                    for value in block.get_all(key, []):
                        addr = str(value).split(";", 1)[-1].strip().strip("<>").lower()
                        if "@" in addr and addr not in recipients and addr not in own:
                            recipients.append(addr)
        elif ctype in ("message/rfc822", "text/rfc822-headers") and original is None:
            inner = part.get_payload(0) if part.is_multipart() else email.message_from_string(part.get_payload())
            original = _header(inner, "Message-ID").strip() or None
    if not structured:  # a plain-text bounce: read only the report, before any copy of our message
        text = _report_text(msg)
        cut = COPY_START.search(text)
        report = text[:cut.start()] if cut else text
        if HARD_TEXT.search(report) or not SOFT_TEXT.search(report):
            recipients = [a for a in dict.fromkeys(m.lower() for m in TEXT_RECIPIENT.findall(report)) if a not in own]
    return recipients, original


def parse_message(raw: bytes, own: frozenset[str] | set[str] = frozenset()) -> ParsedEmail:
    """`own`: our own addresses (sender, mailbox, unsubscribe address), never taken as a bounced recipient."""
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_header(msg, "From"))
    to = [a for _, a in getaddresses(msg.get_all("To", []))]
    try:
        received = parsedate_to_datetime(_header(msg, "Date")).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        received = utcnow()
    subject = " ".join(_decoded(_header(msg, "Subject")).split())  # unfold long headers
    auto = (_header(msg, "Auto-Submitted", "no").lower() not in ("no", "") or msg.get("X-Autoreply") is not None
            or _header(msg, "Precedence").lower() in ("auto_reply", "bulk", "junk") or bool(AUTO_SUBJECT.search(subject)))
    is_bounce = bool(BOUNCE_SENDERS.search(addr or "")) or msg.get_content_type() == "multipart/report" \
        or bool(BOUNCE_SUBJECT.search(subject) and (auto or BOUNCE_SENDERS.search(addr or "")))
    try:
        bounced, original = _dsn_info(msg, frozenset(a.lower() for a in own)) if is_bounce else ([], None)
    except (AttributeError, LookupError, TypeError, ValueError):  # a malformed report: a person reads it
        bounced, original = [], None
    body = _text_of(msg)[:20000]
    in_reply_to = _header(msg, "In-Reply-To").strip() or None
    from_email = (addr or "").lower()
    # Without a Message-ID a second delivery of the same email would be a new email: the same content gets the same id.
    message_id = _header(msg, "Message-ID").strip() or _stand_in_id(from_email, _header(msg, "Date"), subject, in_reply_to, body)
    return ParsedEmail(
        message_id=message_id, in_reply_to=in_reply_to,
        references=" ".join(_header(msg, "References").split()), from_email=from_email,
        from_name=" ".join(_decoded(name or "").split()) or None, to_email=(to[0].lower() if to else None),
        subject=subject[:300], received_at=received,
        body_text=body, auto_submitted=auto, is_bounce=is_bounce, bounced_addresses=bounced,
        bounced_message_id=original, auth_results=(_header(msg, "Authentication-Results").strip()[:2000] or None))


def _stand_in_id(*fields: str | None) -> str:
    digest = hashlib.sha256("\0".join(f or "" for f in fields).encode("utf-8", "replace")).hexdigest()[:32]
    return f"<no-id-{digest}@aime.invalid>"


NEW_TEXT_LIMIT = 5000
QUOTE_LINE = re.compile(rf"^(?:>.*|{_QUOTED_BY}|{_COPY_LINE}|sent from my \w+.*|_{{5,}}|-{{3}})$", re.I)
OUR_FOOTER = re.compile(r"(to stop receiving emails from us|receiving emails from us|reply with \"?unsubscribe\"?|"
                        r"\"?unsubscribe\"? or write to|we emailed you because|you are receiving this because)", re.I)


def _split_reply(body: str) -> tuple[str, list[str]]:
    """(what they wrote above the quote, every line from the quote or their signature on). Our own compliance footer
    ends the reply too: quoted in a form nothing here recognises, it must still never be read as their words."""
    lines = body.replace("\r\n", "\n").split("\n")
    above: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        footer = OUR_FOOTER.search(line)
        if footer:
            above.append(line[:footer.start()])
            return "\n".join(above).strip(), lines[i + 1:]
        if QUOTE_START.match(stripped) or stripped == "--":
            if stripped.lower().startswith("sent from my") and not any(x.strip() for x in above):
                continue  # a device signature above the reply, not the end of it
            return "\n".join(above).strip(), lines[i:]
        above.append(line)
    return "\n".join(above).strip(), []


def _own_lines(lines: list[str]) -> str:
    """The sender's words among these lines: quoted lines, quote headers, signatures and our own footer are dropped, and
    a copied header block ("From:", "De:", ...) ends them, because all below it is the earlier message."""
    keep, signature = [], False
    for line in lines:
        stripped = line.strip()
        if HEADER_COPY.match(stripped):
            break
        if stripped == "--":
            signature = True
        elif QUOTE_LINE.match(stripped):
            signature = False
        elif not signature and not OUR_FOOTER.search(line):
            keep.append(line)
    return "\n".join(keep).strip()


def new_text(body: str) -> str:
    """The reply itself: everything before the quoted earlier message or the signature delimiter."""
    return _split_reply(body)[0][:NEW_TEXT_LIMIT]


def own_text(body: str) -> str:
    """The sender's own words when nothing was written above the quote (a reply below or inside the quote, or
    after "Sent from my iPhone"), so our "reply unsubscribe" line is never read as theirs."""
    return _own_lines(body.replace("\r\n", "\n").split("\n"))[:NEW_TEXT_LIMIT]


def rest_of_reply(body: str) -> str:
    """The sender's own words that new_text leaves out: past its limit, or written below the quote. An opt-out there
    is still an opt-out."""
    above, below = _split_reply(body)
    past_limit = above[NEW_TEXT_LIMIT - 100:] if len(above) > NEW_TEXT_LIMIT else ""  # a phrase may sit across the cut
    return "\n".join(x for x in (past_limit, _own_lines(below)) if x)


FETCH_TRIES = 3  # a message that cannot be fetched is tried again on this many runs, then skipped


class Mailbox:
    """Read-only IMAP access. `fetch_new(state)` returns ([(uid_key, raw bytes)], new_state). With MAIL_AUTH=oauth
    it signs in with XOAUTH2 using `token()`, an access token from aime/integrations/oauth.py."""

    def __init__(self, settings, imap_factory=imaplib.IMAP4_SSL, first_run_days: int = 14, batch: int = 50):
        self.settings = settings
        self.imap_factory = imap_factory
        self.first_run_days = first_run_days
        self.batch = batch

    def fetch_new(self, state: dict | None, token=None) -> tuple[list[tuple[str, bytes]], dict]:
        s = self.settings
        user, password = s.imap_credentials
        conn = self.imap_factory(s.imap_host, s.imap_port, ssl_context=tls_context(), timeout=MAIL_TIMEOUT)
        try:
            if s.mail_auth == "oauth":
                from aime.integrations.oauth import xoauth2

                if token is None:
                    raise RuntimeError("MAIL_AUTH=oauth needs the account connected on the Integrations page")
                bearer = token()
                conn.authenticate("XOAUTH2", lambda _challenge: xoauth2(user, bearer).encode())
            else:
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
            failed = dict(state.get("failed") or {})
            for uid in uids:
                typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None)
                if typ == "OK" and raw:
                    out.append((f"{validity}:{uid}", raw))
                    failed.pop(str(uid), None)
                else:
                    tries = failed.get(str(uid), 0) + 1
                    if tries < FETCH_TRIES:  # stop here: this message (and the ones after it) are fetched next run
                        failed[str(uid)] = tries
                        break
                    failed.pop(str(uid), None)
                    state["skipped_uids"] = (list(state.get("skipped_uids") or []) + [uid])[-20:]
                state["last_uid"] = uid
            state["failed"] = failed
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
