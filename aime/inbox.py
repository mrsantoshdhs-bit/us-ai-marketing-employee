"""The monitored sales mailbox: read-only IMAP fetching, parsing, and quoted-text removal.

The mailbox is opened with EXAMINE (read-only) and messages are fetched with BODY.PEEK, so nothing is
marked read, moved or deleted. Progress is tracked by UID (with UIDVALIDITY); the first run only looks
back `first_run_days`. Delivery-status reports (bounces) are recognised and their failed recipients and
original Message-ID extracted, and told apart from delays, read and delivery receipts and refusals about our own sending.

Anyone can send anything: every header and part is cut to a fixed size before any pattern or parser reads it, and HTML
is read in one pass, so the time taken grows with the size of the email and no faster.
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

HEADER_LIMIT = 4000  # no real Subject or From is longer; decoding and address parsing slow down badly on longer ones
THREAD_LIMIT = 20000  # In-Reply-To and References
PART_LIMIT = 2_000_000  # bytes of one text part
HTML_LIMIT = 1_000_000  # characters of HTML read
HTML_TEXT_LIMIT = 200_000  # characters of text taken from it: far more than is stored
REPORT_LIMIT = 100_000  # characters of a plain-text delivery report
BODY_LIMIT = 20000  # characters stored
NO_ADDRESS = "unknown@unreadable.invalid"  # the sender, when the From line holds no usable address

THREAD_ID = re.compile(r"<[^<>\s]{1,290}>")
BOUNCE_SENDERS = re.compile(r"^(mailer-daemon|postmaster|mail-daemon|mailerdaemon|microsoftexchange[0-9a-f]*)(@|$)", re.I)
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
ATTACHED_MARK = "-----Attached message-----"  # stands above the words of an email attached to this one (not the writer's)
_HEADER_COPY = (rf"{_COPY_LINE}|from: .+|(?:de|von|da|van)\s?:\s.{{0,300}}@.{{0,300}}|"
                r"(?:enviado|gesendet|envoy[eé]|inviato|verzonden)\s?:\s.+|_{5,}")
QUOTE_START = re.compile(rf"^(?:{_QUOTED_BY}|{_HEADER_COPY}|{ATTACHED_MARK}|sent from my \w+|>.*)$", re.I)
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
    kind: str | None = None  # bounce | delay | receipt | sending_problem (see _delivery_notice) | calendar
    note: str | None = None  # what a person should know: no readable text, or why a delivery failed


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
    raw = raw[:PART_LIMIT]
    try:
        return raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return raw.decode("utf-8", errors="replace")


def _html_text(html: str) -> str:
    """The text of an HTML part, in one pass: a stranger's "<<<<" or "<script<script" costs no more than plain text. Tags
    become spaces or line breaks, <script> and <style> go, and a <blockquote> becomes ">" lines, so a quote is recognised
    in any language. What only looks like a tag ("a < b", or a "<" with no ">" anywhere after it) stays as text."""
    html = html[:HTML_LIMIT]
    out: list[str] = []
    quote: list[str] = []
    depth, size, i, n = 0, 0, 0, len(html)
    closes = {"script": True, "style": True}  # False once a search for the closing tag has failed: it is not searched for again
    while i < n and size < HTML_TEXT_LIMIT:
        lt = html.find("<", i)
        chunk = html[i:] if lt < 0 else html[i:lt]
        (quote if depth else out).append(chunk)
        size += len(chunk) + 1
        if lt < 0:
            break
        i = lt + 1
        following = html[i:i + 1]
        if not (following.isascii() and (following.isalpha() or following in ("/", "!", "?"))):
            (quote if depth else out).append("<")
            continue
        if html.startswith("<!--", lt) and (end := html.find("-->", lt + 4)) >= 0:
            i = end + 3
            continue
        gt = html.find(">", i)
        if gt < 0:  # no ">" anywhere ahead: all that is left is text
            (quote if depth else out).append(html[lt:])
            break
        words = html[i:min(gt, i + 40)].split()
        name = words[0].rstrip("/").lower() if words else ""
        i = gt + 1
        if name in closes:
            if closes[name] and (end_tag := re.compile(f"</{name}", re.I).search(html, i)):
                gt = html.find(">", end_tag.end())
                i = n if gt < 0 else gt + 1
            else:
                closes[name] = False
            (quote if depth else out).append(" ")
        elif name == "blockquote":
            if depth == 0:
                quote = []
            depth += 1
        elif name == "/blockquote" and depth:
            depth -= 1
            if depth == 0:
                out.append("\n" + "\n".join("> " + line.strip() for line in "".join(quote).split("\n")) + "\n")
        else:
            (quote if depth else out).append("\n" if name in ("br", "/p", "/div") else " ")
    if depth:  # a <blockquote> never closed marks nothing: what follows it may be the writer's own words
        out.extend(quote)
    return unescape("".join(out))


def _file_label(part: Message) -> str:
    """An attachment as a person knows it: "winmail.dat (application/ms-tnef)"."""
    found = re.search(r"""\bfilename\*?=\s*"?([^";\r\n]{1,60})""", _header(part, "Content-Disposition")[:HEADER_LIMIT], re.I) or \
        re.search(r"""\bname\*?=\s*"?([^";\r\n]{1,60})""", _header(part, "Content-Type")[:HEADER_LIMIT], re.I)
    return f"{found.group(1).strip()} ({part.get_content_type()})" if found else part.get_content_type()


def _is_attachment(part: Message) -> bool:
    return _header(part, "Content-Disposition")[:HEADER_LIMIT].lstrip().lower().startswith("attachment")


def _collect(part: Message, plain: list, html: list, attached: list, files: list) -> None:
    """Sort a part's content. An email inside this one (a forward as attachment, a gateway's wrapper) is not the writer's
    words; a delivery report's machine parts are nobody's words."""
    maintype = part.get_content_maintype()
    if maintype == "message":
        if part.get_content_type() == "message/rfc822" and part.is_multipart():
            attached.extend(_text_of(inner) for inner in part.get_payload())
        return
    if maintype == "multipart":
        for inner in part.get_payload() if part.is_multipart() else []:
            _collect(inner, plain, html, attached, files)
        return
    ctype = part.get_content_type()
    if ctype in ("text/plain", "text/html") and not _is_attachment(part):
        (plain if ctype == "text/plain" else html).append(_part_text(part))
    elif ctype != "text/rfc822-headers":
        files.append(part)


def _text_of(msg: Message, files: list | None = None) -> str:
    plain, html, attached = [], [], []
    _collect(msg, plain, html, attached, [] if files is None else files)
    own = "\n".join(plain) if any(p.strip() for p in plain) else _html_text("\n".join(html))  # an empty text part hides nothing
    # A report's attached copy is our own email. Any other is read apart: a person looks, it is never taken as the writer's.
    below = [a for a in attached if a.strip()] if msg.get_content_type() != "multipart/report" else []
    return "\n".join([own, *(f"\n{ATTACHED_MARK}\n{a}" for a in below)]) if below else own


def _no_text_note(body: str, files: list) -> str | None:
    """Why an entry on Replies is blank: it names what the email does hold."""
    if body.strip() or not files:
        return None
    return f"No readable text: the email holds only {', '.join(_file_label(f) for f in files[:3])}. Open it in the mailbox."


# Where a plain-text bounce starts copying the original message (our own footer and addresses are in it).
COPY_START = re.compile(r"(?im)^.*\b(copy of (the|your) (original )?message|original message|returned message|"
                        r"message headers follow|undelivered message)\b.*$")
SOFT_TEXT = re.compile(r"\b(delay(ed)?|will (retry|keep trying|try again)|temporar(y|ily)|deferred|4\.\d{1,3}\.\d{1,3})\b", re.I)
HARD_TEXT = re.compile(r"\b(permanent(ly)?|given up|5\.\d{1,3}\.\d{1,3}|no such|user unknown|does not exist|"
                       r"address not found|no mailbox)\b", re.I)
TEXT_RECIPIENT = re.compile(r"(?:Final-Recipient: rfc822;\s*|\bto\s+<?|\baddress:?\s+<?|\brecipient:?\s+<?|^\s*<?)"
                            r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.I | re.M)
COPY_MESSAGE_ID = re.compile(r"(?im)^message-id:\s*(<[^<>\s]{1,290}>)")
# A failure that says something about US, not about the address: our server's own limit, our address or server refused
# or blocklisted, our email judged as spam. The prospect's address is good: nothing is suppressed, and the owner is told.
ABOUT_US = re.compile(r"\b(5\.7\.\d{1,3}|exceeded the max|(rate|sending|hourly|daily|message|send) limit|too many (messages|e-?mails|connections)|"
                      r"block(ed|list|listed|ing)?|black-?list(ed)?|spamhaus|barracuda|ipcheck|reputation|spam|"
                      r"administrative prohibition|not authori[sz]ed to send|relay(ing)? (access )?(denied|not permitted)|"
                      r"(spf|dkim|dmarc)\W+(\w+\W+)?(fail\w*|reject\w*)|security policy)\b", re.I)
# Beats the above: the address itself is reported as gone.
ADDRESS_GONE = re.compile(r"\b(5\.1\.\d{1,3}|5\.2\.1|user unknown|unknown user|no such (user|address|mailbox|recipient)|does ?n.?t exist|"
                          r"address not found|recipient not found|no mailbox|unrouteable)\b", re.I)


def _report_text(part: Message) -> str:
    """The delivery report's own text: attachments and attached copies of the original message left out."""
    if part.get_content_maintype() == "message":
        return ""
    if part.is_multipart():
        return "\n".join(_report_text(p) for p in part.get_payload())
    if part.get_content_type() != "text/plain" or _is_attachment(part):
        return ""
    return _part_text(part)


def _message_id(value: str) -> str | None:
    """The id in a Message-ID line, without what a folded or crafted line carries after it."""
    found = THREAD_ID.search(value[:HEADER_LIMIT])
    return found.group(0) if found else (value[:HEADER_LIMIT].split() or [None])[0]


def _copy_id(part: Message) -> str | None:
    """The Message-ID in a report's copy of the original email."""
    inner = part.get_payload(0) if part.is_multipart() else email.message_from_string(str(part.get_payload())[:REPORT_LIMIT])
    return _message_id(_header(inner, "Message-ID"))


def _delivery_notice(msg: Message, sender: str, subject: str, auto: bool,
                     own: frozenset[str] = frozenset()) -> tuple[str | None, list[str], str | None, str | None]:
    """(kind, failed recipients, the original Message-ID, the reason given) of a delivery notice; kind None when it is not one.
    bounce: failed for good. delay: still being tried (it lists no recipient: nothing may be suppressed).
    receipt: read or delivered. sending_problem: failed because of our own sending, not because of the address.
    Whether any of it is believed is decided where it can be checked against what we sent (reply_intel)."""
    parts = list(msg.walk())
    types = {p.get_content_type() for p in parts}
    is_report = msg.get_content_type() == "multipart/report" or "message/delivery-status" in types
    if "message/disposition-notification" in types or (
            is_report and "disposition-notification" in _header(msg, "Content-Type")[:HEADER_LIMIT].lower()):
        return "receipt", [], None, None
    system = bool(BOUNCE_SENDERS.search(sender))
    if not is_report and not system and not (BOUNCE_SUBJECT.search(subject) and auto):
        return None, [], None, None
    recipients, original, why, delayed, delivered = [], None, [], False, False
    for part in parts:
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            for block in part.get_payload() if part.is_multipart() else [part]:
                if not hasattr(block, "get_all"):
                    continue
                original = original or _message_id(_header(block, "X-Original-Message-ID") or _header(block, "Original-Message-ID"))
                action = _header(block, "Action").strip().lower()
                status = _header(block, "Status").strip()
                if action != "failed" and not status.startswith("5"):  # delayed, delivered, relayed, or the per-message block
                    delayed = delayed or action == "delayed" or status.startswith("4")
                    delivered = delivered or action in ("delivered", "relayed", "expanded")
                    continue
                why.append(f"{status} {_header(block, 'Diagnostic-Code')[:300]}")
                for key in ("Final-Recipient", "Original-Recipient"):
                    for value in block.get_all(key, []):
                        addr = str(value).split(";", 1)[-1].strip().strip("<>").lower()
                        if "@" in addr and addr not in recipients and addr not in own:
                            recipients.append(addr)
        elif ctype in ("message/rfc822", "text/rfc822-headers") and original is None:
            original = _copy_id(part)
    if "message/delivery-status" not in types:  # a plain-text bounce: read only the report, before any copy of our message
        text = _report_text(msg)[:REPORT_LIMIT]
        cut = COPY_START.search(text)
        report = text[:cut.start()] if cut else text
        if cut and (copied := COPY_MESSAGE_ID.search(text, cut.start())):
            original = original or copied.group(1)
        if HARD_TEXT.search(report) or not SOFT_TEXT.search(report):
            why.append(report[:2000])
            named = [m.lower() for m in TEXT_RECIPIENT.findall(report)]
            listed = [a.lower() for _, a in getaddresses([_header(msg, "X-Failed-Recipients")[:HEADER_LIMIT]]) if "@" in a]
            recipients = [a for a in dict.fromkeys([*listed, *named]) if a not in own]
        else:
            delayed = True
    reason = " ".join(" ".join(why).split())
    if why:
        kind = "sending_problem" if ABOUT_US.search(reason) and not ADDRESS_GONE.search(reason) else "bounce"
        return kind, recipients, original, reason[:300] or None
    return ("delay" if delayed else "receipt" if delivered else "bounce"), [], original, None


_ADDRESS = re.compile(r"[^@\s<>,;:()\"]+@[^@\s<>,;:()\"]+\.[^@\s<>,;:()\".]+")


def is_address(value: str | None) -> bool:
    """A real mailbox address: only such a sender may ever be suppressed. "From: somecompany.com" holds none, and a value
    with no @ on the suppression list would stop a whole domain."""
    return bool(value) and len(value) <= 200 and value != NO_ADDRESS and bool(_ADDRESS.fullmatch(value))


def _thread_ids(value: str) -> list[str]:
    """Every id in an In-Reply-To or References line, also in the older forms: a comment beside it, two ids, or no spaces."""
    value = value[:THREAD_LIMIT]
    return THREAD_ID.findall(value) or value.split()[:1]


def parse_message(raw: bytes, own: frozenset[str] | set[str] = frozenset()) -> ParsedEmail:
    """`own`: our own addresses (sender, mailbox, unsubscribe address), never taken as a bounced recipient."""
    msg = email.message_from_bytes(raw)
    raw_from = _header(msg, "From")[:HEADER_LIMIT]
    name, addr = parseaddr(raw_from)
    to = [a for _, a in getaddresses([str(v)[:HEADER_LIMIT] for v in msg.get_all("To", [])[:10]])]
    try:
        received = parsedate_to_datetime(_header(msg, "Date")[:200]).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        received = utcnow()
    subject = " ".join(_decoded(_header(msg, "Subject")[:HEADER_LIMIT]).split())  # unfold long headers
    auto = (_header(msg, "Auto-Submitted", "no").lower() not in ("no", "") or msg.get("X-Autoreply") is not None
            or _header(msg, "Precedence").lower() in ("auto_reply", "bulk", "junk") or bool(AUTO_SUBJECT.search(subject)))
    sender = (addr or "").lower()
    try:
        kind, bounced, original, reason = _delivery_notice(msg, sender, subject, auto, frozenset(a.lower() for a in own))
    except (AttributeError, LookupError, TypeError, ValueError):  # a malformed report: a person reads it
        kind, bounced, original, reason = "bounce", [], None, None
    is_bounce = kind in ("bounce", "delay", "sending_problem")
    files: list = []
    body = _text_of(msg, files)[:BODY_LIMIT]
    if kind is None and any(f.get_content_type() in ("text/calendar", "application/ics")
                            or ".ics" in _file_label(f).lower() for f in files):
        kind = "calendar"
    answers = _thread_ids(_header(msg, "In-Reply-To"))
    in_reply_to = answers[0] if answers else None
    references = list(dict.fromkeys([*answers[1:], *_thread_ids(_header(msg, "References"))]))
    # "From: somecompany.com" names no mailbox: the sender is recorded as unknown (a mail system may have no domain)
    from_email = sender if is_address(sender) or BOUNCE_SENDERS.search(sender) else NO_ADDRESS
    # Without a Message-ID a second delivery of the same email would be a new email: the same content gets the same id.
    message_id = _message_id(_header(msg, "Message-ID")) or _stand_in_id(from_email, _header(msg, "Date")[:200], subject, in_reply_to, body)
    shown_name = " ".join(_decoded(name or ("" if from_email != NO_ADDRESS else raw_from)).split())[:200]
    return ParsedEmail(
        message_id=message_id[:300], in_reply_to=in_reply_to, references=" ".join(references), from_email=from_email,
        from_name=shown_name or None, to_email=(to[0].lower()[:200] if to else None),
        subject=subject[:300], received_at=received,
        body_text=body, auto_submitted=auto, is_bounce=is_bounce, bounced_addresses=bounced,
        bounced_message_id=original, auth_results=(_header(msg, "Authentication-Results").strip()[:2000] or None),
        kind=kind, note=reason if kind == "sending_problem" else _no_text_note(body, files))


def _stand_in_id(*fields: str | None) -> str:
    digest = hashlib.sha256("\0".join(f or "" for f in fields).encode("utf-8", "replace")).hexdigest()[:32]
    return f"<no-id-{digest}@aime.invalid>"


NEW_TEXT_LIMIT = 5000
QUOTE_LINE = re.compile(rf"^(?:>.*|{_QUOTED_BY}|{_COPY_LINE}|{ATTACHED_MARK}|sent from my \w+.*|_{{5,}}|-{{3}})$", re.I)
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


def below_header_copy(body: str) -> str:
    """The sender's words below a copied header block, when nothing was written above it. The earlier email is in there
    too, so this is only read for an opt-out, never for anything to act on."""
    lines = body.replace("\r\n", "\n").split("\n")
    last = max((i for i, line in enumerate(lines) if HEADER_COPY.match(line.strip())), default=None)
    return "" if last is None else _own_lines(lines[last + 1:])[:NEW_TEXT_LIMIT]


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
                since = utcnow() - timedelta(days=self.first_run_days)
                if state.get("read_at"):  # numbered anew (moved or rebuilt): from our last reading, if that was longer ago
                    since = min(since, datetime.fromisoformat(state["read_at"]) - timedelta(days=1))
                state = {"uidvalidity": validity, "last_uid": 0}
                typ, data = conn.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
            else:
                typ, data = conn.uid("SEARCH", None, f"UID {int(state['last_uid']) + 1}:*")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > int(state.get("last_uid", 0)))[: self.batch]
            out = []
            failed = dict(state.get("failed") or {})
            for uid in uids:
                broken = False
                try:
                    typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                except (imaplib.IMAP4.error, OSError, MemoryError) as exc:  # counted like a refusal: the mail before it is still handled
                    typ, parts = "NO", None
                    broken = not isinstance(exc, imaplib.IMAP4.error) or isinstance(exc, imaplib.IMAP4.abort)
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
                if broken:  # the connection is gone: the rest wait for the next run
                    break
            state["failed"] = failed
            state["read_at"] = utcnow().isoformat(timespec="seconds")
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
