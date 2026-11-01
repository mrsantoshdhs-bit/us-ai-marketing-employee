"""Phase 5, reading the mailbox: a junk backlog, an opt-out from another address, a refused sign-in, a subject naming a company we emailed.
Each test states what must hold. Reading goes through the real imaplib against the small server of test_p3_mail. Synthetic data only."""
from __future__ import annotations

import dataclasses
import imaplib
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import aime.inbox as inbox
import aime.timeutil
from aime import mail_accounts as ma
from aime import outreach, reply_intel, workflows
from aime.agents import inbox_agent as ia
from aime.agents.inbox_agent import InboxAgent, addresses_we_wrote_to, names_someone_we_wrote_to, sales_related
from aime.db import kv_get
from aime.inbox import BODY_LIMIT, Mailbox, parse_message
from aime.models import Alert, Company, FollowUp, InboundEmail, Lead, MailAccount, SkippedMail, Suppression
from aime.timeutil import utcnow
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import FakeIMAP
from tests.test_time_and_alerts import IN_WINDOW, MAIL, add_lead, add_mail, mail_world, status_of
from tests.uat import p2_mail_corpus as K
from tests.uat.test_p3_mail import Server

TICK = timedelta(minutes=15)
PERSONAL = "J Person <j.person@private.example>"


class Picky(Server):
    """Answers the search for addresses as it is told to: refuses it, or goes down on it. Everything else as a real server."""

    def __init__(self, *a, **k):
        self.on_addresses = None
        super().__init__(*a, **k)

    def _answer(self, conn, line):
        tag, _, rest = line.partition(" ")
        if self.on_addresses and rest.upper().startswith("UID SEARCH") and "@" in rest:
            self.commands.append(rest)
            if self.on_addresses == "drop":
                return True
            answer = "NO [CANNOT] search not possible" if self.on_addresses == "no" else "BAD too long"
            conn.sendall(f"{tag} {answer}\r\n".encode())
            return False
        return super()._answer(conn, line)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when):
        aime.timeutil.CLOCK_OFFSET = when - aime.timeutil.utcnow() + aime.timeutil.CLOCK_OFFSET
        return when
    return set_to


@pytest.fixture
def server():
    made = []

    def make(folders, **kw):
        s = Picky({name: dict(mails) for name, mails in folders.items()}, **kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.stop()


def we_wrote(ctx, name=None):
    """A lead whose first email went out, with step 2 approved and due. (lead id, thread facts, step 2)"""
    lead_id = add_lead(ctx, stage="approved")
    mid = f"<first-{lead_id}@us.example>"
    add_mail(ctx, lead_id, step=1, status="sent", message_id=mid, sent_at=utcnow() - timedelta(days=4))
    step2 = add_mail(ctx, lead_id, step=2)
    with ctx.db.session() as s:
        lead = s.get(Lead, lead_id)
        if name:
            lead.company.name = name
        v = SimpleNamespace(to=lead.contact.email, subj="Step 1", mid=mid)
    return lead_id, v, step2


def tick(ctx, srv=None):
    out = workflows.job_inbox(ctx)
    workflows.job_followups(ctx)
    if srv is not None:
        srv.assert_read_only()
    return out


def read(ctx, srv):
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=srv.factory)).run({})
    srv.assert_read_only()
    return res


def stored(ctx):
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()


def skipped(ctx):
    with ctx.db.session() as s:
        return list(s.execute(select(SkippedMail.subject)).scalars())


def suppressed(ctx):
    with ctx.db.session() as s:
        return {v.lower() for v in s.execute(select(Suppression.value)).scalars()}


def open_alerts(ctx):
    with ctx.db.session() as s:
        return list(s.execute(select(Alert).where(Alert.resolved_at.is_(None), Alert.acknowledged.is_(False))).scalars())


def state_of(ctx, key="inbox.state"):
    with ctx.db.session() as s:
        return kv_get(s, key) or {}


def address_searches(srv):
    return [c for c in srv.commands if c.upper().startswith("UID SEARCH") and "@" in c]


def footer_searches(srv):
    return [c for c in srv.commands if c.upper().startswith("UID SEARCH") and ia.FOOTER_WORDS in c]


def spam(n, first=1, subject="Offer"):
    return {i: K.msg(f"S <s{i}@spam.example>", f"{subject} {i}", "Buy.", tag="sp") for i in range(first, first + n)}


# ============================================================ replies-4a: a short read of the junk folder holds sending
@pytest.mark.parametrize("backlog", ["130 spam", "11 spam under Re:", "the deadline"])
def test_an_opt_out_in_junk_behind_a_backlog_is_read_before_step_2_can_go(make_ctx, monkeypatch, clock, server, backlog):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}, "Junk": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    _, v, step2 = we_wrote(ctx)
    junk = spam(130) if backlog == "130 spam" else spam(11, subject="Re: pills") if backlog == "11 spam under Re:" else {}
    junk[len(junk) + 1] = K.thr(v, K.OPT)
    srv.folders["Junk"].update(junk)
    if backlog == "the deadline":
        monkeypatch.setattr(inbox, "JUNK_SECONDS", 0)
    tick(ctx, srv)
    assert FakeSMTP.sent == [], "step 2 went out in front of the opt-out"
    assert status_of(ctx, step2)[0] == "approved" and v.to not in suppressed(ctx)
    monkeypatch.setattr(inbox, "JUNK_SECONDS", 60)
    for i in range(1, 4):
        clock(IN_WINDOW + TICK * i)
        tick(ctx, srv)
    assert FakeSMTP.sent == [] and v.to in suppressed(ctx) and status_of(ctx, step2)[0] != "approved"


def test_a_junk_backlog_with_no_opt_out_holds_sending_and_then_lets_it_go(make_ctx, monkeypatch, clock, server):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}, "Junk": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    _, _, step2 = we_wrote(ctx)
    srv.folders["Junk"].update(spam(250))
    tick(ctx, srv)
    assert status_of(ctx, step2)[0] == "approved" and FakeSMTP.sent == []
    clock(IN_WINDOW + TICK)
    tick(ctx, srv)
    assert FakeSMTP.sent == [], "150 still unread after the second run"
    clock(IN_WINDOW + TICK * 2)
    tick(ctx, srv)
    assert status_of(ctx, step2)[0] == "sent", "the hold clears when the folder has been read through"
    assert stored(ctx) == []


def test_a_database_error_while_storing_junk_cannot_lose_the_hold(make_ctx, monkeypatch, clock, server):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}, "Junk": spam(12, subject="Re: pills")})
    ctx = make_ctx(**MAIL)

    def fails(self, session, *a, **k):
        raise OperationalError("insert", {}, Exception("database is locked"))
    monkeypatch.setattr(InboxAgent, "_store", fails)
    res = read(ctx, srv)
    assert res.ok, "the junk folder never fails the run"
    assert not res.output.read_to_end


def test_a_junk_hold_that_lasts_is_said_once_and_closes_when_the_folder_is_read(make_ctx, monkeypatch, clock, server):
    clock(IN_WINDOW)
    monkeypatch.setattr(ia, "JUNK_HOLD_RUNS", 2)
    srv = server({"INBOX": {}, "Junk": spam(250)})
    ctx = make_ctx(**MAIL)
    said = lambda: [a for a in open_alerts(ctx) if "long junk folder" in a.message]  # noqa: E731
    assert read(ctx, srv).ok and said() == []
    assert read(ctx, srv).ok and len(said()) == 1
    assert read(ctx, srv).output.read_to_end, "the last 50 are read to the end"
    assert said() == []


# ============================================================ replies-4b: an opt-out from another address under a new subject
def personal_opt_out(v):
    return K.msg(PERSONAL, "Please stop writing to me", f"Please take {v.to} off your list. You keep writing to my work address.", tag="pers")


def test_an_opt_out_from_a_personal_address_naming_the_address_reaches_a_person(make_ctx, monkeypatch, clock, server):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    _, v, _ = we_wrote(ctx)
    srv.folders["INBOX"][1] = personal_opt_out(v)
    tick(ctx, srv)
    rows = stored(ctx)
    assert len(srv.body_fetches(1)) == 1, "the server's hit must fetch the body"
    assert len(rows) == 1 and rows[0].from_email == "j.person@private.example" and skipped(ctx) == [], "the email is kept, not noted as not sales"
    assert v.to not in suppressed(ctx), "anyone can write someone's address: it is never suppressed by this"


def test_an_opt_out_from_a_personal_address_naming_the_address_lets_no_step_go(make_ctx, monkeypatch, clock, server):
    """Read and kept, the mail holds the lead it names (reply_intel.lead_named_in_body) until a person decides. The named address is not suppressed."""
    clock(IN_WINDOW)
    srv = server({"INBOX": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    lead_id, v, step2 = we_wrote(ctx)
    srv.folders["INBOX"][1] = personal_opt_out(v)
    tick(ctx, srv)
    row = stored(ctx)[0]
    assert (row.category, row.status, row.lead_id, row.holds_sending) == ("unsubscribe", "needs_review", lead_id, True)
    assert FakeSMTP.sent == [] and status_of(ctx, step2)[0] == "approved"
    assert v.to not in suppressed(ctx)


def test_a_stranger_opting_out_and_naming_the_address_holds_the_step_and_a_review_does_not_let_it_go(make_ctx, monkeypatch, clock, server):
    """What the docs say of this mail. It fails safe: no email goes wrongly. Only reading it another way takes the opt-out back."""
    clock(IN_WINDOW)
    srv = server({"INBOX": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    lead_id, v, step2 = we_wrote(ctx)
    srv.folders["INBOX"][1] = K.msg(PERSONAL, "Hello", f"Unsubscribe me. By the way {v.to} gave me your name.", tag="pers3")
    tick(ctx, srv)
    row = stored(ctx)[0]
    assert (row.category, row.status, row.lead_id, row.holds_sending) == ("unsubscribe", "needs_review", lead_id, True)
    assert status_of(ctx, step2)[0] == "approved" and FakeSMTP.sent == []
    assert suppressed(ctx) == {"j.person@private.example"}, "the sender only, never the address named"
    with ctx.db.session() as s:
        assert outreach.company_opted_out(s, s.get(Lead, lead_id).company)
        s.get(InboundEmail, row.id).status = "reviewed"
    with ctx.db.session() as s:
        assert outreach.company_opted_out(s, s.get(Lead, lead_id).company), "reviewed, the mail still counts: it is tied to the lead"
    tick(ctx, srv)
    assert status_of(ctx, step2)[0] == "cancelled" and "opted out" in status_of(ctx, step2)[1], "the send gate stops it, and it stays stopped"
    assert FakeSMTP.sent == []
    with ctx.db.session() as s:
        assert list(s.execute(select(FollowUp.id).where(FollowUp.lead_id == lead_id, FollowUp.status.in_(ia.STEP_WAITING))).scalars()) == [], "nothing takes its place"


@pytest.mark.xfail(not hasattr(reply_intel, "lead_named_in_body"), reason="for the integrator: the hold is replies-acting's lead_named_in_body", strict=True)
def test_other_mail_from_a_personal_address_naming_the_address_holds_step_2(make_ctx, monkeypatch, clock, server):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    _, v, step2 = we_wrote(ctx)
    srv.folders["INBOX"][1] = K.msg(PERSONAL, "A word about your emails", f"You have been writing to {v.to}. Please call me before you write again.", tag="pers2")
    tick(ctx, srv)
    assert FakeSMTP.sent == [] and status_of(ctx, step2)[0] == "approved"
    assert v.to not in suppressed(ctx)


def test_the_address_search_is_a_command_of_its_own_with_every_address(make_ctx, server):
    ctx = make_ctx(**MAIL)
    _, one, _ = we_wrote(ctx)
    _, two, _ = we_wrote(ctx)
    _, three, _ = we_wrote(ctx)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Stop", f"Take {two.to} off.", tag="p2")}})
    assert read(ctx, srv).ok
    asked = address_searches(srv)
    assert len(asked) == 1 and len(footer_searches(srv)) == 1 and ia.FOOTER_WORDS not in asked[0]
    assert all(v.to in asked[0] for v in (one, two, three))
    assert re.search(r'UID 1:1 OR BODY "[^"]+" OR BODY "[^"]+" BODY "[^"]+"$', asked[0]), asked[0]
    assert len(stored(ctx)) == 1


def test_only_a_plain_address_gets_into_the_command():
    bad = ['a"b@x.example', "a b@x.example", "a\\b@x.example", "pé@x.example", "a\r\nb@x.example", "nope", "", inbox.NO_ADDRESS]
    assert Mailbox._address_query(bad) == []
    assert Mailbox._address_query(["a@x.example", 'a"b@x.example', "a@x.example"]) == ["BODY", '"a@x.example"']
    assert Mailbox._address_query(["a@x.example", "b@x.example"]) == ["OR", "BODY", '"a@x.example"', "BODY", '"b@x.example"']


def test_a_refused_address_search_leaves_the_footer_search_working(make_ctx, server):
    ctx = make_ctx(**MAIL)
    _, v, _ = we_wrote(ctx)
    quoted = K.msg(PERSONAL, "Hello there", "Stop this.\n\n> To stop receiving emails from us, reply unsubscribe", tag="q1")
    srv = server({"INBOX": {1: quoted}})
    srv.on_addresses = "refuse"
    assert read(ctx, srv).ok, "a refusal only leaves the header lines to decide"
    state = state_of(ctx)
    assert state.get("no_address_search") is True and not state.get("no_body_search")
    assert len(stored(ctx)) == 1, "the footer search still found the quoted footer"
    said = [a for a in open_alerts(ctx) if "search for the addresses" in a.message]
    assert len(said) == 1, "a protection gone quiet is said"
    srv.folders["INBOX"][2] = K.msg(PERSONAL, "Hello again", "Stop this.\n\n> To stop receiving emails from us, reply unsubscribe", tag="q2")
    srv.commands.clear()
    assert read(ctx, srv).ok
    assert address_searches(srv) == [] and len(footer_searches(srv)) == 1 and len(stored(ctx)) == 2
    assert len([a for a in open_alerts(ctx) if "search for the addresses" in a.message]) == 1, "one keyed alert"


def test_an_address_search_answered_no_is_said_like_any_refusal_and_leaves_the_footer_search_working(make_ctx, server):
    """imaplib raises on BAD only: NO comes back as an answer, and it is how RFC 3501 has a server refuse a search."""
    ctx = make_ctx(**MAIL)
    _, v, _ = we_wrote(ctx)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Please stop writing to me", f"Please take {v.to} off.", tag="no1"),
                            2: K.msg(PERSONAL, "Hello there", "Stop this.\n\n> To stop receiving emails from us, reply unsubscribe", tag="no2")}})
    srv.on_addresses = "no"
    assert read(ctx, srv).ok, "a refusal only leaves the header lines to decide"
    state = state_of(ctx)
    assert state.get("no_address_search") is True and not state.get("no_body_search")
    assert skipped(ctx) == ["Please stop writing to me"] and len(stored(ctx)) == 1, "the footer search still found the quoted footer"
    assert len([a for a in open_alerts(ctx) if "search for the addresses" in a.message]) == 1, "a protection gone quiet is said"
    srv.folders["INBOX"][3] = K.msg(PERSONAL, "Hello again", "Hi.", tag="no3")
    srv.commands.clear()
    assert read(ctx, srv).ok and address_searches(srv) == [] and len(footer_searches(srv)) == 1


def test_a_dropped_address_search_fails_the_read_and_never_switches_off_the_footer_search(make_ctx, server):
    ctx = make_ctx(**MAIL)
    we_wrote(ctx)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello there", "Stop.\n\n> To stop receiving emails from us, reply unsubscribe", tag="q3")}})
    srv.on_addresses = "drop"
    first = read(ctx, srv)
    assert not first.ok and first.attempts == 2, "a dropped connection is a failed read, tried again once"
    assert not state_of(ctx).get("no_body_search") and not state_of(ctx).get("no_address_search"), "one bad connection switches nothing off"
    assert stored(ctx) == []
    srv.on_addresses = None
    assert read(ctx, srv).ok and "address_search_drops" not in state_of(ctx) and len(stored(ctx)) == 1


def test_an_address_search_that_drops_each_time_is_given_up_and_the_mail_still_comes_in(make_ctx, server):
    ctx = make_ctx(**MAIL)
    we_wrote(ctx)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello there", "Stop.\n\n> To stop receiving emails from us, reply unsubscribe", tag="q4")}})
    srv.on_addresses = "drop"
    assert not read(ctx, srv).ok
    second = read(ctx, srv)  # the third drop is this run's first try; its retry no longer asks
    state = state_of(ctx)
    assert second.ok and state.get("no_address_search") is True and not state.get("no_body_search")
    assert len(stored(ctx)) == 1 and any("search for the addresses" in a.message for a in open_alerts(ctx))


@pytest.mark.parametrize("named, keeps", [
    ("{to}", True), ("{TO}", True), ("<{to}>", True), ("mailto:{to}", True), ("Please remove {to}.", True), ("'{to}'", True), ("({to}), thanks", True),
    ("x{to}", False), ("first.{to}", False), ("first+{to}", False), ("first-{to}", False), ("{to}m", False), ("{to}.other.example", False), ("{to}-other.example", False),
])
def test_only_the_whole_address_counts(named, keeps):
    to = "pat@acme.example"
    p = parse_message(K.msg(PERSONAL, "Stop", named.format(to=to, TO=to.upper()), tag="w"))
    assert names_someone_we_wrote_to(p, [to]) is keeps


def test_a_longer_address_holding_ours_is_not_kept(make_ctx, server):
    ctx = make_ctx(**MAIL)
    _, v, _ = we_wrote(ctx)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello", f"Write to x{v.to}, or to {v.to}.other.example", tag="long")}})
    assert read(ctx, srv).ok
    assert len(srv.body_fetches(1)) == 1, "the server cannot tell the difference: it does find it"
    assert stored(ctx) == [] and skipped(ctx) == ["Hello"]


def test_an_address_beyond_what_is_kept_goes_to_a_person(make_ctx, server):
    ctx = make_ctx(**MAIL)
    _, v, _ = we_wrote(ctx)
    long_text = ("Lorem ipsum dolor sit amet. " * 1000)[:BODY_LIMIT + 2000] + f" Take {v.to} off."
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello", long_text, tag="big")}})
    assert read(ctx, srv).ok
    assert len(stored(ctx)) == 1 and skipped(ctx) == []
    assert v.to not in suppressed(ctx)


@pytest.mark.parametrize("sender, extra", [
    ("News <news@letter.example>", ("List-Unsubscribe: <https://u.example/x>",)),
    ("Robot <bot@letter.example>", ("Auto-Submitted: auto-generated",)),
    ("No Reply <no-reply@letter.example>", ()),
])
def test_a_newsletter_or_a_robot_naming_the_address_is_not_kept_for_it(make_ctx, monkeypatch, clock, server, sender, extra):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    _, v, step2 = we_wrote(ctx)
    srv.folders["INBOX"][1] = K.msg(sender, "Your digest", f"This digest went to {v.to}.", extra=extra, tag="dig")
    tick(ctx, srv)
    assert stored(ctx) == [] and skipped(ctx) == ["Your digest"]
    assert status_of(ctx, step2)[0] == "sent", "it holds nothing"


def test_a_limit_on_addresses_never_leaves_out_one_with_an_email_waiting_and_is_said(make_ctx, monkeypatch, server):
    ctx = make_ctx(**MAIL)
    monkeypatch.setattr(ia, "ADDRESS_SEARCH_LIMIT", 1)
    _, oldest, _ = we_wrote(ctx)  # step 2 is waiting
    leads = []
    for _ in range(3):
        lead_id = add_lead(ctx)
        add_mail(ctx, lead_id, step=1, status="sent", message_id=f"<m{lead_id}@us.example>", sent_at=utcnow())
        leads.append(lead_id)
    with ctx.db.session() as s:
        addresses, left_out = addresses_we_wrote_to(s)
    assert oldest.to in addresses and left_out == 3 and len(addresses) == 1
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Stop", f"Take {oldest.to} off.", tag="p9")}})
    assert read(ctx, srv).ok
    assert oldest.to in address_searches(srv)[0] and len(stored(ctx)) == 1
    assert [a for a in open_alerts(ctx) if "leaves out" in a.message]
    monkeypatch.setattr(ia, "ADDRESS_SEARCH_LIMIT", 10)
    assert read(ctx, srv).ok
    assert not [a for a in open_alerts(ctx) if "leaves out" in a.message], "closed when nothing is left out"


def test_our_own_address_is_not_looked_for(make_ctx, server):
    ctx = make_ctx(**MAIL)
    lead_id, v, _ = we_wrote(ctx)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=lead_id, channel="email", kind="outreach", step=9, status="sent", subject="To ourselves", body="x", to_email="hello@us.example", due_at=utcnow(), sent_at=utcnow()))
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello", "As you wrote from hello@us.example, thanks.", tag="own")}})
    assert read(ctx, srv).ok
    assert "hello@us.example" not in address_searches(srv)[0] and v.to in address_searches(srv)[0]
    assert stored(ctx) == [] and skipped(ctx) == ["Hello"]


def test_the_junk_folder_is_not_searched_for_addresses_a_gap_that_is_stated(make_ctx, server):
    ctx = make_ctx(**MAIL)
    _, v, _ = we_wrote(ctx)
    srv = server({"INBOX": {}, "Junk": {1: personal_opt_out(v)}})
    assert read(ctx, srv).ok
    assert address_searches(srv) == [] and stored(ctx) == []


def test_with_every_email_kept_there_is_no_address_search(make_ctx, server):
    ctx = make_ctx(**{**MAIL, "inbox_scope": "all"})
    we_wrote(ctx)
    srv = server({"INBOX": {1: K.msg(PERSONAL, "Hello", "Hi", tag="all")}})
    assert read(ctx, srv).ok
    assert address_searches(srv) == [] and len(stored(ctx)) == 1


# ============================================================ journey-9, owner's decision: a subject naming a company we emailed
def subject_keeps(ctx, subject):
    with ctx.db.session() as s:
        return sales_related(s, parse_message(K.msg(PERSONAL, subject, "Hello", tag="sub")))


@pytest.mark.parametrize("subject, keeps", [
    ("About Northwind Robotics", True), ("northwind robotics: a question", True), ("Re: NORTHWIND ROBOTICS?", True), ("Northwind-Robotics", True),
    ("About Northwind", False), ("About Northwind Roboticsy", False), ("Southnorthwind Robotics", False), ("Nothing to do with it", False),
])
def test_a_whole_name_in_the_subject_keeps_the_mail(make_ctx, subject, keeps):
    ctx = make_ctx(**MAIL)
    we_wrote(ctx, "Northwind Robotics, Inc.")
    assert subject_keeps(ctx, subject) is keeps


@pytest.mark.parametrize("name, subject", [
    ("Focus", "Focus on your goals"), ("Summit Inc", "Summit next week"), ("The Group", "The group call"), ("Global Solutions LLC", "Global solutions for you"),
    ("Labs", "Labs results"), ("AI", "AI news"), ("360", "360 review"), ("Target", "Target reached"),
])
def test_a_common_or_generic_name_keeps_nothing(make_ctx, name, subject):
    ctx = make_ctx(**MAIL)
    we_wrote(ctx, name)
    assert subject_keeps(ctx, subject) is False


def test_only_a_company_a_sent_outreach_email_went_to_counts(make_ctx):
    ctx = make_ctx(**MAIL)
    lead_id = add_lead(ctx)
    add_mail(ctx, lead_id, step=1, status="awaiting_approval")
    with ctx.db.session() as s:
        s.get(Lead, lead_id).company.name = "Northwind Robotics"
        s.add(Company(name="Southwind Robotics", normalized_name="southwind robotics", source="test"))
    assert subject_keeps(ctx, "About Northwind Robotics") is False
    assert subject_keeps(ctx, "About Southwind Robotics") is False


def test_mail_naming_a_company_reaches_a_person_ties_to_no_lead_and_changes_no_gate(make_ctx, monkeypatch, clock, server):
    clock(IN_WINDOW)
    srv = server({"INBOX": {}})
    ctx = mail_world(make_ctx, monkeypatch, imap=srv.factory)
    _, v, step2 = we_wrote(ctx, "Northwind Robotics")
    srv.folders["INBOX"][1] = K.msg(PERSONAL, "Northwind Robotics and your offer", "We may be interested. Can you tell me more?", tag="int")
    tick(ctx, srv)
    rows = stored(ctx)
    assert len(rows) == 1 and rows[0].lead_id is None and rows[0].status == "needs_review", "a person reads it, as any other unmatched mail"
    assert skipped(ctx) == []
    assert status_of(ctx, step2)[0] == "sent", "the step is not held by it"
    assert v.to not in suppressed(ctx)


# ============================================================ pc-8a, pc-8b: a refused sign-in
class Refused:
    tries: list = []
    answer = b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)"

    def __init__(self, host, port, ssl_context=None, timeout=None):
        pass

    def login(self, user, password):
        Refused.tries.append(user)
        raise imaplib.IMAP4.error(Refused.answer)

    def logout(self):
        pass


@pytest.fixture
def refused(monkeypatch):
    monkeypatch.setattr(Refused, "tries", [])
    monkeypatch.setattr(Refused, "answer", b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
    return Refused


def test_a_refused_mailbox_sign_in_is_said_and_not_tried_again_in_the_run(make_ctx, refused):
    ctx = make_ctx(**MAIL)
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Refused)).run({}, job="inbox")
    assert not res.ok
    with ctx.db.session() as s:
        said = list(s.execute(select(Alert).where(Alert.key == "mail:sign_in")).scalars())
    assert len(Refused.tries) == 1, f"{len(Refused.tries)} refused sign-ins in one run"
    assert len(said) == 1 and re.search(r"sign.?in", said[0].message, re.I) and "change the app password on Mailboxes" in said[0].message
    assert said[0].resolved_at is None


@pytest.mark.parametrize("answer", [
    b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)", b"LOGIN failed.", b"[AUTHENTICATIONFAILED] Authentication failed.",
    b"[ALERT] Application-specific password required: https://support.example/x (Failure)", b"AUTHENTICATE failed.",
])
def test_what_servers_say_when_they_refuse_the_password(make_ctx, refused, monkeypatch, answer):
    monkeypatch.setattr(Refused, "answer", answer)
    ctx = make_ctx(**MAIL)
    assert not InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Refused)).run({}).ok
    assert len(Refused.tries) == 1


@pytest.mark.parametrize("answer", [b"[UNAVAILABLE] Temporary authentication failure", b"[UNAVAILABLE] Authentication failed, try again later", b"[SERVERBUG] Internal error"])
def test_a_login_that_fails_for_now_is_tried_again_in_the_run_as_before(make_ctx, refused, monkeypatch, answer):
    monkeypatch.setattr(Refused, "answer", answer)
    ctx = make_ctx(**MAIL)
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Refused)).run({})
    assert not res.ok and res.attempts == 2 and len(Refused.tries) == 2
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == "mail:sign_in")).first() is None
        assert kv_get(s, "inbox.state.refused") is None


def test_after_a_few_refused_ticks_it_is_tried_hourly_and_mail_stays_held(make_ctx, monkeypatch, clock, refused):
    now = clock(IN_WINDOW)
    ctx = mail_world(make_ctx, monkeypatch, imap=Refused)
    fid = add_mail(ctx, add_lead(ctx, stage="approved"))
    for i in range(ia.REFUSED_TICKS + 3):  # three real tries, three ticks waiting
        clock(now + TICK * i)
        assert workflows.job_inbox(ctx).failed, "a mailbox not read is a failed read, also while it waits"
        workflows.job_followups(ctx)
    assert len(Refused.tries) == ia.REFUSED_TICKS, "tried on each of the first ticks only"
    assert status_of(ctx, fid)[0] == "approved" and FakeSMTP.sent == [], "mail stays held"
    clock(now + TICK * (ia.REFUSED_TICKS - 1) + timedelta(hours=1))
    assert workflows.job_inbox(ctx).failed
    assert len(Refused.tries) == ia.REFUSED_TICKS + 1, "one try an hour"
    clock(now + TICK * (ia.REFUSED_TICKS - 1) + timedelta(hours=1, minutes=15))
    assert workflows.job_inbox(ctx).failed
    assert len(Refused.tries) == ia.REFUSED_TICKS + 1
    assert status_of(ctx, fid)[0] == "approved"
    with ctx.db.session() as s:
        assert len(list(s.execute(select(Alert).where(Alert.key == "mail:sign_in", Alert.resolved_at.is_(None))).scalars())) == 1, "one alert, not one a tick"


def wait_out_the_tries(ctx, box_for, clock, when):
    for i in range(ia.REFUSED_TICKS):
        clock(when + TICK * i)
        InboxAgent(ctx, **box_for()).run({})
    before = len(Refused.tries)
    clock(when + TICK * ia.REFUSED_TICKS)
    InboxAgent(ctx, **box_for()).run({})
    assert len(Refused.tries) == before, "it waits now"
    return before


def test_a_changed_password_is_tried_at_once(make_ctx, clock, refused):
    ctx = make_ctx(**MAIL)
    before = wait_out_the_tries(ctx, lambda: {"mailbox": Mailbox(ctx.settings, imap_factory=Refused)}, clock, IN_WINDOW)
    changed = dataclasses.replace(ctx.settings, imap_password=ctx.settings.imap_password * 2)
    InboxAgent(ctx, mailbox=Mailbox(changed, imap_factory=Refused)).run({})
    assert len(Refused.tries) == before + 1


def test_nothing_of_the_password_is_kept_but_a_sign_too_short_to_say_anything(make_ctx, refused):
    ctx = make_ctx(**MAIL)
    InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Refused)).run({})
    seen = state_of(ctx, "inbox.state.refused")
    assert set(seen) == {"count", "sign", "at"} and len(seen["sign"]) == 4


def test_a_mailboxes_test_that_passes_ends_the_wait_on_a_panel_mailbox(make_ctx, clock, refused):
    ctx = make_ctx(**{k: v for k, v in MAIL.items() if k not in ("imap_host", "imap_username", "imap_password")})
    with ctx.db.session() as s:
        account = ma.save(s, "owner", {"provider": "gmail", "label": "one", "email": "one@us.example", "secret": "a" * 16})
        ma.change(s, "owner", account.id, "enable")
        ma.change(s, "owner", account.id, "read_on")
        aid = account.id
    now = clock(IN_WINDOW)
    before = wait_out_the_tries(ctx, lambda: {"imap_factory": Refused}, clock, now)
    with ctx.db.session() as s:
        row = s.get(MailAccount, aid)
        row.last_test_at, row.last_test_ok = utcnow(), False
    InboxAgent(ctx, imap_factory=Refused).run({})
    assert len(Refused.tries) == before, "a test that failed says nothing"
    with ctx.db.session() as s:
        row = s.get(MailAccount, aid)
        row.last_test_at, row.last_test_ok = utcnow() + timedelta(seconds=1), True
    InboxAgent(ctx, imap_factory=Refused).run({})
    assert len(Refused.tries) == before + 1


def test_the_alert_closes_and_the_count_ends_when_the_mailbox_signs_in_again(make_ctx, refused):
    ctx = make_ctx(**MAIL)
    assert not InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Refused)).run({}).ok
    FakeIMAP.messages = {}
    assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({}).ok
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == "mail:sign_in", Alert.resolved_at.is_(None))).first() is None
        assert kv_get(s, "inbox.state.refused") is None


def two_panel_mailboxes(ctx):
    with ctx.db.session() as s:
        for address in ("a@us.example", "b@us.example"):
            account = ma.save(s, "owner", {"provider": "gmail", "label": address, "email": address, "secret": "a" * 16})
            ma.change(s, "owner", account.id, "enable")
            ma.change(s, "owner", account.id, "read_on")


def sign_in_alerts(ctx):
    with ctx.db.session() as s:
        return list(s.execute(select(Alert).where(Alert.key == "mail:sign_in", Alert.resolved_at.is_(None))).scalars())


def test_the_alert_stays_while_another_mailbox_waits_out_its_refusals(make_ctx, clock, refused):
    class Two(FakeIMAP):
        refusing = {"a@us.example"}

        def login(self, user, password):
            if user in Two.refusing:
                return Refused.login(self, user, password)

    FakeIMAP.messages = {}
    ctx = make_ctx(inbox_scope="all")
    two_panel_mailboxes(ctx)
    now = clock(IN_WINDOW)
    for i in range(ia.REFUSED_TICKS - 1):
        clock(now + TICK * i)
        assert not InboxAgent(ctx, imap_factory=Two).run({}).ok
    Two.refusing = {"a@us.example", "b@us.example"}
    clock(now + TICK * (ia.REFUSED_TICKS - 1))
    assert not InboxAgent(ctx, imap_factory=Two).run({}).ok
    Two.refusing = {"a@us.example"}
    asked = Refused.tries.count("a@us.example")
    assert asked == ia.REFUSED_TICKS
    clock(now + TICK * ia.REFUSED_TICKS)
    assert not InboxAgent(ctx, imap_factory=Two).run({}).ok
    assert Refused.tries.count("a@us.example") == asked, "a waits, b signed in again"
    assert len(sign_in_alerts(ctx)) == 1, "mail is still held, so the alert still says why"
    Two.refusing = set()
    clock(now + TICK * (ia.REFUSED_TICKS - 1) + ia.REFUSED_WAIT)
    assert InboxAgent(ctx, imap_factory=Two).run({}).ok
    assert sign_in_alerts(ctx) == [], "closed once the last of them signs in"


def test_the_alert_stays_when_a_mailbox_that_refused_only_dropped_this_time(make_ctx, refused):
    class Two(FakeIMAP):
        trouble = "refused"

        def login(self, user, password):
            if Two.trouble == "refused":
                return Refused.login(self, user, password)
            if user == "a@us.example":
                raise imaplib.IMAP4.abort("connection dropped")

    FakeIMAP.messages = {}
    ctx = make_ctx(inbox_scope="all")
    two_panel_mailboxes(ctx)
    assert not InboxAgent(ctx, imap_factory=Two).run({}).ok
    Two.trouble = "dropped"
    assert not InboxAgent(ctx, imap_factory=Two).run({}).ok
    assert len(sign_in_alerts(ctx)) == 1, "b signed in, but nothing says a's password is good"


def test_a_good_read_does_not_close_a_refusal_only_the_sending_saw(make_ctx):
    """The send path raises this alert too, and only a sent email or a passed test ends that."""
    ctx = make_ctx(**MAIL)
    with ctx.db.session() as s:
        s.add(Alert(level="error", source="sales_workflow", message="refused", key="mail:sign_in"))
    FakeIMAP.messages = {}
    assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({}).ok
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.key == "mail:sign_in", Alert.resolved_at.is_(None))).first() is not None


def test_a_retry_for_another_mailbox_does_not_ask_the_one_that_refused_again(make_ctx, refused):
    class TwoBoxes(FakeIMAP):
        def login(self, user, password):
            if user == "bad@us.example":
                return Refused.login(self, user, password)
            raise imaplib.IMAP4.abort("connection dropped")

    ctx = make_ctx(inbox_scope="all")
    with ctx.db.session() as s:
        for address in ("bad@us.example", "flaky@us.example"):
            account = ma.save(s, "owner", {"provider": "gmail", "label": address, "email": address, "secret": "a" * 16})
            ma.change(s, "owner", account.id, "enable")
            ma.change(s, "owner", account.id, "read_on")
    res = InboxAgent(ctx, imap_factory=TwoBoxes).run({})
    assert not res.ok and res.attempts == 2, "the dropped connection is still tried again"
    assert Refused.tries == ["bad@us.example"]


def test_a_followup_on_the_step_stays_held_on_the_refused_read(make_ctx, monkeypatch, clock, refused):
    clock(IN_WINDOW)
    ctx = mail_world(make_ctx, monkeypatch, imap=Refused)
    _, _, step2 = we_wrote(ctx)
    tick(ctx)
    assert status_of(ctx, step2)[0] == "approved" and FakeSMTP.sent == []
    with ctx.db.session() as s:
        assert s.get(FollowUp, step2).status == "approved"
