"""Meetings: bookings detected from Calendly's notification emails in the sales mailbox, or logged by hand.

Calendly sends the host "New Event: <invitee> - <time> <date> - <event type>" and "Canceled: ..." emails
with the invitee's name and email and the event date/time with its time zone. A booking is matched to the
lead by the invitee's email, then by their company's domain; the meeting is recorded, the lead moves to
"meeting", the opportunity to 30%, and the Sales Manager is notified. Cancellations reopen the lead
("interested") with a follow-up next step. Reschedules ("Rescheduled:"/"Updated:", with the former time when
Calendly gives it) move the recorded meeting instead of adding a second one. A cancellation or reschedule whose
time matches nothing applies to the invitee's only upcoming meeting; otherwise it goes to review. Overlapping
meetings raise an alert (Calendly itself prevents double booking on the calendar it manages). Unmatched or
unparseable notifications go to review, and an unmatched booking can be linked to a lead on the Meetings page.
Nothing here creates, moves or cancels anything in Calendly: it only records what Calendly reports.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
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
TIME_RANGE = re.compile(r"(\d{1,2}(?::\d{2})?\s*[ap]m)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*[ap]m)", re.I)
DATE_RE = re.compile(r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun),?\s+"
                     r"([a-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
WHEN_ENDS = r"Former|Previous|Invitee|Location|Description|Questions|View event|$"  # what follows a date/time field


def _zone(text: str, default: str) -> ZoneInfo:
    m = re.search(r"\(([^)]+)\)", text)
    label = (m.group(1) if m else "").strip()
    try:
        if "/" in label:
            return ZoneInfo(label.split()[0])
    except ZoneInfoNotFoundError:
        pass
    for key, name in TZ_NAMES.items():
        if key in label.lower():
            return ZoneInfo(name)
    return ZoneInfo(default)


def _clock(value: str) -> tuple[int, int]:
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", value.strip(), re.I)
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    return (hour % 12) + (12 if ampm == "pm" else 0), minute


def parse_calendly(subject: str, body: str, own: set[str], default_tz: str = "America/New_York") -> dict | None:
    """{kind, invitee_name, invitee_email, starts_at, ends_at (UTC naive), event, tz, previous_starts_at} or None.
    kind: booked | cancelled | rescheduled."""
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
    when = re.search(rf"(?<!former )(?<!previous )Event Date/Time:\s*(.+?)(?:{WHEN_ENDS})", text, re.I)
    parsed = _when(when.group(1) if when else subject, default_tz)
    if not (parsed and email):
        return None
    before = re.search(rf"(?:Former|Previous)(?: Event)? Date/Time:\s*(.+?)(?:Event Date/Time|{WHEN_ENDS})", text, re.I)
    previous = _when(before.group(1), default_tz) if before else None
    event = re.split(r"\s+-\s+", subject)[-1].strip() if " - " in subject else "Meeting"
    return {"kind": kind, "invitee_name": name, "invitee_email": email, "starts_at": parsed[0], "ends_at": parsed[1],
            "event": event[:200], "tz": parsed[2], "previous_starts_at": previous[0] if previous else None}


def _when(when_text: str, default_tz: str) -> tuple[datetime, datetime, str] | None:
    """(start, end) in UTC (naive) and the zone name, from "10:00am - 10:20am (Eastern Time) Thursday, October 29, 2026"."""
    times, date = TIME_RANGE.search(when_text), DATE_RE.search(when_text)
    if not (times and date):
        return None
    try:
        day = datetime.strptime(f"{date.group(1)[:3]} {date.group(2)} {date.group(3)}", "%b %d %Y")
    except ValueError:
        return None
    tz = _zone(when_text, default_tz)
    (h1, m1), (h2, m2) = _clock(times.group(1)), _clock(times.group(2))
    start = day.replace(hour=h1, minute=m1, tzinfo=tz)
    end = day.replace(hour=h2, minute=m2, tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    to_utc = lambda d: d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)  # noqa: E731
    return to_utc(start), to_utc(end), str(tz)


def match_lead(session: Session, email: str) -> tuple[Lead | None, Contact | None]:
    contact = session.execute(select(Contact).where(func.lower(Contact.email) == email.lower())).scalars().first()
    if contact:
        lead = session.execute(select(Lead).where(Lead.contact_id == contact.id)).scalars().first() or \
            session.execute(select(Lead).where(Lead.company_id == contact.company_id)).scalars().first()
        return lead, contact
    domain = registrable_domain(email.split("@", 1)[1])
    company = session.execute(select(Company).where(Company.domain == domain, Company.merged_into_id.is_(None))).scalars().first()
    if company:
        return session.execute(select(Lead).where(Lead.company_id == company.id)).scalars().first(), None
    # The address we emailed (or that replied to us) for a lead: e.g. a prospect booking from their reply address,
    # or the owner's test address in a controlled test.
    f = session.execute(select(FollowUp).where((func.lower(FollowUp.to_email) == email) | (func.lower(FollowUp.delivered_to) == email))
                        .order_by(FollowUp.id.desc())).scalars().first()
    if f is not None:
        return session.get(Lead, f.lead_id), None
    r = session.execute(select(InboundEmail).where(func.lower(InboundEmail.from_email) == email, InboundEmail.lead_id.is_not(None))
                        .order_by(InboundEmail.id.desc())).scalars().first()
    return (session.get(Lead, r.lead_id) if r else None), None


def record_meeting(session: Session, settings, info: dict, actor: str, row: InboundEmail | None = None,
                   source: str = "calendly_email") -> tuple[Meeting | None, str]:
    """Create, or cancel, the meeting described by `info`. Returns (meeting, note)."""
    lead, contact = match_lead(session, info["invitee_email"])
    simulated = bool(row and row.is_simulated)
    existing = session.execute(select(Meeting).where(func.lower(Meeting.invitee_email) == info["invitee_email"],
                                                     Meeting.starts_at == info["starts_at"])).scalars().first()
    if info["kind"] == "rescheduled":
        if existing is not None:
            return existing, "already recorded"
        moved = _the_meeting(session, info["invitee_email"], info.get("previous_starts_at"))
        if moved is None:
            info = {**info, "kind": "booked"}  # nothing to move: record it as a booking
        else:
            old = moved.starts_at
            moved.starts_at, moved.ends_at, moved.timezone = info["starts_at"], info["ends_at"], info.get("tz")
            for opp in session.execute(select(Opportunity).where(Opportunity.id == moved.opportunity_id,
                                                                 Opportunity.stage.not_in(("won", "lost")))).scalars():
                opp.next_step = f"Meeting {moved.starts_at:%Y-%m-%d %H:%M} UTC (rescheduled): prepare, then record the outcome"
            msg = f"{'SIMULATED: ' if moved.is_simulated else ''}Meeting rescheduled: {info['invitee_name'] or info['invitee_email']} " \
                  f"from {old:%a %b %d %H:%M} to {moved.starts_at:%a %b %d %H:%M} UTC"
            _tell(session, settings, msg, actor, moved)
            return moved, "meeting rescheduled"
    if info["kind"] == "cancelled":
        if existing is None:
            existing = _the_meeting(session, info["invitee_email"], None)
        if existing is None:
            return None, f"cancellation for an unknown meeting with {info['invitee_email']}"
        existing.status = "cancelled"
        if existing.lead_id:
            lead = session.get(Lead, existing.lead_id)
            if lead.outreach_status == "meeting":
                set_status(session, lead, "interested", actor, "meeting cancelled")
            for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id,
                                                                 Opportunity.stage.not_in(("won", "lost")))).scalars():
                opp.next_step = f"Meeting on {info['starts_at']:%Y-%m-%d} was cancelled: follow up to rebook"
        _tell(session, settings, f"{'SIMULATED: ' if existing.is_simulated else ''}Meeting cancelled: "
                                 f"{info['invitee_name'] or info['invitee_email']} ({existing.starts_at:%a %b %d %H:%M} UTC)",
              actor, existing)
        return existing, "meeting cancelled"
    if existing:
        return existing, "already recorded"
    meeting = Meeting(lead_id=lead.id if lead else None, company_id=lead.company_id if lead else None,
                      contact_id=contact.id if contact else None, invitee_email=info["invitee_email"],
                      invitee_name=info.get("invitee_name"), title=info.get("event") or "Meeting",
                      starts_at=info["starts_at"], ends_at=info["ends_at"], timezone=info.get("tz"), source=source,
                      status="scheduled", inbound_email_id=row.id if row else None, is_simulated=simulated)
    session.add(meeting)
    session.flush()
    if lead is None:
        return meeting, f"meeting with {info['invitee_email']} recorded but not matched to a lead"
    meeting.opportunity_id = _advance_opportunity(session, lead, meeting, actor).id
    set_status(session, lead, "meeting", actor, f"{info['starts_at']:%Y-%m-%d %H:%M} UTC")
    lead.last_activity_at = utcnow()
    overlap = session.execute(select(Meeting.id).where(Meeting.id != meeting.id, Meeting.status == "scheduled",
                                                       Meeting.starts_at < meeting.ends_at, Meeting.ends_at > meeting.starts_at)).first()
    if overlap:
        session.add(Alert(level="warning", source="meetings",
                          message=f"Meeting #{meeting.id} overlaps meeting #{overlap[0]}: check your calendar"))
    label = "SIMULATED: " if simulated else ""
    msg = f"{label}Meeting booked with {info['invitee_name'] or info['invitee_email']} ({lead.company.name}) on " \
          f"{info['starts_at']:%a %b %d %H:%M} UTC"
    session.add(Alert(level="info", source="meetings", message=msg[:500]))
    notify_event(settings, msg)
    log_activity(session, msg, actor, type_="meeting", company_id=lead.company_id, lead_id=lead.id,
                 opportunity_id=meeting.opportunity_id)
    return meeting, "meeting recorded; lead moved to meeting; opportunity advanced"


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
    log_activity(session, f"Meeting with {meeting.invitee_name or meeting.invitee_email} linked to this lead by {actor}",
                 actor, type_="meeting", company_id=lead.company_id, lead_id=lead.id, opportunity_id=meeting.opportunity_id)


def _advance_opportunity(session: Session, lead: Lead, meeting: Meeting, actor: str) -> Opportunity:
    from aime.reply_intel import ensure_opportunity
    opp = ensure_opportunity(session, lead, actor)
    if opp.stage in ("new", "discovery"):
        opp.stage, opp.probability = "discovery", max(opp.probability or 0, 30)
    opp.next_step = f"Meeting {meeting.starts_at:%Y-%m-%d %H:%M} UTC: prepare, then record the outcome"
    return opp


def handle_calendly_email(session: Session, settings, row: InboundEmail, actor: str) -> str:
    own = {a.lower() for a in (settings.smtp_from, settings.imap_credentials[0]) if a}
    info = parse_calendly(row.subject or "", row.body_text or "", own,
                          settings.section("followup").get("send_window_timezone", "America/New_York"))
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
