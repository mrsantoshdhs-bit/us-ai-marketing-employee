"""Meetings: bookings detected from Calendly's notification emails in the sales mailbox, or logged by hand.

Calendly sends the host "New Event: <invitee> - <time> <date> - <event type>" and "Canceled: ..." emails
with the invitee's name and email and the event date/time with its time zone, given either as a range
("10:00am - 10:30am (Eastern Time) Thursday, October 29, 2026") or as the start only ("10:00am - Thursday,
October 29, 2026 (India Standard Time)"; the length then comes from the event type, "30 Minute Meeting"). A booking
is matched to the lead by the invitee's email, then by their company's domain; the meeting is recorded, the lead
moves to "meeting", the opportunity to 30%, the lead's remaining sequence emails and any booking link still waiting
to go out are cancelled, and the Sales Manager is notified. Cancellations reopen the lead ("interested") with a
follow-up next step, unless another meeting with it is still booked. Reschedules ("Rescheduled:"/"Updated:", or a
"New Event:" that names its former time) move the recorded meeting instead of adding a second one; Calendly's
cancellation of the former time then changes nothing. A cancellation or reschedule whose time matches nothing
applies to the invitee's only upcoming meeting; otherwise it goes to review. Two upcoming meetings for one lead
(the same person booking twice) raise a warning until one is cancelled; overlapping meetings with different people
raise an alert (Calendly itself prevents double booking on the calendar it manages). Unmatched or unparseable
notifications go to review, and an unmatched booking can be linked to a lead on the Meetings page. A time whose zone
is not named or not known is not guessed: the notice goes to review. A booking from someone who opted out or is
suppressed, on a disqualified lead, or on a deal already lost, is recorded, and the lead and its deal wait for a person.
Nothing here creates, moves or cancels anything in Calendly: it only records what Calendly reports.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.crm_ops import WROTE_AGAIN, canonical_address, canonical_sql, is_suppressed, lead_with_the_deals, log_activity
from aime.dedupe import registrable_domain
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, MeetingProposal, Opportunity
from aime.notify import notify_event
from aime.outreach import company_opted_out, set_status
from aime.timeutil import utcnow

TZ_NAMES = {
    "eastern": "America/New_York", "central": "America/Chicago", "mountain": "America/Denver",
    "arizona": "America/Phoenix", "pacific": "America/Los_Angeles", "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu", "india": "Asia/Kolkata", "ist": "Asia/Kolkata", "utc": "UTC", "gmt": "UTC",
}
FOREIGN_ZONES = {
    "central european": "Europe/Berlin", "eastern european": "Europe/Athens", "western european": "Europe/Lisbon",
    "british": "Europe/London", "greenwich": "Europe/London", "australian eastern": "Australia/Sydney",
    "australian central": "Australia/Adelaide", "australian western": "Australia/Perth", "singapore": "Asia/Singapore",
    "japan": "Asia/Tokyo", "china": "Asia/Shanghai", "gulf": "Asia/Dubai", "brasilia": "America/Sao_Paulo",
}
TIME_RANGE = re.compile(r"(\d{1,2}(?::\d{2})?\s*[ap]m)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*[ap]m)", re.I)
ONE_TIME = re.compile(r"\d{1,2}(?::\d{2})?\s*[ap]m", re.I)
# Calendly writes the date as the account's locale does: "Thursday, October 29, 2026" or "Wednesday, 28 October 2026"
_WEEKDAY = r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun),?\s+)?"
DATE_MDY = re.compile(_WEEKDAY + r"\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
DATE_DMY = re.compile(_WEEKDAY + r"\b(\d{1,2})\s+([a-z]{3,9})\.?,?\s+(\d{4})", re.I)
LENGTH = re.compile(r"\b(\d{1,3})\s*-?\s*(min|mins|minute|minutes|hr|hrs|hour|hours)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# what follows a date/time field
WHEN_ENDS = r"Former|Previous|Invitee|Location|Description|Questions|View event|Event Type|Cancel|Reason|$"
OPEN = ("pending", "awaiting_approval", "approved")
OFFSET_LABEL = re.compile(r"\b(?:utc|gmt)\s*[-+\u2212\u2013]\s*\d")  # "GMT+5:30": read as plain GMT it would be 5.5 hours off
HELD_NOTE = "meeting recorded, nothing moved"  # how record_meeting says the lead waits for a person
HAS_OUTCOME_NOTE = "this meeting already has an outcome"
UNKNOWN_CANCEL = "cancellation for an unknown meeting"
FORMER_TIME = "former time"
OPTED_OUT_HOLD = "they opted out or are on the do-not-contact list"
BOOKING_ARRIVED = "They booked a meeting while this answer waits: check that it still fits"
REBOOK_TASK, OUTCOME_TASK = "Rebook the meeting with", "Record the outcome of the meeting"
OVERLAP_ALERT, WAITING_ALERT = "meeting_overlap:", "calendly_waiting:"


def _zone(text: str, default: str | None = None) -> ZoneInfo | None:
    """The zone the label in brackets names; with none named, or one it cannot place, `default` if given, else None."""
    m = re.search(r"\(([^)]+)\)", text)
    label = (m.group(1) if m else "").strip()
    try:
        if "/" in label:
            return ZoneInfo(label.split()[0])
    except (ZoneInfoNotFoundError, ValueError, OSError):  # "a/../b", "America/": anyone can write a notice
        pass
    low = label.lower()
    if OFFSET_LABEL.search(low):
        return ZoneInfo(default) if default else None
    for foreign, name in FOREIGN_ZONES.items():  # "Central European Time" is not US Central
        if foreign in low:
            return ZoneInfo(name)
    for key, name in TZ_NAMES.items():
        if re.search(rf"\b{key}\b", low):
            return ZoneInfo(name)
    return ZoneInfo(default) if default else None


def _clock(value: str) -> tuple[int, int]:
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", value.strip(), re.I)
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    return (hour % 12) + (12 if ampm == "pm" else 0), minute


def _length(text: str, default: int) -> int:
    """Minutes named in an event type or link ("30 Minute Meeting", "30min", "1 Hour"), else `default`."""
    m = LENGTH.search(text or "")
    if not m:
        return default
    return max(5, min(int(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1), 480))


def booking_minutes(settings, default: int = 30) -> int:
    """The length of the meeting the booking link offers, from the link itself (calendly.com/<you>/30min)."""
    return _length(urlparse(getattr(settings, "calendly_url", "") or "").path.replace("/", " "), default)


def local_time(at: datetime, tz: str | None) -> str:
    """"Thu Oct 29, 10:00 AM IST" for a naive UTC time, in the meeting's own time zone (UTC when unknown)."""
    try:
        zone = ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    local = at.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
    return f"{local:%a %b %d}, {local.hour % 12 or 12}:{local:%M %p} {local.tzname()}"


def _when_label(m: Meeting) -> str:
    local = local_time(m.starts_at, m.timezone)
    return local if local.endswith(" UTC") else f"{local} ({m.starts_at:%H:%M} UTC)"


def _notice_head(subject: str, body: str, own: set[str]) -> tuple[str, str | None, str, str | None]:
    """The subject without its forward marks, the kind of notice (None: not one we know), the body on one line, and who it names."""
    subject = re.sub(r"^\s*(?:(?:fwd?|fw|wg|rv|tr|sv|aw)\s*:\s*)+", "", subject, flags=re.I)  # a notice forwarded to the sales mailbox
    kind = ("cancelled" if re.match(r"\s*cancel+ed\s*:", subject, re.I)
            else "booked" if re.match(r"\s*new event\s*:", subject, re.I)
            else "rescheduled" if re.match(r"\s*(?:rescheduled|updated)\s*:", subject, re.I) else None)
    text = " ".join(" ".join(re.sub(r"^[\s>]+", "", line) for line in body.splitlines()).split())  # a forward may quote each line with ">"
    m = re.search(r"Invitee Email:\s*(" + EMAIL_RE.pattern + ")", text, re.I)
    email = m.group(1).lower() if m else next((e.lower() for e in EMAIL_RE.findall(text) if e.lower() not in own and "calendly" not in e.lower()), None)
    return subject, kind, text, email


def parse_calendly(subject: str, body: str, own: set[str], default_minutes: int = 30) -> dict | None:
    """{kind, invitee_name, invitee_email, starts_at, ends_at (UTC naive), event, tz, previous_starts_at} or None.
    kind: booked | cancelled | rescheduled. Without an end time the length comes from the event type
    ("30 Minute Meeting"), else `default_minutes`. A time with no zone, or one it cannot place, gives None: a person looks."""
    subject, kind, text, email = _notice_head(subject, body, own)
    if kind is None:
        return None
    name = None
    m = re.search(r"Invitee(?: Name)?:\s*(.+?)\s+(?:Invitee Email|Email|Event)", text, re.I)
    if m:
        name = m.group(1).strip()
    else:
        sm = re.match(r"\s*(?:new event|cancel+ed|rescheduled|updated)\s*:\s*(.+?)\s+-\s+", subject, re.I)
        name = sm.group(1).strip() if sm else None
    m = re.search(r"Event Type:\s*(.+?)\s+(?:Invitee|Event Date|Date/Time|Location|$)", text, re.I)
    event = (m.group(1) if m else re.split(r"\s+-\s+", subject)[-1] if " - " in subject else "Meeting").strip()
    minutes = _length(event, default_minutes)
    when = re.search(rf"(?<!former )(?<!previous )Event Date/Time:\s*(.+?)(?:{WHEN_ENDS})", text, re.I)
    parsed = _when(when.group(1), minutes) if when else None
    if parsed is None:  # the subject has the start: "New Event: Ana - 10:00am Thu, Oct 29, 2026 - 30 Minute Meeting"
        zone = re.search(r"\([^)]*\)", when.group(1)) if when else None
        parsed = _when(f"{subject} {zone.group(0) if zone else ''}", minutes)
    if not (parsed and email):
        return None
    # "Former Event Date/Time: ...", or as Calendly's "Updated:" email has it: "Date & Time: Former: ... Updated: ..."
    before = re.search(rf"(?:Former|Previous)(?: Event)?(?: Date/Time)?:\s*(.+?)(?:Updated:|Event Date/Time|{WHEN_ENDS})",
                       text, re.I)
    previous = _when(before.group(1), minutes) if before else None
    if before and previous is None and _date(before.group(1)) and ONE_TIME.search(before.group(1)):
        return None  # a former time it cannot place: moving the wrong meeting is worse than asking
    if kind == "booked" and previous is not None:
        kind = "rescheduled"  # a new event that names its former time is a reschedule
    return {"kind": kind, "invitee_name": name, "invitee_email": email, "starts_at": parsed[0], "ends_at": parsed[1],
            "event": event[:200], "tz": parsed[2], "previous_starts_at": previous[0] if previous else None}


def _when(when_text: str, minutes: int = 30) -> tuple[datetime, datetime, str] | None:
    """(start, end) in UTC (naive) and the zone name, from "10:00am - 10:20am (Eastern Time) Thursday, October 29, 2026"
    or, with the start only, "10:00am - Thursday, October 29, 2026 (India Standard Time)" (end = start + minutes).
    None without a date, a time and a zone it can place."""
    times, day = TIME_RANGE.search(when_text), _date(when_text)
    one = None if times else ONE_TIME.search(when_text)
    if not (day and (times or one)):
        return None
    tz = _zone(when_text)
    if tz is None:
        return None
    to_utc = lambda d: d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)  # noqa: E731
    h1, m1 = _clock(times.group(1) if times else one.group(0))
    start = day.replace(hour=h1, minute=m1, tzinfo=tz)
    if not times:
        return to_utc(start), to_utc(start) + timedelta(minutes=minutes), str(tz)
    h2, m2 = _clock(times.group(2))
    end = day.replace(hour=h2, minute=m2, tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    return to_utc(start), to_utc(end), str(tz)


def _date(text: str) -> datetime | None:
    """The first date written month first ("Oct 29, 2026") or day first ("28 October 2026"), with a real month name."""
    found = [(m.start(), m.group(1), m.group(2), m.group(3)) for m in (DATE_MDY.search(text),) if m] + \
            [(m.start(), m.group(2), m.group(1), m.group(3)) for m in (DATE_DMY.search(text),) if m]
    for _, month, day, year in sorted(found):
        try:
            return datetime.strptime(f"{month[:3]} {day} {year}", "%b %d %Y")
        except ValueError:
            continue
    return None


def match_lead(session: Session, email: str, test_recipients=frozenset()) -> tuple[Lead | None, Contact | None]:
    contact = session.execute(select(Contact).where(func.lower(Contact.email) == email.lower())).scalars().first()
    # a returning customer has a closed lead and a new one: the booking belongs to the one still being worked
    closed_last = (Lead.status.in_(("converted", "merged", "disqualified")), Lead.id)
    if contact:
        lead = session.execute(select(Lead).where(Lead.contact_id == contact.id).order_by(*closed_last)).scalars().first() or \
            session.execute(select(Lead).where(Lead.company_id == contact.company_id).order_by(*closed_last)).scalars().first()
        return lead, contact
    domain = registrable_domain(email.split("@", 1)[1])
    company = session.execute(select(Company).where(Company.domain == domain, Company.merged_into_id.is_(None))).scalars().first()
    if company:
        return session.execute(select(Lead).where(Lead.company_id == company.id).order_by(*closed_last)).scalars().first(), None
    # The address we emailed (or that replied to us) for a lead: e.g. a prospect booking from their reply address.
    # Only an email that went out counts: a draft nobody approved says nothing about who booked.
    went_to = ((func.lower(FollowUp.to_email) == email.lower()) | (func.lower(FollowUp.delivered_to) == email.lower())) \
        & FollowUp.sent_at.is_not(None)
    if email.lower() in test_recipients:
        # Test mode: every lead's email went to this address, so the last one emailed is not who booked. The lead
        # that was sent a booking link here is; with none or several, a person links the booking on Meetings.
        leads = set(session.execute(select(FollowUp.lead_id).where(went_to, FollowUp.purpose == "booking_link")).scalars())
        return (session.get(Lead, leads.pop()) if len(leads) == 1 else None), None
    f = session.execute(select(FollowUp).where(went_to).order_by(FollowUp.id.desc())).scalars().first()
    if f is not None:
        return session.get(Lead, f.lead_id), None
    r = session.execute(select(InboundEmail).where(func.lower(InboundEmail.from_email) == email, InboundEmail.lead_id.is_not(None))
                        .order_by(InboundEmail.id.desc())).scalars().first()
    return (session.get(Lead, r.lead_id) if r else None), None


def record_meeting(session: Session, settings, info: dict, actor: str, row: InboundEmail | None = None,
                   source: str = "calendly_email", lead: Lead | None = None) -> tuple[Meeting | None, str]:
    """Create, or cancel, the meeting described by `info`. Returns (meeting, note). A notice from Calendly names only
    the invitee, so their email finds the lead; a person logging a meeting on a lead's page gives `lead`, and the
    meeting belongs to that lead whatever address they typed: it never moves a lead found by that address."""
    logged_on = lead
    if lead is None:
        lead, contact = match_lead(session, info["invitee_email"], getattr(settings, "test_recipients", frozenset()))
    else:
        contact = session.execute(select(Contact).where(Contact.company_id == lead.company_id,
                                                        func.lower(Contact.email) == info["invitee_email"])).scalars().first()
    simulated = bool(row and row.is_simulated)
    same_slot = select(Meeting).where(func.lower(Meeting.invitee_email) == info["invitee_email"],
                                      Meeting.starts_at == info["starts_at"])
    if logged_on is not None:
        same_slot = same_slot.where(Meeting.lead_id == logged_on.id)
    # A booking repeats only a meeting that is still scheduled: re-booking a cancelled time is a new booking.
    scheduled = session.execute(same_slot.where(Meeting.status == "scheduled")).scalars().first()
    existing = scheduled or session.execute(same_slot.order_by(Meeting.id.desc())).scalars().first()
    if info["kind"] == "rescheduled":
        if scheduled is not None:
            return scheduled, "already recorded"
        moved = _the_meeting(session, info["invitee_email"], info.get("previous_starts_at"))
        if moved is None:
            info = {**info, "kind": "booked"}  # nothing to move: record it as a booking
        else:
            old = moved.starts_at
            moved.starts_at, moved.ends_at, moved.timezone = info["starts_at"], info["ends_at"], info.get("tz")
            moved.rescheduled_from = old
            for opp in session.execute(select(Opportunity).where(Opportunity.id == moved.opportunity_id,
                                                                 Opportunity.stage.not_in(("won", "lost")))).scalars():
                if not _person_decides(session, moved):
                    opp.next_step = f"Meeting {_when_label(moved)} (rescheduled): prepare, then record the outcome"
            close_outcome_tasks(session, moved.lead_id)
            msg = f"{'SIMULATED: ' if moved.is_simulated else ''}Meeting rescheduled: {info['invitee_name'] or info['invitee_email']} " \
                  f"from {local_time(old, moved.timezone)} to {_when_label(moved)}"
            _tell(session, settings, msg, actor, moved)
            return moved, "meeting rescheduled"
    if info["kind"] == "cancelled":
        if existing is None:
            moved = session.execute(select(Meeting).where(func.lower(Meeting.invitee_email) == info["invitee_email"],
                                                          Meeting.rescheduled_from == info["starts_at"])).scalars().first()
            if moved is not None:  # Calendly also cancels the former time of a meeting it moved
                return moved, f"{FORMER_TIME} of a rescheduled meeting: nothing to cancel"
            if _an_earlier_notice_names(session, settings, info, row):  # the meeting keeps only the last time it left
                return _the_meeting(session, info["invitee_email"], None), f"{FORMER_TIME} named by an earlier notice: nothing to cancel"
            existing = _the_meeting(session, info["invitee_email"], None)
        if existing is None:
            return None, f"{UNKNOWN_CANCEL} with {info['invitee_email']}"
        if existing.status == "cancelled":
            return existing, "already cancelled"
        if existing.status in ("completed", "no_show"):  # a person recorded what happened: a late notice does not undo it
            return existing, f"{HAS_OUTCOME_NOTE} ({existing.status.replace('_', ' ')}), so the cancellation changed nothing: check it in Calendly"
        existing.status = "cancelled"
        reopen_after_cancel(session, existing, actor)
        close_outcome_tasks(session, existing.lead_id)
        _tell(session, settings, f"{'SIMULATED: ' if existing.is_simulated else ''}Meeting cancelled: "
                                 f"{info['invitee_name'] or info['invitee_email']} ({_when_label(existing)})",
              actor, existing)
        return existing, "meeting cancelled"
    if scheduled or (existing is not None and existing.status in ("completed", "no_show")):
        return existing, "already recorded"  # the same notice again; only a cancelled time can be booked again
    if (moved := _already_moved(session, settings, info, row)) is not None:
        return moved, f"{FORMER_TIME} of a rescheduled meeting: nothing to add"
    if _cancel_read_first(session, settings, info, row):
        return None, ("not recorded: a cancellation of this time, dated after this notice, was read first. Check it in Calendly; "
                      "if the meeting is live, log it on the lead's page")
    meeting = Meeting(lead_id=lead.id if lead else None, company_id=lead.company_id if lead else None,
                      contact_id=contact.id if contact else None, invitee_email=info["invitee_email"],
                      invitee_name=info.get("invitee_name"), title=info.get("event") or "Meeting",
                      starts_at=info["starts_at"], ends_at=info["ends_at"], timezone=info.get("tz"), source=source,
                      status="scheduled", inbound_email_id=row.id if row else None, is_simulated=simulated,
                      rescheduled_from=info.get("previous_starts_at"))  # a reschedule read before its booking names the time it left
    session.add(meeting)
    session.flush()
    label = "SIMULATED: " if simulated else ""
    who = info["invitee_name"] or info["invitee_email"]
    if lead is None:
        msg = f"{label}Meeting booked with {who} on {_when_label(meeting)}, not matched to a lead: link it on Meetings"
        session.add(Alert(level="info", source="meetings", message=msg[:500]))
        notify_event(settings, msg)
        return meeting, f"meeting with {info['invitee_email']} recorded but not matched to a lead"
    hold = book_on_lead(session, settings, lead, contact, meeting, actor, f"{info['starts_at']:%Y-%m-%d %H:%M} UTC")
    if hold is None:
        lead.last_activity_at = utcnow()
        if not simulated:
            from aime.handover import handover
            handover(session, settings, session.get(Opportunity, meeting.opportunity_id),
                     f"meeting booked for {_when_label(meeting)} ({info['invitee_email']})", actor)
    closed = close_open_emails(session, lead, f"Meeting booked for {_when_label(meeting)}: not needed")
    if source == "calendly_email":
        close_open_proposals(session, lead)
    overlap = session.execute(select(Meeting.id).where(Meeting.id != meeting.id, Meeting.status == "scheduled",
                                                       func.lower(Meeting.invitee_email) != info["invitee_email"],
                                                       Meeting.starts_at < meeting.ends_at, Meeting.ends_at > meeting.starts_at)).first()
    if overlap:
        session.add(Alert(level="warning", source="meetings", key=f"{OVERLAP_ALERT}{min(meeting.id, overlap[0])}-{max(meeting.id, overlap[0])}",
                          message=f"Meeting #{meeting.id} overlaps meeting #{overlap[0]}: check your calendar"))
    msg = f"{label}Meeting booked with {who} ({lead.company.name}) on {_when_label(meeting)}"
    if hold is None:
        session.add(Alert(level="info", source="meetings", message=msg[:500]))
        notify_event(settings, msg)
    log_activity(session, msg + (f", but {hold}" if hold else "") + (f"; {closed} unsent email(s) to them cancelled" if closed else ""), actor,
                 type_="meeting", company_id=lead.company_id, lead_id=lead.id, opportunity_id=meeting.opportunity_id)
    if hold:
        return meeting, _held_note(hold)
    return meeting, "meeting recorded; lead moved to meeting; opportunity advanced"


def held_by_opt_out(session: Session, lead: Lead, contact: Contact | None, invitee: str | None) -> bool:
    """Someone said no here, or an address here is on the do-not-contact list: the lead's own contact, the contact the
    booking matched, the address that booked, and anyone at the company (outreach.company_opted_out). A booking does
    not take that back: only a person does. This is the opt-out part of outreach.reopen_refusal, which also refuses
    every lead that is not stopped."""
    people = [p for p in (lead.contact, contact) if p is not None]
    addresses = {a.lower() for a in (invitee, *(p.email for p in people)) if a}
    return (lead.outreach_status == "unsubscribed" or any(p.consent_status == "opted_out" for p in people)
            or any(is_suppressed(session, a, lead.company.domain) for a in addresses)
            or session.execute(select(Contact.id).where(canonical_sql(Contact.email).in_({canonical_address(a) for a in addresses}),
                                                        Contact.consent_status == "opted_out").limit(1)).first() is not None
            or company_opted_out(session, lead.company))


def _the_deal(session: Session, lead: Lead) -> Opportunity | None:
    """The deal a booking would act on, as reply_intel.ensure_opportunity picks it: an open one first."""
    return session.execute(select(Opportunity).where(Opportunity.lead_id == lead_with_the_deals(session, lead).id)
                           .order_by(Opportunity.stage.in_(("won", "lost")), Opportunity.id)).scalars().first()


def booking_hold(session: Session, lead: Lead, contact: Contact | None, invitee: str | None) -> str | None:
    """Why a booking must not move this lead or its deal, in words, or None."""
    if held_by_opt_out(session, lead, contact, invitee):
        return OPTED_OUT_HOLD
    if lead.status == "disqualified":
        return "this lead is disqualified"
    deal = _the_deal(session, lead)
    if deal is not None and deal.stage == "lost":
        return f"its deal was lost ({deal.lost_reason or 'no reason recorded'})"
    return None


def book_on_lead(session: Session, settings, lead: Lead, contact: Contact | None, meeting: Meeting, actor: str, note: str) -> str | None:
    """What a booking changes on its lead (record_meeting, link_meeting, booking.book): the deal moves on and the lead
    goes to "meeting", unless booking_hold says a person decides first. The Meeting row stays either way, because the
    meeting did happen in the calendar; it then has no deal. Returns why it holds, or None. A deal lost for another
    reason stays lost, and its next step asks. The handover and what follows are the caller's."""
    _close_tasks(session, lead.id, REBOOK_TASK)
    hold = booking_hold(session, lead, contact, meeting.invitee_email)
    if hold is None:
        meeting.opportunity_id = _advance_opportunity(session, lead, meeting, actor).id
        set_status(session, lead, "meeting", actor, note)
        return None
    deal = _the_deal(session, lead)
    if hold != OPTED_OUT_HOLD and deal is not None and deal.stage == "lost":
        deal.next_step = f"They booked a meeting on {_when_label(meeting)}: a person decides whether to reopen this deal"
    msg = (f"{'SIMULATED: ' if meeting.is_simulated else ''}Meeting booked with {meeting.invitee_name or meeting.invitee_email} "
           f"({lead.company.name}) on {_when_label(meeting)}, but {hold}. It is recorded; the lead and its deal stay as they are until a person decides.")
    session.add(Alert(level="warning", source="meetings", message=msg[:500]))
    notify_event(settings, msg)
    return hold


def _held_note(hold: str) -> str:
    return f"{HELD_NOTE}: {hold}. The lead and its deal stay as they are until a person decides"


def _person_decides(session: Session, meeting: Meeting) -> bool:
    """A later change to this meeting must not write "prepare" on its deal: nobody is to be prompted to contact them."""
    lead = session.get(Lead, meeting.lead_id) if meeting.lead_id else None
    return lead is not None and (lead.status == "disqualified" or held_by_opt_out(
        session, lead, session.get(Contact, meeting.contact_id) if meeting.contact_id else None, meeting.invitee_email))


def _close_tasks(session: Session, lead_id: int | None, subject: str) -> None:
    """Mark a lead's pending tasks of this kind done: what they asked a person to do no longer applies."""
    if not lead_id:
        return
    for t in session.execute(select(FollowUp).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task", FollowUp.status == "pending",
                                                    FollowUp.subject.startswith(subject, autoescape=True))).scalars():
        t.status = "done"


def close_outcome_tasks(session: Session, lead_id: int | None) -> None:
    """The meeting was cancelled, moved or given an outcome: nobody needs to record how it went."""
    _close_tasks(session, lead_id, OUTCOME_TASK)


def close_open_proposals(session: Session, lead: Lead) -> None:
    """They booked another way: the times we offered, or were about to offer, are not open any more."""
    for p in session.execute(select(MeetingProposal).where(MeetingProposal.lead_id == lead.id, MeetingProposal.status.in_(("draft", "offered")))).scalars():
        p.status = "cancelled"


def _own_addresses(settings) -> set[str]:
    return {a.lower() for a in (settings.smtp_from, settings.imap_credentials[0]) if a}


def _read_notice(settings, row: InboundEmail) -> dict | None:
    """The notice as it reads, or None. It runs on mail nobody has vouched for yet, and on what waits at every inbox run: a time that is no time ("10:99am") must not stop either."""
    try:
        return parse_calendly(row.subject or "", row.body_text or "", _own_addresses(settings), booking_minutes(settings))
    except (ValueError, OverflowError):
        return None


def _notices(session: Session, settings, invitee: str, row: InboundEmail) -> list[tuple[InboundEmail, dict]]:
    """The other Calendly notices already read about this invitee, as they read."""
    found = []
    for other in session.execute(select(InboundEmail).where(InboundEmail.category == "calendly", InboundEmail.id != row.id,
                                                            func.lower(InboundEmail.body_text).contains(invitee, autoescape=True))).scalars():
        info = _read_notice(settings, other)
        if info is not None and info["invitee_email"] == invitee:
            found.append((other, info))
    return found


def _not_older(row: InboundEmail, than: InboundEmail) -> bool:
    return bool(row.received_at and than.received_at) and row.received_at >= than.received_at


def _cancel_read_first(session: Session, settings, info: dict, row: InboundEmail | None) -> bool:
    """A cancellation of this time matched nothing when it was read, and is dated at or after this booking: it was
    booked and cancelled, and the notices came in the wrong order. A rebooking is dated after it, so it stays new."""
    return row is not None and any(
        other["kind"] == "cancelled" and other["starts_at"] == info["starts_at"] and _not_older(r, row)
        and (UNKNOWN_CANCEL in (r.action_note or "") or FORMER_TIME in (r.action_note or ""))
        for r, other in _notices(session, settings, info["invitee_email"], row))


def _already_moved(session: Session, settings, info: dict, row: InboundEmail | None) -> Meeting | None:
    """The live meeting a reschedule moved away from this time, when that reschedule is dated at or after this booking."""
    if row is None:
        return None
    live = session.execute(select(Meeting).where(func.lower(Meeting.invitee_email) == info["invitee_email"], Meeting.status == "scheduled",
                                                 Meeting.rescheduled_from == info["starts_at"])).scalars().first()
    if live is None or not any(other["previous_starts_at"] == info["starts_at"] and _not_older(r, row)
                               for r, other in _notices(session, settings, info["invitee_email"], row)):
        return None
    return live


def _an_earlier_notice_names(session: Session, settings, info: dict, row: InboundEmail | None) -> bool:
    """A notice read earlier booked this time or moved a meeting from it, and no meeting is there now."""
    return row is not None and any(
        r.id < row.id and other["kind"] != "cancelled" and info["starts_at"] in (other["starts_at"], other["previous_starts_at"])
        for r, other in _notices(session, settings, info["invitee_email"], row))


def close_open_emails(session: Session, lead: Lead, reason: str) -> int:
    """A lead who booked gets no more sequence emails, and nothing still waiting that asks for a meeting: a booking
    link, however a person has edited it, an offer of times, a request for times. An answer to a question being written
    to them stays, and says that a booking arrived; so does every other reply, and every note about the meeting.
    Returns how many were cancelled."""
    n = 0
    for f in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                    FollowUp.status.in_(OPEN))).scalars():
        ends = f.kind == "outreach" or (f.kind == "response" and _asks_for_a_meeting(f))
        if ends and f.kind == "response" and _answers_a_question(session, f):
            if BOOKING_ARRIVED not in (f.blocked_reason or "") and f.blocked_reason != WROTE_AGAIN:
                f.blocked_reason = "; ".join(x for x in (f.blocked_reason, BOOKING_ARRIVED) if x)[:300]
        elif ends:
            f.status, f.blocked_reason = "cancelled", reason[:300]
            n += 1
    return n


def _asks_for_a_meeting(f: FollowUp) -> bool:
    from aime.automation import MEETING_NOTES

    if f.purpose in MEETING_NOTES:
        return False
    return f.purpose in ("meeting_offer", "ask_times", "booking_link") or "calendly.com/" in (f.body or "").lower()


def _answers_a_question(session: Session, f: FollowUp) -> bool:
    """A person writes this one: they asked about prices or details, and a link in it is only an offer beside the answer."""
    from aime.reply_intel import QUESTION_PLACEHOLDER

    if f.purpose in ("meeting_offer", "ask_times"):
        return False
    return QUESTION_PLACEHOLDER in (f.body or "") or any(
        intent in ("pricing_request", "more_info")
        for intent in session.execute(select(InboundEmail.intent).where(InboundEmail.suggested_followup_id == f.id)).scalars())


def next_meeting(session: Session, lead_id: int, exclude: int | None = None, upcoming: bool = True) -> Meeting | None:
    """The lead's next scheduled meeting (with `upcoming=False`, also one that has passed without an outcome)."""
    q = select(Meeting).where(Meeting.lead_id == lead_id, Meeting.status == "scheduled", Meeting.id != (exclude or 0))
    if upcoming:
        q = q.where(Meeting.ends_at >= utcnow())
    return session.execute(q.order_by(Meeting.starts_at)).scalars().first()


def reopen_after_cancel(session: Session, meeting: Meeting, actor: str) -> None:
    """After a cancellation the lead goes back to "interested" with a rebooking next step, unless another meeting with
    it is still booked (for example the new time of a reschedule that Calendly reported as a new event)."""
    if not meeting.lead_id:
        return
    lead = session.get(Lead, meeting.lead_id)
    other = next_meeting(session, lead.id, exclude=meeting.id, upcoming=False)
    contact = session.get(Contact, meeting.contact_id) if meeting.contact_id else None
    opted_out = held_by_opt_out(session, lead, contact, meeting.invitee_email)
    if other is None and lead.outreach_status == "meeting" and not opted_out:
        set_status(session, lead, "interested", actor, "meeting cancelled")
    if opted_out or lead.status == "disqualified":  # nobody is told to follow up with them: a person decides
        return
    for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == lead_with_the_deals(session, lead).id,
                                                         Opportunity.stage.not_in(("won", "lost")))).scalars():
        opp.next_step = (f"Meeting {_when_label(other)}: prepare, then record the outcome" if other
                         else f"Meeting on {meeting.starts_at:%Y-%m-%d} was cancelled: follow up to rebook")


def check_double_bookings(session: Session, settings) -> int:
    """Warn when one lead (or one unmatched invitee) has two or more upcoming meetings: usually the same person booked
    twice. The extra one is cancelled in Calendly, and its cancellation email updates the CRM; the warning then
    closes by itself. Runs after each inbox batch, so a reschedule reported as new event + cancellation stays quiet."""
    from aime.monitoring import alert_once, resolve_alerts

    groups: dict[str, list[Meeting]] = defaultdict(list)
    for m in session.execute(select(Meeting).where(Meeting.status == "scheduled", Meeting.ends_at >= utcnow())
                             .order_by(Meeting.starts_at)).scalars():
        groups[f"lead{m.lead_id}" if m.lead_id else m.invitee_email.lower()].append(m)
    active = set()
    for group, ms in groups.items():
        if len(ms) < 2:
            continue
        key = f"meeting_double:{group}"
        active.add(key)
        first = ms[0]
        where = f" ({first.lead.company.name})" if first.lead else ""
        times = "; ".join(f"#{m.id} {local_time(m.starts_at, m.timezone)}" for m in ms)
        msg = (f"{'SIMULATED: ' if all(m.is_simulated for m in ms) else ''}Possible double booking: "
               f"{first.invitee_name or first.invitee_email}{where} has {len(ms)} upcoming meetings ({times}). "
               "Cancel the extra one in Calendly; its cancellation email updates the CRM.")
        alert_once(session, "warning", "meetings", msg, settings, key=key)
    resolve_alerts(session, ("meeting_double:",), active)
    _close_overlap_alerts(session)
    close_waiting_alerts(session, settings)
    return len(active)


def _close_overlap_alerts(session: Session) -> None:
    from aime.monitoring import resolve_alerts

    still = set()
    for key in session.execute(select(Alert.key).where(Alert.key.startswith(OVERLAP_ALERT, autoescape=True), Alert.resolved_at.is_(None))).scalars():
        a, b = (session.get(Meeting, int(i)) for i in key.removeprefix(OVERLAP_ALERT).split("-"))
        if a is not None and b is not None and a.status == b.status == "scheduled" and a.starts_at < b.ends_at and a.ends_at > b.starts_at:
            still.add(key)
    resolve_alerts(session, (OVERLAP_ALERT,), still)


def _waiting_key(row: InboundEmail, info: dict | None) -> str:
    return f"{WAITING_ALERT}{info['invitee_email'] if info else f'notice{row.id}'}"[:120]


def close_waiting_alerts(session: Session, settings) -> None:
    """A notice applied or marked reviewed waits no longer: its alert closes once no other notice about that invitee waits."""
    from aime.monitoring import resolve_alerts

    waiting = session.execute(select(InboundEmail).where(InboundEmail.category == "calendly", InboundEmail.status == "needs_review")).scalars()
    resolve_alerts(session, (WAITING_ALERT,), {_waiting_key(row, _read_notice(settings, row)) for row in waiting})


def _the_meeting(session: Session, email: str, starts_at: datetime | None) -> Meeting | None:
    """The scheduled meeting a change refers to: the one at `starts_at`, else the invitee's only upcoming one."""
    q = select(Meeting).where(func.lower(Meeting.invitee_email) == email, Meeting.status == "scheduled")
    if starts_at is not None:
        hit = session.execute(q.where(Meeting.starts_at == starts_at)).scalars().first()
        if hit is not None:
            return hit
    upcoming = session.execute(q.where(Meeting.starts_at >= utcnow() - timedelta(hours=2))).scalars().all()
    return upcoming[0] if len(upcoming) == 1 else None


def _tell(session: Session, settings, msg: str, actor: str, meeting: Meeting) -> None:
    session.add(Alert(level="info", source="meetings", message=msg[:500]))
    notify_event(settings, msg)
    log_activity(session, msg, actor, type_="meeting", company_id=meeting.company_id, lead_id=meeting.lead_id,
                 opportunity_id=meeting.opportunity_id)


def link_meeting(session: Session, meeting: Meeting, lead: Lead, actor: str, settings=None) -> None:
    """A person links an unmatched booking to the right lead: the pipeline then moves as for a matched one."""
    if settings is None:
        from aime.context import get_context
        settings = get_context().settings
    meeting.lead_id, meeting.company_id = lead.id, lead.company_id
    if meeting.status == "scheduled":
        contact = session.get(Contact, meeting.contact_id) if meeting.contact_id else None
        hold = book_on_lead(session, settings, lead, contact, meeting, actor, f"{meeting.starts_at:%Y-%m-%d %H:%M} UTC (linked by hand)")
        if hold is None and not meeting.is_simulated:
            from aime.handover import handover
            handover(session, settings, session.get(Opportunity, meeting.opportunity_id),
                     f"meeting booked for {_when_label(meeting)} ({meeting.invitee_email})", actor)
        close_open_emails(session, lead, f"Meeting booked for {_when_label(meeting)}: not needed")
        close_open_proposals(session, lead)
    log_activity(session, f"Meeting with {meeting.invitee_name or meeting.invitee_email} linked to this lead by {actor}",
                 actor, type_="meeting", company_id=lead.company_id, lead_id=lead.id, opportunity_id=meeting.opportunity_id)


def _advance_opportunity(session: Session, lead: Lead, meeting: Meeting, actor: str) -> Opportunity:
    from aime.reply_intel import ensure_opportunity
    opp = ensure_opportunity(session, lead, actor)
    if opp.stage in ("new", "discovery"):
        from aime.stages import move

        move(session, opp, "discovery", actor, "meeting booked", probability=max(opp.probability or 0, 30))
    opp.next_step = f"Meeting {_when_label(meeting)}: prepare, then record the outcome"
    return opp


def _say(row: InboundEmail, sentiment: str, words: str) -> None:
    """What Replies recommends, by what really happened to this notice."""
    row.sentiment, row.recommended_status = sentiment, words[:120]


def _waits(session: Session, settings, row: InboundEmail, info: dict | None) -> None:
    from aime.monitoring import alert_once

    who = f"about {info['invitee_email']}" if info else f"from {row.from_email}"
    alert_once(session, "warning", "meetings", f"{'SIMULATED: ' if row.is_simulated else ''}A Calendly notice {who} waits on Replies. "
               f"{row.recommended_status}. Apply it or mark it reviewed there.", settings, key=_waiting_key(row, info))


def _hold_who_it_names(session: Session, settings, row: InboundEmail, info: dict | None) -> str:
    """A notice that waits holds the lead it names, even when its time cannot be read: that person may well have booked,
    and what is approved for them must not go. It only reads the lead; a forged notice can only delay email. Returns
    what to add to the note."""
    from aime.reply_intel import _hold_lead

    if info is not None:
        invitee = info["invitee_email"]
    else:
        _, kind, _, invitee = _notice_head(row.subject or "", row.body_text or "", _own_addresses(settings))
        invitee = invitee if kind else None
    lead, contact = match_lead(session, invitee, getattr(settings, "test_recipients", frozenset())) if invitee else (None, None)
    if lead is None:
        return ""
    _hold_lead(row, lead)
    row.contact_id = contact.id if contact else None
    return f". {lead.company.name}'s emails wait until Apply or Mark reviewed"


def _still_held(session: Session, info: dict, meeting: Meeting | None) -> str | None:
    """Why the lead of a meeting this notice repeats or moves still waits for a person, or None."""
    if info["kind"] == "cancelled" or meeting is None or meeting.status != "scheduled" or not meeting.lead_id:
        return None
    contact = session.get(Contact, meeting.contact_id) if meeting.contact_id else None
    return booking_hold(session, session.get(Lead, meeting.lead_id), contact, meeting.invitee_email)


def handle_calendly_email(session: Session, settings, row: InboundEmail, actor: str, confirmed_by: str | None = None) -> str:
    """Apply a Calendly notice. Unless the mail server vouches for it (aime.mail_auth) or a person confirmed it
    (`confirmed_by`), it changes nothing and waits for review: a forged notice must not book or cancel meetings. While it
    waits, the lead it names is held, so that nothing more goes out to someone who may have booked: a forged notice can
    only delay email. What Replies recommends comes from what happened here, for the inbox and for Apply alike."""
    info = _read_notice(settings, row)
    if confirmed_by:
        row.holds_sending = None  # a person has decided
    if getattr(settings, "calendly_verified_only", True) and not row.is_simulated and not confirmed_by:
        from aime.mail_auth import calendly_verified

        trusted, why = calendly_verified(session, settings, row.from_email, row.auth_results, to_email=row.to_email or "")
        if not trusted:
            row.status = "needs_review"
            note = f"Calendly notice not applied: {why}. Check it in Calendly"
            if info is None:
                _say(row, "neutral", "Could not be read: check it in Calendly and record it by hand")
            else:
                _say(row, "neutral", "Not applied: check it in Calendly, then Apply")
                note += ", then press Apply on Replies if it is genuine"
            note += _hold_who_it_names(session, settings, row, info)
            _waits(session, settings, row, info)
            return note
    if info is None:
        row.status = "needs_review"
        _say(row, "neutral", "Could not be read: check it in Calendly and record it by hand")
        note = "Calendly notification could not be read (time or invitee missing): check it in Calendly"
        if not confirmed_by:
            note += _hold_who_it_names(session, settings, row, info)
        _waits(session, settings, row, info)
        return note
    meeting, note = record_meeting(session, settings, info, actor, row)
    held = note.startswith(HELD_NOTE)
    if not held and (hold := _still_held(session, info, meeting)):  # a repeat or a move leaves them as held as the first notice did
        note, held = f"{note}. {_held_note(hold)}", True
    decided = note.startswith(HAS_OUTCOME_NOTE)
    if meeting is not None and meeting.lead_id:
        row.lead_id, row.company_id, row.contact_id = meeting.lead_id, meeting.company_id, meeting.contact_id
        row.status = "needs_review" if decided or (held and not confirmed_by) else "actioned"
    else:
        row.status = "needs_review"
    if held:
        _say(row, "neutral", "Meeting recorded, lead not moved: a person decides")
    elif decided:
        _say(row, "neutral", "This meeting already has an outcome: check the cancellation in Calendly")
    elif info["kind"] == "cancelled":
        _say(row, "neutral", "Cancellation for an unknown meeting: check it in Calendly" if note.startswith(UNKNOWN_CANCEL)
             else "Former time cancelled: nothing to do" if note.startswith(FORMER_TIME) else "Meeting cancelled: see the lead's next step")
    elif meeting is None:
        _say(row, "neutral", "Not applied: a later cancellation names this time; check it in Calendly")
    elif meeting.status != "scheduled":
        _say(row, "neutral", "Already recorded: nothing to do")
    elif meeting.lead_id is None:
        _say(row, "positive", "Meeting recorded: link it to a lead on Meetings")
    else:
        _say(row, "positive", "Meeting booked: prepare for it")
    if row.status == "needs_review" and not held and (meeting is None or meeting.lead_id):
        _waits(session, settings, row, info)  # a booking with no lead has its own alert and its own place on Meetings
    close_waiting_alerts(session, settings)
    return note
