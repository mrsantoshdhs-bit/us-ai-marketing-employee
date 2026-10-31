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
notifications go to review, and an unmatched booking can be linked to a lead on the Meetings page.
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

from aime.crm_ops import log_activity
from aime.dedupe import registrable_domain
from aime.models import Alert, Company, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity
from aime.notify import notify_event
from aime.outreach import set_status
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


def _zone(text: str, default: str) -> ZoneInfo:
    m = re.search(r"\(([^)]+)\)", text)
    label = (m.group(1) if m else "").strip()
    try:
        if "/" in label:
            return ZoneInfo(label.split()[0])
    except ZoneInfoNotFoundError:
        pass
    low = label.lower()
    for foreign, name in FOREIGN_ZONES.items():  # "Central European Time" is not US Central
        if foreign in low:
            return ZoneInfo(name)
    for key, name in TZ_NAMES.items():
        if re.search(rf"\b{key}\b", low):
            return ZoneInfo(name)
    return ZoneInfo(default)


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


def parse_calendly(subject: str, body: str, own: set[str], default_tz: str = "America/New_York",
                   default_minutes: int = 30) -> dict | None:
    """{kind, invitee_name, invitee_email, starts_at, ends_at (UTC naive), event, tz, previous_starts_at} or None.
    kind: booked | cancelled | rescheduled. Without an end time the length comes from the event type
    ("30 Minute Meeting"), else `default_minutes`."""
    subject = re.sub(r"^\s*(?:(?:fwd?|fw|wg|rv|tr|sv|aw)\s*:\s*)+", "", subject, flags=re.I)  # a notice forwarded to the sales mailbox
    kind = ("cancelled" if re.match(r"\s*cancel+ed\s*:", subject, re.I)
            else "booked" if re.match(r"\s*new event\s*:", subject, re.I)
            else "rescheduled" if re.match(r"\s*(?:rescheduled|updated)\s*:", subject, re.I) else None)
    if kind is None:
        return None
    text = " ".join(body.split())
    name = None
    m = re.search(r"Invitee(?: Name)?:\s*(.+?)\s+(?:Invitee Email|Email|Event)", text, re.I)
    if m:
        name = m.group(1).strip()
    else:
        sm = re.match(r"\s*(?:new event|cancel+ed|rescheduled|updated)\s*:\s*(.+?)\s+-\s+", subject, re.I)
        name = sm.group(1).strip() if sm else None
    email = None
    m = re.search(r"Invitee Email:\s*(" + EMAIL_RE.pattern + ")", text, re.I)
    if m:
        email = m.group(1).lower()
    else:
        email = next((e.lower() for e in EMAIL_RE.findall(text) if e.lower() not in own and "calendly" not in e.lower()), None)
    m = re.search(r"Event Type:\s*(.+?)\s+(?:Invitee|Event Date|Date/Time|Location|$)", text, re.I)
    event = (m.group(1) if m else re.split(r"\s+-\s+", subject)[-1] if " - " in subject else "Meeting").strip()
    minutes = _length(event, default_minutes)
    when = re.search(rf"(?<!former )(?<!previous )Event Date/Time:\s*(.+?)(?:{WHEN_ENDS})", text, re.I)
    parsed = _when(when.group(1), default_tz, minutes) if when else None
    if parsed is None:  # the subject has the start: "New Event: Ana - 10:00am Thu, Oct 29, 2026 - 30 Minute Meeting"
        zone = re.search(r"\([^)]*\)", when.group(1)) if when else None
        parsed = _when(f"{subject} {zone.group(0) if zone else ''}", default_tz, minutes)
    if not (parsed and email):
        return None
    # "Former Event Date/Time: ...", or as Calendly's "Updated:" email has it: "Date & Time: Former: ... Updated: ..."
    before = re.search(rf"(?:Former|Previous)(?: Event)?(?: Date/Time)?:\s*(.+?)(?:Updated:|Event Date/Time|{WHEN_ENDS})",
                       text, re.I)
    previous = _when(before.group(1), default_tz, minutes) if before else None
    if kind == "booked" and previous is not None:
        kind = "rescheduled"  # a new event that names its former time is a reschedule
    return {"kind": kind, "invitee_name": name, "invitee_email": email, "starts_at": parsed[0], "ends_at": parsed[1],
            "event": event[:200], "tz": parsed[2], "previous_starts_at": previous[0] if previous else None}


def _when(when_text: str, default_tz: str, minutes: int = 30) -> tuple[datetime, datetime, str] | None:
    """(start, end) in UTC (naive) and the zone name, from "10:00am - 10:20am (Eastern Time) Thursday, October 29, 2026"
    or, with the start only, "10:00am - Thursday, October 29, 2026 (India Standard Time)" (end = start + minutes)."""
    times, day = TIME_RANGE.search(when_text), _date(when_text)
    one = None if times else ONE_TIME.search(when_text)
    if not (day and (times or one)):
        return None
    tz = _zone(when_text, default_tz)
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
    if contact:
        # a returning customer has a closed lead and a new one: the booking belongs to the one still being worked
        closed_last = (Lead.status.in_(("converted", "merged", "disqualified")), Lead.id)
        lead = session.execute(select(Lead).where(Lead.contact_id == contact.id).order_by(*closed_last)).scalars().first() or \
            session.execute(select(Lead).where(Lead.company_id == contact.company_id).order_by(*closed_last)).scalars().first()
        return lead, contact
    domain = registrable_domain(email.split("@", 1)[1])
    company = session.execute(select(Company).where(Company.domain == domain, Company.merged_into_id.is_(None))).scalars().first()
    if company:
        return session.execute(select(Lead).where(Lead.company_id == company.id)).scalars().first(), None
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
                opp.next_step = f"Meeting {moved.starts_at:%Y-%m-%d %H:%M} UTC (rescheduled): prepare, then record the outcome"
            msg = f"{'SIMULATED: ' if moved.is_simulated else ''}Meeting rescheduled: {info['invitee_name'] or info['invitee_email']} " \
                  f"from {local_time(old, moved.timezone)} to {_when_label(moved)}"
            _tell(session, settings, msg, actor, moved)
            return moved, "meeting rescheduled"
    if info["kind"] == "cancelled":
        if existing is None:
            moved = session.execute(select(Meeting).where(func.lower(Meeting.invitee_email) == info["invitee_email"],
                                                          Meeting.rescheduled_from == info["starts_at"])).scalars().first()
            if moved is not None:  # Calendly also cancels the former time of a meeting it moved
                return moved, "former time of a rescheduled meeting: nothing to cancel"
            existing = _the_meeting(session, info["invitee_email"], None)
        if existing is None:
            return None, f"cancellation for an unknown meeting with {info['invitee_email']}"
        if existing.status == "cancelled":
            return existing, "already cancelled"
        existing.status = "cancelled"
        reopen_after_cancel(session, existing, actor)
        _tell(session, settings, f"{'SIMULATED: ' if existing.is_simulated else ''}Meeting cancelled: "
                                 f"{info['invitee_name'] or info['invitee_email']} ({_when_label(existing)})",
              actor, existing)
        return existing, "meeting cancelled"
    if scheduled:
        return scheduled, "already recorded"
    meeting = Meeting(lead_id=lead.id if lead else None, company_id=lead.company_id if lead else None,
                      contact_id=contact.id if contact else None, invitee_email=info["invitee_email"],
                      invitee_name=info.get("invitee_name"), title=info.get("event") or "Meeting",
                      starts_at=info["starts_at"], ends_at=info["ends_at"], timezone=info.get("tz"), source=source,
                      status="scheduled", inbound_email_id=row.id if row else None, is_simulated=simulated)
    session.add(meeting)
    session.flush()
    label = "SIMULATED: " if simulated else ""
    who = info["invitee_name"] or info["invitee_email"]
    if lead is None:
        msg = f"{label}Meeting booked with {who} on {_when_label(meeting)}, not matched to a lead: link it on Meetings"
        session.add(Alert(level="info", source="meetings", message=msg[:500]))
        notify_event(settings, msg)
        return meeting, f"meeting with {info['invitee_email']} recorded but not matched to a lead"
    opp = _advance_opportunity(session, lead, meeting, actor)
    meeting.opportunity_id = opp.id
    set_status(session, lead, "meeting", actor, f"{info['starts_at']:%Y-%m-%d %H:%M} UTC")
    lead.last_activity_at = utcnow()
    if not simulated:
        from aime.handover import handover
        handover(session, settings, opp, f"meeting booked for {_when_label(meeting)} ({info['invitee_email']})", actor)
    closed = close_open_emails(session, lead, f"Meeting booked for {_when_label(meeting)}: not needed")
    overlap = session.execute(select(Meeting.id).where(Meeting.id != meeting.id, Meeting.status == "scheduled",
                                                       func.lower(Meeting.invitee_email) != info["invitee_email"],
                                                       Meeting.starts_at < meeting.ends_at, Meeting.ends_at > meeting.starts_at)).first()
    if overlap:
        session.add(Alert(level="warning", source="meetings",
                          message=f"Meeting #{meeting.id} overlaps meeting #{overlap[0]}: check your calendar"))
    msg = f"{label}Meeting booked with {who} ({lead.company.name}) on {_when_label(meeting)}"
    session.add(Alert(level="info", source="meetings", message=msg[:500]))
    notify_event(settings, msg)
    log_activity(session, msg + (f"; {closed} unsent email(s) to them cancelled" if closed else ""), actor,
                 type_="meeting", company_id=lead.company_id, lead_id=lead.id, opportunity_id=meeting.opportunity_id)
    return meeting, "meeting recorded; lead moved to meeting; opportunity advanced"


def close_open_emails(session: Session, lead: Lead, reason: str) -> int:
    """A lead who booked gets no more sequence emails, and no booking link that is still waiting to be approved or
    sent. Other answers being written to them stay. Returns how many were cancelled."""
    n = 0
    for f in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                    FollowUp.status.in_(OPEN))).scalars():
        if f.kind == "outreach" or (f.kind == "response" and "calendly.com/" in (f.body or "").lower()):
            f.status, f.blocked_reason = "cancelled", reason[:300]
            n += 1
    return n


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
    if other is None and lead.outreach_status == "meeting":
        set_status(session, lead, "interested", actor, "meeting cancelled")
    for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id,
                                                         Opportunity.stage.not_in(("won", "lost")))).scalars():
        opp.next_step = (f"Meeting {other.starts_at:%Y-%m-%d %H:%M} UTC: prepare, then record the outcome" if other
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
    return len(active)


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


def link_meeting(session: Session, meeting: Meeting, lead: Lead, actor: str) -> None:
    """A person links an unmatched booking to the right lead: the pipeline then moves as for a matched one."""
    meeting.lead_id, meeting.company_id = lead.id, lead.company_id
    if meeting.status == "scheduled":
        meeting.opportunity_id = _advance_opportunity(session, lead, meeting, actor).id
        set_status(session, lead, "meeting", actor, f"{meeting.starts_at:%Y-%m-%d %H:%M} UTC (linked by hand)")
        close_open_emails(session, lead, f"Meeting booked for {_when_label(meeting)}: not needed")
    log_activity(session, f"Meeting with {meeting.invitee_name or meeting.invitee_email} linked to this lead by {actor}",
                 actor, type_="meeting", company_id=lead.company_id, lead_id=lead.id, opportunity_id=meeting.opportunity_id)


def _advance_opportunity(session: Session, lead: Lead, meeting: Meeting, actor: str) -> Opportunity:
    from aime.reply_intel import ensure_opportunity
    opp = ensure_opportunity(session, lead, actor)
    if opp.stage in ("new", "discovery"):
        from aime.stages import move

        move(session, opp, "discovery", actor, "meeting booked", probability=max(opp.probability or 0, 30))
    opp.next_step = f"Meeting {meeting.starts_at:%Y-%m-%d %H:%M} UTC: prepare, then record the outcome"
    return opp


def handle_calendly_email(session: Session, settings, row: InboundEmail, actor: str, confirmed_by: str | None = None) -> str:
    """Apply a Calendly notice. Unless the mail server vouches for it (aime.mail_auth) or a person confirmed it
    (`confirmed_by`), it changes nothing and waits for review: a forged notice must not book or cancel meetings."""
    if getattr(settings, "calendly_verified_only", True) and not row.is_simulated and not confirmed_by:
        from aime.mail_auth import calendly_verified

        trusted, why = calendly_verified(session, settings, row.from_email, row.auth_results, to_email=row.to_email or "")
        if not trusted:
            row.status = "needs_review"
            return (f"Calendly notice not applied: {why}. Check it in Calendly, then press Apply on Replies "
                    "if it is genuine")
    own = {a.lower() for a in (settings.smtp_from, settings.imap_credentials[0]) if a}
    info = parse_calendly(row.subject or "", row.body_text or "", own,
                          settings.section("followup").get("send_window_timezone", "America/New_York"),
                          booking_minutes(settings))
    if info is None:
        row.status = "needs_review"
        return "Calendly notification could not be read (time or invitee missing): check it in Calendly"
    meeting, note = record_meeting(session, settings, info, actor, row)
    if meeting is not None and meeting.lead_id:
        row.lead_id, row.company_id, row.contact_id = meeting.lead_id, meeting.company_id, meeting.contact_id
        row.status = "actioned"
    else:
        row.status = "needs_review"
    return note
