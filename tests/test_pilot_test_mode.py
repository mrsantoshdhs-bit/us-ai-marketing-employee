"""A pilot in test mode (OUTBOUND_TEST_RECIPIENTS): every lead's email goes to the owner's own test address and the
owner answers from it. One lead's unsubscribe must not put the shared test address on the do-not-contact list
(that cancelled every later test email to every other lead), test subjects carry exactly one "[TEST for ...]"
marker, and an approval given while the dashboard shows sending on is not asked to "Confirm again" just because
the follow-ups job had not run since the switch. Fake LinkedIn, SMTP and IMAP servers only."""

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.integrations.email_smtp import EmailSender, marked_subject
from aime.models import Contact, FollowUp, InboundEmail, Lead, Opportunity, Suppression
from aime.timeutil import utcnow
from aime.web.app import create_app
from aime.workflows import job_inbound
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import FakeIMAP, mail, run_inbox
from tests.test_linkedin_leads import OURS, SENDING, FakeLinkedIn, leads_ctx, ms
from tests.test_outreach import approve_all

ORIGIN = {"origin": "http://testserver"}
OWNER = "owner-test@owner.example"
ANN, BEN = "ann.lead@brightpath-realty.example", "ben.lead@acme-devshop.example"
MAILBOX = dict(imap_host="imap.example", smtp_username="sales@us.example", smtp_password="x" * 4,
               calendly_url="https://calendly.com/dhs/intro")


def form_lead(lead_id, first, email, company, state):
    answers = [(1, first), (2, "Lead"), (3, email), (4, company), (5, "Operations Director"), (6, "United States"),
               (7, state), (9, "11-50"), (10, "We want a custom CRM with automatic follow-up reminders")]
    return {"id": lead_id, "owner": {"organization": OURS}, "leadType": "EVENT", "testLead": False,
            "submittedAt": ms(utcnow() - timedelta(days=1)),
            "versionedLeadGenFormUrn": "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,1)",
            "formResponse": {"answers": [{"questionId": q, "answerDetails": {"textQuestionAnswer": {"answer": a}}}
                                         for q, a in answers],
                             "consentResponses": [{"consentId": 4, "accepted": True}]}}


def pilot(make_ctx, test_recipients=OWNER):
    """Two LinkedIn leads at different companies, their first emails approved and sent (in test mode by default)."""
    fake = FakeLinkedIn([form_lead("t1", "Ann", ANN, "Brightpath Realty Group", "TX"),
                         form_lead("t2", "Ben", BEN, "Acme Dev Shop", "CA")])
    ctx = leads_ctx(make_ctx, fake, **SENDING, **MAILBOX, outbound_test_recipients=test_recipients)
    job_inbound(ctx)
    FakeSMTP.sent, FakeIMAP.messages = [], {}
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})  # sending is on from here
    approve_all(ctx)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})
    return ctx, sender


def first_email(ctx, address):
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).join(Contact, FollowUp.contact_id == Contact.id).where(
            Contact.email == address, FollowUp.step == 1)).scalar_one()


def reply(ctx, uid, frm, to_email, text):
    FakeIMAP.messages[uid] = mail(frm, "Re: your email", text,
                                  in_reply_to=to_email.message_id, msg_id=f"<reply-{uid}@owner.example>")
    run_inbox(ctx)


def test_a_test_mode_unsubscribe_suppresses_only_that_lead_and_the_other_leads_still_get_their_emails(make_ctx):
    ctx, sender = pilot(make_ctx)
    assert [m["To"] for m in FakeSMTP.sent] == [OWNER, OWNER]
    assert {m["Subject"].split("]")[0] for m in FakeSMTP.sent} == {f"[TEST for {ANN}", f"[TEST for {BEN}"}
    ann, ben = first_email(ctx, ANN), first_email(ctx, BEN)
    reply(ctx, 1, f"Owner <{OWNER}>", ann, "Please unsubscribe me.")
    reply(ctx, 2, f"Owner <{OWNER}>", ben, "Sounds good, can we talk Thursday?")
    with ctx.db.session() as s:
        suppressed = {x.value for x in s.execute(select(Suppression)).scalars()}
        assert ANN in suppressed and OWNER not in suppressed  # the lead, never the shared test address
        assert s.execute(select(Contact).where(Contact.email == ANN)).scalar_one().consent_status == "opted_out"
        unsub = s.execute(select(InboundEmail).where(InboundEmail.message_id == "<reply-1@owner.example>")).scalar_one()
        assert unsub.category == "unsubscribe" and "stays deliverable" in unsub.action_note
        ben_lead = s.execute(select(Lead).join(Contact, Lead.contact_id == Contact.id).where(Contact.email == BEN)).scalar_one()
        assert s.execute(select(Opportunity).where(Opportunity.lead_id == ben_lead.id)).scalar_one() is not None
        answer = s.execute(select(FollowUp).where(FollowUp.purpose == "booking_link")).scalar_one()
        assert answer.to_email == OWNER and answer.status == "awaiting_approval"
        answer.status, answer.approved_by, answer.approved_at = "approved", "owner", utcnow()
        answer_id = answer.id
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(minutes=5)).isoformat()})
    with ctx.db.session() as s:  # Ben's answer goes out: the test address was not suppressed by Ann's opt-out
        assert s.get(FollowUp, answer_id).status == "sent"
    assert len(FakeSMTP.sent) == 3 and FakeSMTP.sent[-1]["To"] == OWNER
    # Ann's sequence is over; Ben's continues (he asked for a meeting, so it stops for the meeting, not for Ann)
    later = TUESDAY_11AM_ET + timedelta(days=4)
    with ctx.db.session() as s:
        for f in s.execute(select(FollowUp).where(FollowUp.status == "sent")).scalars():
            f.sent_at = f.sent_at - timedelta(days=4)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": later.isoformat()})
    with ctx.db.session() as s:
        ann_contact = s.execute(select(Contact).where(Contact.email == ANN)).scalar_one()
        assert not s.execute(select(FollowUp).where(FollowUp.contact_id == ann_contact.id, FollowUp.step == 2)).first()


def test_outside_test_mode_an_opt_out_suppresses_the_sender_and_the_address_we_wrote_to(make_ctx):
    ctx, sender = pilot(make_ctx)
    ann = first_email(ctx, ANN)
    reply(ctx, 1, "Colleague <colleague@brightpath-realty.example>", ann, "Please unsubscribe us.")
    with ctx.db.session() as s:  # a sender who is not a test address: production behaviour, both are suppressed
        suppressed = {x.value for x in s.execute(select(Suppression)).scalars()}
        assert {"colleague@brightpath-realty.example", ANN} <= suppressed and OWNER not in suppressed
    ctx2, _ = pilot(make_ctx, test_recipients="")
    with ctx2.db.session() as s:  # and without test mode the first emails went to the leads themselves
        assert {m["To"] for m in FakeSMTP.sent} == {ANN, BEN}


def test_an_unmatched_opt_out_from_the_test_address_suppresses_nothing_and_asks_a_person(make_ctx):
    ctx, _sender = pilot(make_ctx)
    ctx.settings.inbox_scope = "all"  # a dedicated sales mailbox keeps every email, so this one is read and kept
    FakeIMAP.messages[1] = mail(f"Owner <{OWNER}>", "unsubscribe", "unsubscribe", msg_id="<loose@owner.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail).where(InboundEmail.message_id == "<loose@owner.example>")).scalar_one()
        assert row.category == "unsubscribe" and row.status == "needs_review" and "matches no lead" in row.action_note
        assert s.execute(select(Suppression)).first() is None  # not the test address; no lead to suppress


def test_test_mode_subjects_carry_exactly_one_marker():
    a, b = "a@lead.example", "b@lead.example"
    assert marked_subject("Hello", a) == f"[TEST for {a}] Hello"
    assert marked_subject(f"Re: [TEST for {a}] Hello", a) == f"Re: [TEST for {a}] Hello"  # threaded reply: unchanged
    assert marked_subject(f"Re: [TEST for {a.upper()}] Hello", a) == f"Re: [TEST for {a.upper()}] Hello"
    assert marked_subject(f"[TEST for {a}] Re: [TEST for {a}] Hello", a) == f"[TEST for {a}] Re: Hello"
    assert marked_subject(f"Re: [TEST for {b}] Hello", a) == f"[TEST for {a}] Re: Hello"  # another lead's marker


def test_a_redirected_email_never_gets_a_second_marker(make_ctx):
    ctx = make_ctx(**SENDING, outbound_test_recipients=OWNER)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    sender.send(ANN, f"Re: [TEST for {BEN}] [TEST for {ANN}] Following up", "Hi")
    sender.send(OWNER, f"Re: [TEST for {ANN}] Thanks", "Hi")  # to the test address itself: left as it is
    assert [m["Subject"] for m in FakeSMTP.sent] == [f"[TEST for {ANN}] Re: Following up", f"Re: [TEST for {ANN}] Thanks"]
    assert [m["To"] for m in FakeSMTP.sent] == [OWNER, OWNER]


def test_an_approval_given_while_the_dashboard_shows_sending_on_needs_no_second_confirmation(make_ctx):
    fake = FakeLinkedIn([form_lead("t1", "Ann", ANN, "Brightpath Realty Group", "TX"),
                         form_lead("t2", "Ben", BEN, "Acme Dev Shop", "CA")])
    ctx = leads_ctx(make_ctx, fake, **SENDING, **MAILBOX, outbound_test_recipients=OWNER)
    ctx.settings.outbound_email_enabled = False
    job_inbound(ctx)
    FakeSMTP.sent = []
    sender = EmailSender(ctx.settings, smtp_factory=FakeSMTP)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": TUESDAY_11AM_ET.isoformat()})  # the job sees sending off
    client = TestClient(create_app(ctx))
    ann, ben = first_email(ctx, ANN), first_email(ctx, BEN)
    client.post(f"/followups/{ann.id}/approve", headers=ORIGIN)  # approved while the dashboard shows sending off
    ctx.settings.outbound_email_enabled = True  # .env switched on and the dashboard restarted: it shows sending on
    client.post(f"/followups/{ben.id}/approve", headers=ORIGIN)  # before the follow-ups job has run again
    with ctx.db.session() as s:
        assert am.needs_reconfirmation(s, s.get(FollowUp, ann.id))  # approved while off: confirm again (unchanged)
        assert not am.needs_reconfirmation(s, s.get(FollowUp, ben.id))  # approved while on: goes out as approved
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(hours=1)).isoformat()})
    with ctx.db.session() as s:
        assert s.get(FollowUp, ben.id).status == "sent" and s.get(FollowUp, ann.id).status == "approved"
        assert "Confirm" in (s.get(FollowUp, ann.id).blocked_reason or "") or am.needs_reconfirmation(s, s.get(FollowUp, ann.id))
    assert [m["Subject"].split("]")[0] for m in FakeSMTP.sent] == [f"[TEST for {BEN}"]
