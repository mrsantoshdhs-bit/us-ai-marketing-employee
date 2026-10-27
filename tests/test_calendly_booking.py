"""The 30-minute Calendly link end to end: a personal booking link in the prospect's thread after approval; bookings
in Calendly's host layout (start time only, in the host's time zone); the same notice twice; double bookings;
reschedules reported either way; a late cancellation of a former time; cancellations; Calendly newsletters."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

import aime.meetings as meetings
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.integrations.email_smtp import EmailSender
from aime.meetings import booking_minutes, parse_calendly
from aime.models import Alert, FollowUp, InboundEmail, Lead, Meeting, Opportunity
from aime.web.app import create_app
from tests.test_company_research import SITE
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import MAIL, FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.test_outreach import lead_of

LINK = "https://calendly.com/owner-example/30min"
DAVID = f"david.chen@{SITE}"
ORIGIN = {"origin": "http://testserver"}
NOW = datetime(2026, 10, 27, 12, 0)
THU = ("10:00am", "Thursday, October 29, 2026", "Thu, Oct 29, 2026")


def notice_text(kind: str, when=THU, extra: str = "") -> tuple[str, str]:
    """Calendly's host email: the start time only, in the host's time zone (India for this account)."""
    start, day, short = when
    head = {"New Event": "A new event has been scheduled.", "Canceled": "The event below has been canceled."}[kind]
    body = (f"Hi Santosh,\n\n{head}\n\nEvent Type:\n30 Minute Meeting\n\nInvitee:\nDavid Chen\n\nInvitee Email:\n{DAVID}\n\n"
            f"Event Date/Time:\n{start} - {day} (India Standard Time)\n\n{extra}Location:\nThis is a Google Meet web "
            "conference.\n\nInvitee Time Zone:\nEastern Time - US & Canada\n\nView event in Calendly")
    return f"{kind}: David Chen - {start} {short} - 30 Minute Meeting", body


def notice(kind: str, when=THU, extra: str = "", copy: str = "") -> bytes:
    subject, body = notice_text(kind, when, extra)
    return mail("Calendly <notifications@calendly.com>", subject, body,
                msg_id=f"<{abs(hash((subject, extra, copy)))}@calendly.example>")


def interested(sent, text="Sound interesting, can we schedule meeting this Thursday?"):
    return mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", text, in_reply_to=sent.message_id,
                headers={"References": sent.message_id})  # as mail programs thread a reply


def test_parse_the_host_layout_with_the_start_time_only():
    subject, body = notice_text("New Event")
    info = parse_calendly(subject, body, set())
    assert info["kind"] == "booked" and info["invitee_email"] == DAVID and info["invitee_name"] == "David Chen"
    assert info["event"] == "30 Minute Meeting" and info["tz"] == "Asia/Kolkata"
    assert (info["starts_at"], info["ends_at"]) == (datetime(2026, 10, 29, 4, 30), datetime(2026, 10, 29, 5, 0))  # IST
    assert parse_calendly(f"Fwd: {subject}", body, set())["starts_at"] == info["starts_at"]  # forwarded to the mailbox
    # a date/time field it cannot read: the subject's start, in the zone the field names
    odd = body.replace("10:00am - Thursday, October 29, 2026 (India Standard Time)", "see Calendly (India Standard Time)")
    assert parse_calendly(subject, odd, set())["starts_at"] == info["starts_at"]
    moved = parse_calendly(*notice_text("New Event", ("2:00pm", "Friday, October 30, 2026", "Fri, Oct 30, 2026"),
                                        "Former Event Date/Time:\n10:00am - Thursday, October 29, 2026 (India Standard Time)\n\n"),
                           set())
    assert moved["kind"] == "rescheduled" and moved["previous_starts_at"] == info["starts_at"]
    assert moved["starts_at"] == datetime(2026, 10, 30, 8, 30)


# The layout of a real host notice from the owner's account (27 Oct 2026), names, addresses and the meeting link
# replaced: the start time only, the date day first, the account's time zone.
REAL_SUBJECT = "New Event: Ana Lima - 09:00am Wed, 28 Oct 2026 - 30 Minute Meeting"
REAL_BODY = ("  \n   Calendly \n\n \n    Hi Owner,\n\n\n\n \n  A new event has been scheduled.\n\n\n\n \n   \n    Event Type:\n"
             "   \n   \n  30 Minute Meeting\n\n\n\n \n   \n    Invitee:\n   \n   \n  Ana Lima\n\n\n\n \n   \n    Invitee Email:\n"
             "   \n   \n   ana@x.example \n\n\n\n\n\n \n   \n    Event Date/Time:\n   \n   \n  09:00am - Wednesday, 28 October "
             "2026 (India Standard Time)\n\n\n\n\n\n \n   \n    Location: \n   \n   \n  This is a Zoom web conference.\n\n"
             "       Attendees can join this meeting from a computer, tablet or smartphone.  \n       https://zoom.example/j/1\n"
             "\n \n   \n    Invitee Time Zone:\n   \n   \n    India Standard Time\n\n\n\n\n\n Questions:\n\n \n \nPlease share "
             "anything that will help prepare for our meeting.\n \n \n\n\n This is test \n\n\n\n \n View event in Calendly \n"
             "\n\n\n\n\n\n \n \nPro Tip!\n \nCheck multiple calendars for availability\n \n Sent from  Calendly \n")


def test_parse_the_real_notice_layout_with_the_date_day_first():
    info = parse_calendly(REAL_SUBJECT, REAL_BODY, {"owner@x.example"})
    assert (info["kind"], info["invitee_name"], info["invitee_email"], info["event"], info["tz"]) == \
        ("booked", "Ana Lima", "ana@x.example", "30 Minute Meeting", "Asia/Kolkata")
    assert (info["starts_at"], info["ends_at"]) == (datetime(2026, 10, 28, 3, 30), datetime(2026, 10, 28, 4, 0))
    cancelled = parse_calendly(REAL_SUBJECT.replace("New Event", "Canceled"),
                               REAL_BODY.replace("A new event has been scheduled.", "The event below has been canceled."),
                               set())
    assert cancelled["kind"] == "cancelled" and cancelled["starts_at"] == info["starts_at"]
    # the subject alone, also day first, when the body's date cannot be read
    odd = REAL_BODY.replace("Wednesday, 28 October 2026", "see Calendly")
    assert parse_calendly(REAL_SUBJECT, odd, set())["starts_at"] == info["starts_at"]


def test_meeting_length_comes_from_the_link():
    assert booking_minutes(SimpleNamespace(calendly_url=LINK)) == 30
    assert booking_minutes(SimpleNamespace(calendly_url="https://calendly.com/x/1-hour")) == 60
    assert booking_minutes(SimpleNamespace(calendly_url="https://calendly.com/x/intro")) == 30
    assert booking_minutes(SimpleNamespace(calendly_url="")) == 30


def test_interested_reply_gets_a_personal_booking_link_in_the_thread_only_after_approval(make_ctx):
    ctx, sender, sent = contacted(make_ctx, calendly_url=LINK)
    FakeIMAP.messages = {1: interested(sent)}
    run_inbox(ctx)
    r = latest_reply(ctx)
    assert r.category == "interested" and r.status == "actioned"
    with ctx.db.session() as s:
        company = s.execute(select(Lead)).scalar_one().company.name
        d = s.get(FollowUp, r.suggested_followup_id)
        assert (d.kind, d.status, d.to_email, d.in_reply_to) == ("response", "awaiting_approval", DAVID, r.message_id)
        assert d.subject == f"Re: {sent.subject}"
        assert d.body.startswith("Hi David,") and company in d.body and "You mentioned Thursday" in d.body
        assert "30-minute slot" in d.body and LINK in d.body and "unsubscribe" in d.body
    before = len(FakeSMTP.sent)
    wednesday = TUESDAY_11AM_ET + timedelta(days=1)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": wednesday.isoformat()})
    assert len(FakeSMTP.sent) == before  # nothing goes out until the Sales Manager approves it
    TestClient(create_app(ctx)).post(f"/followups/{d.id}/approve", headers=ORIGIN)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": wednesday.isoformat()})
    msg = FakeSMTP.sent[-1]
    assert len(FakeSMTP.sent) == before + 1 and msg["To"] == DAVID and msg["Subject"] == f"Re: {sent.subject}"
    assert msg["In-Reply-To"] == r.message_id and msg["References"].split()[-1] == r.message_id
    assert sent.message_id in msg["References"] and LINK in msg.get_content()


def test_a_booking_updates_the_crm_and_tells_the_sales_manager(make_ctx, monkeypatch):
    monkeypatch.setattr(meetings, "utcnow", lambda: NOW)
    told = []
    monkeypatch.setattr(meetings, "notify_event", lambda settings, msg: told.append(msg))
    ctx, _, sent = contacted(make_ctx, calendly_url=LINK)
    FakeIMAP.messages = {1: interested(sent)}
    run_inbox(ctx)
    link_reply = latest_reply(ctx).suggested_followup_id
    # David books through the link before anyone approved the reply; Calendly's notice arrives twice
    FakeIMAP.messages.update({2: notice("New Event"), 3: notice("New Event", copy="again")})
    run_inbox(ctx)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert (m.status, m.starts_at, m.ends_at, m.timezone) == ("scheduled", datetime(2026, 10, 29, 4, 30),
                                                                  datetime(2026, 10, 29, 5, 0), "Asia/Kolkata")
        opp = s.get(Opportunity, m.opportunity_id)
        assert (opp.stage, opp.probability) == ("discovery", 30) and "Meeting 2026-10-29 04:30 UTC" in opp.next_step
        stale = s.get(FollowUp, link_reply)  # a second booking link would only confuse them
        assert stale.status == "cancelled" and stale.blocked_reason.startswith("Meeting booked for")
        rows = s.execute(select(InboundEmail).where(InboundEmail.category == "calendly")).scalars().all()
        assert [r.status for r in rows] == ["actioned", "actioned"] and "already recorded" in rows[1].action_note
        assert s.execute(select(Alert).where(Alert.message.like("Meeting booked with David Chen%"))).scalar_one()
    assert lead_of(ctx).outreach_status == "meeting"
    assert told == [f"Meeting booked with David Chen ({lead_name(ctx)}) on Thu Oct 29, 10:00 AM IST (04:30 UTC)"]
    page = TestClient(create_app(ctx)).get("/meetings").text
    assert "David Chen" in page and "30 Minute Meeting" in page


def lead_name(ctx) -> str:
    with ctx.db.session() as s:
        return s.execute(select(Lead)).scalar_one().company.name


def test_double_bookings_reschedules_and_cancellations(make_ctx, monkeypatch):
    monkeypatch.setattr(meetings, "utcnow", lambda: NOW)
    ctx, _, _ = contacted(make_ctx, calendly_url=LINK)
    FakeIMAP.messages = {1: notice("New Event")}
    run_inbox(ctx)
    fri11 = ("11:00am", "Friday, October 30, 2026", "Fri, Oct 30, 2026")
    FakeIMAP.messages[2] = notice("New Event", fri11)  # the same person books a second time
    run_inbox(ctx)

    def open_double(s):
        return s.execute(select(Alert).where(Alert.key.like("meeting_double:%"), Alert.resolved_at.is_(None))).scalars().all()

    with ctx.db.session() as s:
        assert len(s.execute(select(Meeting).where(Meeting.status == "scheduled")).scalars().all()) == 2
        [warning] = open_double(s)
        assert warning.level == "warning" and "Possible double booking: David Chen" in warning.message
    FakeIMAP.messages[3] = notice("Canceled", fri11)  # the extra one is cancelled in Calendly
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert not open_double(s)
        first = s.execute(select(Meeting).order_by(Meeting.id)).scalars().first()
        assert first.status == "scheduled" and "Meeting 2026-10-29 04:30 UTC" in s.get(Opportunity, first.opportunity_id).next_step
    assert lead_of(ctx).outreach_status == "meeting"  # one meeting is still booked
    # a reschedule reported as a new event and a cancellation of the old time, the new event first
    fri2 = ("2:00pm", "Friday, October 30, 2026", "Fri, Oct 30, 2026")
    FakeIMAP.messages.update({4: notice("New Event", fri2), 5: notice("Canceled")})
    run_inbox(ctx)
    with ctx.db.session() as s:
        [m] = s.execute(select(Meeting).where(Meeting.status == "scheduled")).scalars().all()
        assert m.starts_at == datetime(2026, 10, 30, 8, 30) and not open_double(s)
    assert lead_of(ctx).outreach_status == "meeting"
    # a reschedule that names its former time moves the meeting; the former time's cancellation changes nothing
    fri3 = ("3:00pm", "Friday, October 30, 2026", "Fri, Oct 30, 2026")
    FakeIMAP.messages.update({6: notice("New Event", fri3, "Former Event Date/Time:\n2:00pm - Friday, October 30, 2026 "
                                                         "(India Standard Time)\n\n"),
                              7: notice("Canceled", fri2, copy="former")})
    run_inbox(ctx)
    with ctx.db.session() as s:
        [m] = s.execute(select(Meeting).where(Meeting.status == "scheduled")).scalars().all()
        assert (m.starts_at, m.rescheduled_from) == (datetime(2026, 10, 30, 9, 30), datetime(2026, 10, 30, 8, 30))
        assert "former time of a rescheduled meeting" in latest_reply(ctx).action_note
    assert lead_of(ctx).outreach_status == "meeting"
    FakeIMAP.messages[8] = notice("Canceled", fri3)
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert not s.execute(select(Meeting).where(Meeting.status == "scheduled")).scalars().all()
        opp = s.execute(select(Opportunity)).scalar_one()
        assert "was cancelled: follow up to rebook" in opp.next_step
    assert lead_of(ctx).outreach_status == "interested"


def test_a_lead_who_already_booked_gets_a_confirmation_not_a_second_link(make_ctx, monkeypatch):
    monkeypatch.setattr(meetings, "utcnow", lambda: NOW)
    ctx, _, sent = contacted(make_ctx, calendly_url=LINK)
    FakeIMAP.messages = {1: notice("New Event")}
    run_inbox(ctx)
    with ctx.db.session() as s:  # booking straight from the link: the rest of the sequence is not sent
        assert not s.execute(select(FollowUp).where(FollowUp.kind == "outreach",
                                                    FollowUp.status.in_(meetings.OPEN))).scalars().all()
    FakeIMAP.messages[2] = interested(sent, "Great, looking forward to it. Can we talk Thursday?")
    run_inbox(ctx)
    with ctx.db.session() as s:
        d = s.get(FollowUp, latest_reply(ctx).suggested_followup_id)
        # in US Eastern, the zone the emails are written for (10:00 IST is 00:30 EDT)
        assert "Our meeting is in my calendar for Thu Oct 29, 12:30 AM EDT" in d.body and LINK not in d.body


def test_manual_cancellation_reopens_the_lead_like_a_calendly_one(make_ctx, monkeypatch):
    monkeypatch.setattr(meetings, "utcnow", lambda: NOW)
    ctx, _, _ = contacted(make_ctx, calendly_url=LINK)
    FakeIMAP.messages = {1: notice("New Event")}
    run_inbox(ctx)
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    TestClient(create_app(ctx)).post(f"/meetings/{mid}/cancel", headers=ORIGIN)
    assert lead_of(ctx).outreach_status == "interested"


def test_calendly_newsletters_are_not_kept_but_forwarded_notices_are(make_ctx, monkeypatch):
    monkeypatch.setattr(meetings, "utcnow", lambda: NOW)
    ctx, _, _ = contacted(make_ctx, calendly_url=LINK)
    subject, body = notice_text("New Event")
    FakeIMAP.messages = {
        1: mail("Calendly <teamcalendly@send.calendly.com>", "Welcome to Calendly", "Tips at https://calendly.com/app"),
        2: mail("Owner <owner@owner-test.example>", f"Fwd: {subject}",
                f"---------- Forwarded message ---------\nFrom: Calendly <notifications@calendly.com>\n\n{body}")}
    out = run_inbox(ctx)
    assert out.skipped_unrelated == 1
    with ctx.db.session() as s:
        [row] = s.execute(select(InboundEmail)).scalars().all()
        assert row.category == "calendly" and row.status == "actioned"
        assert s.execute(select(Meeting)).scalar_one().starts_at == datetime(2026, 10, 29, 4, 30)


def test_test_mode_replies_keep_their_thread_subject(make_ctx):
    owner = "owner@owner-test.example"
    ctx = make_ctx(outbound_test_recipients=owner, **MAIL)
    FakeSMTP.sent = []
    EmailSender(ctx.settings, smtp_factory=FakeSMTP).send(DAVID, f"Re: [TEST for {DAVID}] Hello", "Thanks",
                                                          in_reply_to="<parent@x.example>")
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == owner and msg["Subject"] == f"Re: [TEST for {DAVID}] Hello"
    assert msg.get_content().startswith("TEST MODE")
