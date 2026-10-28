"""A4, meeting booking: free slots from the owner's calendar inside both parties' hours, an offer that always waits
for a person's approval, booking the chosen slot once (checked against the calendar again; idempotent), moving,
cancelling, reminders, honest dashboard status, and US time-zone labels that no longer swallow European ones. Fake
Google Calendar and Microsoft Graph servers and a mock token server: no real calendar, account or meeting."""

import json
from datetime import datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aime import automation as am
from aime import booking
from aime.integrations.calendar import GoogleCalendar, MicrosoftCalendar
from aime.meetings import _zone
from aime.models import Alert, AuditEvent, FollowUp, Lead, Meeting, MeetingProposal
from aime.timeutil import utcnow
from aime.web.app import create_app, manager_today
from tests.test_a2_mailbox import GOOGLE, sign_in, token_server
from tests.test_automation import authorize, run
from tests.test_inbox import DAVID, FakeIMAP, contacted, latest_reply, mail, run_inbox

ORIGIN = {"origin": "http://testserver"}
TUESDAY = datetime(2026, 10, 27, 14, 0)  # 10:00 US Eastern (EDT), 19:30 in India


class FakeGoogle:
    """Google Calendar v3, in memory: free/busy, events insert (409 for a known id), patch, delete."""

    def __init__(self):
        self.busy, self.events, self.calls = [], {}, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.googleapis.com":
            return token_server(request)
        path, body = request.url.path, json.loads(request.content or b"{}")
        self.calls.append((request.method, path, dict(request.url.params)))
        if path.endswith("/freeBusy"):
            spans = self.busy + [(e["start"]["dateTime"], e["end"]["dateTime"]) for e in self.events.values()]
            return httpx.Response(200, json={"calendars": {"primary": {"busy": [{"start": s, "end": e} for s, e in spans]}}})
        if request.method == "POST":
            if body["id"] in self.events:
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            self.events[body["id"]] = body
            return httpx.Response(200, json={"id": body["id"]})
        event_id = path.rsplit("/", 1)[1]
        if request.method == "PATCH":
            self.events[event_id].update(body)
            return httpx.Response(200, json=self.events[event_id])
        if request.method == "DELETE":
            return httpx.Response(204) if self.events.pop(event_id, None) else httpx.Response(410)
        return httpx.Response(400)


def booking_ctx(make_ctx, enabled=True, connect=True):
    ctx, sender, sent = contacted(make_ctx, **GOOGLE, calendar_provider="google", meeting_booking_enabled=enabled)
    cal = FakeGoogle()
    ctx.http_transport = httpx.MockTransport(cal)  # research is done: from here on, the fake calendar and sign-in
    if connect:
        sign_in(ctx, purpose="calendar")
    return ctx, sender, sent, cal


def meeting_request(ctx, sent):
    FakeIMAP.messages = {1: mail(f"David Chen <{DAVID}>", f"Re: {sent.subject}", "Sounds good. Can we talk Thursday?",
                                 in_reply_to=sent.message_id)}
    run_inbox(ctx)
    return latest_reply(ctx)


def proposal(ctx):
    with ctx.db.session() as s:
        return s.execute(select(MeetingProposal)).scalars().first()


# --------------------------------------------------------------------------- slots

def test_free_slots_respect_both_time_zones_the_calendar_and_the_notice(make_ctx):
    ctx = make_ctx()
    slots = booking.free_slots([], TUESDAY, "America/New_York", ctx.settings)
    # 24 h notice -> Wednesday 10:00 EDT; then the first free time of Thursday and Friday (one per day)
    assert slots == [datetime(2026, 10, 28, 14, 0), datetime(2026, 10, 29, 13, 0), datetime(2026, 10, 30, 13, 0)]
    busy = [(datetime(2026, 10, 29, 13, 0), datetime(2026, 10, 29, 14, 0))]
    assert booking.free_slots(busy, TUESDAY, "America/New_York", ctx.settings)[1] == datetime(2026, 10, 29, 14, 0)
    friday = datetime(2026, 10, 30, 14, 0)
    assert [s.weekday() for s in booking.free_slots([], friday, "America/New_York", ctx.settings)] == [0, 1, 2]  # no weekend
    # 23:30 in India is 14:00 in New York (EDT): later US afternoon times are outside the owner's hours
    assert all(s.hour < 18 for s in booking.free_slots([], TUESDAY, "America/New_York", ctx.settings))
    la = booking.free_slots([], TUESDAY, "America/Los_Angeles", ctx.settings)
    assert la and all(16 <= s.hour < 18 for s in la)  # 09:00-10:30 Pacific fits 23:30 India; 09:00 PDT = 16:00 UTC


def test_us_time_zone_labels_no_longer_swallow_other_zones():
    assert str(_zone("(Central European Time)", "America/New_York")) == "Europe/Berlin"
    assert str(_zone("(Central Time - US & Canada)", "America/New_York")) == "America/Chicago"
    assert str(_zone("(Eastern Time - US & Canada)", "UTC")) == "America/New_York"
    assert str(_zone("(Australian Eastern Time)", "UTC")) == "Australia/Sydney"
    assert booking.prospect_zone(None) == "America/New_York"


# --------------------------------------------------------------------------- the offer

def test_a_times_offer_waits_for_a_person_unless_a_campaign_covers_answers(make_ctx):
    ctx, sender, sent, cal = booking_ctx(make_ctx)
    r = meeting_request(ctx, sent)
    p = proposal(ctx)
    assert p is not None and p.status == "draft" and len(p.slots) == 3 and p.inbound_email_id == r.id
    with ctx.db.session() as s:
        offer = s.get(FollowUp, p.offer_followup_id)
        assert offer.status == "awaiting_approval" and offer.id == r.suggested_followup_id  # one reply, not two
        assert "Would one of these times work" in offer.body and "calendly.com" not in offer.body
        drafts = s.execute(select(FollowUp).where(FollowUp.kind == "response", FollowUp.lead_id == p.lead_id)).scalars().all()
        assert len(drafts) == 1
    assert [c[0] for c in cal.calls] == ["POST"] and cal.calls[0][1].endswith("/freeBusy") and cal.events == {}
    # Hybrid mode, or a campaign that does not cover answers: the offer waits for a person
    ctx.settings.require_human_approval = False
    authorize(ctx, kinds=("outreach",))
    run(ctx, sender, utcnow() + timedelta(days=1))
    with ctx.db.session() as s:
        assert s.get(FollowUp, p.offer_followup_id).status == "awaiting_approval"
    # Automatic mode with a campaign that covers answers: the offer to a clear meeting request goes out on its own
    aid = authorize(ctx, kinds=("outreach", "response"))
    with ctx.db.session() as s:
        am.set_mode(s, "automatic", "admin")
    run(ctx, sender, utcnow() + timedelta(days=1))
    with ctx.db.session() as s:
        offer = s.get(FollowUp, p.offer_followup_id)
        assert offer.status == "sent" and offer.approved_by == f"campaign #{aid}" and offer.purpose == "meeting_offer"


@pytest.mark.parametrize("change", ["disabled", "not_connected", "paused"])
def test_nothing_touches_the_calendar_unless_booking_is_on_connected_and_running(make_ctx, change):
    ctx, _sender, sent, cal = booking_ctx(make_ctx, enabled=change != "disabled", connect=change != "not_connected")
    if change == "paused":
        with ctx.db.session() as s:
            am.change_state(s, "pause", "manager")
    r = meeting_request(ctx, sent)
    assert proposal(ctx) is None and cal.calls == []
    with ctx.db.session() as s:
        assert "calendly.com" in s.get(FollowUp, r.suggested_followup_id).body  # the booking-link reply, as before


# --------------------------------------------------------------------------- booking, moving, cancelling

def test_booking_the_chosen_time_creates_one_event_and_a_confirmation_to_approve(make_ctx):
    ctx, _sender, sent, cal = booking_ctx(make_ctx)
    meeting_request(ctx, sent)
    p = proposal(ctx)
    client = TestClient(create_app(ctx))
    page = client.get("/meetings").text
    assert "Meeting times offered" in page and "Book this time" in page
    r = client.post(f"/meetings/proposals/{p.id}/book", data={"slot": "1"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Booked" in r.headers["location"]
    assert list(cal.events) == [f"aimep{p.id}"]
    created = [c for c in cal.calls if c[0] == "POST" and c[1].endswith("/events")]
    assert created[0][2] == {"sendUpdates": "none"}  # Google sends nothing; our approved email does
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        assert (m.source, m.event_id, m.provider, m.status) == ("calendar", f"aimep{p.id}", "google", "scheduled")
        assert m.starts_at == datetime.fromisoformat(p.slots[1]) and s.get(Lead, m.lead_id).outreach_status == "meeting"
        confirm = s.execute(select(FollowUp).where(FollowUp.subject.like("Confirmed:%"))).scalar_one()
        assert confirm.status == "awaiting_approval"
        assert s.get(MeetingProposal, p.id).status == "booked"
        assert s.execute(select(AuditEvent).where(AuditEvent.action == "meeting_booked")).scalars().first()
    again = client.post(f"/meetings/proposals/{p.id}/book", data={"slot": "1"}, headers=ORIGIN, follow_redirects=False)
    assert again.status_code == 303 and len(created) == 1 and len(cal.events) == 1  # no second event or meeting
    with ctx.db.session() as s:
        assert len(s.execute(select(Meeting)).scalars().all()) == 1


def test_a_retry_after_a_lost_answer_does_not_book_twice():
    cal = FakeGoogle()
    with httpx.Client(transport=httpx.MockTransport(cal)) as http:
        g = GoogleCalendar(http, lambda: "token")
        start = datetime(2026, 11, 2, 15, 0)
        first = g.create_event("p7", "Call", "x", start, start + timedelta(minutes=30), DAVID)
        second = g.create_event("p7", "Call", "x", start, start + timedelta(minutes=30), DAVID)  # Google answers 409
    assert first == second == "aimep7" and len(cal.events) == 1


def test_a_slot_that_became_busy_is_not_booked(make_ctx):
    ctx, _sender, sent, cal = booking_ctx(make_ctx)
    meeting_request(ctx, sent)
    p = proposal(ctx)
    start = datetime.fromisoformat(p.slots[0])
    cal.busy.append((start.isoformat() + "Z", (start + timedelta(hours=1)).isoformat() + "Z"))
    r = TestClient(create_app(ctx)).post(f"/meetings/proposals/{p.id}/book", data={"slot": "0"}, headers=ORIGIN,
                                         follow_redirects=False)
    assert "no longer free" in unquote(r.headers["location"]) and cal.events == {}
    with ctx.db.session() as s:
        assert s.execute(select(Meeting)).first() is None and s.get(MeetingProposal, p.id).status == "draft"


def test_moving_and_cancelling_go_through_the_calendar_and_draft_a_note(make_ctx):
    ctx, _sender, sent, cal = booking_ctx(make_ctx)
    meeting_request(ctx, sent)
    p = proposal(ctx)
    client = TestClient(create_app(ctx))
    client.post(f"/meetings/proposals/{p.id}/book", data={"slot": "0"}, headers=ORIGIN)
    with ctx.db.session() as s:
        m = s.execute(select(Meeting)).scalar_one()
        mid, old = m.id, m.starts_at
    r = client.post(f"/meetings/{mid}/move", data={"start_local": "2026-11-03T11:00"}, headers=ORIGIN,
                    follow_redirects=False)
    assert "moved" in unquote(r.headers["location"])
    with ctx.db.session() as s:
        m = s.get(Meeting, mid)
        assert m.starts_at == datetime(2026, 11, 3, 16, 0) and m.rescheduled_from == old  # 11:00 EST = 16:00 UTC
        assert s.execute(select(FollowUp).where(FollowUp.subject.like("New time for our call%"))).scalar_one().status \
            == "awaiting_approval"
    assert cal.events[f"aimep{p.id}"]["start"]["dateTime"] == "2026-11-03T16:00:00Z"
    client.post(f"/meetings/{mid}/cancel", headers=ORIGIN)
    assert cal.events == {} and any(c[0] == "DELETE" for c in cal.calls)
    with ctx.db.session() as s:
        assert s.get(Meeting, mid).status == "cancelled"
        assert s.execute(select(FollowUp).where(FollowUp.subject.like("Our call on%"))).scalar_one().status \
            == "awaiting_approval"


def test_the_sales_manager_is_reminded_once_before_each_meeting(make_ctx):
    ctx = make_ctx()
    now = utcnow()
    with ctx.db.session() as s:
        s.add(Meeting(invitee_email="ann@prospect.example", starts_at=now + timedelta(hours=3),
                      ends_at=now + timedelta(hours=3, minutes=30), timezone="America/Chicago", source="calendar"))
        s.add(Meeting(invitee_email="bob@prospect.example", starts_at=now + timedelta(days=3),
                      ends_at=now + timedelta(days=3, minutes=30), source="calendar"))
    with ctx.db.session() as s:
        assert booking.reminders(s, ctx.settings) == 1 and booking.reminders(s, ctx.settings) == 0
        [alert] = s.execute(select(Alert).where(Alert.source == "meetings")).scalars().all()
        assert "ann@prospect.example" in alert.message and "for them" in alert.message


def test_microsoft_365_reads_busy_times_and_books_idempotently():
    seen = {"txn": {}}

    def graph(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/calendarView"):
            assert request.headers["Prefer"] == 'outlook.timezone="UTC"'
            return httpx.Response(200, json={"value": [
                {"start": {"dateTime": "2026-11-02T15:00:00.0000000"}, "end": {"dateTime": "2026-11-02T16:00:00.0000000"},
                 "showAs": "busy"},
                {"start": {"dateTime": "2026-11-02T17:00:00.0000000"}, "end": {"dateTime": "2026-11-02T18:00:00.0000000"},
                 "showAs": "free"}]})
        if request.method == "POST":
            body = json.loads(request.content)
            event_id = seen["txn"].setdefault(body["transactionId"], f"evt{len(seen['txn']) + 1}")
            return httpx.Response(201, json={"id": event_id})
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(graph)) as http:
        cal = MicrosoftCalendar(http, lambda: "token")
        assert cal.busy(datetime(2026, 11, 2), datetime(2026, 11, 3)) == [(datetime(2026, 11, 2, 15), datetime(2026, 11, 2, 16))]
        start = datetime(2026, 11, 3, 15)
        assert cal.create_event("p9", "Call", "x", start, start + timedelta(minutes=30), DAVID) == "evt1"
        assert cal.create_event("p9", "Call", "x", start, start + timedelta(minutes=30), DAVID) == "evt1"
        cal.cancel_event("evt1")


# --------------------------------------------------------------------------- dashboard status

@pytest.mark.parametrize("provider,enabled,connect,status", [
    ("", False, False, "NOT CONFIGURED"),
    ("google", False, False, "NOT CONFIGURED"),
    ("google", False, True, "WAITING FOR AUTHORIZATION"),
    ("google", True, True, "ACTIVE"),
])
def test_manager_today_says_where_meeting_booking_stands(make_ctx, provider, enabled, connect, status):
    ctx = make_ctx(handler=token_server, **GOOGLE, calendar_provider=provider, meeting_booking_enabled=enabled)
    if connect:
        sign_in(ctx, purpose="calendar")
    with ctx.db.session() as s:
        board = {b["name"]: b["status"] for b in manager_today(ctx, s)["board"]}
    assert board["Meeting booking"] == status
