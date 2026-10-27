"""Sales mailbox: read-only fetching, parsing, classification, CRM actions, responses, test mode, limits."""

from datetime import timedelta
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime.agents.inbox_agent import InboxAgent
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.inbox import Mailbox, new_text, parse_message
from aime.reply_intel import reply_footer
from aime.integrations.email_smtp import EmailSender
from aime.models import Activity, Alert, Contact, FollowUp, InboundEmail, Lead, Opportunity, Suppression
from aime.reply_intel import classify
from aime.web.app import create_app
from tests.test_company_research import SITE
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_outreach import SENDING, approve_all, drafts, lead_of, researched

ORIGIN = {"origin": "http://testserver"}
DAVID = f"david.chen@{SITE}"
MAIL = {**SENDING, "imap_host": "imap.example", "smtp_username": "sales@us.example", "smtp_password": "x" * 4,
        "calendly_url": "https://calendly.com/dhs/intro"}


class FakeIMAP:
    messages: dict[int, bytes] = {}
    validity = "777"
    readonly = None
    ssl_context = None
    timeout = None

    def __init__(self, host, port, ssl_context=None, timeout=None):
        FakeIMAP.ssl_context, FakeIMAP.timeout = ssl_context, timeout

    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        FakeIMAP.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.validity.encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            crit = " ".join(a for a in args if a)
            lo = int(crit.split()[1].split(":")[0]) if crit.startswith("UID ") else 0
            return "OK", [" ".join(str(u) for u in sorted(self.messages) if u >= lo).encode()]
        uid = int(args[0])
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[uid]), b")"]

    def logout(self):
        pass


def mail(frm, subject, body, in_reply_to=None, msg_id=None, headers=None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "sales@us.example", subject
    m["Message-ID"] = msg_id or f"<{abs(hash((frm, subject, body)))}@mail.example>"
    m["Date"] = "Tue, 27 Oct 2026 16:00:00 +0000"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return bytes(m)


def bounce_for(addr, original_id) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Mail Delivery Subsystem <mailer-daemon@mx.example>", "sales@us.example", \
        "Delivery Status Notification (Failure)"
    m["Message-ID"] = "<bounce-1@mail.example>"
    m.set_content(f"Address not found. Your message wasn't delivered to {addr}.")
    status = EmailMessage()
    status.set_content(f"Reporting-MTA: dns; mx.example\n\nFinal-Recipient: rfc822; {addr}\nAction: failed\nStatus: 5.1.1\n")
    m.add_attachment(f"Final-Recipient: rfc822; {addr}\nAction: failed\nStatus: 5.1.1\n", subtype="plain",
                     filename="details.txt")
    original = EmailMessage()
    original["Message-ID"], original["To"], original["Subject"] = original_id, addr, "Question about manual data entry"
    original.set_content("...")
    m.add_attachment(original)
    return bytes(m)


def contacted(make_ctx, **overrides):
    """A researched lead whose first outreach email went out through the fake SMTP server."""
    ctx = researched(make_ctx, **{**MAIL, **overrides})
    FakeSMTP.sent = []
    FakeIMAP.messages = {}
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    sent = [f for f in drafts(ctx) if f.status == "sent"]
    assert len(sent) == 1
    return ctx, sender, sent[0]


def run_inbox(ctx):
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=FakeIMAP)).run({})
    assert res.ok, res.error
    return res.output


def latest_reply(ctx):
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()


def test_interested_reply_opens_an_opportunity_and_suggests_a_booking(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}",
                                 "Sounds interesting. Could we talk Thursday afternoon?\n\nOn Tue, Oct 27, Sales wrote:\n> Hi David",
                                 in_reply_to=sent.message_id)}
    out = run_inbox(ctx)
    assert FakeIMAP.readonly is True and out.stored == 1 and out.categories == {"interested": 1}
    r = latest_reply(ctx)
    assert r.category == "interested" and r.confidence == "high" and r.status == "actioned" and r.followup_id == sent.id
    assert "On Tue" not in r.new_text and "Thursday" in r.new_text
    assert lead_of(ctx).outreach_status == "interested"
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalar_one().stage == "discovery"
        assert "Interested reply" in s.execute(select(Alert).where(Alert.source == "inbox")).scalar_one().message
        suggestion = s.get(FollowUp, r.suggested_followup_id)
        assert suggestion.kind == "response" and suggestion.status == "awaiting_approval"
        assert "https://calendly.com/dhs/intro" in suggestion.body and suggestion.to_email == DAVID
    TestClient(create_app(ctx)).post(f"/followups/{suggestion.id}/approve", headers=ORIGIN)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(minutes=30)).isoformat()})
    reply = FakeSMTP.sent[-1]
    assert reply["To"] == DAVID and reply["In-Reply-To"] == r.message_id and "calendly" in reply.get_content()


def test_unsubscribe_suppresses_and_stops(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(DAVID, f"Re: {sent.subject}", "Please remove me from your list.", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert r.category == "unsubscribe" and r.status == "actioned" and r.suggested_followup_id is None
    with ctx.db.session() as s:
        assert s.execute(select(Suppression).where(Suppression.value == DAVID)).scalar_one()
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().consent_status == "opted_out"
    assert lead_of(ctx).outreach_status == "unsubscribed"


def test_bounce_marks_the_address_and_stops(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: bounce_for(DAVID, sent.message_id)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert r.category == "bounce" and DAVID in r.bounced_addresses and r.followup_id == sent.id
    with ctx.db.session() as s:
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().email_status == "bounced"
        assert s.execute(select(Suppression).where(Suppression.value == DAVID)).scalar_one()
    assert lead_of(ctx).outreach_status == "bounced"


def test_out_of_office_keeps_the_sequence_and_unmatched_mail_goes_to_review(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {
        1: mail(DAVID, "Automatic reply: Question", "I am out of the office until Monday.", in_reply_to=sent.message_id,
                headers={"Auto-Submitted": "auto-replied"}),
        2: mail("someone@unknown.example", "Hello", "Who are you?"),
        3: mail("sales@us.example", "our own copy", "sent copy"),
    }
    out = run_inbox(ctx)
    assert out.skipped_own == 1 and out.stored == 2
    with ctx.db.session() as s:
        rows = {r.from_email: r for r in s.execute(select(InboundEmail)).scalars()}
    assert rows[DAVID].category == "out_of_office" and rows[DAVID].status == "actioned"
    assert rows["someone@unknown.example"].status == "needs_review"
    assert lead_of(ctx).outreach_status == "contacted"
    FakeIMAP.messages[4] = mail(DAVID, f"Re: {sent.subject}", "Not interested, thanks.", in_reply_to=sent.message_id)
    out = run_inbox(ctx)  # only the new message is fetched
    assert out.fetched == 1 and lead_of(ctx).outreach_status == "not_interested" and lead_of(ctx).status == "nurture"


def test_test_mode_redirects_to_the_owners_address(make_ctx):
    ctx, _, sent = contacted(make_ctx, outbound_test_recipients="owner-test@owner.example")
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "owner-test@owner.example" and msg["Subject"].startswith(f"[TEST for {DAVID}]")
    assert "TEST MODE: this email would have gone to" in msg.get_content()
    assert sent.delivered_to == "owner-test@owner.example" and sent.to_email == DAVID


def test_outreach_limits_hold_emails_until_later(make_ctx):
    ctx = researched(make_ctx, **MAIL)
    ctx.settings.business["outreach"]["daily_send_limit"] = 0
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    FakeSMTP.sent = []
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    approve_all(ctx)
    out = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()}).output
    assert out.emails_sent == 0 and "Daily outreach limit" in out.blocked[0].reason
    assert drafts(ctx)[0].status == "approved"


def test_a_question_needs_your_answer_before_it_can_go(make_ctx):
    ctx, sender, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: mail(DAVID, f"Re: {sent.subject}", "Do you work with Ohio carriers?", in_reply_to=sent.message_id)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert r.category == "question" and r.status == "needs_review"
    client = TestClient(create_app(ctx))
    resp = client.post(f"/followups/{r.suggested_followup_id}/approve", headers=ORIGIN, follow_redirects=False)
    assert "placeholder" in resp.headers["location"]


def test_simulated_reply_is_labelled_and_reclassifying_applies_actions(make_ctx, capsys):
    from argparse import Namespace

    from aime.cli import cmd_simulate_reply
    ctx, _, _ = contacted(make_ctx)
    lead = lead_of(ctx)
    assert cmd_simulate_reply(Namespace(lead=lead.id, text="Hmm, maybe. Who else do you work with?", from_email=None), ctx) == 0
    assert "SIMULATED reply" in capsys.readouterr().out
    r = latest_reply(ctx)
    assert r.is_simulated and r.status == "needs_review"
    client = TestClient(create_app(ctx))
    assert "SIMULATED" in client.get("/inbox").text and "SIMULATED" in client.get("/").text
    client.post(f"/inbox/{r.id}/reclassify", data={"category": "interested"}, headers=ORIGIN)
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalar_one()
        assert s.execute(select(Activity).where(Activity.type == "override")).scalars().first()
    assert lead_of(ctx).outreach_status == "interested"


@pytest.mark.parametrize("text,expected", [
    ("Yes, sounds good. Let's set up a call next week.", ("interested", "high")),
    ("No thanks, we're all set.", ("not_interested", "high")),
    ("Too expensive for us right now, maybe next quarter.", ("objection", "high")),
    ("You should talk to our COO, I'm looping in Priya.", ("referral", "high")),
    ("What industries do you usually work with?", ("question", "medium")),
    ("Unsubscribe", ("unsubscribe", "high")),
    ("This is spam. How did you get my email?", ("unsubscribe", "medium")),
    ("Interested, but honestly not interested until next year.", ("not_interested", "low")),
    ("Thanks.", ("other", "low")),
    ("Not really interested, thanks.", ("other", "low")),
    ("We are not at all interested.", ("other", "low")),
    ("I am not available until January.", ("other", "low")),
    ("Please don't send me more info.", ("other", "low")),
])
def test_classification(text, expected):
    row = InboundEmail(from_email="x@y.example", subject="Re: hello", body_text=text, new_text=text)
    assert classify(row)[:2] == expected


def test_parsing_helpers():
    p = parse_message(mail("Ana <ana@x.example>", "=?utf-8?q?Caf=C3=A9_question?=", "Hello"))
    assert p.from_email == "ana@x.example" and p.subject == "Café question" and not p.is_bounce
    assert new_text("Great, Thursday works.\n\nOn Mon, Bob wrote:\n> earlier") == "Great, Thursday works."
    assert new_text("Sure.\n-----Original Message-----\nFrom: x") == "Sure."
    b = parse_message(bounce_for("gone@x.example", "<orig@us.example>"))
    assert b.is_bounce and "gone@x.example" in b.bounced_addresses and b.bounced_message_id == "<orig@us.example>"


def test_lead_unaffected_when_inbox_not_configured(make_ctx):
    ctx = researched(make_ctx)
    out = InboxAgent(ctx).run({}).output
    assert out.mailbox == "not configured" and out.fetched == 0
    with ctx.db.session() as s:
        assert s.execute(select(Lead)).scalar_one().outreach_status in (None, "drafted")


def test_folded_subjects_and_unbuildable_emails_do_not_break_sending(make_ctx):
    long_subject = "Re: [TEST for someone@example.com] Question about manual data entry at a company with a long name"
    assert "\n" not in parse_message(mail("a@x.example", long_subject, "hi")).subject
    ctx, sender, sent = contacted(make_ctx)
    with ctx.db.session() as s:  # one broken approved email (as an old record could be) and one good one
        for subject in ("Line one\nBcc: injected@evil.example", "A normal subject"):
            s.add(FollowUp(lead_id=sent.lead_id, contact_id=sent.contact_id, company_id=sent.company_id, channel="email",
                           kind="response", step=1, status="approved", approved_at=sent.approved_at, approved_by="x",
                           subject=subject, body="Thanks." + reply_footer(ctx.settings), to_email=DAVID,
                           in_reply_to="<m1@mail.example>"))
        s.add(InboundEmail(message_id="<m1@mail.example>", from_email=DAVID, subject="Re: hi", status="actioned"))
    before = len(FakeSMTP.sent)
    out = SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    assert out.ok
    new = FakeSMTP.sent[before:]
    assert len(new) == 2 and all("\n" not in m["Subject"] for m in new) and all(m["Bcc"] is None for m in new)
    try:
        sender.send("a@x.example\r\nBcc: b@y.example", "s", "b")
        raise AssertionError("an address with a line break must be refused")
    except Exception as exc:  # EmailNotReady
        assert "invalid recipient" in str(exc)


def test_a_reply_below_the_quote_is_never_read_as_our_own_unsubscribe_line(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    quoted = "\n".join("> " + line for line in sent.body.split("\n"))
    header = "On Tue, Oct 27, 2026 at 11:00 AM Sales <sales@us.example> wrote:"
    FakeIMAP.messages = {
        1: mail(DAVID, "Re: " + sent.subject, f"{header}\n{quoted}\n\nYes, let's set up a call next week.\n\nDavid",
                in_reply_to=sent.message_id),
        2: mail(DAVID, "Re: " + sent.subject, f"Sent from my iPhone\n\n{header}\n{quoted}", in_reply_to=sent.message_id),
    }
    run_inbox(ctx)
    with ctx.db.session() as s:
        below, empty = s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()
        assert (below.category, below.confidence, below.status) == ("interested", "medium", "needs_review")
        assert (empty.category, empty.confidence, empty.status) == ("other", "low", "needs_review")
        assert not s.execute(select(Suppression)).first()
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().consent_status != "opted_out"
    assert new_text("Sent from my iPhone\n\nYes please, call me.") == "Yes please, call me."


def delay_report(addr, original_id) -> bytes:
    lines = ["From: Mail Delivery Subsystem <mailer-daemon@mx.example>", "To: sales@us.example",
             "Subject: Delivery Status Notification (Delay)", "Message-ID: <delay-1@mail.example>",
             "Date: Tue, 27 Oct 2026 17:00:00 +0000", "MIME-Version: 1.0",
             'Content-Type: multipart/report; report-type=delivery-status; boundary="B"', "",
             "--B", "Content-Type: text/plain; charset=utf-8", "",
             f"Your message to {addr} has been delayed. Gmail will retry for 46 more hours.", "",
             "--B", "Content-Type: message/delivery-status", "", "Reporting-MTA: dns; mx.example", "",
             f"Final-Recipient: rfc822; {addr}", "Action: delayed", "Status: 4.4.7", "",
             "--B", "Content-Type: text/rfc822-headers", "", f"Message-ID: {original_id}", f"To: {addr}", "",
             "--B--", ""]
    return "\r\n".join(lines).encode()


def test_a_delay_notice_is_not_a_bounce(make_ctx):
    ctx, _, sent = contacted(make_ctx)
    FakeIMAP.messages = {1: delay_report(DAVID, sent.message_id)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert (r.category, r.confidence, r.status, r.bounced_addresses) == ("bounce", "low", "needs_review", [])
    with ctx.db.session() as s:
        assert not s.execute(select(Suppression)).first()
        assert s.execute(select(Contact).where(Contact.email == DAVID)).scalar_one().email_status != "bounced"
    assert lead_of(ctx).outreach_status == "contacted"  # the sequence goes on; the server is still trying


def test_a_plain_text_bounce_names_the_failed_recipient_not_our_own_address(make_ctx):
    ctx, _, sent = contacted(make_ctx, unsubscribe_email="optout@us.example")
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "MAILER-DAEMON@mx.example", "sales@us.example", "failure notice"
    m["Message-ID"], m["Date"] = "<qmail-1@mx.example>", "Tue, 27 Oct 2026 16:05:00 +0000"
    m.set_content("Hi. This is the qmail-send program at mx.example.\n"
                  "I'm afraid I wasn't able to deliver your message to the following addresses.\n"
                  "This is a permanent error; I've given up.\n\n"
                  f"<{DAVID}>:\nSorry, no mailbox here by that name. (#5.1.1)\n\n"
                  "--- Below this line is a copy of the message.\n\n"
                  f"Message-ID: {sent.message_id}\nTo: {DAVID}\n\n{sent.body}\n")
    assert "optout@us.example" in sent.body  # our footer, copied into the bounce
    unknown = EmailMessage()  # a bounce for an address we never emailed
    unknown["From"], unknown["To"], unknown["Subject"] = "MAILER-DAEMON@mx.example", "sales@us.example", "failure notice"
    unknown["Message-ID"] = "<qmail-2@mx.example>"
    unknown.set_content("This is a permanent error.\n\n<stranger@elsewhere.example>:\nUser unknown (#5.1.1)\n")
    FakeIMAP.messages = {1: bytes(m), 2: bytes(unknown)}
    run_inbox(ctx)
    with ctx.db.session() as s:
        ours, theirs = s.execute(select(InboundEmail).order_by(InboundEmail.id)).scalars().all()
        assert ours.bounced_addresses == [DAVID] and ours.status == "actioned"
        assert theirs.bounced_addresses == ["stranger@elsewhere.example"] and theirs.status == "needs_review"
        suppressed = {x.value for x in s.execute(select(Suppression)).scalars()}
        assert DAVID in suppressed and not suppressed & {"optout@us.example", "hello@us.example"}
