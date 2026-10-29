"""Which lead a Calendly booking belongs to when it comes from an address we emailed rather than a contact's own.
In test mode every lead's email goes to the owner's test address, so "the lead last emailed there" is not who
booked: on a copy of the 29 Oct pilot, the owner's test booking was linked to the lead with the newest unsent draft
instead of the lead that had been sent the booking link. Only emails that went out count, and a booking from the test
address goes to the one lead that was sent a booking link there; with several, a person links it."""

from datetime import timedelta

from sqlalchemy import select

from aime.agents.sales_workflow import SalesWorkflowAgent
from aime.models import Contact, FollowUp, InboundEmail, Lead, Meeting
from aime.timeutil import utcnow
from tests.test_inbound_sales import TUESDAY_11AM_ET
from tests.test_inbox import FakeIMAP, mail, run_inbox
from tests.test_pilot_test_mode import ANN, BEN, OWNER, first_email, pilot


def booking(uid, name, email, start="10:00am", day="Thursday, October 29, 2026", short="Thu, Oct 29, 2026"):
    body = (f"Hi Santosh,\n\nA new event has been scheduled.\n\nEvent Type:\n30 Minute Meeting\n\nInvitee:\n{name}\n\n"
            f"Invitee Email:\n{email}\n\nEvent Date/Time:\n{start} - {day} (India Standard Time)\n\nLocation:\n"
            "This is a Google Meet web conference.\n\nView event in Calendly")
    FakeIMAP.messages[uid] = mail("Calendly <notifications@calendly.com>",
                                  f"New Event: {name} - {start} {short} - 30 Minute Meeting", body, msg_id=f"<b{uid}@cal.example>")


def lead_id(ctx, address):
    with ctx.db.session() as s:
        return s.execute(select(Lead.id).join(Contact, Lead.contact_id == Contact.id).where(Contact.email == address)).scalar_one()


def asks_for_a_meeting(ctx, uid, address):
    FakeIMAP.messages[uid] = mail(f"Owner <{OWNER}>", "Re: your email", "Sounds good, can we talk Thursday?",
                                  in_reply_to=first_email(ctx, address).message_id, msg_id=f"<m{uid}@owner.example>")
    run_inbox(ctx)
    with ctx.db.session() as s:
        return s.execute(select(FollowUp).where(FollowUp.purpose == "booking_link", FollowUp.lead_id == lead_id(ctx, address))
                         ).scalar_one().id


def approve_and_send(ctx, sender, fid, minutes):
    with ctx.db.session() as s:
        f = s.get(FollowUp, fid)
        f.status, f.approved_by, f.approved_at = "approved", "owner", utcnow()
    SalesWorkflowAgent(ctx, sender=sender).run({"now": (TUESDAY_11AM_ET + timedelta(minutes=minutes)).isoformat()})
    with ctx.db.session() as s:
        assert s.get(FollowUp, fid).status == "sent"


def the_booking(ctx, uid):
    with ctx.db.session() as s:
        row = s.execute(select(InboundEmail).where(InboundEmail.message_id == f"<b{uid}@cal.example>")).scalar_one()
        m = s.execute(select(Meeting).where(Meeting.invitee_email == OWNER)).scalars().all()[-1]
        return row.status, m.lead_id


def test_a_test_mode_booking_goes_to_the_lead_that_was_sent_the_booking_link(make_ctx):
    ctx, sender = pilot(make_ctx)
    approve_and_send(ctx, sender, asks_for_a_meeting(ctx, 1, BEN), 5)  # Ben gets the booking link
    asks_for_a_meeting(ctx, 2, ANN)  # Ann's link is drafted after Ben's, and not sent yet
    booking(3, "Owner Test", OWNER)  # the owner books from the test address, as Ben would
    run_inbox(ctx)
    assert the_booking(ctx, 3) == ("actioned", lead_id(ctx, BEN))  # not Ann, whose draft is the newest


def test_with_two_booking_links_out_a_person_links_the_booking(make_ctx):
    ctx, sender = pilot(make_ctx)
    approve_and_send(ctx, sender, asks_for_a_meeting(ctx, 1, BEN), 5)
    approve_and_send(ctx, sender, asks_for_a_meeting(ctx, 2, ANN), 10)
    booking(3, "Owner Test", OWNER)
    run_inbox(ctx)
    assert the_booking(ctx, 3) == ("needs_review", None)  # either could have booked: no guess


def test_a_prospect_booking_from_their_own_address_is_unchanged(make_ctx):
    ctx, sender = pilot(make_ctx)
    approve_and_send(ctx, sender, asks_for_a_meeting(ctx, 1, BEN), 5)
    booking(2, "Ann Lead", ANN)  # a contact's own address: matched by the contact, test mode or not
    run_inbox(ctx)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting).where(Meeting.invitee_email == ANN)).scalar_one()
        assert m.lead_id == lead_id(ctx, ANN)
