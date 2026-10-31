"""What the inbox keeps and tells a person, beyond the Phase 2 defect tests in tests/uat: mail read as unrelated leaves a trace,
a message with no text says what it holds, and an opt-out is never traded away for a kinder reading. Synthetic data only."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.timeutil
from aime.agents.inbox_agent import InboxAgent
from aime.crm_ops import erase_person
from aime.inbox import Mailbox, parse_message
from aime.mail_auth import passing_domains, sender_signed
from aime.models import Alert, FollowUp, InboundEmail, Opportunity, SkippedMail, Suppression
from aime.reply_intel import forget_old_skipped_mail, skipped_mail
from aime.timeutil import utcnow
from aime.web.app import create_app
from tests.test_company_research import SITE
from tests.test_inbox import ORIGIN, FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import lead_of
from tests.uat import p2_mail_corpus as K
from tests.test_automation import run
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_outreach import approve_all, drafts
from tests.uat.test_p2_mail import scene, vars_for

DAVID = f"david.chen@{SITE}"


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(aime.timeutil, "CLOCK_OFFSET", aime.timeutil.CLOCK_OFFSET)

    def set_to(when):
        aime.timeutil.CLOCK_OFFSET = when - datetime.now(timezone.utc).replace(tzinfo=None)
        return when

    return set_to


def deliver(ctx, *raws):
    start = max(FakeIMAP.messages, default=0) + 1
    for i, raw in enumerate(raws):
        FakeIMAP.messages[start + i] = raw
    return run_inbox(ctx)


def suppressed(ctx):
    with ctx.db.session() as s:
        return {x.value for x in s.execute(select(Suppression)).scalars()}


def alerts(ctx):
    with ctx.db.session() as s:
        return [a.message for a in s.execute(select(Alert)).scalars()]


def drafts_to_answer(ctx):
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().all()


def in_thread(sent, text, **kw):
    return mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", text, in_reply_to=sent.message_id, **kw)


# ------------------------------------------------------------------ mail skipped as unrelated
def test_mail_read_as_unrelated_leaves_sender_subject_and_date_for_thirty_days(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    raw = mail("News <news@unrelated.example>", "Offer of the week", "A secret word in the text.", msg_id="<n1@unrelated.example>")
    out = deliver(ctx, raw)
    assert out.skipped_unrelated == 1 and latest_reply(ctx) is None
    FakeIMAP.validity = "778"  # read again after the mailbox is rebuilt: one record, not two
    try:
        deliver(ctx)
    finally:
        FakeIMAP.validity = "777"
    with ctx.db.session() as s:
        [kept] = skipped_mail(s)
        assert (kept.from_email, kept.subject) == ("news@unrelated.example", "Offer of the week")
        assert kept.received_at is not None and not hasattr(kept, "body_text")
        kept.skipped_at = utcnow() - timedelta(days=31)
        s.commit()
        assert skipped_mail(s) == []
        forget_old_skipped_mail(s)
        s.commit()
        assert s.execute(select(SkippedMail)).first() is None


def test_erasing_a_person_also_erases_their_skipped_mail_record(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, mail("News <news@unrelated.example>", "Offer", "x", msg_id="<n2@unrelated.example>"))
    with ctx.db.session() as s:
        erase_person(s, "news@unrelated.example", "test")
        s.commit()
        assert skipped_mail(s) == []


# ------------------------------------------------------------------ a person is told what the mail is
def test_a_message_with_no_text_says_what_it_holds(make_ctx):
    ctx, *_ = scene(make_ctx, K.STRUCTURE["tnef_only"])
    r = latest_reply(ctx)
    assert r.status == "needs_review" and "winmail.dat" in r.action_note and "application/ms-tnef" in r.action_note
    assert r.mail_note and "No readable text" in r.mail_note


@pytest.mark.parametrize("name", ["ics_gmail_invite_in_thread", "ics_only_in_thread"])
def test_a_calendar_invitation_is_labelled_and_not_answered_as_a_question(make_ctx, name):
    ctx, *_ = scene(make_ctx, K.CALENDAR[name])
    r = latest_reply(ctx)
    assert r.mail_kind == "calendar" and r.category == "other" and r.status == "needs_review"
    assert any("calendar" in reason for reason in r.reasons)
    assert drafts_to_answer(ctx) == [] and lead_of(ctx).outreach_status == "replied"


def test_a_ticket_confirmation_goes_to_a_person_and_the_sequence_goes_on(make_ctx):
    ctx, *_ = scene(make_ctx, K.AUTO["zendesk_confirm_thread"])
    r = latest_reply(ctx)
    assert r.category == "out_of_office" and r.status == "needs_review" and "Automatic confirmation" in r.action_note
    assert lead_of(ctx).outreach_status == "contacted"


def test_a_ticket_confirmation_with_no_thread_headers_is_kept_and_tied_to_the_lead(make_ctx):
    ctx, *_ = scene(make_ctx, K.AUTO["freshdesk_confirm_no_thread"])
    r = latest_reply(ctx)
    assert r.status == "needs_review" and r.lead_id == lead_of(ctx).id and lead_of(ctx).outreach_status == "contacted"


def test_a_real_absence_note_with_available_or_a_question_stays_an_absence(make_ctx):
    text = "I am out of the office and will be available again on Monday. Need help? Call the front desk."
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, text, headers={"Auto-Submitted": "auto-replied"}))
    r = latest_reply(ctx)
    assert (r.category, r.confidence, r.status) == ("out_of_office", "high", "actioned")
    assert lead_of(ctx).outreach_status == "contacted"


# ------------------------------------------------------------------ an opt-out is never traded away
def test_a_plain_opt_out_beside_interest_is_honoured_and_a_person_reads_it(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, "Sounds interesting, but please remove me from your list."))
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("unsubscribe", "needs_review") and DAVID in suppressed(ctx)
    assert lead_of(ctx).outreach_status == "unsubscribed"


@pytest.mark.parametrize("line", ["unsubscribe", "Unsubscribe me", "opt out", "I want to unsubscribe", "You can unsubscribe me"])
def test_the_writers_own_words_are_never_read_as_a_footer(make_ctx, line):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, f"Sounds interesting.\n\n{line}\n"))
    assert latest_reply(ctx).category == "unsubscribe" and DAVID in suppressed(ctx)


def test_a_mixed_message_stops_the_sequence_suppresses_nothing_and_is_shown(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, "Yes, tell me more.\n\nAcme Inc\nClick here to unsubscribe from our newsletter.\n"))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and any("footer" in reason for reason in r.reasons)
    assert DAVID not in suppressed(ctx) and lead_of(ctx).outreach_status != "contacted"


def test_an_opt_out_in_an_attached_email_with_no_note_of_their_own_is_still_honoured(make_ctx):
    """A gateway may wrap the sender's mail as an attachment: with nothing else said, those are the words we have."""
    def wrapped(v):
        ctype, body = K.mp("mixed", [("message/rfc822", "From: Pat <p@x.example>\nSubject: x\n\nPlease remove me from your mailing list.\n")], "wrap")
        return K.thr(v, body, ctype=ctype, tag="wrap")
    ctx, *_ = scene(make_ctx, wrapped)
    assert latest_reply(ctx).category == "unsubscribe" and DAVID in suppressed(ctx)


def test_a_sender_with_no_address_in_our_thread_still_stops_the_lead_and_names_no_domain(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)
    deliver(ctx, K.WHO["no_from_address_thread"](v))
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("unsubscribe", "needs_review") and lead_of(ctx).outreach_status == "unsubscribed"
    assert DAVID in suppressed(ctx) and all("@" in value for value in suppressed(ctx))
    assert r.from_email not in suppressed(ctx)


def test_an_unproven_opt_out_with_no_deal_stays_as_simple_as_before(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, mail(f"David <{DAVID}>", "Please stop", "Please stop emailing me."))
    assert latest_reply(ctx).status == "actioned" and DAVID in suppressed(ctx)


def test_a_proven_opt_out_still_closes_an_early_deal(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        lead = lead_of(ctx)
        s.add(Opportunity(lead_id=lead.id, company_id=lead.company_id, stage="discovery", probability=20, name="Deal"))
    deliver(ctx, in_thread(sent, "Please stop emailing me."))
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalars().first().stage == "lost"
    assert latest_reply(ctx).status == "actioned"


# ------------------------------------------------------------------ delivery reports
def test_a_person_can_still_confirm_a_report_as_a_bounce(make_ctx):
    """A stranger's report changes nothing by itself; a person who has checked it may."""
    from tests.uat.test_p2_hostile import forged_report

    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, forged_report("stranger@evil.example", [DAVID]))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and DAVID not in suppressed(ctx)
    client = TestClient(create_app(ctx))
    client.post(f"/inbox/{r.id}/reclassify", data={"category": "bounce"}, headers=ORIGIN)
    assert DAVID in suppressed(ctx) and lead_of(ctx).outreach_status == "bounced"


def test_refusals_about_our_own_sending_raise_one_alert_however_many_arrive(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    v = vars_for(sent)
    deliver(ctx, K.BOUNCES["exim_hourly_limit_dsn"](v), K.BOUNCES["proofpoint_ip_block_in_exim_dsn"](v))
    assert len([a for a in alerts(ctx) if "our own sending" in a]) == 1
    assert DAVID not in suppressed(ctx) and lead_of(ctx).outreach_status == "contacted"


# ------------------------------------------------------------------ who signed it
def test_whether_the_senders_domain_signed_is_recorded(make_ctx, monkeypatch):
    ctx, _, sent = contacted(make_ctx)
    monkeypatch.setattr("aime.dkim_verify.auth_results", lambda raw, resolver=None: f"aime.dkim; dkim=pass header.d={SITE}")
    deliver(ctx, in_thread(sent, "Thanks."), mail("Other <other@elsewhere.example>", f"Re: {sent.subject}", "Thanks.", in_reply_to=sent.message_id))
    with ctx.db.session() as s:
        rows = s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()
        assert [r.sender_verified for r in rows] == [True, False]


@pytest.mark.parametrize("line,expected", [
    ("aime.dkim; dkim=pass header.d=calendly.com", {"calendly.com"}),
    ("mx.example; dkim=pass (good) header.d = calendly.com header.i=@sub.calendly.com; spf=pass smtp.mailfrom=x.example", {"calendly.com", "sub.calendly.com"}),
    ("aime.dkim; dkim=pass header.d=a,header.d=calendly.com,.evil.example", set()),
    ("aime.dkim; dkim=pass (header.d=calendly.com) header.d=evil.example", {"evil.example"}),
    ("aime.dkim; dkim=fail header.d=calendly.com", set()),
    ("aime.dkim; dkim=none", set()),
    (None, set()),
])
def test_a_passing_domain_is_read_only_from_a_whole_property(line, expected):
    assert passing_domains(line) == expected


def test_a_lookalike_domain_is_not_the_senders_signature():
    assert sender_signed("a@sub.acme.example", "aime.dkim; dkim=pass header.d=acme.example")
    assert not sender_signed("a@acme.example", "aime.dkim; dkim=pass header.d=notacme.example")
    assert not sender_signed("acme.example", "aime.dkim; dkim=pass header.d=acme.example")


# ------------------------------------------------------------------ a run that dies
def test_an_email_set_aside_after_killed_runs_is_named_and_kept(make_ctx, monkeypatch):
    ctx, _, sent = contacted(make_ctx)
    killer = mail("Stranger <stranger@evil.example>", "x", "y", msg_id="<kill@evil.example>")
    FakeIMAP.messages = {1: killer, 2: in_thread(sent, "Please remove me from your list.")}
    import aime.agents.inbox_agent as ia

    real = ia.parse_message

    class Killed(BaseException):
        pass

    def dies(raw, own=frozenset()):
        if b"<kill@evil.example>" in raw:
            raise Killed
        return real(raw, own)
    monkeypatch.setattr(ia, "parse_message", dies)
    runs = 0
    while DAVID not in suppressed(ctx):
        runs += 1
        assert runs <= 4, "the opt-out behind it was held back"
        try:
            InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({})
        except Killed:
            pass
    assert any("stranger@evil.example" in a and "could not be read" in a for a in alerts(ctx))
    with ctx.db.session() as s:
        assert s.execute(select(InboundEmail).where(InboundEmail.from_email == "stranger@evil.example")).first()


def test_a_database_failure_does_not_count_against_the_email_being_read(make_ctx, monkeypatch):
    from sqlalchemy.exc import OperationalError

    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: in_thread(sent, "Please remove me from your list.")}
    import aime.agents.inbox_agent as ia

    real = ia.parse_message
    calls = []

    def locked(raw, own=frozenset()):
        calls.append(1)
        if len(calls) <= 5:
            raise OperationalError("insert", {}, Exception("database is locked"))
        return real(raw, own)
    monkeypatch.setattr(ia, "parse_message", locked)
    while len(calls) <= 5:  # each run tries twice
        InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({})
    assert DAVID in suppressed(ctx), "after five database failures the email was still read, not set aside"
    assert not any("could not be read" in a for a in alerts(ctx))


def test_a_stranger_cannot_slow_the_sender_lookup_of_an_unreadable_email():
    from aime.agents.inbox_agent import _sender_of_unreadable
    from aime.inbox import NO_ADDRESS

    assert _sender_of_unreadable(b"Subject: x\r\nfrom: Pat <pat@x.example>\r\n\r\n") == "pat@x.example"
    assert _sender_of_unreadable(b"Subject: x\r\n\r\n") == NO_ADDRESS
    assert _sender_of_unreadable(b"From: " + b"a" * 300_000 + b"\r\n\r\n") == NO_ADDRESS


# ------------------------------------------------------------------ small readings
@pytest.mark.parametrize("html,words", [
    ("<p>a < b</p><p>please stop</p>", "please stop"),
    ("<p>x</p><y please stop", "please stop"),
    ("<blockquote>never closed<div>please stop</div>", "please stop"),
    ("<style>p{}<p>please stop</p>", "please stop"),
    ("<SCRIPT>hidden()</script ><p>please stop</p>", "please stop"),
    ("<!-- hidden --><p>please stop</p>", "please stop"),
])
def test_html_words_survive_broken_markup(html, words):
    raw = f"From: a@b.example\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}".encode()
    body = parse_message(raw).body_text
    assert words in body and "hidden" not in body


def test_a_blockquote_is_a_quote_at_any_depth():
    raw = b"From: a@b.example\r\nContent-Type: text/html\r\n\r\nmine<blockquote>one<blockquote>two</blockquote>three</blockquote>after"
    lines = [line.strip() for line in parse_message(raw).body_text.split("\n") if line.strip()]
    assert lines[0] == "mine" and lines[-1] == "after"
    assert all(line.startswith(">") for line in lines[1:-1]) and len(lines) >= 3


def test_thread_ids_are_read_in_every_valid_form():
    raw = (b"From: a@b.example\r\nMessage-ID: <m@x.example>\r\n\tBcc: z@evil.example\r\nIn-Reply-To: <a@x> (a comment) <b@x>\r\n"
           b"References: <c@x><d@x>\r\n\r\nhi")
    p = parse_message(raw)
    assert p.message_id == "<m@x.example>" and p.in_reply_to == "<a@x>" and p.references.split() == ["<b@x>", "<c@x>", "<d@x>"]


@pytest.mark.parametrize("frm", ["somecompany.example", "undisclosed-recipients:;", "<>", "Pat Doe"])
def test_a_from_line_with_no_address_names_nobody(frm):
    p = parse_message(f"From: {frm}\r\nSubject: x\r\n\r\nhi".encode())
    assert p.from_email == "unknown@unreadable.invalid"


# ================================================================== the independent review's findings
def body_of(text, subject="Re: Question", auto=False, kind=None):
    """How the rules read these words as a reply, with nothing else known."""
    from types import SimpleNamespace

    from aime.inbox import new_text
    from aime.reply_intel import classify

    return classify(SimpleNamespace(new_text=new_text(text), body_text=text, subject=subject, bounced_addresses=[], auto_submitted=auto,
                                    from_email="b@x.example", is_simulated=False, mail_kind=kind, mail_note=None))


# ---- 1. a line that looks like a footer can be the writer's only sentence
@pytest.mark.parametrize("text", [
    "Please unsubscribe from this list.", "This email was sent to the wrong person, remove me from your list.", "Unsubscribe from this list",
    "unsubscribe from the mailing list", "Please unsubscribe from the newsletter", "Please unsubscribe from these mailings.",
    "Where do I click to unsubscribe?", "To unsubscribe, do I just reply?", "You can unsubscribe this address now.",
    "You may unsubscribe john@acme.example from all of this.", "You can opt out this address.", "This message was sent to me by mistake, please remove me.",
    "Please update your email preferences and remove us.", "I tried to click here to unsubscribe but it doesn't work",
])
def test_a_writers_only_sentence_that_looks_like_a_footer_is_an_opt_out(text):
    assert body_of(text)[0] == "unsubscribe"


def test_a_question_above_a_bare_unsubscribe_line_is_honoured_and_read():
    assert body_of("Who are your customers?\n\nJane Doe\nUnsubscribe")[:2] == ("unsubscribe", "medium")
    assert body_of("Can you remove me from your list?")[:2] == ("unsubscribe", "high")


def test_a_footer_alone_counts_but_beside_a_real_signal_it_does_not():
    assert body_of("Thanks,\nJane\n\nClick here to unsubscribe.")[:2] == ("unsubscribe", "medium")
    assert body_of("Yes, tell me more.\n\nClick here to unsubscribe.")[:2] == ("interested", "low")
    assert body_of("Yes, tell me more.\n\nPlease unsubscribe from this list.")[0] == "unsubscribe"


# ---- 2. the read-first gate
def _world(make_ctx, monkeypatch, clock, imap=None):
    from tests.test_time_and_alerts import IN_WINDOW, add_lead, add_mail, mail_world

    ctx = mail_world(make_ctx, monkeypatch, **({"imap": imap} if imap else {}))
    clock(IN_WINDOW)
    return ctx, add_mail(ctx, add_lead(ctx, stage="approved"))


def test_a_backlog_over_one_batch_is_no_read_and_the_email_waits(make_ctx, monkeypatch, clock):
    from aime import workflows
    from tests.test_time_and_alerts import status_of

    ctx, fid = _world(make_ctx, monkeypatch, clock)
    FakeIMAP.messages = {i: mail(f"News <news{i}@unrelated.example>", f"Offer {i}", "Buy.", msg_id=f"<o{i}@u.example>") for i in range(1, 56)}
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    state, why = status_of(ctx, fid)
    assert FakeSMTP.sent == [] and state == "approved" and "more mail is waiting" in why
    assert not workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert status_of(ctx, fid)[0] == "sent"


def test_a_failed_read_clears_an_earlier_good_one_in_the_same_tick(make_ctx, monkeypatch, clock):
    from aime import workflows
    from tests.test_time_and_alerts import DownIMAP, status_of

    ctx, fid = _world(make_ctx, monkeypatch, clock)
    assert not workflows.job_inbox(ctx).failed  # the last good read, just now
    monkeypatch.setattr(workflows, "InboxAgent", lambda c: InboxAgent(c, mailbox=Mailbox(c.settings, imap_factory=DownIMAP)))
    assert workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert FakeSMTP.sent == [] and "could not be read" in status_of(ctx, fid)[1]


def test_a_connection_dropped_on_the_email_holding_the_opt_out_holds_the_send(make_ctx, monkeypatch, clock):
    import imaplib

    from aime import workflows
    from tests.test_time_and_alerts import status_of

    class Drops(FakeIMAP):
        def uid(self, command, *args):
            if command == "FETCH":
                raise imaplib.IMAP4.abort("connection dropped")
            return super().uid(command, *args)

    ctx, fid = _world(make_ctx, monkeypatch, clock, imap=Drops)
    Drops.messages = {1: mail("x@y.example", "Hello", "unsubscribe")}
    assert workflows.job_inbox(ctx).failed
    workflows.job_followups(ctx)
    assert FakeSMTP.sent == [] and status_of(ctx, fid)[0] == "approved"


# ---- 3. a dropped connection says nothing about the email
def test_dropped_connections_never_skip_an_email(make_ctx):
    import imaplib

    ctx, _, sent = contacted(make_ctx)

    class Drops(FakeIMAP):
        down = True

        def uid(self, command, *args):
            if command == "FETCH" and Drops.down:
                raise imaplib.IMAP4.abort("connection dropped")
            return super().uid(command, *args)

    Drops.messages = {1: in_thread(sent, "Please remove me from your list.")}
    for _ in range(5):
        assert not InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Drops)).run({}).ok
    assert DAVID not in suppressed(ctx) and any("drops each time" in a for a in alerts(ctx))
    Drops.down = False
    assert InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=Drops)).run({}).ok
    assert DAVID in suppressed(ctx), "the opt-out was read as soon as the connection held"


def test_an_email_the_server_refuses_for_good_is_named_by_its_headers(make_ctx):
    ctx, _, sent = contacted(make_ctx)

    class BodyRefused(FakeIMAP):
        def uid(self, command, *args):
            if command == "FETCH" and "HEADER" not in args[1]:
                return "NO", [b"too big"]
            return super().uid(command, *args)

    BodyRefused.messages = {1: mail(f"David <{DAVID}>", f"Re: {sent.subject}", "x", msg_id="<big@x.example>")}
    for _ in range(3):
        InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=BodyRefused)).run({})
    assert any(DAVID in a and "could not be fetched" in a for a in alerts(ctx))
    assert lead_of(ctx).outreach_status != "contacted", "a known sender's sequence stops, and a person sees who it was"


# ---- 4 and 10. a stranger under our subject, or a sub-domain, holds; only the company itself stops
def _approved_next_step(ctx, sender):
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=6))
    approve_all(ctx)
    return [f for f in drafts(ctx) if f.status == "approved"]


def test_a_stranger_under_our_subject_holds_the_email_until_a_person_decides(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    deliver(ctx, mail("Cold <cold@vendor.example>", sent.subject, "We sell SEO services. Interested?"))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and r.lead_id == lead_of(ctx).id and r.holds_sending and lead_of(ctx).outreach_status == "contacted"
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).first() is None
    assert drafts_to_answer(ctx) == []
    assert _approved_next_step(ctx, sender)
    before = len(FakeSMTP.sent)
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=6, minutes=30))
    assert len(FakeSMTP.sent) == before, "held while the reply waits for a person"
    assert any("unknown address" in (f.blocked_reason or "") for f in drafts(ctx) if f.status == "approved")
    client = TestClient(create_app(ctx))
    client.post(f"/inbox/{r.id}/reviewed", headers=ORIGIN)
    run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=6, minutes=60))
    assert len(FakeSMTP.sent) == before + 1, "Mark reviewed releases it"


def test_a_stranger_who_really_is_interested_is_settled_by_reclassifying(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, mail("Pat <pat.personal@home.example>", f"Re: {sent.subject}", "Yes, let's talk on Tuesday."))
    r = latest_reply(ctx)
    assert r.holds_sending and drafts_to_answer(ctx) == []
    TestClient(create_app(ctx)).post(f"/inbox/{r.id}/reclassify", data={"category": "interested"}, headers=ORIGIN)
    assert lead_of(ctx).outreach_status == "interested" and not latest_reply(ctx).holds_sending
    assert [f.to_email for f in drafts_to_answer(ctx)] == ["pat.personal@home.example"]


def test_a_strangers_opt_out_under_our_subject_suppresses_only_them_until_a_person_confirms(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, mail("Z <z@stranger.example>", f"Re: {sent.subject}", "unsubscribe"))
    r = latest_reply(ctx)
    assert "z@stranger.example" in suppressed(ctx) and DAVID not in suppressed(ctx)
    assert r.status == "needs_review" and r.holds_sending and lead_of(ctx).outreach_status == "contacted"
    TestClient(create_app(ctx)).post(f"/inbox/{r.id}/reclassify", data={"category": "unsubscribe"}, headers=ORIGIN)
    assert DAVID in suppressed(ctx) and lead_of(ctx).outreach_status == "unsubscribed"


@pytest.mark.parametrize("subject", ["One more {}", "Harbour {}", "Fwd: sent back: {}"])
def test_only_the_whole_subject_counts(make_ctx, subject):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, mail("S <s@other.example>", subject.format(sent.subject), "See attached."))
    assert latest_reply(ctx) is None and lead_of(ctx).outreach_status == "contacted"


def test_the_subject_of_a_reply_we_sent_is_not_ours(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    with ctx.db.session() as s:
        s.add(FollowUp(lead_id=sent.lead_id, company_id=sent.company_id, channel="email", kind="response", step=1, status="sent",
                       subject="Re: Quick question", to_email=DAVID, due_at=utcnow(), body="x"))
    deliver(ctx, mail("S <s@other.example>", "Quick question", "Do you have a minute for our survey?"))
    assert latest_reply(ctx) is None


def test_a_sub_domain_opt_out_holds_and_does_not_stop_the_lead(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, mail(f"News <news@e.{SITE}>", "October news", "Our new release is out.\n\nUnsubscribe"))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and r.holds_sending and lead_of(ctx).outreach_status == "contacted"
    assert DAVID not in suppressed(ctx)


def test_a_newsletter_with_list_headers_from_a_sub_domain_is_not_sales_mail(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, mail(f"News <news@e.{SITE}>", "October news", "Our new release is out.\n\nUnsubscribe",
                      headers={"List-Unsubscribe": "<mailto:u@e.example>"}))
    assert latest_reply(ctx) is None and lead_of(ctx).outreach_status == "contacted"


def test_a_newsletter_footer_with_list_headers_is_nobody_opting_out(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, mail(f"News <news@{SITE}>", "October news", "Our new release is out.\n\nUnsubscribe",
                      headers={"List-Unsubscribe": "<mailto:u@e.example>"}))
    r = latest_reply(ctx)
    assert r.category != "unsubscribe" and f"news@{SITE}" not in suppressed(ctx) and lead_of(ctx).outreach_status == "contacted"


def test_someone_writing_through_a_mailing_list_can_still_opt_out(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, "Please remove me from your list.", headers={"List-Id": "<team.acme.example>"}))
    assert latest_reply(ctx).category == "unsubscribe" and DAVID in suppressed(ctx)


# ---- 5. language rules suppress for good, so only plain wording does
@pytest.mark.parametrize("text", [
    "Sáquenme de una duda: ¿cuánto cuesta?", "Quítame la duda, ¿trabajan con pymes?", "Necesitamos dar de baja empleados automáticamente.",
    "Para dar de baja un proveedor hoy usamos una hoja de cálculo.", "No quiero recibir llamadas, mejor por correo: ¿cuánto cuesta?",
    "No quiero recibir la factura hasta el lunes.", "Trabajamos en Baja California", "Estamos en temporada baja, escríbeme en marzo",
    "Si no me contacten antes del viernes pierdo el presupuesto", "Ich habe keine E-Mail von Ihnen bekommen.",
    "Keine Mails erhalten, schicken Sie das Angebot nochmal.", "Ich habe keine Nachrichten erhalten, bitte nochmal senden.",
    "Wir haben keine Werbung geschaltet.", "Ich möchte mich für das Webinar abmelden und einen Termin machen.",
    "Bitte den Konflikt nicht hier austragen.", "Je vais me retirer du projet en mars.", "mail nahi mila, dobara bhejiye",
    "Email nahi aaya, please resend the proposal", "message nahi mila", "Your email na.", "Please send me mail band details", "The band karo is playing",
])
def test_an_ordinary_sentence_in_another_language_suppresses_nobody_and_a_person_reads_it(text):
    category, confidence, _ = body_of(text)
    assert category != "unsubscribe" and confidence != "high"


@pytest.mark.parametrize("text", [
    "Baja", "BAJA", "Por favor, elimíneme de su lista.", "No me envíen más correos", "Quiero darme de baja", "Por favor no me contacten",
    "Bitte aus dem Verteiler nehmen", "Bitte keine E-Mails mehr", "Keine Werbung", "Abmelden", "Bitte abmelden",
    "Merci de me retirer de votre liste", "Je ne souhaite plus recevoir vos emails", "Désabonnez-moi",
    "mail mat bhejo", "band karo", "email band karo", "list se hatao", "email na bheje",
])
def test_a_plain_opt_out_in_another_language_is_honoured_and_checked(text):
    assert body_of(text)[:2] == ("unsubscribe", "medium")


@pytest.mark.parametrize("footer", [
    "Newsletter abbestellen", "Wenn Sie keine weiteren E-Mails erhalten möchten, klicken Sie hier.", "Si no desea recibir más correos, puede darse de baja aquí.",
    "Para dar de baja su correo haga clic aquí.", "Pour ne plus recevoir nos messages, cliquez ici.",
])
def test_a_footer_in_another_language_does_not_beat_interest(footer):
    assert body_of(f"Yes, interested. What times work for you?\n\nRegards\nJane\n{footer}")[0] == "interested"


# ---- 6. anger at us, not at their spreadsheets
@pytest.mark.parametrize("text", [
    "As I told you on the phone, we're interested. When can we talk?", "This is the third time we've looked at this. Sounds good, let's talk.",
    "It's ridiculous how much time we lose on manual data entry. Sounds interesting, let's talk.",
    "Our team is fed up with the current tool. Tell me more about yours.", "We're annoyed by our vendor. Interested in a call.",
    "I'm sick of spreadsheets, tell me more.", "Unacceptable churn in our support team lately, so yes, interested.",
])
def test_pain_is_not_anger(text):
    assert body_of(text)[0] == "interested"


@pytest.mark.parametrize("text", ["Interesting how you people never listen. This is the third time.", "I am furious with you, how much do I have to say this, interested?",
                                  "Your emails are ridiculous. Interesting approach to annoy people."])
def test_anger_at_us_opens_no_deal(text):
    assert body_of(text)[0] != "interested"


@pytest.mark.parametrize("text", ["How much can you automate for us?", "How much would a pilot be?", "How much?", "How much time would this take?"])
def test_asking_how_much_is_still_interest(text):
    assert body_of(text)[0] == "interested"


# ---- 7 and 12. what a report may do
def _report(sender, final, original_id):
    quoted = f'--b\r\nContent-Type: text/rfc822-headers\r\n\r\nMessage-ID: {original_id}\r\n\r\n' if original_id else ""
    return (f'From: {sender}\r\nTo: sales@us.example\r\nSubject: Delivery Status Notification (Failure)\r\nMessage-ID: <r-{abs(hash(sender + final))}@mx.example>\r\n'
            'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nFailed.\r\n'
            f'--b\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example\r\n\r\nFinal-Recipient: rfc822; {final}\r\n'
            f'Action: failed\r\nStatus: 5.1.1\r\nDiagnostic-Code: smtp; 550 5.1.1 user unknown\r\n\r\n{quoted}--b--\r\n').encode()


def test_a_report_quoting_our_id_but_naming_an_alias_target_stops_and_goes_to_a_person(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, _report("mailer-daemon@mx.example", f"old.person@{SITE}", sent.message_id))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and lead_of(ctx).outreach_status == "stopped" and not suppressed(ctx)


def test_a_postmaster_report_quoting_nothing_stops_but_suppresses_nothing_until_a_person_confirms(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, _report("postmaster@anywhere.example", DAVID, None))
    r = latest_reply(ctx)
    assert r.status == "needs_review" and lead_of(ctx).outreach_status == "stopped" and DAVID not in suppressed(ctx)
    TestClient(create_app(ctx)).post(f"/inbox/{r.id}/reclassify", data={"category": "bounce"}, headers=ORIGIN)
    assert DAVID in suppressed(ctx) and lead_of(ctx).outreach_status == "bounced"


def test_a_report_quoting_nothing_from_anyone_else_changes_nothing(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, _report("bob@evil.example", DAVID, None))
    assert latest_reply(ctx).status == "needs_review" and lead_of(ctx).outreach_status == "contacted" and not suppressed(ctx)


# ---- 8. the hold says why
def test_the_hold_says_when_reading_is_switched_off_or_too_rare(make_ctx, monkeypatch, clock):
    from aime import workflows
    from aime.doctor import run_checks
    from aime.models import JobState

    ctx, _ = _world(make_ctx, monkeypatch, clock)
    ctx.settings.schedule["jobs"]["inbox"]["every_minutes"] = 60
    assert "every 60 minutes" in workflows.mailbox_hold(ctx)
    ctx.settings.schedule["jobs"]["inbox"]["every_minutes"] = 15
    with ctx.db.session() as s:
        s.merge(JobState(job="inbox", consecutive_failures=0, enabled=False))
    assert "switched off" in workflows.mailbox_hold(ctx)
    with ctx.db.session() as s:
        s.get(JobState, "inbox").enabled = True
    assert "could not be read" in workflows.mailbox_hold(ctx)
    assert "could not be read" not in "".join(c["detail"] for c in run_checks(ctx) if c["name"] == "Mailbox read before sending")


def test_the_doctor_flags_an_inbox_job_read_too_rarely(make_ctx):
    from aime.doctor import run_checks

    ctx = make_ctx(outbound_email_enabled=True)
    ctx.settings.schedule["jobs"]["inbox"]["every_minutes"] = 60
    ctx.settings.section("outreach")["b2b_enabled"] = True
    check = next(c for c in run_checks(ctx) if c["name"] == "Mailbox read before sending")
    assert not check["ok"] and "60 minutes" in check["detail"]


# ---- 9. killed on the first email of a new position
def test_a_kill_on_the_first_email_does_not_restart_from_uid_one(make_ctx, monkeypatch):
    from aime.db import kv_get

    ctx, _, _ = contacted(make_ctx)
    FakeIMAP.messages = {480: mail("Stranger <x@evil.example>", "Old mail", "y", msg_id="<kill2@evil.example>")}
    import aime.agents.inbox_agent as ia

    real = ia.parse_message

    class Killed(BaseException):
        pass

    def dies(raw, own=frozenset()):
        if b"<kill2@evil.example>" in raw:
            raise Killed
        return real(raw, own)
    monkeypatch.setattr(ia, "parse_message", dies)
    with pytest.raises(Killed):
        InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({})
    with ctx.db.session() as s:
        assert kv_get(s, "inbox.state")["last_uid"] == 479


# ---- 11. who a 5.7 failure is about
@pytest.mark.parametrize("diag,kind", [
    ("550 5.7.133 RESOLVER.RST.SenderNotAuthenticatedForGroup; sender not authenticated for group", "bounce"),
    ("550 5.7.1 <a@b.example>: Recipient address rejected: Access denied", "bounce"),
    ("550 5.4.1 Recipient address rejected: Access denied", "bounce"),
    ("550 5.7.1 Service unavailable, Client host blocked using Spamhaus", "sending_problem"),
    ("550 5.7.1 Message rejected as spam by Content Filtering", "sending_problem"),
    ("554 5.7.0 Blocked - see https://ipcheck.proofpoint.com/?ip=192.0.2.9", "sending_problem"),
    ("550 5.7.26 Unauthenticated email is not accepted", "sending_problem"),
    ("550 5.1.1 no such user (spam filter gateway)", "bounce"),
])
def test_a_refusal_is_about_us_or_about_the_recipient(diag, kind):
    status = diag.split()[1].encode()
    raw = _report("mailer-daemon@mx.example", DAVID, "<x@us.example>").replace(b"550 5.1.1 user unknown", diag.encode())
    raw = raw.replace(b"Status: 5.1.1", b"Status: " + status)
    assert parse_message(raw).kind == kind


# ---- 13. a robot's greeting
@pytest.mark.parametrize("text", ["Do you need urgent help? Call our hotline.", "Thanks for contacting Acme support! How can we help?",
                                  "Our office hours are 9-5. We are available Monday through Friday.",
                                  "Thank you for contacting us. Interested in a demo? Book a time on our site."])
def test_a_robots_greeting_does_not_stop_the_sequence(make_ctx, text):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, text, headers={"Auto-Submitted": "auto-replied"}))
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("out_of_office", "needs_review") and lead_of(ctx).outreach_status == "contacted"


def test_someone_answering_through_an_automatic_mark_still_stops_it(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, in_thread(sent, "Hi, yes, interested, let's talk.", headers={"Auto-Submitted": "auto-replied"}))
    assert latest_reply(ctx).status == "needs_review" and lead_of(ctx).outreach_status != "contacted"


# ---- opt-outs that were never honoured
@pytest.mark.parametrize("text", [
    "Do not email this address.", "Please cease all communications.", "Please don't follow up.", "Refrain from contacting me.", "I never signed up for this.",
    "Cancel my subscription.", "Lose my email.", "Who gave you my address? Remove it.", "Please do not contact anyone at our company again",
])
def test_these_are_opt_outs_too(text):
    assert body_of(text)[0] == "unsubscribe"


@pytest.mark.parametrize("text", ["Don't follow up on Monday, I'm away. Tuesday works.", "Please stop by our booth.", "No need to follow up on the invoice, it's paid.",
                                  "We never signed the contract, what is your pricing?"])
def test_these_are_not(text):
    assert body_of(text)[0] != "unsubscribe"


def test_a_person_writing_from_postmaster_is_a_person(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    deliver(ctx, mail(f"Admin <postmaster@{SITE}>", f"Re: {sent.subject}", "Please remove us from your list.", in_reply_to=sent.message_id))
    r = latest_reply(ctx)
    assert r.category == "unsubscribe" and DAVID in suppressed(ctx) and lead_of(ctx).outreach_status == "unsubscribed"


def test_our_unsubscribe_link_from_an_unknown_alias_is_kept_and_shown(make_ctx):
    ctx, _, _ = contacted(make_ctx)
    deliver(ctx, mail("Alias <alias@personal.example>", "unsubscribe", ""))
    r = latest_reply(ctx)
    assert (r.category, r.status) == ("unsubscribe", "needs_review") and "alias@personal.example" in suppressed(ctx)


# ---- Microsoft 365 commonly leaves To out of its signature
def _signed_to_our_mailbox(frm, domain, signed):
    from tests.test_dkim import sign

    raw = (f"From: {frm}\r\nTo: sales@us.example\r\nSubject: Hello\r\nMessage-ID: <d@x.example>\r\n\r\nhello\r\n").encode()
    return sign(raw, domain, signed_headers=signed)


def test_a_signature_without_to_proves_the_sender_but_not_a_direct_calendly_notice(make_ctx):
    import aime.dkim_verify as dv
    from aime.mail_auth import calendly_verified
    from tests.test_dkim import RSA_KEY, dns_record

    resolver = lambda name: dns_record(RSA_KEY)  # noqa: E731
    line = dv.auth_results(_signed_to_our_mailbox("a@acme.example", "acme.example", ("from", "subject")), resolver)
    assert "header.d=acme.example" in line and "header.to=unsigned" in line and sender_signed("a@acme.example", line)
    ctx, _, _ = contacted(make_ctx)
    with ctx.db.session() as s:
        direct = dv.auth_results(_signed_to_our_mailbox("n@calendly.com", "calendly.com", ("from", "subject")), resolver)
        assert not calendly_verified(s, ctx.settings, "n@calendly.com", direct, to_email="sales@us.example")[0]
        full = dv.auth_results(_signed_to_our_mailbox("n@calendly.com", "calendly.com", ("from", "subject", "to")), resolver)
        assert calendly_verified(s, ctx.settings, "n@calendly.com", full, to_email="sales@us.example")[0]
        ok, why = calendly_verified(s, ctx.settings, "n@calendly.com", full, to_email="")
        assert not ok and "no To line" in why
        own = dv.auth_results(_signed_to_our_mailbox("sales@us.example", "us.example", ("from", "subject")), resolver)
        assert calendly_verified(s, ctx.settings, "sales@us.example", own, to_email="sales@us.example")[0], "a forwarder needs no To"
