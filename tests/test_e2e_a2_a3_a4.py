"""A2 + A3 + A4 together, end to end, against fakes only: a mock contact at a reserved .example domain, a fake IMAP
and SMTP server that only accept OAuth (XOAUTH2), a fake Google sign-in service and a fake Google Calendar. Nothing
leaves the test. A person approves every outbound email and books the meeting; the AI employee does the rest."""

from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import booking
from aime.agents.inbox_agent import InboxAgent
from aime.cli import cmd_automation
from aime.inbox import Mailbox
from aime.integrations.email_smtp import EmailSender
from aime.models import Alert, AuditEvent, FollowUp, InboundEmail, Meeting, MeetingProposal
from aime.scheduler.runner import run_job
from aime.timeutil import utcnow
from aime.web.app import create_app, manager_today
from tests.test_a2_mailbox import GOOGLE, OAuthIMAP, sign_in
from tests.test_a3_sending import OAuthSMTP
from tests.test_a4_meetings import ORIGIN, FakeGoogle
from tests.test_automation import followup, run
from tests.test_inbound_sales import FakeSMTP
from tests.test_inbox import DAVID, contacted, mail


def read_mail(ctx, *messages):
    first = max(OAuthIMAP.messages or {0: None}) + 1  # new mail gets new UIDs: the mailbox remembers what it read
    OAuthIMAP.messages = {**OAuthIMAP.messages, **{first + i: m for i, m in enumerate(messages)}}
    res = InboxAgent(ctx, mailbox=Mailbox(ctx.settings, imap_factory=OAuthIMAP)).run({})
    assert res.ok, res.error
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).order_by(InboundEmail.id.desc())).scalars().first()


def test_reply_to_booked_meeting_with_a_person_at_every_outbound_step(make_ctx, monkeypatch):
    from types import SimpleNamespace
    # first touch already sent (password sign-in, as production today); then the owner switches to OAuth
    ctx, _sender, first = contacted(make_ctx, **GOOGLE, calendar_provider="google", meeting_booking_enabled=True)
    ctx.settings.mail_auth, ctx.settings.smtp_password, ctx.settings.imap_password = "oauth", "", ""
    cal = FakeGoogle()
    ctx.http_transport = httpx.MockTransport(cal)
    sign_in(ctx, purpose="mail")
    sign_in(ctx, purpose="calendar")
    sender = EmailSender(ctx.settings, smtp_factory=OAuthSMTP)
    client = TestClient(create_app(ctx))
    OAuthIMAP.messages = {}

    # A2: the prospect asks for a call; read over OAuth, classified, matched, sentiment and recommendation stored
    ask = read_mail(ctx, mail(f"David Chen <{DAVID}>", f"Re: {first.subject}", "Sounds good. Can we talk Thursday?",
                              in_reply_to=first.message_id, msg_id="<ask@mail.example>"))
    assert (ask.category, ask.intent, ask.sentiment) == ("interested", "meeting_request", "positive")
    assert OAuthIMAP.authenticated[0] == "XOAUTH2" and followup(ctx, first.id).delivery_status == "replied"

    # A4: free times proposed from the calendar, in a reply that waits for a person
    with ctx.db.session() as s:
        p = s.execute(select(MeetingProposal)).scalar_one()
        offer_id, slots = p.offer_followup_id, [datetime.fromisoformat(x) for x in p.slots]
    assert followup(ctx, offer_id).status == "awaiting_approval" and len(slots) == 3 and cal.events == {}
    FakeSMTP.sent, OAuthSMTP.auths = [], []
    run(ctx, sender)
    assert FakeSMTP.sent == []  # nothing goes out before a person approves

    # A3: the person approves; it goes out over OAuth SMTP, inside the window and limits
    client.post(f"/followups/{offer_id}/approve", headers=ORIGIN)
    run(ctx, sender)
    assert [m["To"] for m in FakeSMTP.sent] == [DAVID] and OAuthSMTP.auths[0][0] == "XOAUTH2"
    assert followup(ctx, offer_id).delivery_status == "sent"

    # A2 + A4: the prospect picks a time; the proposal shows as offered; the person books it
    read_mail(ctx, mail(f"David Chen <{DAVID}>", "Re: times", "The second one works for me, thanks.",
                        in_reply_to=followup(ctx, offer_id).message_id, msg_id="<pick@mail.example>"))
    assert followup(ctx, offer_id).delivery_status == "replied"
    with ctx.db.session() as s:
        assert s.get(MeetingProposal, p.id).status == "offered"
    client.post(f"/meetings/proposals/{p.id}/book", data={"slot": "1"}, headers=ORIGIN)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        confirm = s.execute(select(FollowUp).where(FollowUp.subject.like("Confirmed:%"))).scalar_one()
        assert m.starts_at == slots[1] and m.event_id in cal.events and confirm.status == "awaiting_approval"
    client.post(f"/followups/{confirm.id}/approve", headers=ORIGIN)
    run(ctx, sender)
    assert len(FakeSMTP.sent) == 2 and followup(ctx, confirm.id).status == "sent"

    # the day before: the Sales Manager is reminded once; Manager Today shows it all honestly
    with ctx.db.session() as s:
        assert booking.reminders(s, ctx.settings, now=slots[1] - timedelta(hours=3)) == 1
        assert s.execute(select(Alert).where(Alert.source == "meetings")).scalars().first() is not None
        board = {b["name"]: b["status"] for b in manager_today(ctx, s)["board"]}
        view = manager_today(ctx, s)
    assert board["Meeting booking"] == "ACTIVE" and board["Mailbox reading"] == "ACTIVE"
    assert [x.id for x in view["meetings"]] == [m.id]

    # every step is on record
    with ctx.db.session() as s:
        actions = {e.action for e in s.execute(select(AuditEvent)).scalars()}
    assert {"account_connected", "meeting_proposed", "meeting_booked", "email_approve"} <= actions

    # the kill switch from the command line holds sending and booking at once
    assert cmd_automation(SimpleNamespace(action="emergency-stop", reason="end of test"), ctx) == 0
    assert run_job(ctx, "followups")["status"] == "skipped"
    with ctx.db.session() as s:
        assert booking.ready(s, ctx.settings) == (False, "automation is paused or stopped")
    assert utcnow()  # simulated clock still in use
