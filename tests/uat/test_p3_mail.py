"""Phase 3: unrelated mail delays nothing, junk is read, Reply-To is shown. Synthetic data only.

Reading goes through the real imaplib against a small IMAP server on 127.0.0.1, in the two forms real servers answer in (Dovecot,
Gmail), so what is tested is what is parsed. AIME_EXTRA_CORPUS may name a further file with cases() -> {name: (subject, body, automatic)}
for the header comparison; without it that test is skipped."""

from __future__ import annotations

import base64
import email
import importlib.util
import imaplib
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from email.header import Header
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

import aime.timeutil
from aime.agents import inbox_agent as ia
from aime.agents.inbox_agent import InboxAgent, sales_related
from aime.db import kv_get
from aime.inbox import (DROP_TRIES, HEAD_FIELDS, HEAD_LIMIT, HEADERS_PER_RUN, JUNK_BODIES_PER_RUN, JUNK_HEADS_PER_RUN, LARGE_MESSAGE,
                        LARGE_READ, Mailbox, _only_the_body_can_tell, parse_message)
from aime.meetings import parse_calendly
from aime.models import Alert, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity, SkippedMail, Suppression, User
from aime.reply_intel import CALENDLY_NOTICE, lead_on_hold
from aime.visibility import record_condition, scope_for
from aime.web.auth import COOKIE, hash_password, make_cookie, session_secret
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import MAIL, contacted, latest_reply
from tests.test_outreach import lead_of
from tests.test_time_and_alerts import IN_WINDOW, add_lead, add_mail, mail_world, status_of
from tests.uat import p2_mail_corpus as K
from tests.uat.test_p2_mail import DAVID, vars_for
from tests.uat.test_uat_email import ORIGIN, meetings_of, notice, signed_ctx, suppressed_values, web

STYLES = ("dovecot", "gmail")


def head_lines(raw: bytes, fields=HEAD_FIELDS) -> bytes:
    """What a server answers to BODY[HEADER.FIELDS (...)]: the lines asked for, in their order and folding, and an empty line."""
    head = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[0]
    kept, keep = [], False
    for line in re.split(rb"\r?\n", head):
        if line[:1] not in (b" ", b"\t"):
            keep = line.split(b":", 1)[0].strip().upper().decode("latin-1") in fields
        if keep:
            kept.append(line)
    return b"\r\n".join(kept) + b"\r\n\r\n"


def body_structure(part) -> str:
    """BODYSTRUCTURE as a server builds it: parts within parts, an attached email's parts included."""
    if part.get_content_maintype() == "multipart" and part.is_multipart():
        return "(" + "".join(body_structure(p) for p in part.get_payload()) + f' "{part.get_content_subtype().upper()}")'
    if part.get_content_type() == "message/rfc822" and part.is_multipart():
        return f'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 1 NIL {body_structure(part.get_payload(0))} 1)'
    return f'("{part.get_content_maintype().upper()}" "{part.get_content_subtype().upper()}" NIL NIL NIL "7BIT" 1 1)'


def text_of(raw: bytes) -> str:
    """What a server looks through for SEARCH BODY: the decoded text."""
    parts = [p.get_payload(decode=True) or b"" for p in email.message_from_bytes(raw).walk() if not p.is_multipart()]
    return b" ".join(parts).decode("utf-8", "replace").lower()


class Server(threading.Thread):
    """A small IMAP server on this PC. It answers LIST, EXAMINE, UID SEARCH and UID FETCH the way the real ones do, and writes
    down every command, so a test can say what was asked for and that nothing could change the mailbox."""

    def __init__(self, folders, style="dovecot", listing=None, validity=1700000001):
        super().__init__(daemon=True)
        self.folders, self.style, self.listing, self.validity = folders, style, listing, validity
        self.commands: list[str] = []
        self.stall: set[int] = set()  # a fetch of these bodies is never answered
        self.drop: set[int] = set()  # a fetch of these bodies takes the connection down
        self.fail_folder: set[str] = set()
        self.endless_structure: set[int] = set()
        self.odd_file_name: set[int] = set()  # BODYSTRUCTURE of these brings a literal of its own, ahead of the email data
        self.drop_head: set[int] = set()  # asking for the header lines of these takes the connection down
        self.quirk: str | None = None  # how the answer to a request for header lines is odd
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.timeout = 5
        self.current: dict[int, bytes] = {}
        self.start()

    def factory(self, host, port, ssl_context=None, timeout=None):
        return imaplib.IMAP4("127.0.0.1", self.port, timeout=self.timeout)

    def stop(self):
        self.sock.close()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                with conn, conn.makefile("rb") as lines:
                    conn.sendall(b"* OK [CAPABILITY IMAP4rev1] ready\r\n")
                    for raw in lines:
                        if self._answer(conn, raw.decode("latin-1").rstrip("\r\n")):
                            break
            except OSError:
                continue

    def _answer(self, conn, line) -> bool:
        tag, _, rest = line.partition(" ")
        self.commands.append(rest)
        verb = rest.upper()
        out = b""
        if verb.startswith("CAPABILITY"):
            out = b"* CAPABILITY IMAP4rev1\r\n"
        elif verb.startswith("LIST"):
            out = b"".join(b"* LIST " + item + b"\r\n" for item in self._list_lines())
        elif verb.startswith("EXAMINE"):
            name = rest.split(" ", 1)[1]
            name = re.sub(r"\\(.)", r"\1", name[1:-1]) if name.startswith('"') else name
            if name not in self.folders or name in self.fail_folder:
                conn.sendall(f"{tag} NO no such folder\r\n".encode())
                return False
            self.current = self.folders[name]
            validity = self.validity + sorted(self.folders).index(name)  # each folder numbers its mail on its own
            out = (f"* {len(self.current)} EXISTS\r\n* 0 RECENT\r\n* FLAGS (\\Seen)\r\n"
                   f"* OK [UIDVALIDITY {validity}] UIDs valid\r\n* OK [UIDNEXT 99999] next\r\n").encode()
            conn.sendall(out + f"{tag} OK [READ-ONLY] done\r\n".encode())
            return False
        elif verb.startswith("UID SEARCH"):
            found = re.search(r"UID (\d+):", rest)
            low = int(found.group(1)) if found else 0
            words = re.search(r'BODY "([^"]*)"', rest, re.I)
            hits = [u for u in sorted(self.current) if u >= low and (not words or words.group(1).lower() in text_of(self.current[u]))]
            out = ("* SEARCH " + " ".join(str(u) for u in hits) + "\r\n").encode()
        elif verb.startswith("UID FETCH"):
            _, _, uid, items = rest.split(" ", 3)
            answered = self._fetch(conn, int(uid), items)
            if answered is None:
                return False  # stalled: nothing goes back
            if answered is False:
                return True  # the connection goes down
            out = answered
        elif verb.startswith("LOGOUT"):
            conn.sendall(f"* BYE\r\n{tag} OK done\r\n".encode())
            return True
        conn.sendall(out + f"{tag} OK done\r\n".encode())
        return False

    def _list_lines(self):
        if self.listing is not None:
            return self.listing
        return [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]

    def _fetch(self, conn, uid, items):
        raw = self.current[uid]
        wants_head = "HEADER.FIELDS" in items
        seq = sorted(self.current).index(uid) + 1
        if items == "(UID)":
            return f"* {seq} FETCH (UID {uid})\r\n".encode()
        if wants_head and uid in self.drop_head:
            return False
        if wants_head and self.quirk:
            return self._odd_answer(seq, uid, raw, items)
        if not wants_head and uid in self.stall:
            return None
        if not wants_head and uid in self.drop:
            return False
        part = re.search(r"BODY\.PEEK\[([^\]]*)\](?:<0\.(\d+)>)?", items)
        section, limit = part.group(1), part.group(2)
        if section.startswith("HEADER.FIELDS"):
            data = head_lines(raw, tuple(section.split("(")[1].rstrip(")").split()))
        else:
            data = raw
        data = data[:int(limit)] if limit else data
        item = f"BODY[{section}]" + ("<0>" if limit else "")
        structure = f" BODYSTRUCTURE {body_structure(email.message_from_bytes(raw))}" if "BODYSTRUCTURE" in items else ""
        if structure and uid in self.endless_structure:
            structure = " BODYSTRUCTURE (" + '("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1)' * 40000 + ' "MIXED")'
        size = f" RFC822.SIZE {len(raw)}" if "RFC822.SIZE" in items else ""
        if structure and uid in self.odd_file_name:
            structure = ' BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" {7}\r\nodd"nam) NIL NIL "7BIT" 1 1)'
        literal = f"{item} {{{len(data)}}}\r\n".encode() + data
        if self.style == "gmail":  # the UID first and BODYSTRUCTURE after the data, as Gmail answers
            return f"* {seq} FETCH (X-GM-THRID 1 X-GM-MSGID 2 UID {uid}{size} ".encode() + literal + f"{structure} FLAGS (\\Seen))\r\n".encode()
        return f"* {seq} FETCH (UID {uid}{size}{structure} ".encode() + literal + b")\r\n"

    def _odd_answer(self, seq, uid, raw, items):
        """Answers a real server might give, or a broken one. None of them may make an email pass unread."""
        fields = tuple(re.search(r"HEADER\.FIELDS \(([^)]*)\)", items).group(1).split())
        head = head_lines(raw, fields)
        name = re.search(r"BODY\.PEEK(\[[^\]]*\])", items).group(1)
        name = f"BODY{name}<0>"
        structure = body_structure(email.message_from_bytes(raw))
        size = f"RFC822.SIZE {len(raw)}"
        literal = lambda data: f"{{{len(data)}}}\r\n".encode() + data  # noqa: E731
        if self.quirk == "leading blank line":
            return f"* {seq} FETCH (UID {uid} {size} BODYSTRUCTURE {structure} {name} ".encode() + literal(b"\r\n" + head) + b")\r\n"
        if self.quirk == "only some fields":
            some = b"\r\n".join(line for line in head.split(b"\r\n") if line.lower().startswith(b"subject")) + b"\r\n\r\n"
            return f"* {seq} FETCH (UID {uid} {size} BODYSTRUCTURE {structure} {name} ".encode() + literal(some) + b")\r\n"
        if self.quirk == "only the sender":
            some = b"\r\n".join(line for line in head.split(b"\r\n") if line.lower().startswith(b"from")) + b"\r\n\r\n"
            return f"* {seq} FETCH (UID {uid} {size} BODYSTRUCTURE {structure} {name} ".encode() + literal(some) + b")\r\n"
        if self.quirk == "quoted string, literal in the structure":
            quoted = head.replace(b"\r\n", b" ").replace(b'"', b'\\"')
            return (f'* {seq} FETCH (UID {uid} {size} BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" {{7}}\r\nodd"nam) NIL NIL "7BIT" 1 1) {name} "'.encode()
                    + quoted + b'")\r\n')
        if self.quirk == "another email's answer first":
            other = head_lines(self.current[1], fields)
            return (f"* 1 FETCH (UID 1 {name} ".encode() + literal(other)
                    + f")\r\n* {seq} FETCH (UID {uid} {size} BODYSTRUCTURE {structure} {name} ".encode() + literal(head) + b")\r\n")
        if self.quirk == "structure cut short":
            return f'* {seq} FETCH (UID {uid} {size} BODYSTRUCTURE ("MIXED") {name} '.encode() + literal(head) + b")\r\n"
        if self.quirk == "structure not closed":
            return f'* {seq} FETCH (UID {uid} {size} {name} '.encode() + literal(head) + b' BODYSTRUCTURE (("TEXT" "PLAIN")\r\n'
        raise AssertionError(self.quirk)

    def body_fetches(self, uid=None) -> list[str]:
        asked = [c for c in self.commands if c.upper().startswith("UID FETCH") and "HEADER.FIELDS" not in c.upper()]
        return [c for c in asked if uid is None or c.split()[2] == str(uid)]

    def assert_read_only(self):
        for c in self.commands:
            verb = c.split(" ", 1)[0].upper()
            assert verb in ("CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID", "LOGOUT"), c
            if verb == "UID":
                assert c.split()[1].upper() in ("SEARCH", "FETCH"), c
                assert "PEEK" in c.upper() or c.split()[1].upper() == "SEARCH" or c.endswith("(UID)"), c


@pytest.fixture
def server():
    made = []

    def make(folders, **kw):
        s = Server({name: dict(mails) for name, mails in folders.items()}, **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.stop()


def read(ctx, srv):
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory)).run({})
    srv.assert_read_only()
    return res


def news(i):
    return K.msg(f"News <news{i}@unrelated.example>", f"Offer {i}", "Buy now.", tag="news")


def newsletter(i):
    """As newsletters are: multipart/alternative, with list headers."""
    ctype, body = K.mp("alternative", [('text/plain; charset="UTF-8"', "Buy now."), ('text/html; charset="UTF-8"', "<b>Buy now.</b>")], f"nl{i}")
    return K.msg(f"News <news{i}@unrelated.example>", f"Offer {i}", body, ctype=ctype, tag="nl", extra=("List-Unsubscribe: <https://u.example/x>",))


def skipped_count(ctx):
    with ctx.db.session() as s:
        return s.execute(select(func.count(SkippedMail.id))).scalar_one()


def alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert).where(Alert.acknowledged.is_(False))).scalars()]


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when):
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when
    return set_to


def rows(ctx):
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()


def position(ctx, key="inbox.state"):
    with ctx.db.session() as s:
        return (kv_get(s, key) or {}).get("last_uid")


def scene(make_ctx, **over):
    ctx, sender, sent = contacted(make_ctx, **over)
    return ctx, sender, sent, vars_for(sent)


# ============================================================ 1. unrelated mail delays nothing
@pytest.mark.parametrize("style", STYLES)
def test_an_opt_out_behind_a_pile_of_unrelated_mail_is_read_at_once_and_their_bodies_are_not_fetched(make_ctx, server, style):
    ctx, _, _, v = scene(make_ctx)
    mails = {i: (news(i) if i % 2 else newsletter(i)) for i in range(1, 121)}
    mails[121] = K.thr(v, K.OPT)
    srv = server({"INBOX": mails}, style=style)
    out = read(ctx, srv).output
    assert DAVID in suppressed_values(ctx) and out.read_to_end
    assert out.skipped_unrelated == 120 and skipped_count(ctx) == 120 and position(ctx) == 121
    assert [c.split()[2] for c in srv.body_fetches()] == ["121"], "the unrelated mail's bodies were fetched"
    with ctx.db.session() as s:
        one = s.execute(select(SkippedMail).where(SkippedMail.subject == "Offer 7")).scalar_one()
        assert one.from_email == "news7@unrelated.example" and one.message_id and one.mailbox_uid.endswith(":7")


def test_more_than_fifty_related_emails_still_read_in_batches_and_hold(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    mails = {i: K.msg(f"S{i} <s{i}@free-mail.example>", f"Re: thing {i}", "Hello", tag="rel") for i in range(1, 56)}
    srv = server({"INBOX": mails})
    first = read(ctx, srv).output
    assert (len(srv.body_fetches()), first.read_to_end, position(ctx)) == (50, False, 50)
    second = read(ctx, srv).output
    assert second.read_to_end and position(ctx) == 55


def test_mail_past_what_one_run_looks_at_waits_and_nothing_is_passed_unrecorded(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    total = HEADERS_PER_RUN + 20
    srv = server({"INBOX": {**{i: news(i) for i in range(1, total)}, total: K.thr(v, K.OPT)}})
    first = read(ctx, srv).output
    assert not first.read_to_end and position(ctx) == HEADERS_PER_RUN and skipped_count(ctx) == HEADERS_PER_RUN
    assert DAVID not in suppressed_values(ctx)
    second = read(ctx, srv).output
    assert second.read_to_end and DAVID in suppressed_values(ctx) and skipped_count(ctx) == total - 1


def test_the_position_never_passes_mail_that_was_neither_handled_nor_recorded_when_the_database_fails(make_ctx, server, monkeypatch):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {**{i: news(i) for i in range(1, 11)}, 11: K.thr(v, K.OPT)}})
    real = InboxAgent._note_unrelated
    calls = []

    def failing(session, key, p, out):
        calls.append(key)
        if len(calls) == 5:
            raise OperationalError("insert", {}, Exception("database is locked"))
        return real(session, key, p, out)
    monkeypatch.setattr(InboxAgent, "_note_unrelated", staticmethod(failing))
    agent = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory))
    res = agent.run({})  # a retry inside the run succeeds, or the next run does
    if not res.ok:
        assert position(ctx) in (None, 0)
        res = agent.run({})
    assert res.ok and skipped_count(ctx) == 10 and position(ctx) == 11 and DAVID in suppressed_values(ctx)


def test_a_run_killed_on_a_real_email_loses_none_of_the_unrelated_mail_before_it(make_ctx, server, monkeypatch):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {**{i: news(i) for i in range(1, 6)}, 6: K.thr(v, K.OPT, mid="<kill3@x.example>"), 7: news(7)}})

    class Killed(BaseException):
        pass
    real = ia.parse_message

    def dies(raw, own=frozenset()):
        if b"<kill3@x.example>" in raw:
            raise Killed
        return real(raw, own)
    monkeypatch.setattr(ia, "parse_message", dies)
    with pytest.raises(Killed):
        InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory)).run({})
    assert position(ctx) == 5 and skipped_count(ctx) == 5  # written together: all that the position has passed is on record
    monkeypatch.setattr(ia, "parse_message", real)
    assert read(ctx, srv).ok and DAVID in suppressed_values(ctx) and skipped_count(ctx) == 6


@pytest.mark.parametrize("style", STYLES)
def test_a_report_hidden_inside_a_multipart_has_its_body_fetched_and_is_kept(make_ctx, server, style):
    """A gateway's wrapper round a delivery report: the header lines say only multipart/mixed. BODYSTRUCTURE shows the report."""
    ctx, _, sent, v = scene(make_ctx)
    report = K.BOUNCES["postfix_dsn"](v)
    inner = email.message_from_bytes(report)
    ctype, body = K.mp("mixed", [('text/plain; charset="UTF-8"', "Scanned by the gateway."),
                                 (f'{inner.get_content_type()}; report-type=delivery-status; boundary="{inner.get_boundary()}"',
                                  report.split(b"\r\n\r\n", 1)[1])], "wrap")
    wrapped = K.msg("Gateway <gateway@relay.example>", "Scan result", body, ctype=ctype, tag="wrap")
    srv = server({"INBOX": {1: wrapped, 2: newsletter(2)}}, style=style)
    out = read(ctx, srv).output
    assert srv.body_fetches(1) and not srv.body_fetches(2)
    assert out.stored == 1 and latest_reply(ctx).category == "bounce"


def test_a_file_name_sent_as_a_literal_is_not_taken_for_the_email(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {1: news(1)}})
    srv.odd_file_name.add(1)
    assert read(ctx, srv).output.skipped_unrelated == 1 and not srv.body_fetches()
    with ctx.db.session() as s:
        assert s.execute(select(SkippedMail)).scalar_one().subject == "Offer 1"


def test_a_server_that_gives_no_structure_has_the_multipart_bodies_fetched(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)

    class Plain(Server):
        def _fetch(self, conn, uid, items):
            return super()._fetch(conn, uid, items.replace("BODYSTRUCTURE", ""))

    srv = Plain({"INBOX": {1: newsletter(1), 2: news(2)}})
    try:
        out = read(ctx, srv).output
    finally:
        srv.stop()
    assert srv.body_fetches(1) and not srv.body_fetches(2) and out.skipped_unrelated == 2


def test_header_lines_cut_short_have_the_body_fetched(make_ctx, server):
    ctx, _, sent, v = scene(make_ctx)
    long_refs = f"{sent.message_id} " + " ".join(f"<r{i}@x.example>" for i in range(7000))
    assert len(long_refs) > HEAD_LIMIT
    raw = K.msg("Pat <pat@free-mail.example>", "A long story", "Please remove me.", refs=long_refs, tag="long")
    srv = server({"INBOX": {1: raw}})
    assert read(ctx, srv).output.stored == 1 and srv.body_fetches(1)


# ---- never skip on headers what the full check would keep
def own_of(ctx):
    return {a.lower() for a in (ctx.settings.smtp_from, ctx.settings.imap_credentials[0], ctx.settings.unsubscribe_email) if a}


def full_check_keeps(session, raw, own) -> bool:
    p = parse_message(raw, own)
    if p.from_email in own and not (CALENDLY_NOTICE.match(p.subject or "") and ia.looks_like_calendly(p)):
        return False
    return sales_related(session, p)


def skipped_on_headers(session, agent, raw, own, structure) -> bool:
    head = re.split(rb"\r?\n\r?\n", head_lines(raw), maxsplit=1)[0]
    if ia.FOOTER_WORDS in text_of(raw):  # the server finds these for us
        return False
    return not _only_the_body_can_tell(head, structure) and not agent._wanted(session, own)(head)


def variants(raw: bytes, v) -> dict[str, bytes]:
    """The same mail from someone else, without thread headers, or without its subject's prefix: what an answer looks like when
    its sender's tools change it."""
    unthreaded = re.sub(rb"(?im)^(in-reply-to|references):[^\r\n]*(\r?\n[ \t][^\r\n]*)*\r?\n", b"", raw.split(b"\r\n\r\n", 1)[0] + b"\r\n") + b"\r\n" + raw.split(b"\r\n\r\n", 1)[-1]
    stranger = re.sub(rb"(?im)^from:[^\r\n]*", b"From: Someone <someone@free-mail.example>", unthreaded, count=1)
    sub_domain = re.sub(rb"(?im)^from:[^\r\n]*", f"From: Someone <someone@mail.{v.dom}>".encode(), unthreaded, count=1)
    return {"as is": raw, "no thread": unthreaded, "stranger": stranger, "sub-domain": sub_domain,
            "no prefix": re.sub(rb"(?im)^subject:\s*(re|fwd?|aw):\s*", b"Subject: ", stranger, count=1)}


def corpus(v):
    for group in (K.WHO, K.AUTO, K.STRUCTURE, K.BOUNCES, K.CALENDAR):
        for name, build in group.items():
            yield name, build(v)
    for name, text in K.LANG.items():
        yield name, K.thr(v, text + K.q_gmail(v), tag="lang")
    # The same footer quoted in a reply under a subject of no meaning to us, a Calendly notice nobody expects, and mail with list headers.
    yield "footer", K.msg("A <a@free-mail.example>", "Re: something else", "Stop this." + K.q_gmail(v), tag="ft")
    yield "footer under a person's own subject", K.msg("A <a@free-mail.example>", "A question of mine", "Stop this." + K.q_gmail(v), tag="fo")
    yield "failed delivery by its subject", K.msg("A <a@relay.example>", "Undeliverable: hello", "It could not be delivered.", tag="ud")
    yield "calendly", K.msg("A <a@free-mail.example>", "New Event: X - 10:00am", "See https://calendly.com/app", tag="cl")
    yield "newsletter", newsletter(1)
    yield "plain news", news(2)
    ctype, body = K.mp("mixed", [("message/rfc822", K.BOUNCES["postfix_dsn"](v))], "att")
    yield "report in an attached email", K.msg("A <a@relay.example>", "Scan", body, ctype=ctype, tag="att")


def assert_never_skips_what_the_full_check_keeps(ctx, agent, messages, v):
    own = own_of(ctx)
    lost, skipped, kept, seen = [], 0, 0, 0
    with ctx.db.session() as s:
        for name, raw in messages:
            for how, form in variants(raw, v).items():
                seen += 1
                try:
                    keeps = full_check_keeps(s, form, own)
                except Exception:  # noqa: BLE001 -- kept as unreadable by the real path
                    keeps = True
                kept += keeps
                structure = b"BODYSTRUCTURE " + body_structure(email.message_from_bytes(form)).encode()
                for known in (structure, None):
                    if skipped_on_headers(s, agent, form, own, known):
                        skipped += 1
                        if keeps:
                            lost.append((name, how, "structure known" if known else "structure unknown"))
    assert not lost, lost[:20]
    return seen, kept, skipped


def test_the_header_decision_never_skips_what_the_full_check_keeps_over_the_whole_corpus(make_ctx):
    ctx, _, _, v = scene(make_ctx)
    agent = InboxAgent(ctx)
    seen, kept, skipped = assert_never_skips_what_the_full_check_keeps(ctx, agent, list(corpus(v)), v)
    assert seen > 500 and kept > 100 and skipped > 20, (seen, kept, skipped)  # it does skip: the comparison means something


@pytest.mark.skipif(not os.environ.get("AIME_EXTRA_CORPUS"), reason="a further corpus is only on the machine that holds it")
def test_the_header_decision_never_skips_what_the_full_check_keeps_over_a_further_corpus(make_ctx):
    spec = importlib.util.spec_from_file_location("extra_corpus", os.environ["AIME_EXTRA_CORPUS"])
    extra = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extra)
    ctx, _, _, v = scene(make_ctx)
    messages = []
    for name, (subject, body, automatic) in extra.cases().items():
        messages.append((name, K.msg(f"Pat Doe <{v.to}>", subject, body, tag="x", extra=("Auto-Submitted: auto-replied",) if automatic else ())))
    seen, kept, skipped = assert_never_skips_what_the_full_check_keeps(ctx, InboxAgent(ctx), messages, v)
    print(f"further corpus: {seen} forms, kept by the full check {kept}, skipped on headers {skipped}, lost 0")


# ---- very large messages
@pytest.mark.parametrize("style", STYLES)
def test_a_very_large_reply_is_read_only_as_far_as_its_start_and_its_words_still_count(make_ctx, server, style):
    ctx, _, _, v = scene(make_ctx)
    raw = K.huge_attachment(v, mb=int(LARGE_MESSAGE * 1.1 / 1_000_000))
    assert len(raw) > LARGE_MESSAGE
    srv = server({"INBOX": {1: raw}}, style=style)
    assert read(ctx, srv).ok
    assert [c for c in srv.body_fetches(1)] == [f"UID FETCH 1 (BODY.PEEK[]<0.{LARGE_READ}>)"]
    r = latest_reply(ctx)
    assert r.category == "unsubscribe" and DAVID in suppressed_values(ctx)
    assert "very large" in r.mail_note and "very large" in r.action_note and r.status == "needs_review"


def test_a_large_reply_whose_text_lies_past_what_was_read_is_kept_for_a_person(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    ctype, body = K.mp("mixed", [('application/pdf; name="a.pdf"', b"x" * (LARGE_MESSAGE + 100), "Content-Transfer-Encoding: base64"),
                                 ('text/plain; charset="UTF-8"', K.OPT)], "big2")
    raw = K.msg("Pat <pat@free-mail.example>", "Re: nothing we sent", body, ctype=ctype, tag="big2")
    srv = server({"INBOX": {1: raw}})
    assert read(ctx, srv).ok
    r = latest_reply(ctx)
    assert r is not None and r.status == "needs_review" and "very large" in (r.action_note or "")


def test_large_bodies_stop_at_what_one_run_may_hold_and_the_rest_wait(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {i: K.thr(v, "Thanks. " + "x" * 900_000, tag=f"b{i}") for i in range(1, 6)}})
    box = Mailbox(ctx.settings, imap_factory=srv.factory)
    with box.connected() as conn:
        got, state = box.read_folder(conn, "INBOX", None, run_bytes=2_000_000)
    assert len(got) == 3 and state["last_uid"] == 3 and state["complete"] is False
    with box.connected() as conn:
        got, state = box.read_folder(conn, "INBOX", state, run_bytes=2_000_000)
    assert len(got) == 2 and state["last_uid"] == 5 and state["complete"] is True


# ============================================================ 2. junk
JUNK_LISTINGS = {
    "dovecot": ([b'(\\HasNoChildren) "." "INBOX"', b'(\\HasNoChildren \\Junk) "." "INBOX.Junk"', b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"'], "INBOX.Junk"),
    "gmail": ([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasChildren \\Noselect) "/" "[Gmail]"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
               b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Bin"'], "[Gmail]/Spam"),
}


@pytest.mark.parametrize("style", STYLES)
def test_a_real_answer_filtered_as_spam_is_found_and_handled_like_one_in_the_inbox(make_ctx, server, style):
    ctx, _, _, v = scene(make_ctx)
    listing, name = JUNK_LISTINGS[style]
    srv = server({"INBOX": {1: news(1)}, name: {7: K.thr(v, K.OPT), 8: news(8)}}, style=style, listing=listing)
    out = read(ctx, srv).output
    assert out.read_to_end and DAVID in suppressed_values(ctx) and lead_of(ctx).outreach_status == "unsubscribed"
    r = latest_reply(ctx)
    assert r.in_junk is True and r.mailbox_uid.startswith("junk:") and "junk folder" in r.action_note
    assert skipped_count(ctx) == 1, "unrelated junk leaves no trace in the skipped list"
    assert any(c.upper().startswith("EXAMINE") and name in c for c in srv.commands)
    assert not any(c.upper().startswith("SELECT") for c in srv.commands)
    with ctx.db.session() as s:
        inbox, junk = kv_get(s, "inbox.state"), kv_get(s, "inbox.state.junk")
        assert junk["last_uid"] == 8 and inbox["last_uid"] == 1 and junk["uidvalidity"] != inbox["uidvalidity"]
        assert not {"unrelated", "large", "complete", "skipped_headers", "connection_lost"} & (set(inbox) | set(junk)), "only the position is saved"
    assert "Found in junk" in web(ctx).get("/inbox?show=all").text


def test_interest_from_junk_gets_its_draft_like_any_other(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {}, "Junk": {3: K.thr(v, K.HOT)}})
    assert read(ctx, srv).ok
    r = latest_reply(ctx)
    assert r.category == "interested" and r.in_junk and r.suggested_followup_id
    with ctx.db.session() as s:
        assert s.get(FollowUp, r.suggested_followup_id).status == "awaiting_approval"


@pytest.mark.parametrize("name", ["Junk", "Spam", "INBOX.Junk", "INBOX.spam", "Junk E-mail", "[Gmail]/Spam", "SPAM", "junk", "inbox.JUNK"])
def test_the_junk_folder_is_found_by_its_common_names_in_any_case(make_ctx, server, name):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {}, "Archive": {}, name: {1: K.thr(v, K.OPT)}})
    assert read(ctx, srv).ok and DAVID in suppressed_values(ctx)


def test_no_junk_folder_is_a_normal_state(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {1: K.thr(v, K.OPT)}, "Archive": {2: K.thr(v, K.HOT)}})
    out = read(ctx, srv).output
    assert out.read_to_end and out.stored == 1 and not any("junk" in a.lower() for a in alerts(ctx))
    assert not any("Archive" in c for c in srv.commands if c.upper().startswith("EXAMINE"))


def test_a_noselect_folder_a_name_that_needs_quoting_and_a_name_sent_as_a_literal(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    tricky = 'Junk "mail" \\ here'
    escaped = tricky.replace("\\", "\\\\").replace('"', '\\"')
    listing = [b'(\\Noselect \\Junk) "/" "Junk"', f'(\\Junk) "/" "{escaped}"'.encode()]
    srv = server({"INBOX": {}, "Junk": {1: news(1)}, tricky: {1: K.thr(v, K.OPT)}}, listing=listing)
    assert read(ctx, srv).ok and DAVID in suppressed_values(ctx)
    assert f'EXAMINE "{escaped}"' in srv.commands and 'EXAMINE "Junk"' not in srv.commands

    box = Mailbox(ctx.settings)
    conn = SimpleNamespace(list=lambda: ("OK", [(b'(\\Junk) "/" {9}', b"Junk Mail"), b"", b'(\\HasNoChildren) "/" Other']))
    assert box.junk_folder(conn) == "Junk Mail"
    assert box.junk_folder(SimpleNamespace(list=lambda: ("OK", [b'(\\HasNoChildren) "/" Spam', b'(\\NonExistent) "/" Junk']))) == "Spam"
    assert box.junk_folder(SimpleNamespace()) is None, "a server fake without list has no junk folder"


def test_junk_is_read_a_little_each_run_and_its_size_slows_no_tick(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    total = JUNK_HEADS_PER_RUN * 2 + 5
    spam = {i: K.msg(f"S <s{i}@spam.example>", f"Re: pills {i}" if i % 3 == 0 else f"Pills {i}", "Buy.", tag="sp") for i in range(1, total)}
    spam[total] = K.thr(v, K.OPT)
    srv = server({"INBOX": {1: news(1)}, "Junk": spam})
    first = read(ctx, srv).output
    assert first.read_to_end, "junk never holds sending"
    assert len(srv.body_fetches()) == JUNK_BODIES_PER_RUN, "bodies in junk are bounded per run"
    heads = [c for c in srv.commands if "HEADER.FIELDS" in c.upper()]
    assert len(heads) <= JUNK_HEADS_PER_RUN + 1
    assert position(ctx, "inbox.state.junk") <= JUNK_HEADS_PER_RUN and rows(ctx) == [] and skipped_count(ctx) == 1
    for _ in range(12):
        read(ctx, srv)
    assert DAVID in suppressed_values(ctx), "the real answer behind the spam is reached within a few ticks"
    assert [r.message_id for r in rows(ctx) if r.category != "unsubscribe"] == []


def test_a_first_read_of_junk_uses_the_same_look_back_as_the_inbox(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {}, "Junk": {1: news(1)}})
    read(ctx, srv)
    searches = [c for c in srv.commands if "SEARCH" in c.upper()]
    assert len(searches) == 2 and all("SINCE" in c for c in searches)
    read(ctx, srv)
    assert any("UID 2:*" in c for c in srv.commands), "the second read goes on from its own position"


@pytest.mark.parametrize("how", ["cannot open", "list refused", "connection lost", "stalls"])
def test_trouble_with_the_junk_folder_raises_one_warning_fails_nothing_and_holds_nothing(make_ctx, monkeypatch, clock, server, how):
    from aime import workflows

    srv = server({"INBOX": {}, "Junk": {1: news(1), 2: K.msg("S <s@x.example>", "Re: pills", "Buy.", tag="sp2")}})
    if how == "cannot open":
        srv.fail_folder.add("Junk")
    elif how == "list refused":
        srv.listing = None
        srv._list_lines = lambda: (_ for _ in ()).throw(OSError("gone"))  # the server thread drops the connection
    elif how == "connection lost":
        srv.drop.add(2)
    else:
        srv.stall.add(2)
        srv.timeout = 1
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    clock(IN_WINDOW)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    outcome = workflows.job_inbox(ctx)
    assert not outcome.failed, "the junk folder must not fail the inbox run"
    workflows.job_followups(ctx)
    assert status_of(ctx, fid)[0] == "sent", "the junk folder must not hold sending"
    assert sum("junk folder" in a for a in alerts(ctx)) == 1
    workflows.job_inbox(ctx)
    assert sum("junk folder" in a for a in alerts(ctx)) == 1, "one keyed warning, not one per tick"
    srv.fail_folder.clear(), srv.drop.clear(), srv.stall.clear()
    srv.__dict__.pop("_list_lines", None)
    assert not workflows.job_inbox(ctx).failed
    assert sum("junk folder" in a for a in alerts(ctx)) == 0, "closed by the next good read"


def test_each_panel_mailbox_keeps_its_own_junk_position(make_ctx, server):
    from tests.test_mailbox_admin import GMAIL, add
    from aime import mail_accounts as ma

    ctx = make_ctx(**{k: v for k, v in MAIL.items() if k != "imap_host"})
    one = add(ctx, GMAIL)
    with ctx.db.session() as s:
        ma.change(s, "owner", one, "enable")
        ma.change(s, "owner", one, "read_on")
    srv = server({"INBOX": {}, "Spam": {4: news(4)}})
    res = InboxAgent(ctx, imap_factory=srv.factory).run({})
    assert res.ok, res.error
    assert position(ctx, f"inbox.state.mailbox{one}.junk") == 4


# ============================================================ the read-first gate
def gate(make_ctx, monkeypatch, clock, srv_factory):
    ctx = mail_world(make_ctx, monkeypatch, imap=srv_factory)
    clock(IN_WINDOW)
    return ctx, add_lead(ctx, stage="approved")


def test_a_backlog_of_unrelated_mail_does_not_hold_sending(make_ctx, monkeypatch, clock, server):
    from aime import workflows

    srv = server({"INBOX": {i: news(i) for i in range(1, 121)}})
    ctx, lead = gate(make_ctx, monkeypatch, clock, srv.factory)
    fid = add_mail(ctx, lead)
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert status_of(ctx, fid)[0] == "sent" and len(FakeSMTP.sent) == 1


def test_a_real_opt_out_inside_that_backlog_is_read_before_any_send_in_the_same_tick(make_ctx, monkeypatch, clock, server):
    from aime import workflows

    srv = server({"INBOX": {}})
    ctx, lead = gate(make_ctx, monkeypatch, clock, srv.factory)
    fid = add_mail(ctx, lead)
    with ctx.db.session() as s:
        addr = s.get(Lead, lead).contact.email
    mails = {i: news(i) for i in range(1, 121)}
    mails[60] = K.msg(f"Person <{addr}>", "Re: Step 1", "Please remove me from your list.", tag="optout")
    srv.folders["INBOX"].update(mails)
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert FakeSMTP.sent == [] and status_of(ctx, fid)[0] == "cancelled" and addr in suppressed_values(ctx)


def test_over_fifty_related_emails_and_a_failed_read_still_hold(make_ctx, monkeypatch, clock, server):
    from aime import workflows

    srv = server({"INBOX": {i: K.msg(f"S{i} <s{i}@free-mail.example>", f"Re: thing {i}", "Hello", tag="r") for i in range(1, 56)}})
    ctx, lead = gate(make_ctx, monkeypatch, clock, srv.factory)
    fid = add_mail(ctx, lead)
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    state, why = status_of(ctx, fid)
    assert FakeSMTP.sent == [] and state == "approved" and "more mail is waiting" in why
    assert not workflows.job_inbox(ctx).failed  # the other five
    srv.folders["INBOX"].update({60: K.msg("Pat <pat@free-mail.example>", "Re: thing", "Hello", tag="r2")})
    srv.drop.add(60)
    assert workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    state, why = status_of(ctx, fid)
    assert FakeSMTP.sent == [] and state == "approved" and "could not be read" in why
    srv.drop.clear()
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert status_of(ctx, fid)[0] == "sent"


# ============================================================ 3. Reply-To
def test_reply_to_is_stored_and_shown_and_the_draft_still_goes_to_the_sender(make_ctx, server):
    ctx, _, sent, v = scene(make_ctx)
    raw = K.thr(v, K.HOT, extra=("Reply-To: Pat Personal <pat.personal@elsewhere.example>",))
    assert read(ctx, server({"INBOX": {1: raw}})).ok
    r = latest_reply(ctx)
    assert r.reply_to == "pat.personal@elsewhere.example"
    with ctx.db.session() as s:
        draft = s.get(FollowUp, r.suggested_followup_id)
        assert draft.to_email == r.from_email and draft.status == "awaiting_approval"
    assert "pat.personal@elsewhere.example" in web(ctx).get("/inbox?show=all").text
    assert "pat.personal@elsewhere.example" in web(ctx).get("/followups").text
    assert "no-reply" not in (r.action_note or "")


@pytest.mark.parametrize("value,stored", [("Pat <PAT@elsewhere.example>", "pat@elsewhere.example"), ("not an address", None), ("<>", None),
                                          ("x" * 5000, None), ("A <a@one.example>, B <b@two.example>", "a@one.example")])
def test_reply_to_keeps_only_a_usable_address(value, stored):
    raw = K.msg("Pat <pat@x.example>", "Hi", "Hello", extra=(f"Reply-To: {value}",))
    assert parse_message(raw).reply_to == stored


def test_a_reply_to_of_the_sender_themselves_is_nothing_to_show():
    assert parse_message(K.msg("Pat <pat@x.example>", "Hi", "Hello", extra=("Reply-To: PAT@x.example",))).reply_to is None


@pytest.mark.parametrize("sender", ["no-reply", "noreply", "do-not-reply", "donotreply"])
def test_a_draft_to_a_no_reply_sender_with_a_reply_to_says_so_and_nothing_goes_there_by_itself(make_ctx, server, sender):
    ctx, _, sent, v = scene(make_ctx)
    raw = K.thr(v, K.HOT, frm=f"Acme <{sender}@{v.dom}>", extra=(f"Reply-To: real.person@{v.dom}",))
    assert read(ctx, server({"INBOX": {1: raw}})).ok
    r = latest_reply(ctx)
    assert "no-reply address" in r.action_note and f"real.person@{v.dom}" in r.action_note and "person decides" in r.action_note
    with ctx.db.session() as s:
        drafts = s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().all()
        assert [d.to_email for d in drafts] == [f"{sender}@{v.dom}"] and drafts[0].status == "awaiting_approval"
    assert "no-reply address" in web(ctx).get("/inbox?show=all").text
    assert "no-reply address" in web(ctx).get("/followups").text


def test_erasing_a_person_also_erases_the_reply_to_they_gave_or_were_named_in(make_ctx, server):
    from aime.crm_ops import erase_person

    ctx, _, sent, v = scene(make_ctx)
    raw = K.thr(v, "Thanks, more soon.", extra=("Reply-To: named@elsewhere.example",), frm=f"Pat <colleague@{v.dom}>")
    assert read(ctx, server({"INBOX": {1: raw}})).ok
    with ctx.db.session() as s:
        erase_person(s, "named@elsewhere.example", "owner")
    assert latest_reply(ctx).reply_to is None


def test_the_new_columns_are_added_to_a_database_of_the_old_code_and_the_old_columns_stay(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from aime.migrations import migrate
    from aime.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE inbound_emails DROP COLUMN reply_to"))
        c.execute(text("ALTER TABLE inbound_emails DROP COLUMN in_junk"))
        c.execute(text("INSERT INTO inbound_emails(message_id, from_email, subject, status, auto_submitted, is_simulated, received_at) "
                       "VALUES ('<old>', 'a@b.example', 'x', 'new', 0, 0, '2026-10-01 00:00:00')"))
    applied = migrate(engine)
    assert sum("inbound_emails" in a for a in applied) == 2
    columns = {c["name"]: c for c in inspect(engine).get_columns("inbound_emails")}
    assert columns["reply_to"]["nullable"] and columns["in_junk"]["nullable"]
    with engine.connect() as c:
        assert c.execute(text("SELECT message_id, reply_to, in_junk FROM inbound_emails")).one() == ("<old>", None, None)
    engine.dispose()


# ============================================================ 4. Calendly notice forwarded in another language
PREFIXES = ["WG:", "RV:", "TR:", "SV:", "AW:", "Fw:", "FW:", "Fwd:", "wg: AW:"]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_notice_forwarded_under_any_prefix_reads_as_a_notice_and_a_person_applies_it(make_ctx, server, prefix):
    ctx, _, _, v = scene(make_ctx, calendly_verified_only=True)
    subject = "New Event: Pat Doe - 10:00am Thu, Nov 5, 2026 - 30 Minute Meeting"
    raw = K.msg("Owner <owner@owner-home.example>", f"{prefix} {subject}", "Invitee Email:\nx@y.example\nhttps://calendly.com/app", tag="cal")
    assert read(ctx, server({"INBOX": {1: raw}})).ok
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("calendly", "needs_review") and meetings_of(ctx) == [], "the verified rules stay as they were"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_signed_notice_under_any_prefix_books_as_before(make_ctx, monkeypatch, server, prefix):
    ctx, _, sent = signed_ctx(make_ctx, monkeypatch)
    raw = notice()
    raw = re.sub(rb"(?m)^Subject: ", f"Subject: {prefix} ".encode(), raw, count=1)
    assert read(ctx, server({"INBOX": {1: raw}})).ok
    assert [m.lead_id for m in meetings_of(ctx)] == [lead_of(ctx).id]


def test_the_notice_parser_takes_the_same_prefixes():
    for prefix in PREFIXES:
        body = "Invitee:\nPat Doe\n\nInvitee Email:\npat@x.example\n\nEvent Date/Time:\n10:00am - Thursday, November 5, 2026 (Eastern Time)\n"
        assert parse_calendly(f"{prefix} New Event: Pat Doe - 10:00am Thu, Nov 5, 2026 - 30 Minute Meeting", body, set())["kind"] == "booked"


def test_a_reply_in_english_to_a_notice_is_still_a_reply():
    assert not CALENDLY_NOTICE.match("Re: New Event: Pat - 10am")


# ============================================================ 5. calendar invitations
@pytest.mark.parametrize("name", ["ics_only_in_thread", "ics_gmail_invite_in_thread"])
def test_an_invitation_reads_as_one_and_a_person_is_told_to_book_it_and_nothing_books_itself(make_ctx, server, name):
    ctx, _, sent, v = scene(make_ctx)
    assert read(ctx, server({"INBOX": {1: K.CALENDAR[name](v)}})).ok
    r = latest_reply(ctx)
    assert (r.mail_kind, r.category, r.status) == ("calendar", "other", "needs_review")
    assert r.intent is None and r.suggested_followup_id is None, "it is no question, and nothing is drafted as an answer"
    assert "calendar" in r.recommended_status and "calendar" in r.action_note
    assert meetings_of(ctx) == []
    assert lead_of(ctx).outreach_status == "replied", "the sequence stops: they answered"
    assert any("calendar invitation" in a and "Nothing is booked by itself" in a for a in alerts(ctx))
    page = web(ctx).get("/inbox").text
    assert "calendar" in page


# ============================================================ 6. what was unproven
def test_a_reply_with_no_lead_reaches_a_sales_manager_who_can_act_on_it(make_ctx, server):
    """Production's sales1 and sales2 are Sales Managers: they see what nobody owns, and may mark it reviewed or reclassify it."""
    ctx = make_ctx(**MAIL, dashboard_auth=True)
    with ctx.db.session() as s:
        s.add(User(username="sales1", password_hash=hash_password("x" * 12), role="sales_manager"))
        s.add(InboundEmail(message_id="<nolead@x.example>", from_email="pat@free-mail.example", subject="Re: hello", status="needs_review",
                           category="other", confidence="low", body_text="Who are you?", new_text="Who are you?"))
    from fastapi.testclient import TestClient

    from aime.web.app import create_app

    c = TestClient(create_app(ctx))
    with ctx.db.session() as s:
        user = s.execute(select(User).where(User.username == "sales1")).scalar_one()
        c.cookies.set(COOKIE, make_cookie(session_secret(ctx.settings.data_dir), user))
        scope = scope_for(s, {"username": "sales1", "perms": {"view_team", "view_own", "view_unassigned", "edit_records", "approve_outbound"}})
        assert s.execute(select(InboundEmail.id).where(record_condition(scope, InboundEmail.lead_id))).first()
        rid = s.execute(select(InboundEmail.id)).scalar_one()
    page = c.get("/inbox")
    assert page.status_code == 200 and "pat@free-mail.example" in page.text and "not matched to a lead" in page.text
    assert "Reclassify and apply" in page.text, "a Sales Manager may decide what it is"
    assert c.post(f"/inbox/{rid}/reviewed", headers=ORIGIN, data={"category": "other"}, follow_redirects=False).status_code == 303
    with ctx.db.session() as s:
        assert s.get(InboundEmail, rid).status == "reviewed"


def test_a_fetch_that_stalls_is_a_failed_read_what_came_before_it_is_handled_and_the_next_try_asks_only_for_the_start(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {1: K.thr(v, "Thanks, more soon."), 2: K.thr(v, K.HOT), 3: K.thr(v, K.OPT)}})
    srv.stall.add(2)
    srv.timeout = 1
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory)).run({})
    assert not res.ok and "dropped" in res.error
    assert position(ctx) == 1 and len(rows(ctx)) == 1, "what came before it is handled"
    srv.stall.clear()
    assert read(ctx, srv).ok and DAVID in suppressed_values(ctx) and position(ctx) == 3
    assert srv.body_fetches(2)[-1] == f"UID FETCH 2 (BODY.PEEK[]<0.{LARGE_READ}>)"


def test_a_fetch_that_never_succeeds_is_named_after_some_tries_and_the_mail_behind_it_is_read(make_ctx, server):
    """Not a wedge: its header lines come, its body takes the connection down each time. A person is told who wrote, and the opt-out
    behind it is read, so sending does not stay held for ever."""
    ctx, _, _, v = scene(make_ctx)
    raw = K.thr(v, "A huge story", frm=f"Jane <jane.smith@{v.dom}>")
    srv = server({"INBOX": {1: raw, 2: K.msg(f"Other <{v.to}>", "Re: x", K.OPT, tag="o")}})
    srv.drop.add(1)
    agent = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory))
    for _ in range(DROP_TRIES // 2):  # each run tries twice
        assert not agent.run({}).ok
    assert any("drops each time" in a for a in alerts(ctx)) and DAVID not in suppressed_values(ctx)
    assert agent.run({}).ok
    assert DAVID in suppressed_values(ctx), "the mail behind it was read"
    assert any(f"jane.smith@{v.dom}" in a and "could not be read" in a for a in alerts(ctx))
    with ctx.db.session() as s:
        assert s.execute(select(InboundEmail).where(InboundEmail.from_email == f"jane.smith@{v.dom}")).first(), "it stays on Replies"


def test_an_email_whose_structure_is_longer_than_imaplib_takes_cannot_wedge_the_mailbox(make_ctx, server):
    """Anyone can send an email of thousands of parts. imaplib refuses so long a line, and the connection is lost with it."""
    ctx, _, _, v = scene(make_ctx)
    ctype, body = K.mp("mixed", [('text/plain; charset="UTF-8"', "Buy now.")], "many")
    srv = server({"INBOX": {1: K.msg("S <s@stranger.example>", "Offer", body, ctype=ctype, tag="many"), 2: K.thr(v, K.OPT)}})
    srv.endless_structure.add(1)
    agent = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory))
    assert agent.run({}).ok, "the retry inside the run asks without the structure"
    assert DAVID in suppressed_values(ctx) and position(ctx) == 2


LIST_HEADERS = ("List-Unsubscribe: <mailto:unsubscribe@us.example>", "List-Id: <x.us.example>")


@pytest.mark.parametrize("subject,text,category,suppresses", [
    ("{subj}", "Yes, interested. Can we talk Thursday?", "interested", False),
    ("{subj}", "Please remove me from your list.", "unsubscribe", True),
    ("{subj}", "Unsubscribe", "unsubscribe", True),
    ("unsubscribe", "", "unsubscribe", True),
    ("Re: {subj}", "Who are you?", "question", False),
])
@pytest.mark.parametrize("thread", [True, False], ids=["in thread", "no thread"])
def test_a_person_at_a_free_mail_address_behind_a_list_unsubscribe_of_ours_is_not_taken_for_a_newsletter(make_ctx, server, subject, text, category, suppresses, thread):
    """Their mail program copied our List-Unsubscribe into the answer, or they used our link. It must reach a person or be honoured."""
    ctx, _, sent, v = scene(make_ctx)
    frm = "Pat <pat.personal@free-mail.example>"
    subj = subject.format(subj=f"Re: {v.subj}" if not subject.startswith("Re") else v.subj)
    raw = K.msg(frm, subj, text, v_irt=v.mid if thread else None, extra=LIST_HEADERS, tag="fm")
    srv = server({"INBOX": {1: raw}})
    assert read(ctx, srv).ok
    r = latest_reply(ctx)
    assert r is not None, "it vanished as a newsletter"
    assert r.category == category or r.status == "needs_review", (r.category, r.status)
    assert ("pat.personal@free-mail.example" in suppressed_values(ctx)) == suppresses
    assert r.status == "needs_review" or r.lead_id == lead_of(ctx).id
    assert skipped_count(ctx) == 0


# ---- what stays as it was
def test_read_only_all_the_way(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {1: K.thr(v, K.OPT), 2: news(2), 3: newsletter(3)}, "Junk": {1: K.thr(v, K.HOT), 2: news(2)}})
    assert read(ctx, srv).ok
    fetches = [c for c in srv.commands if c.upper().startswith("UID FETCH")]
    assert fetches and all("PEEK" in c.upper() for c in fetches)
    assert all(not c.upper().startswith(("SELECT", "STORE", "UID STORE", "COPY", "UID COPY", "EXPUNGE", "CREATE", "DELETE", "APPEND")) for c in srv.commands)


def test_mail_read_again_after_a_reset_changes_nothing(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    answer = K.thr(v, K.HOT)
    srv = server({"INBOX": {1: answer, 2: news(2)}, "Junk": {1: answer, 3: news(3)}})
    read(ctx, srv)
    before = (len(rows(ctx)), skipped_count(ctx))
    assert before == (1, 1), "the same email in the inbox and in junk is one email"
    srv.validity = 1700000002
    read(ctx, srv)
    assert (len(rows(ctx)), skipped_count(ctx)) == before


def test_no_contact_or_lead_is_changed_by_passing_mail_on_its_headers(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    with ctx.db.session() as s:
        lead_before = s.execute(select(Lead.outreach_status)).scalars().all()
        contacts = s.execute(select(func.count(Contact.id))).scalar_one()
    read(ctx, server({"INBOX": {i: news(i) for i in range(1, 30)}}))
    with ctx.db.session() as s:
        assert s.execute(select(Lead.outreach_status)).scalars().all() == lead_before
        assert s.execute(select(func.count(Contact.id))).scalar_one() == contacts


# ============================================================ review: forged mail in junk
def forged_opt_out(v):
    return K.msg(v.to, "please stop", "Please remove me from your list.", tag="f", extra=("Authentication-Results: mx.example; spf=fail; dkim=none; dmarc=fail",))


def forged_footer_spam(v):
    return K.msg(v.to, "Big offer for you", "Cheap watches at great prices.\n\nTo unsubscribe click here.", tag="f")


def forged_company(v):
    return K.msg(f"sales@{v.dom}", "remove us", "Please remove us from your list, do not contact us again.", tag="f")


def forged_interest(v):
    return K.msg(v.to, "Your offer", "Yes I am interested, let's talk. Send pricing.", tag="f")


def guessed_subject(v):
    return K.msg("Promo <promo@cheap-stuff.example>", f"Re: {v.subj}", "Buy our SEO services today! Reply unsubscribe to stop.", tag="f")


def sub_domain_spam(v):
    return K.msg(f"offers@mail.{v.dom}", "Offer", "Buy now", tag="f")


def forged_notice(v):
    return K.msg("notifications@calendly.com", "New Event: Pat Doe - 10:00am Mon, Nov 2, 2026 - 30 Minute Meeting",
                 f"Invitee:\nPat Doe\n\nInvitee Email:\n{v.to}\n\nEvent Date/Time:\n10:00am - Monday, November 2, 2026 (Eastern Time)\nhttps://calendly.com/x", tag="f")


def forged_report(v):
    return K.BOUNCES["postfix_dsn_no_id"](v)


FORGED = {"opt-out from the contact's address": forged_opt_out, "spam with a footer": forged_footer_spam, "remove us from the company's domain": forged_company,
          "interest": forged_interest, "a guessed subject": guessed_subject, "a sub-domain": sub_domain_spam, "a Calendly notice": forged_notice,
          "a report quoting nothing of ours": forged_report}


def effects(ctx):
    """All that can hurt a prospect, or be undone by no one."""
    with ctx.db.session() as s:
        lead = s.execute(select(Lead)).scalars().first()
        return {"stage": lead.outreach_status, "consent": lead.contact.consent_status, "suppressed": sorted(x.value for x in s.execute(select(Suppression)).scalars()),
                "deals": s.execute(select(func.count(Opportunity.id))).scalar_one(), "meetings": s.execute(select(func.count(Meeting.id))).scalar_one(),
                "drafts": s.execute(select(func.count(FollowUp.id)).where(FollowUp.kind == "response")).scalar_one(), "email": lead.contact.email_status}


@pytest.mark.parametrize("name", sorted(FORGED))
def test_forged_mail_in_junk_changes_nothing_and_holds_the_lead_for_a_person(make_ctx, server, name):
    ctx, _, _, v = scene(make_ctx)
    before = effects(ctx)
    srv = server({"INBOX": {}, "Junk": {1: FORGED[name](v)}})
    assert read(ctx, srv).ok
    assert effects(ctx) == before, "nothing is done on the word of an email that may be forged"
    r = latest_reply(ctx)
    assert r.in_junk and r.status == "needs_review" and r.intent is None and r.suggested_followup_id is None
    assert r.category == ("calendly" if name == "a Calendly notice" else "other")
    assert "forged" in r.action_note and "forged" in r.recommended_status
    forged_alerts = [a for a in alerts(ctx) if "junk folder" in a and "forged" in a]
    assert len(forged_alerts) == 1
    with ctx.db.session() as s:
        held = lead_on_hold(s, lead_of(ctx).id)
    # A report holds nothing, as anyone can send one, and a notice comes from no lead's address.
    assert held is (name not in ("a report quoting nothing of ours", "a Calendly notice"))


@pytest.mark.parametrize("name", ["opt-out from the contact's address", "remove us from the company's domain", "interest"])
def test_the_same_mail_in_the_inbox_acts_as_before(make_ctx, server, name):
    """Side by side with the test above: only junk is treated as possibly forged."""
    ctx, _, _, v = scene(make_ctx)
    before = effects(ctx)
    assert read(ctx, server({"INBOX": {1: FORGED[name](v)}, "Junk": {}})).ok
    assert effects(ctx) != before
    assert not latest_reply(ctx).in_junk


def test_a_held_email_cannot_send_and_mark_reviewed_lets_the_lead_go_on(make_ctx, monkeypatch, clock, server):
    from aime import workflows

    srv = server({"INBOX": {}, "Junk": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    clock(IN_WINDOW)
    lead = add_lead(ctx, stage="approved")
    fid = add_mail(ctx, lead)
    with ctx.db.session() as s:
        addr = s.get(Lead, lead).contact.email
    srv.folders["Junk"][1] = K.msg(addr, "stop", "Please remove me from your list.", tag="f")
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    state, why = status_of(ctx, fid)
    assert FakeSMTP.sent == [] and state == "approved" and "Held" in why
    assert addr not in suppressed_values(ctx)
    rid = latest_reply(ctx).id
    assert web(ctx).post(f"/inbox/{rid}/reviewed", headers=ORIGIN, data={"category": "other"}, follow_redirects=False).status_code == 303
    assert not workflows.job_inbox(ctx).failed
    assert not [a for a in alerts(ctx) if "forged" in a], "closed once a person has decided"
    workflows.job_followups(ctx)
    assert status_of(ctx, fid)[0] == "sent"


def test_a_person_who_reclassifies_it_as_an_opt_out_applies_it_in_full(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    assert read(ctx, server({"INBOX": {}, "Junk": {1: forged_opt_out(v)}})).ok
    rid = latest_reply(ctx).id
    assert web(ctx).post(f"/inbox/{rid}/reclassify", headers=ORIGIN, data={"category": "unsubscribe"}, follow_redirects=False).status_code == 303
    after = effects(ctx)
    assert v.to in after["suppressed"] and after["stage"] == "unsubscribed" and after["consent"] == "opted_out"
    with ctx.db.session() as s:
        assert not lead_on_hold(s, lead_of(ctx).id)


def test_a_sender_a_person_has_looked_at_does_not_hold_the_lead_again_unless_in_thread(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {}, "Junk": {1: forged_footer_spam(v)}})
    assert read(ctx, srv).ok
    assert web(ctx).post(f"/inbox/{latest_reply(ctx).id}/reviewed", headers=ORIGIN, data={}, follow_redirects=False).status_code == 303
    srv.folders["Junk"][2] = forged_footer_spam(v)
    assert read(ctx, srv).ok
    with ctx.db.session() as s:
        assert not lead_on_hold(s, lead_of(ctx).id), "the same sender's next spam holds nothing"
    assert latest_reply(ctx).status == "needs_review", "and it can still be read"
    srv.folders["Junk"][3] = K.thr(v, K.OPT)
    assert read(ctx, srv).ok
    assert v.to in suppressed_values(ctx), "in our thread it acts"


def test_in_thread_mail_in_junk_acts_at_once(make_ctx, server):
    ctx, _, sent, v = scene(make_ctx)
    assert read(ctx, server({"INBOX": {}, "Junk": {1: K.thr(v, K.OPT)}})).ok
    r = latest_reply(ctx)
    assert r.followup_id == sent.id and r.category == "unsubscribe" and v.to in suppressed_values(ctx)


def test_a_verified_signature_counts_as_proof_in_junk(make_ctx, server, monkeypatch):
    ctx, _, _, v = scene(make_ctx)
    monkeypatch.setattr("aime.dkim_verify.auth_results", lambda raw, resolver=None: f"aime.dkim; dkim=pass header.d={v.dom}")
    assert read(ctx, server({"INBOX": {}, "Junk": {1: forged_opt_out(v)}})).ok
    assert latest_reply(ctx).sender_verified and v.to in suppressed_values(ctx)


def test_a_report_in_junk_quoting_our_id_acts(make_ctx, server):
    ctx, _, sent, v = scene(make_ctx)
    assert read(ctx, server({"INBOX": {}, "Junk": {1: K.BOUNCES["postfix_dsn"](v)}})).ok
    assert latest_reply(ctx).category == "bounce" and v.to in suppressed_values(ctx) and lead_of(ctx).outreach_status == "bounced"


def test_when_handling_fails_nothing_is_done_on_the_word_of_a_forged_email_either(make_ctx, server, monkeypatch):
    import aime.reply_intel as ri

    ctx, _, _, v = scene(make_ctx)
    before = effects(ctx)

    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(ri, "_hold_unproven_in_junk", boom)
    assert read(ctx, server({"INBOX": {}, "Junk": {1: forged_opt_out(v)}})).ok
    assert effects(ctx) == before and latest_reply(ctx).status == "needs_review"


# ============================================================ review: odd answers to the header fetch, through the real imaplib
QUIRKS = ["leading blank line", "only some fields", "only the sender", "quoted string, literal in the structure", "another email's answer first",
          "structure cut short", "structure not closed"]


def known_contact_opt_out(v):
    return K.msg(v.to, "hi there", "Please remove me from your list.", tag="opt")


def report_in_a_wrapper(v):
    ctype, body = K.mp("mixed", [('text/plain; charset="UTF-8"', "fyi"), ('multipart/report; report-type=delivery-status; boundary="B"',
                                 f"--B\r\nContent-Type: message/delivery-status\r\n\r\nFinal-Recipient: rfc822; {v.to}\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n--B--\r\n")], "O")
    return K.msg("Gateway <gw@relay.example>", "fyi", body, ctype=ctype, tag="nest")


@pytest.mark.parametrize("quirk", QUIRKS)
def test_no_odd_answer_lets_a_known_contacts_opt_out_or_a_hidden_report_pass_unread(make_ctx, server, quirk):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {1: news(1), 2: news(2), 3: known_contact_opt_out(v), 4: news(4), 5: report_in_a_wrapper(v)}})
    srv.quirk = quirk
    res = read(ctx, srv)
    assert res.ok and res.output.read_to_end and position(ctx) == 5
    stored = {r.from_email for r in rows(ctx)}
    assert v.to in stored, "the opt-out was passed as unrelated"
    assert v.to in suppressed_values(ctx)
    assert "gw@relay.example" in stored, "the report was passed as unrelated"


# ============================================================ review: a real answer in junk is not lost
def test_an_opt_out_in_junk_the_server_will_not_give_is_kept_and_its_alert_stays(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)

    class Refuses(Server):
        def _fetch(self, conn, uid, items):
            if "HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)" in items:
                return super()._fetch(conn, uid, items)
            if "HEADER.FIELDS" not in items:
                return b""  # an empty answer: OK, and no email
            return super()._fetch(conn, uid, items)

    srv = Refuses({"INBOX": {}, "Junk": {1: K.thr(v, K.OPT), 2: news(2)}})
    try:
        for _ in range(3):
            assert read(ctx, srv).ok
        assert len(rows(ctx)) == 1 and position(ctx, "inbox.state.junk") == 2
        r = latest_reply(ctx)
        assert r.in_junk and r.status == "needs_review" and r.from_email == v.to
        with ctx.db.session() as s:
            assert lead_on_hold(s, lead_of(ctx).id), "its emails wait: it cannot be proven, and it may be an opt-out"
        for _ in range(2):
            assert read(ctx, srv).ok
        assert sum("could not be read" in a for a in alerts(ctx)) == 1, "the alert does not close itself"
    finally:
        srv.stop()


def test_an_email_in_junk_that_breaks_the_parser_is_kept_like_one_in_the_inbox(make_ctx, server, monkeypatch):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {}, "Junk": {1: K.thr(v, K.OPT, mid="<breaks@x.example>"), 2: news(2)}})
    real = ia.parse_message

    def fussy(raw, own=frozenset()):
        if b"<breaks@x.example>" in raw:
            raise RecursionError("deep")
        return real(raw, own)
    monkeypatch.setattr(ia, "parse_message", fussy)
    assert read(ctx, srv).ok
    assert position(ctx, "inbox.state.junk") == 2 and latest_reply(ctx).in_junk and latest_reply(ctx).status == "needs_review"
    assert sum("could not be read" in a for a in alerts(ctx)) == 1


# ============================================================ review: more ways an answer looks
@pytest.mark.parametrize("prefix", ["Re[2]:", "RE(3):", "回复:", "回复：", "答复:", "Odp:", "R:", "Rif:", "YNT:", "Antw:", "Sv:", "Vs:", "Rv:", "Re: Fwd: Odp:"])
def test_every_prefix_on_our_own_subject_is_kept(make_ctx, server, prefix):
    ctx, _, _, v = scene(make_ctx)
    subject = f"{prefix} {v.subj}"
    subject = subject if subject.isascii() else Header(subject, "utf-8").encode()  # as mail programs send it
    srv = server({"INBOX": {1: K.msg("Pat <pat@free-mail.example>", subject, "Who are you?", tag="p")}})
    assert read(ctx, srv).output.stored == 1 and skipped_count(ctx) == 0


@pytest.mark.parametrize("subject", ["回复: nothing of ours", "Odp: nothing of ours", "A question of mine"])
def test_our_quoted_footer_keeps_an_email_whatever_its_subject(make_ctx, server, subject):
    ctx, _, _, v = scene(make_ctx)
    raw = K.msg("Pat <pat@free-mail.example>", subject, "Please stop." + K.q_gmail(v), tag="f")
    srv = server({"INBOX": {1: raw, 2: news(2)}})
    assert read(ctx, srv).output.stored == 1 and srv.body_fetches(1) and not srv.body_fetches(2)
    assert latest_reply(ctx).status == "needs_review"


def test_a_server_that_cannot_search_the_text_leaves_the_header_lines_to_decide_and_is_not_asked_again(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)

    class NoSearch(Server):
        def _answer(self, conn, line):
            if " BODY " in line and "SEARCH" in line.upper():
                conn.sendall(f"{line.split(' ', 1)[0]} BAD no\r\n".encode())
                self.commands.append(line.split(" ", 1)[1])
                return False
            return super()._answer(conn, line)

    srv = NoSearch({"INBOX": {1: news(1), 2: K.thr(v, K.OPT)}})
    try:
        assert read(ctx, srv).ok and v.to in suppressed_values(ctx)
        srv.folders["INBOX"][3] = news(3)
        assert read(ctx, srv).ok and skipped_count(ctx) == 2
        assert sum(" BODY " in c and "SEARCH" in c.upper() for c in srv.commands) == 1
    finally:
        srv.stop()


def test_our_id_at_either_end_of_a_very_long_references_line_is_read():
    filler = " ".join(f"<r{i}@x.example>" for i in range(3000))
    assert len(filler) > 20000
    for line in (f"<ours@us.example> {filler}", f"{filler} <ours@us.example>"):
        assert "<ours@us.example>" in parse_message(K.msg("P <p@x.example>", "Hi", "x", refs=line)).references.split()


@pytest.mark.parametrize("frm,subject", [("<>", "Mail delivery failed: returning message to sender"), ("Postman <post@relay.example>", "Undeliverable: hello"),
                                         ("<>", "Your message")])
def test_a_text_only_failure_notice_without_an_automatic_mark_reaches_a_person(make_ctx, server, frm, subject):
    ctx, _, _, v = scene(make_ctx)
    assert read(ctx, server({"INBOX": {1: K.msg(frm, subject, f"Could not deliver to {v.to}.", tag="b")}})).output.stored == 1
    assert latest_reply(ctx).status == "needs_review" and v.to not in suppressed_values(ctx), "nothing is suppressed on it"


def test_the_longer_prefix_patterns_stay_linear():
    from aime.reply_intel import _bare_subject

    for hostile in ("re[1]:" * 700, "R: " * 1400, "回复:" * 1300, "[x]" * 1300, "re" * 2000 + ":"):
        started = time.perf_counter()
        _bare_subject(hostile)
        ia.REPLY_SUBJECT.match(hostile)
        assert time.perf_counter() - started < 0.2


# ============================================================ review: the small ones
def test_base64_cut_one_past_a_whole_group_is_still_read_as_text():
    words = base64.b64encode(b"Please remove me from your list. " * 30).decode()
    for cut in range(0, 4):
        text = words[:400 + cut]
        raw = K.msg("P <p@x.example>", "Hi", text, cte="base64", tag="b")
        assert parse_message(raw).body_text.startswith("Please remove me"), cut


def test_the_junk_read_stops_at_its_deadline_and_goes_on_next_run(make_ctx, server, monkeypatch):
    import aime.inbox as inbox

    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {}, "Junk": {1: K.thr(v, K.OPT)}})
    monkeypatch.setattr(inbox, "JUNK_SECONDS", 0)
    assert read(ctx, srv).output.read_to_end and v.to not in suppressed_values(ctx)
    monkeypatch.setattr(inbox, "JUNK_SECONDS", 60)
    assert read(ctx, srv).ok and v.to in suppressed_values(ctx)


def test_a_folder_name_with_a_line_break_never_reaches_the_server(make_ctx):
    ctx = make_ctx(**MAIL)
    box = Mailbox(ctx.settings)
    bad = b'Spam\r\nA999 DELETE "INBOX"'
    assert box.junk_folder(SimpleNamespace(list=lambda: ("OK", [(b'(\\Junk) "/" {25}', bad), b""]))) is None
    assert box.junk_folder(SimpleNamespace(list=lambda: ("OK", [b'(\\Junk) "/" "Sp\tam"']))) is None


def test_an_email_whose_header_lines_always_drop_the_connection_is_passed_with_an_alert(make_ctx, server):
    ctx, _, _, v = scene(make_ctx)
    srv = server({"INBOX": {1: news(1), 2: K.thr(v, K.OPT)}})
    srv.drop_head.add(1)
    agent = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory))
    runs = 0
    while v.to not in suppressed_values(ctx):
        runs += 1
        assert runs <= DROP_TRIES, "the position never moved"
        agent.run({})
    assert any("UID 1" in a and "could not be fetched" in a for a in alerts(ctx))
    assert position(ctx) == 2


def test_the_sent_subjects_are_read_once_per_run(make_ctx, server, monkeypatch):
    import aime.reply_intel as ri

    ctx, _, _, v = scene(make_ctx)
    calls = []
    real = ri._sent_subjects
    monkeypatch.setattr(ri, "_sent_subjects", lambda s: (calls.append(1), real(s))[1])
    assert read(ctx, server({"INBOX": {i: news(i) for i in range(1, 40)}})).ok
    assert len(calls) == 1
