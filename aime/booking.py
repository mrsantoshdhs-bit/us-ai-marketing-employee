"""A4, meeting booking with consent and a person's approval at every outbound step.

When a prospect asks for a call (a meeting_request reply, A2), the AI employee looks up when the owner is busy in the
connected calendar (aime/integrations/calendar.py), picks a few free slots inside the prospect's business hours and
the owner's working hours, and drafts a reply offering them. A person approves that email like any other (A3).
When the prospect answers with the time that suits them, a person clicks Book: the slot is checked against the
calendar again, one event is created (idempotent: a retry never books twice), the meeting is recorded in the CRM and
a confirmation is drafted for approval. Moving and cancelling work the same way, and a reminder is raised before
each meeting. Nothing here runs unless CALENDAR_PROVIDER is set, MEETING_BOOKING_ENABLED is true and the calendar
account is connected; the booking-link path (Calendly) keeps working as before.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime import automation as am
from aime.crm_ops import log_activity
from aime.models import Alert, Contact, FollowUp, InboundEmail, Lead, Meeting, MeetingProposal
from aime.outreach import set_status
from aime.reply_intel import _signature, reply_footer
from aime.timeutil import as_aware_utc, to_naive_utc, utcnow

US_ZONES = {
    "America/New_York": "CT DE DC FL GA IN KY ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV",
    "America/Chicago": "AL AR IL IA KS LA MN MS MO NE ND OK SD TN TX WI",
    "America/Denver": "CO ID MT NM UT WY",
    "America/Phoenix": "AZ",
    "America/Los_Angeles": "CA NV OR WA",
    "America/Anchorage": "AK",
    "Pacific/Honolulu": "HI",
}
STATE_ZONE = {state: zone for zone, states in US_ZONES.items() for state in states.split()}
OPEN_PROPOSAL = ("draft", "offered")


def cfg(settings) -> dict:
    c = settings.section("meetings")
    return {"owner_tz": c.get("owner_timezone") or settings.schedule.get("timezone") or "America/New_York",
            "owner_hours": c.get("owner_hours", "08:00-22:00"), "prospect_hours": c.get("prospect_hours", "09:00-17:00"),
            "minutes": int(c.get("slot_minutes", 30)), "days": int(c.get("days_ahead", 5)),
            "offer": int(c.get("slots_offered", 3)), "notice": int(c.get("min_notice_hours", 24)),
            "remind": int(c.get("reminder_hours", 24)), "remind_prospect": bool(c.get("remind_prospect", False))}


def ready(session: Session, settings) -> tuple[bool, str]:
    """Whether meeting booking may touch the calendar now, and why not."""
    from aime.integrations import oauth

    provider = (settings.calendar_provider or "").lower()
    if not provider:
        return False, "no calendar is connected (CALENDAR_PROVIDER is not set)"
    if not settings.meeting_booking_enabled:
        return False, "MEETING_BOOKING_ENABLED=false: meeting booking is switched off"
    if provider not in oauth.PROVIDERS or oauth.connected(session, provider, "calendar") is None:
        return False, f"the {provider} calendar account is not connected (Integrations page)"
    if am.get_state(session) != "running":
        return False, "automation is paused or stopped"
    return True, ""


def prospect_zone(lead: Lead | None, default: str = "America/New_York") -> str:
    state = ((lead.company.state if lead and lead.company else None) or "").upper()
    return STATE_ZONE.get(state, default)


def _window(day_local: datetime, hours: str) -> tuple[datetime, datetime]:
    start, end = (time.fromisoformat(x) if x != "24:00" else time(23, 59) for x in hours.split("-"))
    return day_local.replace(hour=start.hour, minute=start.minute), day_local.replace(hour=end.hour, minute=end.minute)


def free_slots(busy: list[tuple[datetime, datetime]], now: datetime, prospect_tz: str, settings) -> list[datetime]:
    """Up to `slots_offered` free starts (naive UTC), at most one per day so the prospect gets a real choice: inside
    the prospect's business hours on their weekdays, inside the owner's working hours, after the minimum notice."""
    c = cfg(settings)
    ptz, otz, length = ZoneInfo(prospect_tz), ZoneInfo(c["owner_tz"]), timedelta(minutes=c["minutes"])
    earliest = as_aware_utc(now) + timedelta(hours=c["notice"])
    out: list[datetime] = []
    day = earliest.astimezone(ptz).replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(c["days"] + 7):  # weekends are skipped without using up the days asked for
        if len(out) >= c["offer"]:
            break
        if day.weekday() < 5:
            start, end = _window(day, c["prospect_hours"])
            at = start
            while at + length <= end:
                o_start, o_end = _window(at.astimezone(otz).replace(hour=0, minute=0), c["owner_hours"])
                local_owner = at.astimezone(otz)
                fits_owner = o_start <= local_owner and local_owner + length <= o_end
                utc = to_naive_utc(at)
                clash = any(b_start < utc + length and utc < b_end for b_start, b_end in busy)
                if at >= earliest and fits_owner and not clash:
                    out.append(utc)
                    break
                at += timedelta(minutes=30)
        day += timedelta(days=1)
    return out


def label(at_utc: datetime, tz: str) -> str:
    local = as_aware_utc(at_utc).astimezone(ZoneInfo(tz))
    return f"{local:%A %d %B, %I:%M %p} {local.tzname()}".replace(" 0", " ")


def _draft(session: Session, settings, lead: Lead, to: str, subject: str, text: str, in_reply_to: str | None,
           contact_id: int | None) -> FollowUp:
    name = (to.split("@")[0].split(".")[0] or "there").capitalize()
    body = f"Hi {name},\n\n{text}\n\n{_signature(settings)}{reply_footer(settings)}"
    draft = FollowUp(lead_id=lead.id, contact_id=contact_id, company_id=lead.company_id, channel="email",
                     kind="response", step=1, due_at=utcnow(), status="awaiting_approval", subject=subject[:300],
                     body=body, to_email=to, in_reply_to=in_reply_to)
    session.add(draft)
    session.flush()
    return draft


def propose(session: Session, settings, calendar, row: InboundEmail, actor: str, now: datetime | None = None) -> str:
    """Offer free slots for a meeting request: a proposal and a reply that waits for a person's approval."""
    now = now or utcnow()
    lead = session.get(Lead, row.lead_id) if row.lead_id else None
    if lead is None:
        return "not matched to a lead"
    if session.execute(select(MeetingProposal).where(MeetingProposal.lead_id == lead.id,
                                                     MeetingProposal.status.in_(OPEN_PROPOSAL))).first():
        return "a proposal is already open for this lead"
    if session.execute(select(Meeting).where(Meeting.lead_id == lead.id, Meeting.status == "scheduled",
                                             Meeting.ends_at >= now)).first():
        return "a meeting is already booked"
    c, tz = cfg(settings), prospect_zone(lead)
    busy = calendar.busy(now, now + timedelta(days=c["days"] + 9))
    slots = free_slots(busy, now, tz, settings)
    if not slots:
        return "no free slot in the next days: a person proposes times"
    lines = "\n".join(f"  - {label(s, tz)}" for s in slots)
    text = (f"Thank you, happy to talk. Would one of these times work for a {c['minutes']}-minute call?\n\n{lines}\n\n"
            "Just reply with the one that suits you, or suggest another time.")
    existing = session.get(FollowUp, row.suggested_followup_id) if row.suggested_followup_id else None
    subject = f"Re: {row.subject}" if not (row.subject or "").lower().startswith("re:") else row.subject
    if existing is not None and existing.status == "awaiting_approval":  # one reply to them, not two
        existing.body = (f"Hi {(row.from_email.split('@')[0].split('.')[0] or 'there').capitalize()},\n\n{text}\n\n"
                         f"{_signature(settings)}{reply_footer(settings)}")
        existing.blocked_reason = None
        offer = existing
    else:
        offer = _draft(session, settings, lead, row.from_email, subject, text, row.message_id, row.contact_id)
        row.suggested_followup_id = offer.id
    p = MeetingProposal(lead_id=lead.id, contact_id=row.contact_id, inbound_email_id=row.id,
                        prospect_email=row.from_email, prospect_tz=tz, minutes=c["minutes"],
                        slots=[s.isoformat() for s in slots], status="draft", offer_followup_id=offer.id,
                        provider=calendar.name)
    session.add(p)
    session.flush()
    log_activity(session, f"Meeting times proposed ({len(slots)} slots, {tz}); the reply waits for your approval", actor,
                 type_="meeting", company_id=lead.company_id, lead_id=lead.id)
    am.audit(session, actor, "meeting_proposed", f"proposal #{p.id}", f"{len(slots)} free slots for {row.from_email}")
    return f"proposal #{p.id}"


def propose_pending(session: Session, settings, calendar, actor: str, now: datetime | None = None) -> int:
    """Meeting requests of the last 7 days that have no proposal or meeting yet."""
    now = now or utcnow()
    done = 0
    rows = session.execute(select(InboundEmail).where(
        InboundEmail.intent == "meeting_request", InboundEmail.lead_id.is_not(None),
        InboundEmail.is_simulated.is_(False), InboundEmail.received_at >= now - timedelta(days=7))).scalars().all()
    for row in rows:
        if session.execute(select(MeetingProposal.id).where(MeetingProposal.inbound_email_id == row.id)).first():
            continue
        done += propose(session, settings, calendar, row, actor, now).startswith("proposal")
    return done


def book(session: Session, settings, calendar, p: MeetingProposal, index: int, actor: str,
         now: datetime | None = None) -> Meeting:
    """A person books the slot the prospect chose: checked again against the calendar, then one event."""
    now = now or utcnow()
    ok, why = ready(session, settings)
    if not ok:
        raise ValueError(f"Not booked: {why}")
    if p.status == "booked" and p.meeting_id:
        return session.get(Meeting, p.meeting_id)  # already booked: the same meeting, never a second
    if p.status not in OPEN_PROPOSAL:
        raise ValueError(f"Not booked: the proposal is {p.status}")
    slots = [datetime.fromisoformat(s) for s in p.slots or []]
    if not 0 <= index < len(slots):
        raise ValueError("Not booked: no such slot")
    start, end = slots[index], slots[index] + timedelta(minutes=p.minutes)
    if start <= now:
        raise ValueError("Not booked: that time has passed; propose new times")
    if any(b_start < end and start < b_end for b_start, b_end in calendar.busy(start, end)):
        raise ValueError("Not booked: the calendar is no longer free at that time; propose new times")
    lead = session.get(Lead, p.lead_id)
    contact = session.get(Contact, p.contact_id) if p.contact_id else None
    existing = session.execute(select(Meeting).where(Meeting.lead_id == lead.id, Meeting.status == "scheduled",
                                                     Meeting.starts_at == start)).scalars().first()
    if existing is None:
        event_id = calendar.create_event(f"p{p.id}", f"Call with {lead.company.name}",
                                         f"Booked by the AI Sales Employee from proposal #{p.id}.", start, end,
                                         p.prospect_email)
        existing = Meeting(lead_id=lead.id, company_id=lead.company_id, contact_id=p.contact_id,
                           invitee_email=p.prospect_email, invitee_name=" ".join(x for x in (contact.first_name, contact.last_name) if x) or None if contact else None,
                           title=f"Call with {lead.company.name}", starts_at=start, ends_at=end, timezone=p.prospect_tz,
                           source="calendar", status="scheduled", event_id=event_id, provider=calendar.name)
        session.add(existing)
        session.flush()
    from aime.meetings import _advance_opportunity, close_open_emails

    existing.opportunity_id = _advance_opportunity(session, lead, existing, actor).id
    set_status(session, lead, "meeting", actor, f"{start:%Y-%m-%d %H:%M} UTC (booked from proposal #{p.id})")
    close_open_emails(session, lead, f"Meeting booked for {label(start, p.prospect_tz)}: not needed")
    p.status, p.chosen_slot, p.meeting_id, p.approved_by = "booked", start, existing.id, actor
    _draft(session, settings, lead, p.prospect_email, f"Confirmed: our call on {label(start, p.prospect_tz)}",
           f"Thank you, our call is confirmed for {label(start, p.prospect_tz)} ({p.minutes} minutes).",
           None, p.contact_id)
    log_activity(session, f"Meeting booked for {label(start, p.prospect_tz)} by {actor}; the confirmation waits for "
                 "your approval", actor, type_="meeting", company_id=lead.company_id, lead_id=lead.id,
                 opportunity_id=existing.opportunity_id)
    am.audit(session, actor, "meeting_booked", f"meeting #{existing.id}", f"proposal #{p.id}, slot {index + 1}, "
             f"{start:%Y-%m-%d %H:%M} UTC, calendar event created")
    return existing


def move(session: Session, settings, calendar, m: Meeting, start: datetime, actor: str,
         now: datetime | None = None) -> None:
    """A person moves a booked meeting: the new time is checked against the calendar, then the event moves."""
    now = now or utcnow()
    ok, why = ready(session, settings)
    if not ok:
        raise ValueError(f"Not moved: {why}")
    if m.status != "scheduled" or not m.event_id:
        raise ValueError("Not moved: only a scheduled meeting booked through the calendar can be moved here")
    end = start + (m.ends_at - m.starts_at)
    if start <= now:
        raise ValueError("Not moved: that time has passed")
    own = (m.starts_at, m.ends_at)
    if any(b_start < end and start < b_end for b_start, b_end in calendar.busy(start, end) if (b_start, b_end) != own):
        raise ValueError("Not moved: the calendar is not free at that time")
    calendar.move_event(m.event_id, start, end)
    m.rescheduled_from, m.starts_at, m.ends_at, m.reminded_at = m.starts_at, start, end, None
    lead = session.get(Lead, m.lead_id) if m.lead_id else None
    if lead is not None:
        _draft(session, settings, lead, m.invitee_email, f"New time for our call: {label(start, m.timezone or 'UTC')}",
               f"Our call has moved to {label(start, m.timezone or 'UTC')}. I hope that still suits you.",
               None, m.contact_id)
        log_activity(session, f"Meeting moved to {label(start, m.timezone or 'UTC')} by {actor}", actor,
                     type_="meeting", company_id=m.company_id, lead_id=m.lead_id, opportunity_id=m.opportunity_id)
    am.audit(session, actor, "meeting_moved", f"meeting #{m.id}", f"to {start:%Y-%m-%d %H:%M} UTC")


def cancel_event(session: Session, settings, calendar, m: Meeting, actor: str) -> None:
    """With the meeting cancelled in the CRM: cancel the calendar event too, and draft a note for approval."""
    if not m.event_id or calendar is None:
        return
    calendar.cancel_event(m.event_id)
    lead = session.get(Lead, m.lead_id) if m.lead_id else None
    if lead is not None:
        _draft(session, settings, lead, m.invitee_email, f"Our call on {label(m.starts_at, m.timezone or 'UTC')}",
               f"I am sorry, I have to cancel our call on {label(m.starts_at, m.timezone or 'UTC')}. "
               "Could we find another time?", None, m.contact_id)
    am.audit(session, actor, "meeting_cancelled", f"meeting #{m.id}", "calendar event cancelled")


def reminders(session: Session, settings, now: datetime | None = None) -> int:
    """An alert for the Sales Manager before each meeting (and, if configured, a reminder to approve for the
    prospect). Each meeting is reminded once; moving it reminds again."""
    now = now or utcnow()
    c = cfg(settings)
    due = session.execute(select(Meeting).where(
        Meeting.status == "scheduled", Meeting.is_simulated.is_(False), Meeting.reminded_at.is_(None),
        Meeting.starts_at > now, Meeting.starts_at <= now + timedelta(hours=c["remind"]))).scalars().all()
    for m in due:
        who = m.invitee_name or m.invitee_email
        session.add(Alert(level="info", source="meetings",
                          message=f"Meeting with {who} at {label(m.starts_at, c['owner_tz'])} "
                                  f"({label(m.starts_at, m.timezone or 'UTC')} for them): prepare"))
        lead = session.get(Lead, m.lead_id) if m.lead_id else None
        if c["remind_prospect"] and lead is not None:
            _draft(session, settings, lead, m.invitee_email, f"Reminder: our call on {label(m.starts_at, m.timezone or 'UTC')}",
                   f"A short reminder of our call on {label(m.starts_at, m.timezone or 'UTC')}. Talk soon.",
                   None, m.contact_id)
        m.reminded_at = now
    return len(due)
