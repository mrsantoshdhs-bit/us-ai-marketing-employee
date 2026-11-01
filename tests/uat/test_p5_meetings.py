"""Phase 5, meetings: each test states what must hold. Synthetic data only, fake SMTP and IMAP."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import booking
from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.crm_ops import suppress
from aime.mail_auth import calendly_verified
from aime.meetings import (
    REBOOK_TASK,
    WAITING_ALERT,
    _zone,
    close_open_emails,
    close_outcome_tasks,
    held_by_opt_out,
    parse_calendly,
)
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, MeetingProposal, Opportunity
from aime.pipeline import _task
from aime.reply_intel import QUESTION_PLACEHOLDER, lead_on_hold
from aime.web.app import create_app
from tests.test_a4_meetings import booking_ctx, meeting_request, proposal
from tests.test_company_research import SITE
from tests.test_inbound_sales import TUESDAY_11AM_ET, FakeSMTP
from tests.test_inbox import FakeIMAP, contacted, latest_reply, mail, run_inbox
from tests.uat import test_uat_email as U

DAVID = f"david.chen@{SITE}"
ORIGIN = {"origin": "http://testserver"}
LINK = "https://calendly.com/owner-example/30min"
A = ("8:30pm", "Tuesday, 8 December 2026", "Tue, 8 Dec 2026")     # 15:00 UTC
B = ("9:30pm", "Wednesday, 9 December 2026", "Wed, 9 Dec 2026")   # 16:00 UTC
C = ("10:30pm", "Thursday, 10 December 2026", "Thu, 10 Dec 2026")  # 17:00 UTC
FIXTURE_DATE = b"Tue, 27 Oct 2026 16:00:00 +0000"
_n = [0]


def when(t, zone="India Standard Time"):
    return f"{t[0]} - {t[1]} ({zone})"


def notice(kind, to=A, old=None, who=DAVID, frm="Calendly <notifications@calendly.com>", dated=None):
    subject = {"booked": f"New Event: David Chen - {to[0]} {to[2]} - 30 Minute Meeting",
               "moved": f"Updated: David Chen - {to[0]} {to[2]} - 30 Minute Meeting",
               "cancelled": f"Canceled: 30 Minute Meeting with David Chen on {to[1]}"}[kind]
    former = f"Date & Time:\nFormer: {when(old)}\nUpdated: {when(to)}\n\n" if old else ""
    body = (f"Calendly\n\nEvent Type:\n30 Minute Meeting\n\nInvitee:\nDavid Chen\n\nInvitee Email:\n{who}\n\nEvent Date/Time:\n{when(to)}\n\n"
            f"Location:\nGoogle Meet\n\nInvitee Time Zone:\nEastern Time - US & Canada\n\n{former}View event in Calendly\nhttps://calendly.com/app\n")
    _n[0] += 1
    raw = mail(frm, subject, body, msg_id=f"<c{_n[0]}@calendly.example>")
    return raw.replace(FIXTURE_DATE, dated.encode()) if dated else raw


def deliver(ctx, *raws):
    start = max(FakeIMAP.messages, default=0) + 1
    for i, raw in enumerate(raws):
        FakeIMAP.messages[start + i] = raw
    return run_inbox(ctx)


def live(ctx):
    with ctx.db.session() as s:
        return [m.starts_at.strftime("%d %H:%M") for m in s.execute(select(Meeting).where(Meeting.status == "scheduled")).scalars()]


def lead(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Lead).order_by(Lead.id)).scalars().first()


def tasks(ctx):
    with ctx.db.session() as s:
        return [t.subject for t in s.execute(select(FollowUp).where(FollowUp.channel == "task", FollowUp.status == "pending")).scalars()
                if t.subject.startswith(("Record the outcome", "Rebook"))]


def web(c):
    return TestClient(create_app(c))


def notices(ctx):
    with ctx.db.session() as s:
        return s.execute(select(InboundEmail).where(InboundEmail.category == "calendly").order_by(InboundEmail.id)).scalars().all()


def open_alerts(ctx, level=None):
    with ctx.db.session() as s:
        return [a for a in s.execute(select(Alert).where(Alert.resolved_at.is_(None), Alert.acknowledged.is_(False))).scalars()
                if level is None or a.level == level]


@pytest.fixture
def ctx(make_ctx):
    c, _sender, _sent = contacted(make_ctx, calendly_verified_only=False, calendly_url=LINK)
    return c


@pytest.fixture
def signed_only(make_ctx):
    c, _sender, _sent = contacted(make_ctx, calendly_verified_only=True, calendly_url=LINK)
    return c


# ---------------------------------------------------------------- stopping what waits
def _reply_draft(make_ctx, text="Sounds good, can we talk on Thursday?", url=LINK):
    ctx, sender, sent = contacted(make_ctx, calendly_verified_only=False, calendly_url=url)
    deliver(ctx, mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", text, in_reply_to=sent.message_id))
    with ctx.db.session() as s:
        return ctx, sender, s.execute(select(FollowUp).where(FollowUp.kind == "response")).scalars().one().id


def _draft_status(ctx, fid):
    with ctx.db.session() as s:
        return s.get(FollowUp, fid).status


def _sent_after_booking(ctx, sender):
    before = len(FakeSMTP.sent)
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(days=1)).isoformat()})
    return len(FakeSMTP.sent) - before


@pytest.mark.xfail(strict=False, reason="for the integrator: passes once replies-acting sets purpose ask_times; remove this mark then")
@pytest.mark.parametrize("url", ["", "https://cal.example/owner/30min"])
def test_a_booking_stops_the_draft_that_asks_for_a_meeting(make_ctx, url):
    ctx, _sender, fid = _reply_draft(make_ctx, "Sounds good, can we schedule a call?", url)
    deliver(ctx, notice("booked", A))
    assert _draft_status(ctx, fid) == "cancelled"


@pytest.mark.parametrize("url", ["", "https://cal.example/owner/30min"])
def test_a_booking_stops_a_draft_marked_as_asking_for_times(make_ctx, url):
    ctx, _sender, fid = _reply_draft(make_ctx, "Sounds good, can we schedule a call?", url)
    with ctx.db.session() as s:
        s.get(FollowUp, fid).purpose = "ask_times"
    deliver(ctx, notice("booked", A))
    assert _draft_status(ctx, fid) == "cancelled"


def test_an_approved_offer_of_times_stops_when_they_book_another_time(make_ctx):
    ctx, sender, fid = _reply_draft(make_ctx)
    with ctx.db.session() as s:
        d = s.get(FollowUp, fid)
        d.purpose = "meeting_offer"
        d.body = d.body.replace(LINK, "Thursday 10 AM or Friday 2 PM")
    assert U.approve(web(ctx), fid).status_code == 303
    assert _draft_status(ctx, fid) == "approved"
    deliver(ctx, notice("booked", B))
    assert _draft_status(ctx, fid) == "cancelled"
    assert _sent_after_booking(ctx, sender) == 0


def test_a_booking_draft_with_the_link_edited_out_is_cancelled(make_ctx):
    ctx, sender, fid = _reply_draft(make_ctx)
    with ctx.db.session() as s:
        d = s.get(FollowUp, fid)
        assert d.purpose == "booking_link"
        d.body = d.body.replace(LINK, "by replying with two or three times")
    U.approve(web(ctx), fid)
    deliver(ctx, notice("booked", A))
    assert _draft_status(ctx, fid) == "cancelled"
    assert _sent_after_booking(ctx, sender) == 0


def _pricing_draft(make_ctx):
    ctx, sender, fid = _reply_draft(make_ctx, "How much does this cost per month?")
    with ctx.db.session() as s:
        d = s.get(FollowUp, fid)
        assert d.purpose == "booking_link" and QUESTION_PLACEHOLDER in d.body and LINK in d.body
    return ctx, sender, fid


def test_a_written_pricing_answer_with_the_link_removed_still_goes_after_a_booking(make_ctx):
    ctx, sender, fid = _pricing_draft(make_ctx)
    with ctx.db.session() as s:
        body = s.get(FollowUp, fid).body
    written = body.replace(QUESTION_PLACEHOLDER, "It depends on the size of the work; I can send the details.").replace(LINK, "any time")
    web(ctx).post(f"/followups/{fid}/approve", headers=ORIGIN, data={"body": written}, follow_redirects=False)
    assert _draft_status(ctx, fid) == "approved"
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "approved" and "booked a meeting" in s.get(FollowUp, fid).blocked_reason
    assert _sent_after_booking(ctx, sender) == 1
    assert _draft_status(ctx, fid) == "sent"


def test_a_pricing_answer_keeping_the_link_stays_and_says_a_booking_arrived(make_ctx):
    ctx, _sender, fid = _pricing_draft(make_ctx)
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        d = s.get(FollowUp, fid)
        assert d.status == "awaiting_approval" and "booked a meeting" in d.blocked_reason and "Write your answer" in d.blocked_reason


@pytest.mark.parametrize("purpose", ["meeting_confirmation", "meeting_move", "meeting_cancel", "meeting_reminder"])
def test_a_note_about_the_meeting_is_never_cancelled_by_a_booking(ctx, purpose):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(FollowUp(lead_id=ld.id, company_id=ld.company_id, channel="email", kind="response", step=1, status="awaiting_approval",
                       purpose=purpose, to_email=DAVID, subject="Our call", body=f"Our call is confirmed; if it changes: {LINK}"))
        s.flush()
        assert close_open_emails(s, ld, "booked") == 0


def test_a_reply_that_asks_for_nothing_stays(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(FollowUp(lead_id=ld.id, company_id=ld.company_id, channel="email", kind="response", step=1, status="awaiting_approval",
                       to_email=DAVID, subject="Re: x", body="Thank you for your note."))
        s.flush()
        assert close_open_emails(s, ld, "booked") == 0


def test_a_calendly_booking_closes_open_proposals(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        for status in ("draft", "offered"):
            s.add(MeetingProposal(lead_id=ld.id, prospect_email=DAVID, prospect_tz="UTC", minutes=30, slots=[], status=status))
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        assert {p.status for p in s.execute(select(MeetingProposal)).scalars()} == {"cancelled"}


# ---------------------------------------------------------------- bookings that wait for a person
def _opted_out(s, ld):
    ld.outreach_status = "unsubscribed"
    ld.contact.consent_status = "opted_out"
    suppress(s, DAVID, "asked")


def test_a_booking_does_not_reopen_an_opted_out_lead(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        _opted_out(s, ld)
        s.add(Opportunity(lead_id=ld.id, company_id=ld.company_id, stage="lost", probability=0, name="Deal", lost_reason="Unsubscribed"))
    deliver(ctx, notice("booked", A))
    assert lead(ctx).outreach_status == "unsubscribed"
    with ctx.db.session() as s:
        deal = s.execute(select(Opportunity)).scalars().one()
        assert (deal.stage, deal.lost_reason, deal.probability, deal.owner) == ("lost", "Unsubscribed", 0, None)
        m = s.execute(select(Meeting)).scalars().one()
        assert m.status == "scheduled" and m.lead_id == ld.id and m.opportunity_id is None
    [row] = notices(ctx)
    assert row.status == "needs_review" and row.lead_id == ld.id and "nothing moved" in row.action_note
    assert "booked" not in row.recommended_status.lower() and row.sentiment == "neutral"
    assert any("do-not-contact" in a.message for a in open_alerts(ctx, "warning"))
    assert not [a for a in open_alerts(ctx) if a.source == "handover"]


def test_applying_a_held_booking_finishes_in_one_step_and_moves_nothing(signed_only):
    with signed_only.db.session() as s:
        _opted_out(s, s.execute(select(Lead)).scalars().first())
    deliver(signed_only, notice("booked", A))
    [row] = notices(signed_only)
    web(signed_only).post(f"/inbox/{row.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    [row] = notices(signed_only)
    assert row.status == "actioned" and not row.holds_sending and "nothing moved" in row.action_note
    assert lead(signed_only).outreach_status == "unsubscribed" and live(signed_only) == ["08 15:00"]
    with signed_only.db.session() as s:
        assert not s.execute(select(Opportunity)).scalars().all()


def test_a_suppressed_address_is_held_whatever_the_stage(ctx):
    with ctx.db.session() as s:
        suppress(s, DAVID, "Bounced: address not found", bounce=True)
    assert lead(ctx).outreach_status == "contacted"
    deliver(ctx, notice("booked", A))
    assert lead(ctx).outreach_status == "contacted" and live(ctx) == ["08 15:00"]
    with ctx.db.session() as s:
        assert not s.execute(select(Opportunity)).scalars().all()


def test_a_bounced_stage_with_no_entry_is_not_held(ctx):
    with ctx.db.session() as s:
        s.execute(select(Lead)).scalars().first().outreach_status = "bounced"
    deliver(ctx, notice("booked", A))
    assert lead(ctx).outreach_status == "meeting"


def test_a_colleagues_booking_at_a_company_with_an_opt_out_is_held(ctx):
    with ctx.db.session() as s:
        s.execute(select(Lead)).scalars().first().contact.consent_status = "opted_out"
    deliver(ctx, notice("booked", A, who=f"sam@{SITE}"))
    assert lead(ctx).outreach_status == "contacted" and live(ctx) == ["08 15:00"]
    with ctx.db.session() as s:
        assert not s.execute(select(Opportunity)).scalars().all()


def test_an_invitee_who_opted_out_under_another_contact_is_held(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        co = Company(name="Elsewhere", normalized_name="elsewhere", source="test", domain="elsewhere.example")
        s.add(co)
        s.flush()
        other = Contact(company_id=co.id, email="pat@elsewhere.example", consent_status="opted_out", source="website")
        s.add(other)
        s.flush()
        assert held_by_opt_out(s, ld, None, "PAT+x@elsewhere.example")
        other.consent_status = "business_contact"
        assert not held_by_opt_out(s, ld, None, "PAT+x@elsewhere.example")


def test_a_disqualified_lead_stays_disqualified_and_gets_no_deal(ctx):
    with ctx.db.session() as s:
        s.execute(select(Lead)).scalars().first().status = "disqualified"
    deliver(ctx, notice("booked", A))
    ld = lead(ctx)
    assert ld.status == "disqualified" and ld.outreach_status == "contacted" and live(ctx) == ["08 15:00"]
    with ctx.db.session() as s:
        assert not s.execute(select(Opportunity)).scalars().all()
    assert any("disqualified" in a.message for a in open_alerts(ctx, "warning"))
    assert notices(ctx)[0].status == "needs_review"


def test_a_deal_lost_for_another_reason_stays_lost_and_asks_a_person(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(Opportunity(lead_id=ld.id, company_id=ld.company_id, stage="lost", probability=0, name="Deal", lost_reason="Not interested"))
    deliver(ctx, notice("booked", A))
    assert lead(ctx).outreach_status == "contacted" and live(ctx) == ["08 15:00"]
    with ctx.db.session() as s:
        deal = s.execute(select(Opportunity)).scalars().one()
        assert deal.stage == "lost" and deal.lost_reason == "Not interested" and "a person decides" in deal.next_step
    assert any("its deal was lost (Not interested)" in a.message for a in open_alerts(ctx, "warning"))
    assert notices(ctx)[0].status == "needs_review"


def test_a_booking_on_an_open_deal_moves_on_as_before(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(Opportunity(lead_id=ld.id, company_id=ld.company_id, stage="new", probability=10, name="Deal"))
    deliver(ctx, notice("booked", A))
    assert lead(ctx).outreach_status == "meeting" and notices(ctx)[0].status == "actioned"
    with ctx.db.session() as s:
        deal = s.execute(select(Opportunity)).scalars().one()
        assert (deal.stage, deal.probability) == ("discovery", 30) and "prepare" in deal.next_step
        assert s.execute(select(Meeting)).scalars().one().opportunity_id == deal.id


PERSON_DECIDES = "Opted out: a person decides"


def _held_open_deal(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        s.add(Opportunity(lead_id=ld.id, company_id=ld.company_id, stage="proposal", probability=50, name="Deal", next_step=PERSON_DECIDES))
        suppress(s, DAVID, "asked")


def _next_step(ctx):
    with ctx.db.session() as s:
        return s.execute(select(Opportunity)).scalars().one().next_step


def test_a_held_booking_keeps_a_person_decides_next_step_through_move_and_cancel(ctx):
    _held_open_deal(ctx)
    deliver(ctx, notice("booked", A))
    assert _next_step(ctx) == PERSON_DECIDES
    deliver(ctx, notice("moved", B, old=A))
    assert live(ctx) == ["09 16:00"] and _next_step(ctx) == PERSON_DECIDES
    deliver(ctx, notice("cancelled", B))
    assert live(ctx) == [] and _next_step(ctx) == PERSON_DECIDES
    assert lead(ctx).outreach_status != "interested"


def test_a_reschedule_with_nothing_to_move_on_a_held_lead_is_held_too(ctx):
    _held_open_deal(ctx)
    deliver(ctx, notice("moved", B, old=A))
    assert live(ctx) == ["09 16:00"] and _next_step(ctx) == PERSON_DECIDES and lead(ctx).outreach_status == "contacted"


def test_cancelling_a_held_meeting_on_the_page_writes_no_rebook_step(ctx):
    _held_open_deal(ctx)
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    web(ctx).post(f"/meetings/{mid}/cancel", headers=ORIGIN, follow_redirects=False)
    assert _next_step(ctx) == PERSON_DECIDES


def test_a_meeting_logged_by_hand_on_an_opted_out_lead_is_recorded_only(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        _opted_out(s, ld)
        lid = ld.id
    r = web(ctx).post(f"/leads/{lid}/meeting", headers=ORIGIN, follow_redirects=False,
                      data={"date": "2026-12-08", "time_et": "10:00", "minutes": 30, "invitee_email": DAVID})
    assert r.status_code == 303
    assert lead(ctx).outreach_status == "unsubscribed" and live(ctx) == ["08 15:00"]
    with ctx.db.session() as s:
        assert not s.execute(select(Opportunity)).scalars().all()


def test_linking_a_booking_to_an_opted_out_lead_moves_nothing(ctx):
    deliver(ctx, notice("booked", A, who="stranger@unknown-co.example"))
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        _opted_out(s, ld)
        mid, lid = s.execute(select(Meeting.id)).scalar_one(), ld.id
    web(ctx).post(f"/meetings/{mid}/link", headers=ORIGIN, data={"lead_id": lid}, follow_redirects=False)
    assert lead(ctx).outreach_status == "unsubscribed"
    with ctx.db.session() as s:
        m = s.get(Meeting, mid)
        assert m.lead_id == lid and m.opportunity_id is None
        assert not s.execute(select(Opportunity)).scalars().all()
    assert any("do-not-contact" in a.message for a in open_alerts(ctx, "warning"))


def test_booking_an_offered_time_for_someone_who_opted_out_since_is_recorded_only(make_ctx):
    ctx, _sender, sent, _cal = booking_ctx(make_ctx)
    meeting_request(ctx, sent)
    p = proposal(ctx)
    with ctx.db.session() as s:
        _opted_out(s, s.execute(select(Lead)).scalars().first())
    web(ctx).post(f"/meetings/proposals/{p.id}/book", headers=ORIGIN, data={"slot": 0}, follow_redirects=False)
    assert lead(ctx).outreach_status == "unsubscribed"
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).scalars().one().opportunity_id is None
        assert s.get(MeetingProposal, p.id).status == "booked"
        assert not s.execute(select(FollowUp).where(FollowUp.purpose == "meeting_confirmation")).scalars().all()
        for deal in s.execute(select(Opportunity)).scalars():
            assert deal.stage == "lost" or "prepare" not in (deal.next_step or "")


def test_booking_an_offered_time_on_a_lost_deal_keeps_it_lost_and_still_confirms(make_ctx):
    ctx, _sender, sent, _cal = booking_ctx(make_ctx)
    meeting_request(ctx, sent)
    p = proposal(ctx)
    with ctx.db.session() as s:
        deal = s.execute(select(Opportunity)).scalars().one()
        deal.stage, deal.lost_reason = "lost", "Budget"
    web(ctx).post(f"/meetings/proposals/{p.id}/book", headers=ORIGIN, data={"slot": 0}, follow_redirects=False)
    assert lead(ctx).outreach_status != "meeting"
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalars().one().stage == "lost"
        assert s.execute(select(FollowUp).where(FollowUp.purpose == "meeting_confirmation")).scalars().one().status == "awaiting_approval"


# ---------------------------------------------------------------- order of notices
def test_cancel_read_before_its_booking_leaves_no_live_meeting(ctx):
    deliver(ctx, notice("cancelled", A), notice("booked", A))
    assert live(ctx) == [], "the event was cancelled in Calendly"
    assert lead(ctx).outreach_status != "meeting"
    assert [r.status for r in notices(ctx)] == ["needs_review", "needs_review"]


def test_a_rebooking_dated_after_the_cancel_is_a_new_booking(ctx):
    deliver(ctx, notice("cancelled", A, dated="Tue, 27 Oct 2026 16:00:00 +0000"), notice("booked", A, dated="Tue, 27 Oct 2026 17:00:00 +0000"))
    assert live(ctx) == ["08 15:00"] and lead(ctx).outreach_status == "meeting"


def test_a_cancelled_time_stays_rebookable(ctx):
    deliver(ctx, notice("booked", A))
    deliver(ctx, notice("cancelled", A))
    deliver(ctx, notice("booked", A))
    assert live(ctx) == ["08 15:00"]


def test_reschedule_read_before_its_booking_leaves_one_meeting(ctx):
    deliver(ctx, notice("moved", B, old=A), notice("booked", A))
    assert live(ctx) == ["09 16:00"]
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).scalars().one().rescheduled_from == datetime(2026, 12, 8, 15, 0)


def test_booking_the_former_time_again_later_is_a_new_booking(ctx):
    deliver(ctx, notice("booked", A, dated="Tue, 27 Oct 2026 16:00:00 +0000"), notice("moved", B, old=A, dated="Tue, 27 Oct 2026 16:10:00 +0000"))
    deliver(ctx, notice("booked", A, dated="Tue, 27 Oct 2026 17:00:00 +0000"))
    assert sorted(live(ctx)) == ["08 15:00", "09 16:00"]


def test_a_late_cancel_of_a_time_moved_twice_ago_does_not_cancel_the_live_meeting(ctx):
    deliver(ctx, notice("booked", A), notice("moved", B, old=A), notice("moved", C, old=B), notice("cancelled", A))
    assert live(ctx) == ["10 17:00"]
    assert "former time" in latest_reply(ctx).action_note


def test_a_cancel_naming_a_time_nobody_mentioned_still_means_the_only_meeting(ctx):
    deliver(ctx, notice("booked", A), notice("cancelled", B))
    assert live(ctx) == []


def test_a_late_cancel_does_not_overwrite_a_meeting_marked_held(ctx):
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    web(ctx).post(f"/meetings/{mid}/held", headers=ORIGIN, data={"notes": "ok"}, follow_redirects=False)
    deliver(ctx, notice("cancelled", A))
    with ctx.db.session() as s:
        assert s.get(Meeting, mid).status == "completed"
        assert "Meeting held" in s.execute(select(Opportunity)).scalars().one().next_step
    assert latest_reply(ctx).status == "needs_review" and "already has an outcome" in latest_reply(ctx).action_note


def test_a_late_cancel_does_not_overwrite_a_no_show(ctx):
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    web(ctx).post(f"/meetings/{mid}/no_show", headers=ORIGIN, follow_redirects=False)
    deliver(ctx, notice("cancelled", A))
    with ctx.db.session() as s:
        assert s.get(Meeting, mid).status == "no_show"
    assert latest_reply(ctx).status == "needs_review"


@pytest.mark.parametrize("action", ["held", "no_show"])
def test_a_booking_repeated_after_an_outcome_changes_nothing(ctx, action):
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    web(ctx).post(f"/meetings/{mid}/{action}", headers=ORIGIN, data={"notes": "ok"}, follow_redirects=False)
    deliver(ctx, notice("booked", A))
    assert live(ctx) == [] and "already recorded" in latest_reply(ctx).action_note


# ---------------------------------------------------------------- a notice that waits
def test_a_notice_not_applied_does_not_say_meeting_booked(signed_only):
    deliver(signed_only, notice("booked", A))
    [row] = notices(signed_only)
    assert row.status == "needs_review" and row.recommended_status == "Not applied: check it in Calendly, then Apply" and row.sentiment == "neutral"


def test_applying_it_says_what_happened(signed_only):
    deliver(signed_only, notice("booked", A))
    [row] = notices(signed_only)
    web(signed_only).post(f"/inbox/{row.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    [row] = notices(signed_only)
    assert row.status == "actioned" and row.recommended_status == "Meeting booked: prepare for it" and row.sentiment == "positive"


def test_an_unreadable_notice_does_not_mention_apply(signed_only):
    deliver(signed_only, mail("Calendly <notifications@calendly.com>", "New Event: somebody", "Open Calendly for details.", msg_id="<u1@calendly.example>"))
    [row] = notices(signed_only)
    assert row.recommended_status == "Could not be read: check it in Calendly and record it by hand" and row.sentiment == "neutral"
    assert "Apply" not in row.action_note


def test_a_booking_with_no_lead_says_to_link_it(ctx):
    deliver(ctx, notice("booked", A, who="stranger@unknown-co.example"))
    [row] = notices(ctx)
    assert row.recommended_status == "Meeting recorded: link it to a lead on Meetings"


def test_a_cancellation_never_says_booked(ctx):
    deliver(ctx, notice("cancelled", A))
    [unknown] = notices(ctx)
    assert unknown.recommended_status == "Cancellation for an unknown meeting: check it in Calendly" and unknown.sentiment == "neutral"
    deliver(ctx, notice("booked", B), notice("cancelled", B))
    row = latest_reply(ctx)
    assert row.status == "actioned" and "booked" not in row.recommended_status.lower() and row.sentiment == "neutral"


def test_every_recommendation_fits_its_column(signed_only):
    deliver(signed_only, notice("booked", A), notice("cancelled", B))
    assert all(len(r.recommended_status) <= 120 for r in notices(signed_only))


def _waiting(ctx, invitee=DAVID):
    with ctx.db.session() as s:
        return s.execute(select(Alert).where(Alert.key == f"{WAITING_ALERT}{invitee}", Alert.resolved_at.is_(None))).scalars().all()


def test_a_notice_waiting_raises_an_alert_a_manager_sees(signed_only):
    deliver(signed_only, notice("booked", A))
    [alert] = _waiting(signed_only)
    assert alert.level == "warning" and DAVID in alert.message


def test_the_waiting_alert_closes_when_it_is_applied(signed_only):
    deliver(signed_only, notice("booked", A))
    [row] = notices(signed_only)
    web(signed_only).post(f"/inbox/{row.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    assert _waiting(signed_only) == []


def test_the_waiting_alert_closes_after_mark_reviewed_on_the_next_inbox_run(signed_only):
    deliver(signed_only, notice("booked", A))
    [row] = notices(signed_only)
    web(signed_only).post(f"/inbox/{row.id}/reviewed", headers=ORIGIN, follow_redirects=False)
    run_inbox(signed_only)
    assert _waiting(signed_only) == []


def test_one_alert_per_invitee_while_two_notices_wait(signed_only):
    deliver(signed_only, notice("booked", A), notice("booked", B))
    assert len(_waiting(signed_only)) == 1
    first = notices(signed_only)[0]
    web(signed_only).post(f"/inbox/{first.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    assert len(_waiting(signed_only)) == 1, "the other one still waits"


def _step_two_approved(make_ctx):
    ctx, sender, sent = contacted(make_ctx, calendly_verified_only=True, calendly_url=LINK)
    U.stamp_sent(ctx, sent.id, TUESDAY_11AM_ET)
    U.run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=6))
    step2 = U.drafts(ctx)[-1]
    assert step2.step == 2 and step2.status == "awaiting_approval"
    U.approve(web(ctx), step2.id)
    assert U.followup(ctx, step2.id).status == "approved"
    return ctx, sender, step2


def _send_run(ctx, sender):
    before = len(FakeSMTP.sent)
    U.run(ctx, sender, TUESDAY_11AM_ET + timedelta(days=7))
    return len(FakeSMTP.sent) - before


def test_a_waiting_notice_holds_the_lead_it_names_and_changes_nothing_else(make_ctx):
    ctx, sender, step2 = _step_two_approved(make_ctx)
    deliver(ctx, notice("booked", A))
    [row] = notices(ctx)
    assert row.lead_id == lead(ctx).id and row.holds_sending is True and row.status == "needs_review"
    assert "emails wait until Apply or Mark reviewed" in row.action_note
    assert lead(ctx).outreach_status == "contacted" and live(ctx) == []
    with ctx.db.session() as s:
        assert lead_on_hold(s, row.lead_id)
    assert _send_run(ctx, sender) == 0
    assert U.followup(ctx, step2.id).status == "approved", "held, never cancelled, on an unproven notice"


def test_mark_reviewed_releases_the_lead(make_ctx):
    ctx, sender, step2 = _step_two_approved(make_ctx)
    deliver(ctx, notice("booked", A))
    [row] = notices(ctx)
    web(ctx).post(f"/inbox/{row.id}/reviewed", headers=ORIGIN, follow_redirects=False)
    assert _send_run(ctx, sender) == 1
    assert U.followup(ctx, step2.id).status == "sent"


def test_apply_books_and_stops_the_next_step(make_ctx):
    ctx, sender, step2 = _step_two_approved(make_ctx)
    deliver(ctx, notice("booked", A))
    [row] = notices(ctx)
    web(ctx).post(f"/inbox/{row.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    assert notices(ctx)[0].holds_sending is None and lead(ctx).outreach_status == "meeting"
    assert U.followup(ctx, step2.id).status == "cancelled" and _send_run(ctx, sender) == 0


def test_applying_a_cancellation_for_an_unknown_meeting_clears_the_hold(make_ctx):
    ctx, sender, step2 = _step_two_approved(make_ctx)
    deliver(ctx, notice("cancelled", A))
    [row] = notices(ctx)
    assert row.holds_sending is True
    web(ctx).post(f"/inbox/{row.id}/apply_calendly", headers=ORIGIN, follow_redirects=False)
    [row] = notices(ctx)
    assert row.status == "needs_review" and row.holds_sending is None
    assert _send_run(ctx, sender) == 1


def test_a_notice_naming_nobody_we_know_holds_nothing(signed_only):
    deliver(signed_only, notice("booked", A, who="stranger@unknown-co.example"))
    [row] = notices(signed_only)
    assert row.lead_id is None and not row.holds_sending and "emails wait" not in row.action_note


def test_the_forwarder_note_names_the_setting(ctx):
    with ctx.db.session() as s:
        trusted, why = calendly_verified(s, ctx.settings, "someone@owner-home.example", "aime.dkim; dkim=pass header.d=owner-home.example")
    assert not trusted and "if this address is yours, add it to CALENDLY_FORWARDERS" in why


def test_a_forwarder_is_never_added_by_itself(ctx):
    with ctx.db.session() as s:
        calendly_verified(s, ctx.settings, "someone@owner-home.example", "aime.dkim; dkim=pass header.d=owner-home.example")
    assert not ctx.settings.calendly_forwarders


# ---------------------------------------------------------------- tasks and alerts that never clear
def _ended_with_task(ctx):
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalars().first()
        _task(s, m.lead_id, m.company_id, f"Record the outcome of the meeting with {m.invitee_name}", "x")
        return m.id


def test_a_cancel_notice_closes_the_record_the_outcome_task(ctx):
    _ended_with_task(ctx)
    deliver(ctx, notice("cancelled", A))
    assert tasks(ctx) == []


def test_a_reschedule_closes_the_record_the_outcome_task(ctx):
    _ended_with_task(ctx)
    deliver(ctx, notice("moved", B, old=A))
    assert tasks(ctx) == []


def test_closing_outcome_tasks_with_no_lead_touches_nothing(ctx):
    with ctx.db.session() as s:
        ld = s.execute(select(Lead)).scalars().first()
        _task(s, ld.id, ld.company_id, "Record the outcome of the meeting with Pat", "x")
        close_outcome_tasks(s, None)
    assert len(tasks(ctx)) == 1


def test_a_new_booking_closes_the_rebook_task(ctx):
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        mid = s.execute(select(Meeting.id)).scalar_one()
    web(ctx).post(f"/meetings/{mid}/no_show", headers=ORIGIN, follow_redirects=False)
    assert [t for t in tasks(ctx) if t.startswith(REBOOK_TASK)]
    deliver(ctx, notice("booked", B))
    assert tasks(ctx) == []


def test_an_overlap_warning_closes_when_the_other_meeting_is_cancelled(ctx):
    deliver(ctx, notice("booked", A, who="other@elsewhere.example"), notice("booked", A))
    with ctx.db.session() as s:
        [alert] = s.execute(select(Alert).where(Alert.message.like("%overlaps%"))).scalars().all()
        assert alert.key.startswith("meeting_overlap:")
    deliver(ctx, notice("cancelled", A, who="other@elsewhere.example"))
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("%overlaps%"), Alert.resolved_at.is_(None), Alert.acknowledged.is_(False))).first() is None


def test_an_overlap_warning_stays_while_both_meetings_are_on(ctx):
    deliver(ctx, notice("booked", A, who="other@elsewhere.example"), notice("booked", A))
    run_inbox(ctx)
    with ctx.db.session() as s:
        assert s.execute(select(Alert).where(Alert.message.like("%overlaps%"), Alert.resolved_at.is_(None))).first() is not None


# ---------------------------------------------------------------- linking and matching
def test_linking_a_booking_gives_the_deal_an_owner(make_ctx):
    ctx, _s, _f = contacted(make_ctx, calendly_verified_only=False)
    from aime.models import User
    from aime.web.auth import hash_password
    with ctx.db.session() as s:
        s.add(User(username="sales1", password_hash=hash_password("copy-only-pw-1"), role="sales_manager"))
        s.execute(select(Lead)).scalars().first().owner = None
    assert not lead(ctx).owner
    deliver(ctx, notice("booked", A, who="stranger@unknown-co.example"))
    with ctx.db.session() as s:
        mid, lid = s.execute(select(Meeting.id)).scalar_one(), s.execute(select(Lead.id)).scalars().first()
    web(ctx).post(f"/meetings/{mid}/link", headers=ORIGIN, data={"lead_id": lid}, follow_redirects=False)
    with ctx.db.session() as s:
        assert s.execute(select(Opportunity)).scalars().first().owner
        assert any("Handed over" in a.message for a in s.execute(select(Alert)).scalars())


def test_a_colleagues_booking_goes_to_the_lead_being_worked(ctx):
    with ctx.db.session() as s:
        co = Company(name="Two Leads", normalized_name="two leads", source="test", domain="twolead.example")
        s.add(co)
        s.flush()
        closed = Lead(company_id=co.id, source="inbound", status="disqualified", score=1, outreach_status="unsubscribed")
        s.add(closed)
        s.flush()
        working = Lead(company_id=co.id, source="inbound", status="qualified", score=70, outreach_status="contacted")
        s.add(working)
        s.flush()
        working_id = working.id
    deliver(ctx, notice("booked", A, who="sam@twolead.example"))
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).scalars().first().lead_id == working_id


# ---------------------------------------------------------------- reading a notice
@pytest.mark.parametrize("label", ["GMT+5:30", "UTC+05:30", "GMT-8", "UTC - 3", "PST", "Dubai Time", "Hong Kong Time", "UK, Ireland, Lisbon Time",
                                   "Atlantic Time", "Mars Time", "Unknown/Zone"])
def test_a_time_zone_it_cannot_place_is_not_guessed(label):
    body = f"Invitee Email:\n{DAVID}\n\nEvent Date/Time:\n10:00am - Monday, December 7, 2026 ({label})\n\nLocation:\nZoom\n"
    assert parse_calendly("New Event: Pat - 10:00am Mon, Dec 7, 2026 - 30 Minute Meeting", body, set()) is None


@pytest.mark.parametrize("label,zone", [("Eastern Time - US & Canada", "America/New_York"), ("Central Time - US & Canada", "America/Chicago"),
                                        ("Mountain Time", "America/Denver"), ("Arizona Time", "America/Phoenix"), ("Pacific Time", "America/Los_Angeles"),
                                        ("Alaska Time", "America/Anchorage"), ("Hawaii Time", "Pacific/Honolulu"), ("India Standard Time", "Asia/Kolkata"),
                                        ("IST", "Asia/Kolkata"), ("UTC", "UTC"), ("GMT", "UTC"), ("Central European Time", "Europe/Berlin"),
                                        ("British Summer Time", "Europe/London"), ("Greenwich Mean Time", "Europe/London"),
                                        ("Australian Eastern Time", "Australia/Sydney"), ("Japan Standard Time", "Asia/Tokyo"),
                                        ("Gulf Standard Time", "Asia/Dubai"), ("Asia/Dubai", "Asia/Dubai")])
def test_every_known_time_zone_still_reads(label, zone):
    assert str(_zone(f"({label})", "America/New_York")) == zone
    body = f"Invitee Email:\n{DAVID}\n\nEvent Date/Time:\n10:00am - Monday, December 7, 2026 ({label})\n\nLocation:\nZoom\n"
    assert parse_calendly("New Event: Pat - 10:00am Mon, Dec 7, 2026 - 30 Minute Meeting", body, set())["tz"] == zone


def test_zone_tells_no_match_from_a_match():
    assert _zone("(Mars Time)") is None and _zone("no label at all") is None and _zone("(GMT+5:30)") is None
    assert str(_zone("(Eastern Time)")) == "America/New_York"


def test_a_notice_with_no_time_zone_goes_to_a_person():
    info = parse_calendly("New Event: Pat - 8:30pm Mon, 7 Dec 2026 - 30 Minute Meeting", f"Invitee Email:\n{DAVID}\nA new event has been scheduled.\n", set())
    assert info is None


def test_a_former_time_it_cannot_place_goes_to_a_person():
    body = f"Invitee Email:\n{DAVID}\n\nEvent Date/Time:\n10:00am - Monday, December 7, 2026 (Eastern Time)\n\nFormer Event Date/Time:\n9:00am - Monday, December 7, 2026 (Dubai Time)\n"
    assert parse_calendly("Updated: Pat - 10:00am Mon, Dec 7, 2026 - 30 Minute Meeting", body, set()) is None


@pytest.mark.parametrize("label", ["a/../b", "America/", "/etc/passwd"])
def test_a_zone_label_written_to_break_it_goes_to_a_person(label):
    body = f"Invitee Email:\n{DAVID}\n\nEvent Date/Time:\n10:00am - Monday, December 7, 2026 ({label})\n\nLocation:\nZoom\n"
    assert parse_calendly("New Event: Pat - 10:00am Mon, Dec 7, 2026 - 30 Minute Meeting", body, set()) is None


@pytest.mark.parametrize("verified_only", [True, False])
def test_a_time_that_is_no_time_waits_and_stops_nothing(make_ctx, verified_only):
    ctx, _sender, _sent = contacted(make_ctx, calendly_verified_only=verified_only, calendly_url=LINK)
    deliver(ctx, notice("booked", A).replace(b"8:30pm - Tuesday", b"8:99pm - Tuesday").replace(b"8:30pm Tue", b"8:99pm Tue"))
    [row] = notices(ctx)
    assert row.status == "needs_review" and row.recommended_status == "Could not be read: check it in Calendly and record it by hand"
    assert not [a for a in open_alerts(ctx, "error")]
    run_inbox(ctx)  # the next run reads what waits again
    deliver(ctx, notice("booked", B))
    assert "09 16:00" in live(ctx) or verified_only


def test_a_notice_it_cannot_place_goes_to_review_with_no_meeting(ctx):
    raw = notice("booked", A).replace(b"India Standard Time", b"Dubai Time")
    deliver(ctx, raw)
    [row] = notices(ctx)
    assert row.status == "needs_review" and live(ctx) == [] and "could not be read" in row.action_note


def test_a_quoted_forward_names_the_invitee_not_the_forwarder():
    body = ("Begin forwarded message:\n\n> From: Calendly <notifications@calendly.com>\n> To: owner.personal@owner-home.example\n>\n"
            f"> Invitee:\n> David Chen\n>\n> Invitee Email:\n> {DAVID}\n>\n> Event Date/Time:\n> 8:30pm - Tuesday, 8 December 2026 (India Standard Time)\n>\n> Location:\n> Zoom\n")
    info = parse_calendly("Fwd: New Event: David Chen - 8:30pm Tue, 8 Dec 2026 - 30 Minute Meeting", body, set())
    assert info["invitee_email"] == DAVID and info["invitee_name"] == "David Chen" and info["starts_at"] == datetime(2026, 12, 8, 15, 0)


# ---------------------------------------------------------------- what is written
def test_the_reminder_does_not_say_the_hosts_zone_is_theirs(ctx):
    deliver(ctx, notice("booked", A))
    with ctx.db.session() as s:
        assert booking.reminders(s, ctx.settings, datetime(2026, 12, 8, 14, 0)) == 1
        [alert] = [a for a in s.execute(select(Alert).where(Alert.message.like("Meeting with%"))).scalars()]
        assert "for them" not in alert.message and alert.message.endswith(": prepare")


def test_the_deal_names_the_meeting_as_the_meetings_page_can(ctx):
    deliver(ctx, notice("booked", A))
    assert _next_step(ctx).startswith("Meeting Tue Dec 08, 8:30 PM IST (15:00 UTC)")
    deliver(ctx, notice("moved", B, old=A))
    assert _next_step(ctx).startswith("Meeting Wed Dec 09, 9:30 PM IST (16:00 UTC) (rescheduled)")
