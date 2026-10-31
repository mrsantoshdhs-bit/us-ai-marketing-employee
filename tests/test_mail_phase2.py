"""What the inbox keeps and tells a person, beyond the Phase 2 defect tests in tests/uat: mail read as unrelated leaves a trace,
a message with no text says what it holds, and an opt-out is never traded away for a kinder reading. Synthetic data only."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

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
from tests.uat.test_p2_mail import scene, vars_for

DAVID = f"david.chen@{SITE}"


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
    assert r.category == "out_of_office" and r.status == "needs_review" and "Ticket confirmation" in r.action_note
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
