"""Reply intelligence: classify replies, match them to leads, update the CRM, suggest responses.

Categories (first match wins, safety first): bounce, calendly, unsubscribe (including complaints),
out_of_office, then the human answers interested / not_interested / objection / referral / question,
else other. Conflicting signals or a complaint lower the confidence and send the reply to review.

Interest includes meeting requests in everyday wording ("can we talk Thursday?", "let's arrange a call",
"I'd like a demo") and common misspellings ("sound interesting", "intrested"). A negation in front of it
("don't schedule a call") still sends the reply to review.

Every human reply stops the automated sequence. Unsubscribes and bounces suppress the address.
Interested replies open (or advance) an opportunity and notify the Sales Manager. Suggested responses
are drafts that wait for approval like every other email. Simulated replies (`aime simulate-reply`)
are processed the same way and labelled SIMULATED wherever they appear.

When the rules can only say "question" or "other", the local AI model (if one is running) gives a second
opinion. It never acts on its own: a reply it reads as interest stays in review, with the suggestion in its
reasons and a "possible interest" alert, and the Sales Manager decides (Replies page: reclassify).
"""

from __future__ import annotations

import re
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.crm_ops import log_activity, suppress
from aime.inbox import own_text
from aime.llm.base import LLMError
from aime.models import Alert, Contact, FollowUp, InboundEmail, Lead, Meeting, Opportunity
from aime.notify import notify_event
from aime.integrations.email_smtp import POSTAL_PLACEHOLDER
from aime.outreach import stop_sequence
from aime.stages import move, opened
from aime.timeutil import utcnow

PATTERNS: list[tuple[str, re.Pattern]] = [
    # Opt-outs in everyday wording and with typos ("unsubcribe", "remove my email from your list", "no more emails").
    ("unsubscribe", re.compile(
        r"\b(un-?su[bs]{1,2}c?r?i?b?e?d?|"
        r"(remove|delete|drop|take) (me|us|my (e-?mail|address|name|details|contact details)|our (e-?mail|address|details))"
        r"( (from|off) (your|the|this|all) ((mailing|e-?mail|contact|marketing) )?(list|lists|database))?|"
        r"(off|from) (your|the|this) ((mailing|e-?mail|marketing) )?list|"
        r"stop (emailing|contacting|sending|messaging|writing to)|stop (all |the |these |your )?(e-?mails|messages|spam)|"
        r"no (more|further) (e-?mails|messages|contact)|"
        r"(do not|don'?t|never) (e-?mail|contact|message|write to) (me|us)|do not (email|contact)|"
        r"opt (me |us )?out|opt[- ]?out|leave (me|us) alone)\b", re.I)),
    ("not_interested", re.compile(r"\b(not interested|no thanks|no, thank you|not a (good )?fit|we'?re all set|"
                                  r"not (looking|in the market)|pass on this|no need|not for us|we'?re good)\b", re.I)),
    ("interested", re.compile(r"\b(interested|sounds? (good|great|interesting|in(te|t|tr)[a-z]{0,3}st(ed|ing))|"
                              r"in(te|tr)[a-z]{0,3}st(ed|ing)|let'?s (talk|chat|connect|set up)|"
                              r"happy to (chat|talk|connect|meet)|book a (time|call|meeting)|schedule a (call|meeting)|"
                              r"set up a (call|meeting)|tell me more|send (me )?(more|some) (info|details)|"
                              r"what (does it|would it) cost|how much|pricing|availab(le|ility)|free (on|next)|"
                              r"(monday|tuesday|wednesday|thursday|friday) (works|at|morning|afternoon))\b", re.I)),
    ("objection", re.compile(r"\b(too expensive|no budget|budget (is|was)|not (a priority|right now|now)|maybe later|"
                             r"next (quarter|year)|bad timing|already (work|working|use|using|have)|in-house|"
                             r"circle back|check back)\b", re.I)),
    ("referral", re.compile(r"\b(you should (talk|speak|reach out) to|reach out to|the right person|"
                            r"(cc|cc'?ing|copying|looping in)\b|forward(ed|ing)? (this|your)|contact our)", re.I)),
]
COMPLAINT = re.compile(r"\b(spam|report(ed|ing)? (you|this)|lawyer|attorney|legal action|harass|gdpr|can-?spam|"
                       r"how did you get (my|this) (email|address)|"
                       # a request to remove personal data: suppressed at once, and a person handles the deletion
                       r"(remov|delet|eras)(e|es|ed|ing) (all )?(my|our) (personal )?(data|details|information|info)|"
                       r"right to be forgotten|ccpa|data (removal|deletion))\b", re.I)
OOO = re.compile(r"\b(out of (the )?office|on (vacation|holiday|leave)|limited access to (my )?email|"
                 r"will be back|returning on|away until|automatic reply)\b", re.I)
CALENDLY = re.compile(r"calendly\.com", re.I)
# Calendly's own subject, also forwarded ("Fwd: New Event: ..."); a reply ("Re: ...") never starts like this
CALENDLY_NOTICE = re.compile(r"\s*(?:(?:fwd?|fw)\s*:\s*)*(?:new event|cancel+ed|rescheduled|updated)\s*:", re.I)
# Asking for a meeting, a call or a demo in everyday wording (and without the article: "schedule meeting").
_TALK = r"(talk|chat|speak|meet|connect|discuss|catch up|hop on a call|jump on a call)"
_THING = r"(time|meeting|call|demo|chat|session|appointment|slot|conversation|walkthrough|presentation)"
_WHEN = r"(monday|tuesday|wednesday|thursday|friday|tomorrow|today|this week|next week)"
ASKED_WHEN = re.compile(rf"\b{_WHEN}\b", re.I)
# What they ask for, alongside the category (their own words only): a price always goes to a person.
PRICING = re.compile(r"\b(pric(e|es|ing)|how much|cost(s)?|quote|quotation|rates|fees|budget|estimate|ballpark)\b", re.I)
MORE_INFO = re.compile(r"\b(more (info|information|details)|tell me more|learn more|any (info|details)|"
                       r"send (me |us )?(some |more |a |the )?(info|information|details|deck|case stud(y|ies)|brochure|"
                       r"materials?|examples?))\b", re.I)
INTENT_LABELS = {"meeting_request": "Meeting request", "pricing_request": "Pricing request", "more_info": "More information"}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
MEETING = re.compile(
    rf"\b((can|could|shall|should|would|may) (we|you|i) (please )?({_TALK}|schedule|set ?up|arrange|book|organi[sz]e)"
    rf"|(schedule|arrange|set ?up|book|organi[sz]e|plan|fix) (a |an |the |some |our )?{_THING}"
    rf"|(let'?s|lets|let us) ({_TALK}|schedule|arrange|set ?up|book|do|have)"
    rf"|(would|i'?d|we'?d|i|we) (really )?(like|love|want|be happy|be glad|be keen) (to )?(see |get |have |book |schedule |set up )?"
    rf"(a |an )?(demo|call|meeting|chat|walkthrough|presentation|quote|proposal|trial)"
    rf"|(send|share|show) (me |us )?(a |an |the )?(demo|deck|proposal|quote)"
    rf"|{_TALK}\b[^.?!\n]{{0,40}}\b{_WHEN})\b", re.I)
# "not", "never", "don't" ... up to two words before an interest phrase: "not really interested", "don't send me more info"
NEGATED = re.compile(r"(\bnot|\bnever|\bno longer|\bcannot|\bdont|\b\w+n['\u2019]t)\s+(\w+\s+){0,2}$", re.I)
HUMAN = ("interested", "not_interested", "objection", "referral", "question", "other")
OUTREACH_STAGE = {"interested": "interested", "not_interested": "not_interested"}
QUESTION_PLACEHOLDER = "[Write your answer here]"


def classify(row: InboundEmail) -> tuple[str, str, list[str]]:
    category, confidence, reasons = _classify(row)
    if not row.new_text and (row.body_text or "").strip() and confidence == "high" and category not in ("bounce", "calendly"):
        # Nothing was written above the quote: the words were found below or inside it, so a person checks first.
        return category, "medium", [*reasons, "reply written below the quoted email"]
    return category, confidence, reasons


def intent_of(row: InboundEmail) -> str | None:
    """meeting_request | pricing_request | more_info for a person's reply (None for notices and opt-outs)."""
    if row.category not in ("interested", "question", "objection", "referral", "other"):
        return None
    text = row.new_text or own_text(row.body_text or "")
    if PRICING.search(text):
        return "pricing_request"
    meeting = MEETING.search(text)
    if meeting and not NEGATED.search(text[:meeting.start()][-40:]):
        return "meeting_request"
    return "more_info" if MORE_INFO.search(text) else None


UNSUBSCRIBE_SUBJECT = re.compile(r"^\s*((re|fwd?|aw)\s*:\s*)*(unsubscribe|opt[- ]?out|remove me)\s*[.!]?\s*$", re.I)

# How each kind of reply reads, and what the lead should become: shown on Replies for the person who reviews it.
SENTIMENT = {"interested": "positive", "referral": "positive", "calendly": "positive", "question": "neutral",
             "out_of_office": "neutral", "bounce": "neutral", "other": "neutral", "objection": "negative",
             "not_interested": "negative", "unsubscribe": "negative"}
RECOMMENDED = {
    "interested": "Opportunity: answer and offer a meeting",
    "referral": "Contact the person they referred us to",
    "question": "Answer the question; keep the lead active",
    "objection": "A person answers the objection",
    "not_interested": "Close as not interested; nurture later",
    "unsubscribe": "Do not contact: address suppressed",
    "bounce": "Address invalid: find another contact",
    "out_of_office": "Wait until they are back",
    "calendly": "Meeting booked: prepare for it",
    "other": "A person reviews it",
}


def sentiment_and_recommendation(category: str, intent: str | None) -> tuple[str, str]:
    if intent == "meeting_request":
        return "positive", "Opportunity: book the meeting they asked for"
    if intent == "pricing_request":
        return "positive", "A person answers the pricing question (prices always need a person)"
    return SENTIMENT.get(category, "neutral"), RECOMMENDED.get(category, RECOMMENDED["other"])


def _classify(row: InboundEmail) -> tuple[str, str, list[str]]:
    text = row.new_text or own_text(row.body_text or "")
    subject = row.subject or ""
    if row.bounced_addresses:
        return "bounce", "high", ["delivery failure report"]
    if re.match(r"^(mailer-daemon|postmaster)@", row.from_email or "", re.I):
        return "bounce", "low", ["delivery report without a permanently failed address (for example a delay)"]
    if CALENDLY.search(row.from_email or "") or (CALENDLY.search(row.body_text or "")  # also a forwarded notice
                                                  and CALENDLY_NOTICE.match(subject)):
        return "calendly", "high", ["Calendly notification"]
    if not text.strip() and UNSUBSCRIBE_SUBJECT.match(subject):  # our List-Unsubscribe link: subject only, no text
        return "unsubscribe", "high", [f'subject "{subject.strip()[:60]}" with no other text']
    if not text.strip():
        return "other", "low", ["no new text of its own (only the quoted email)"]
    complaint = COMPLAINT.search(f"{subject}\n{text}")
    hits = {name: m.group(0) for name, rx in PATTERNS if (m := rx.search(f"{subject}\n{text}"))}
    negated = None
    if "interested" in hits:
        m = dict(PATTERNS)["interested"].search(f"{subject}\n{text}")
        if NEGATED.search(f"{subject}\n{text}"[:m.start()][-40:]):
            negated = hits.pop("interested")
    meeting = MEETING.search(text)  # their own words only: our quoted email never asks them for a meeting
    if meeting and NEGATED.search(text[:meeting.start()][-40:]):  # "please don't schedule a call"
        negated = negated or meeting.group(0)
        meeting = None
    if meeting:
        hits.setdefault("interested", meeting.group(0))
    if "unsubscribe" in hits or complaint:
        reasons = [f'"{hits["unsubscribe"]}"'] if "unsubscribe" in hits else []
        if complaint:
            reasons.append(f'complaint: "{complaint.group(0)}"')
        return "unsubscribe", "medium" if complaint else "high", reasons
    if row.auto_submitted or OOO.search(f"{subject}\n{text[:400]}"):
        return "out_of_office", "high" if row.auto_submitted else "medium", ["automatic reply"]
    human = [k for k in ("not_interested", "interested", "objection", "referral") if k in hits]
    if negated:  # "not really interested", "don't send me more info": a person reads it, nothing is automated
        reasons = [f'negated interest: "{negated}"'] + [f'{k.replace("_", " ")}: "{hits[k]}"' for k in human]
        return (human[0] if human else "other"), "low", reasons
    if not human:
        if "?" in text:
            return "question", "medium", ["asks a question"]
        return "other", "low", ["no clear signal"]
    category = human[0]
    conflicting = len(set(human) & {"interested", "not_interested"}) == 2 or len(human) > 2
    reasons = [f'{k.replace("_", " ")}: "{hits[k]}"' for k in human if not (k == "interested" and meeting
                                                                              and hits[k] == meeting.group(0))]
    if meeting and "interested" in human:
        reasons.append(f'asks for a meeting, call, demo or proposal: "{meeting.group(0)}"')
    return category, "low" if conflicting else ("high" if len(human) == 1 else "medium"), reasons


AI_SYSTEM = ("You read one reply to a B2B sales email. Answer with exactly one line in the form CATEGORY|MEETING. "
             "CATEGORY is one of: interested, not_interested, question, objection, referral, unsubscribe, other. "
             "MEETING is yes if the writer asks to talk, meet, have a call or see a demo, otherwise no. "
             "Typos are common; judge the meaning. Write nothing else.")
AI_ANSWER = re.compile(r"\b(interested|not[ _]interested|question|objection|referral|unsubscribe|other)\s*\|\s*(yes|no)\b", re.I)
AI_PER_RUN = 5  # each answer takes several seconds on a local model
AI_SLOW_SECONDS = 30  # a slower (or timed-out) answer stops the second opinions for the rest of the run


def ai_second_opinion(llm, row: InboundEmail) -> tuple[str, bool] | None:
    """The local model's reading of a reply the rules could not place: (category, asks for a meeting), or None
    when no model is running or its answer is not in the expected form. Only ever a suggestion."""
    text = (row.new_text or own_text(row.body_text or "")).strip()
    if llm is None or not text:
        return None
    try:
        answer = llm.generate(f"Reply:\n<<<\n{text[:1500]}\n>>>", system=AI_SYSTEM, max_tokens=12, temperature=0.0)
    except (LLMError, OSError, ValueError):
        return None
    m = AI_ANSWER.search(answer or "")
    if not m:
        return None
    return m.group(1).lower().replace(" ", "_"), m.group(2).lower() == "yes"


def match(session: Session, row: InboundEmail) -> tuple[FollowUp | None, Contact | None, Lead | None]:
    ids = [i for i in [row.in_reply_to, *((row.references or "").split())] if i]  # bounces: the failed email's id
    f = None
    if ids:
        f = session.execute(select(FollowUp).where(FollowUp.message_id.in_(ids)).order_by(FollowUp.id.desc())).scalars().first()
    if f is None and row.bounced_addresses:
        f = session.execute(select(FollowUp).where(func.lower(FollowUp.to_email).in_(row.bounced_addresses),
                                                   FollowUp.status == "sent").order_by(FollowUp.id.desc())).scalars().first()
    if f is not None:
        return f, f.contact, f.lead
    contact = session.execute(select(Contact).where(func.lower(Contact.email) == (row.from_email or "").lower())
                              .order_by(Contact.id.desc())).scalars().first()
    if contact:
        lead = session.execute(select(Lead).where(Lead.contact_id == contact.id)).scalars().first() or \
            session.execute(select(Lead).where(Lead.company_id == contact.company_id)).scalars().first()
        return None, contact, lead
    return None, None, None


def _signature(settings) -> str:
    return f"Best regards,\n{settings.sender_name or f'The {settings.company_name} team'}\n{settings.company_name}"


def reply_footer(settings) -> str:
    unsub = settings.unsubscribe_email or settings.smtp_from or "this address"
    address = settings.company_postal_address or POSTAL_PLACEHOLDER
    return (f"\n\n---\nTo stop receiving emails from us, reply with \"unsubscribe\" or write to {unsub}.\n"
            f"{settings.company_name} · {address}")


def suggest(settings, category: str, row: InboundEmail, contact: Contact | None, lead: Lead | None = None,
            meeting: Meeting | None = None) -> tuple[str, str] | None:
    """The reply a person approves (or edits) before it goes out, in the prospect's thread. An interested reply gets
    the booking link, mentioning the day they asked for and what the call is about; one from a lead that has already
    booked (`meeting`) confirms that meeting instead of offering a second booking."""
    name = (row.from_name or "").split()[0] if row.from_name else (contact.first_name if contact and contact.first_name else "there")
    subject = row.subject if row.subject.lower().startswith("re:") else f"Re: {row.subject}"
    sign = _signature(settings)
    if category == "interested":
        from aime.knowledge.services import service_name
        from aime.meetings import booking_minutes, local_time
        minutes = booking_minutes(settings)
        asked = ASKED_WHEN.search(row.new_text or own_text(row.body_text or ""))  # the day they asked for, if any
        day = asked.group(0).lower() if asked else None
        weekday = day in WEEKDAYS
        day = day.capitalize() if weekday else day
        service = service_name(lead.primary_service) if lead else "—"
        topic = (f" about {service} for {lead.company.name}" if lead and service != "—"
                 else f" with the {lead.company.name} team" if lead else "")
        if meeting is not None:  # in the US time zone we write in; Calendly's own emails give them theirs
            us_zone = settings.section("followup").get("send_window_timezone", "America/New_York")
            body = (f"Hi {name},\n\nThank you for getting back to me. Our meeting is in my calendar for "
                    f"{local_time(meeting.starts_at, us_zone)} and I look forward to it. If that time stops "
                    f"working, the link in your Calendly confirmation lets you move it.\n\n{sign}")
        elif settings.calendly_url:
            pick = (f"You mentioned {day}: you can pick any open {minutes}-minute slot then, or at another time that "
                    f"suits you, here:" if day else f"You can pick a {minutes}-minute slot that suits you here:")
            body = (f"Hi {name},\n\nThank you for getting back to me. I would be glad to talk{topic}.\n\n{pick}\n"
                    f"{settings.calendly_url}\n\nIf none of the times work, reply with a few that do and I will fit "
                    f"around you.\n\n{sign}")
        else:
            body = (f"Hi {name},\n\nThank you for getting back to me. I would be glad to talk{topic}. Could you share "
                    f"two or three times {f'on {day}' if weekday else day or 'next week'} that suit "
                    f"you for a {minutes}-minute call?\n\n{sign}")
    elif category == "objection":
        body = (f"Hi {name},\n\nThank you for the honest answer. Would it be useful if I checked back in a few months, "
                f"or would you rather I did not? Either is fine.\n\n{sign}")
    elif category == "referral":
        body = (f"Hi {name},\n\nThank you for pointing me in the right direction. Could you share the best way to "
                f"reach them, or would you prefer to forward my note?\n\n{sign}")
    elif category == "question":
        body = f"Hi {name},\n\nThank you for your question.\n\n{QUESTION_PLACEHOLDER}\n\n{sign}"
    else:
        return None
    if category in ("interested", "question") and row.intent == "pricing_request":  # never an automatic price
        call = (f"If it helps, we can go through it on a short call: {settings.calendly_url}\n\n"
                if settings.calendly_url else "")
        body = f"Hi {name},\n\nThank you for asking about pricing.\n\n{QUESTION_PLACEHOLDER}\n\n{call}{sign}"
    return subject[:300], body + reply_footer(settings)


def ensure_opportunity(session: Session, lead: Lead, actor: str) -> Opportunity:
    opp = session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id).order_by(Opportunity.id)).scalars().first()
    if opp is None:
        from aime.knowledge.services import service_name
        opp = Opportunity(lead_id=lead.id, company_id=lead.company_id, stage="discovery", probability=20,
                          name=f"{lead.company.name} — {service_name(lead.primary_service)}", service=lead.primary_service,
                          next_step="Book a meeting: approve the suggested reply with your booking link")
        session.add(opp)
        session.flush()
        opened(session, opp, actor, "interested reply")
        log_activity(session, f"Opportunity created from an interested reply: {opp.name}", actor, type_="opportunity",
                     company_id=lead.company_id, lead_id=lead.id, opportunity_id=opp.id)
    elif opp.stage == "new":
        move(session, opp, "discovery", actor, "interested reply", probability=max(opp.probability or 0, 20))
    elif opp.stage == "lost":  # they said no before and are interested now: the deal is open again
        log_activity(session, f"Opportunity stage lost → discovery (reopened by an interested reply; it was lost: "
                     f"{opp.lost_reason or 'no reason recorded'})", actor, type_="opportunity",
                     company_id=opp.company_id, lead_id=lead.id, opportunity_id=opp.id)
        move(session, opp, "discovery", actor, f"reopened by an interested reply (lost: {opp.lost_reason or 'no reason'})")
    return opp


# Opportunities the automation opened and no person has moved on yet. A "not interested" or opt-out reply closes
# these as lost; a deal a person has taken further (proposal, negotiation) is theirs to close.
AUTO_CLOSE_STAGES = ("new", "discovery")


def close_opportunities(session: Session, lead: Lead, reason: str, actor: str, sure: bool) -> list[str]:
    """End the lead's open deals after a clear "no": early ones are closed as lost with the reason; a later-stage
    deal, or any deal when the reading is uncertain, stays open with a next step asking a person to decide."""
    notes = []
    for opp in session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id,
                                                         Opportunity.stage.not_in(("won", "lost")))).scalars():
        if sure and opp.stage in AUTO_CLOSE_STAGES:
            old = opp.stage
            move(session, opp, "lost", actor, reason)
            opp.lost_reason, opp.next_step = reason[:200], None
            log_activity(session, f"Opportunity stage {old} → lost ({reason})", actor, type_="opportunity",
                         company_id=opp.company_id, lead_id=lead.id, opportunity_id=opp.id)
            notes.append(f"opportunity closed as lost ({reason})")
        else:
            opp.next_step = f"They replied: {reason.lower()}. Close it as lost or keep it open"[:300]
            notes.append(f"opportunity left open in {opp.stage}: a person decides")
    return notes


def act(session: Session, settings, row: InboundEmail, actor: str) -> None:
    """Apply the CRM consequences of a classified email and record what was done."""
    label = "SIMULATED reply" if row.is_simulated else "Reply"
    f, contact, lead = match(session, row)
    row.followup_id = f.id if f else None
    row.contact_id, row.lead_id = (contact.id if contact else None), (lead.id if lead else None)
    row.company_id = lead.company_id if lead else (contact.company_id if contact else None)
    notes = []
    if f is not None and row.category not in ("bounce", "calendly", "out_of_office") and f.status == "sent":
        f.delivery_status = "replied"  # delivery tracking: it arrived and the person answered
    if row.category == "bounce":
        # Only a permanent failure suppresses. A person who reclassifies a report as a bounce (confidence high)
        # confirms the failed email's address.
        failed = row.bounced_addresses or ([f.to_email] if f and f.to_email and row.confidence == "high" else [])
        for addr in failed:
            for c in session.execute(select(Contact).where(func.lower(Contact.email) == addr.lower())).scalars():
                c.email_status = "bounced"
            suppress(session, addr, "Bounced (delivery failure report)")
            notes.append(f"{addr} bounced: marked bounced and suppressed")
        if failed and f is not None and (f.to_email or "").lower() in {a.lower() for a in failed}:
            f.delivery_status = "bounced"  # delivery tracking: this email did not arrive
        if failed and lead and lead.outreach_status:
            stop_sequence(session, lead, "bounced", "Email bounced", actor)
        if not failed:
            notes.append("No permanently failed address found (for example a delay notice): nothing suppressed; "
                         "check it, and reclassify it as a bounce if delivery failed")
        row.status = "actioned" if failed and (f or contact) else "needs_review"  # unmatched: a person checks
    elif row.category == "unsubscribe":
        # Test mode (OUTBOUND_TEST_RECIPIENTS): every lead's email went to the owner's own test address, so an opt-out
        # from it speaks for the lead in that thread. Suppressing the shared test address would cancel every later
        # test email to every other lead; only the lead's own address is suppressed.
        test_address = (row.from_email or "").lower() in getattr(settings, "test_recipients", set())
        if not test_address:
            suppress(session, row.from_email, f"Unsubscribe request by email{' (complaint)' if row.confidence != 'high' else ''}")
        if contact and contact.consent_status != "opted_out":
            contact.consent_status, contact.consent_at = "opted_out", utcnow()
        if lead and lead.outreach_status:
            stop_sequence(session, lead, "unsubscribed", "Asked not to be contacted", actor)
        if lead:
            notes += close_opportunities(session, lead, "Unsubscribed", actor, sure=row.confidence == "high")
        if contact and contact.email and contact.email.lower() != (row.from_email or "").lower():
            # an opt-out in our thread from another address (a colleague, a personal address, test mode): the
            # address we wrote to is suppressed as well
            suppress(session, contact.email, "Unsubscribe request in the email thread")
        if test_address:
            notes.append(f"{contact.email} suppressed (test mode: the opt-out came from the test address "
                         f"{row.from_email}, which stays deliverable)" if contact and contact.email else
                         f"test mode: the opt-out came from the test address {row.from_email} and matches no lead; "
                         "nothing suppressed, check which lead it was for")
        else:
            notes.append(f"{row.from_email} suppressed; no further email")
        row.status = "actioned" if row.confidence == "high" and (contact or not test_address) else "needs_review"
    elif row.category == "out_of_office":
        notes.append("Automatic reply: the sequence continues")
        row.status = "actioned"
    elif row.category == "calendly":
        from aime.meetings import handle_calendly_email
        notes.append(handle_calendly_email(session, settings, row, actor))
    elif lead is None:
        notes.append("Could not match this email to a lead")
        row.status = "needs_review"
    else:
        # Any human reply stops the automated sequence; the stage records what they said.
        cancelled = stop_sequence(session, lead, OUTREACH_STAGE.get(row.category, "replied"),
                                  f"{label} received ({row.category.replace('_', ' ')})", actor)
        if cancelled:
            notes.append(f"{cancelled} queued follow-up(s) cancelled")
        lead.last_activity_at = utcnow()
        if row.category == "interested":
            opp = ensure_opportunity(session, lead, actor)
            notes.append(f"opportunity {opp.stage}")
            if not row.is_simulated:
                from aime.handover import handover
                handover(session, settings, opp, f"interested reply from {row.from_email}"
                         + (" asking for a meeting" if row.intent == "meeting_request" else ""), actor)
            msg = f"{'SIMULATED: ' if row.is_simulated else ''}Interested reply from {row.from_email} ({lead.company.name})"
            session.add(Alert(level="info", source="inbox", message=msg[:500]))
            notify_event(settings, msg)
        elif row.category == "not_interested":
            if lead.status == "qualified":
                lead.status = "nurture"
            notes.append("lead moved to nurture")
            notes += close_opportunities(session, lead, "Not interested", actor, sure=row.confidence == "high")
        booked = None
        if row.category == "interested":
            from aime.meetings import next_meeting
            booked = next_meeting(session, lead.id)
        proposal = suggest(settings, row.category, row, contact, lead, booked)
        if proposal:
            subject, body = proposal
            draft = FollowUp(lead_id=lead.id, contact_id=contact.id if contact else None, company_id=lead.company_id,
                             channel="email", kind="response", step=1, due_at=utcnow(), status="awaiting_approval",
                             subject=subject, body=body, to_email=row.from_email, in_reply_to=row.message_id,
                             blocked_reason="Write your answer before approving" if QUESTION_PLACEHOLDER in body else None)
            session.add(draft)
            session.flush()
            row.suggested_followup_id = draft.id
            if "calendly.com/" in body.lower():
                draft.purpose = "booking_link"
            notes.append("suggested reply waiting for your approval")
        row.status = "actioned" if row.confidence == "high" and row.category in ("interested", "not_interested") else "needs_review"
        if row.intent == "pricing_request":
            row.status = "needs_review"
            notes.append("asks about pricing: a person writes the answer")
    row.action_note = "; ".join(notes) or None
    row.processed_at = utcnow()
    log_activity(session, f"{label} from {row.from_email}: {row.category.replace('_', ' ')} ({row.confidence})",
                 actor, type_="reply", body=(row.new_text or "")[:2000], company_id=row.company_id, lead_id=row.lead_id)


def process_pending(session: Session, settings, actor: str, llm=None) -> dict:
    stats: dict[str, int] = {}
    ai_budget = AI_PER_RUN
    for row in session.execute(select(InboundEmail).where(InboundEmail.status == "new").order_by(InboundEmail.id)).scalars().all():
        row.category, row.confidence, row.reasons = classify(row)
        row.intent = intent_of(row)
        row.sentiment, row.recommended_status = sentiment_and_recommendation(row.category, row.intent)
        opinion = None
        if llm is not None and ai_budget > 0 and row.category in ("question", "other") and row.confidence != "high":
            ai_budget -= 1
            started = time.perf_counter()
            opinion = ai_second_opinion(llm, row)
            if time.perf_counter() - started > AI_SLOW_SECONDS:  # a stuck model must not hold up the inbox job
                ai_budget = 0
            if opinion:
                ai_cat, ai_meeting = opinion
                row.reasons = list(row.reasons or []) + [
                    f"AI ({getattr(llm, 'label', 'local model')}) suggests: {ai_cat.replace('_', ' ')}"
                    + (", asks for a meeting" if ai_meeting else "") + " (please check)"]
        act(session, settings, row, actor)
        if opinion and (opinion[0] == "interested" or opinion[1]) and row.status == "needs_review":
            where = f" ({row.lead.company.name})" if row.lead else ""
            msg = (f"{'SIMULATED: ' if row.is_simulated else ''}Possible interest{', a meeting request' if opinion[1] else ''} "
                   f"from {row.from_email}{where}: the AI reads it so, the rules were unsure. Please review it on Replies.")
            session.add(Alert(level="info", source="inbox", message=msg[:500]))
            notify_event(settings, msg)
        stats[row.category] = stats.get(row.category, 0) + 1
        session.commit()
    return stats
