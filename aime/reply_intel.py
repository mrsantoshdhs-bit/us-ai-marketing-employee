"""Reply intelligence: classify replies, match them to leads, update the CRM, suggest responses.

Categories (first match wins, safety first): bounce, calendly, unsubscribe (including complaints),
out_of_office, then the human answers interested / not_interested / objection / referral / question,
else other. Conflicting signals or a complaint lower the confidence and send the reply to review.

Every human reply stops the automated sequence. Unsubscribes and bounces suppress the address.
Interested replies open (or advance) an opportunity and notify the Sales Manager. Suggested responses
are drafts that wait for approval like every other email. Simulated replies (`aime simulate-reply`)
are processed the same way and labelled SIMULATED wherever they appear.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.crm_ops import log_activity, suppress
from aime.models import Alert, Contact, FollowUp, InboundEmail, Lead, Opportunity
from aime.notify import notify_event
from aime.outreach import stop_sequence
from aime.timeutil import utcnow

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("unsubscribe", re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|"
                               r"do not (email|contact)|don'?t (email|contact) me|opt[- ]?out)\b", re.I)),
    ("not_interested", re.compile(r"\b(not interested|no thanks|no, thank you|not a (good )?fit|we'?re all set|"
                                  r"not (looking|in the market)|pass on this|no need|not for us|we'?re good)\b", re.I)),
    ("interested", re.compile(r"\b(interested|sounds (good|great|interesting)|let'?s (talk|chat|connect|set up)|"
                              r"happy to (chat|talk|connect|meet)|book a (time|call|meeting)|schedule a (call|meeting)|"
                              r"set up a (call|meeting)|tell me more|send (me )?(more|some) (info|details)|"
                              r"what (does it|would it) cost|how much|pricing|availab(le|ility)|free (on|next)|"
                              r"(monday|tuesday|wednesday|thursday|friday) (works|at|morning|afternoon))\b", re.I)),
    ("objection", re.compile(r"\b(too expensive|no budget|budget (is|was)|not (a priority|right now)|maybe later|"
                             r"next (quarter|year)|bad timing|already (work|working|use|using|have)|in-house|"
                             r"circle back|check back)\b", re.I)),
    ("referral", re.compile(r"\b(you should (talk|speak|reach out) to|reach out to|the right person|"
                            r"(cc|cc'?ing|copying|looping in)\b|forward(ed|ing)? (this|your)|contact our)", re.I)),
]
COMPLAINT = re.compile(r"\b(spam|report(ed|ing)? (you|this)|lawyer|attorney|legal action|harass|gdpr|can-?spam|"
                       r"how did you get (my|this) (email|address))\b", re.I)
OOO = re.compile(r"\b(out of (the )?office|on (vacation|holiday|leave)|limited access to (my )?email|"
                 r"will be back|returning on|away until|automatic reply)\b", re.I)
CALENDLY = re.compile(r"calendly\.com", re.I)
HUMAN = ("interested", "not_interested", "objection", "referral", "question", "other")
OUTREACH_STAGE = {"interested": "interested", "not_interested": "not_interested"}
QUESTION_PLACEHOLDER = "[Write your answer here]"


def classify(row: InboundEmail) -> tuple[str, str, list[str]]:
    text = row.new_text or row.body_text or ""
    subject = row.subject or ""
    if row.bounced_addresses or re.match(r"^(mailer-daemon|postmaster)@", row.from_email or "", re.I):
        return "bounce", "high", ["delivery failure report"]
    if CALENDLY.search(row.from_email or "") or (CALENDLY.search(text) and re.search(r"\b(new event|canceled|cancelled)\b", subject, re.I)):
        return "calendly", "high", ["Calendly notification"]
    complaint = COMPLAINT.search(f"{subject}\n{text}")
    hits = {name: m.group(0) for name, rx in PATTERNS if (m := rx.search(f"{subject}\n{text}"))}
    if "unsubscribe" in hits or complaint:
        reasons = [f'"{hits["unsubscribe"]}"'] if "unsubscribe" in hits else []
        if complaint:
            reasons.append(f'complaint: "{complaint.group(0)}"')
        return "unsubscribe", "medium" if complaint else "high", reasons
    if row.auto_submitted or OOO.search(f"{subject}\n{text[:400]}"):
        return "out_of_office", "high" if row.auto_submitted else "medium", ["automatic reply"]
    human = [k for k in ("not_interested", "interested", "objection", "referral") if k in hits]
    if not human:
        if "?" in text:
            return "question", "medium", ["asks a question"]
        return "other", "low", ["no clear signal"]
    category = human[0]
    conflicting = len(set(human) & {"interested", "not_interested"}) == 2 or len(human) > 2
    reasons = [f'{k.replace("_", " ")}: "{hits[k]}"' for k in human]
    return category, "low" if conflicting else ("high" if len(human) == 1 else "medium"), reasons


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
    address = settings.company_postal_address or "[postal address required before sending]"
    return (f"\n\n---\nTo stop receiving emails from us, reply with \"unsubscribe\" or write to {unsub}.\n"
            f"{settings.company_name} · {address}")


def suggest(settings, category: str, row: InboundEmail, contact: Contact | None) -> tuple[str, str] | None:
    name = (row.from_name or "").split()[0] if row.from_name else (contact.first_name if contact and contact.first_name else "there")
    subject = row.subject if row.subject.lower().startswith("re:") else f"Re: {row.subject}"
    sign = _signature(settings)
    if category == "interested":
        if settings.calendly_url:
            body = (f"Hi {name},\n\nThank you for getting back to me. You can pick a time that suits you here: "
                    f"{settings.calendly_url}\n\nIf none of those times work, reply with a few that do and I will fit "
                    f"around you.\n\n{sign}")
        else:
            body = (f"Hi {name},\n\nThank you for getting back to me. Could you share two or three times next week "
                    f"that suit you for a 20-minute call?\n\n{sign}")
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
        log_activity(session, f"Opportunity created from an interested reply: {opp.name}", actor, type_="opportunity",
                     company_id=lead.company_id, lead_id=lead.id, opportunity_id=opp.id)
    elif opp.stage == "new":
        opp.stage, opp.probability = "discovery", max(opp.probability or 0, 20)
    return opp


def act(session: Session, settings, row: InboundEmail, actor: str) -> None:
    """Apply the CRM consequences of a classified email and record what was done."""
    label = "SIMULATED reply" if row.is_simulated else "Reply"
    f, contact, lead = match(session, row)
    row.followup_id = f.id if f else None
    row.contact_id, row.lead_id = (contact.id if contact else None), (lead.id if lead else None)
    row.company_id = lead.company_id if lead else (contact.company_id if contact else None)
    notes = []
    if row.category == "bounce":
        for addr in row.bounced_addresses or ([f.to_email] if f and f.to_email else []):
            for c in session.execute(select(Contact).where(func.lower(Contact.email) == addr.lower())).scalars():
                c.email_status = "bounced"
            suppress(session, addr, "Bounced (delivery failure report)")
            notes.append(f"{addr} bounced: marked bounced and suppressed")
        if lead and lead.outreach_status:
            stop_sequence(session, lead, "bounced", "Email bounced", actor)
        row.status = "actioned" if notes else "needs_review"
    elif row.category == "unsubscribe":
        suppress(session, row.from_email, f"Unsubscribe request by email{' (complaint)' if row.confidence != 'high' else ''}")
        if contact:
            contact.consent_status = "opted_out"
        if lead and lead.outreach_status:
            stop_sequence(session, lead, "unsubscribed", "Asked not to be contacted", actor)
        notes.append(f"{row.from_email} suppressed; no further email")
        row.status = "actioned" if row.confidence == "high" else "needs_review"
    elif row.category == "out_of_office":
        notes.append("Automatic reply: the sequence continues")
        row.status = "actioned"
    elif row.category == "calendly":
        notes.append("Calendly notification: handled by the meetings step")
        row.status = "needs_review"
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
            msg = f"{'SIMULATED: ' if row.is_simulated else ''}Interested reply from {row.from_email} ({lead.company.name})"
            session.add(Alert(level="info", source="inbox", message=msg[:500]))
            notify_event(settings, msg)
        elif row.category == "not_interested":
            if lead.status == "qualified":
                lead.status = "nurture"
            notes.append("lead moved to nurture")
        proposal = suggest(settings, row.category, row, contact)
        if proposal:
            subject, body = proposal
            draft = FollowUp(lead_id=lead.id, contact_id=contact.id if contact else None, company_id=lead.company_id,
                             channel="email", kind="response", step=1, due_at=utcnow(), status="awaiting_approval",
                             subject=subject, body=body, to_email=row.from_email, in_reply_to=row.message_id,
                             blocked_reason="Write your answer before approving" if QUESTION_PLACEHOLDER in body else None)
            session.add(draft)
            session.flush()
            row.suggested_followup_id = draft.id
            notes.append("suggested reply waiting for your approval")
        row.status = "actioned" if row.confidence == "high" and row.category in ("interested", "not_interested") else "needs_review"
    row.action_note = "; ".join(notes) or None
    row.processed_at = utcnow()
    log_activity(session, f"{label} from {row.from_email}: {row.category.replace('_', ' ')} ({row.confidence})",
                 actor, type_="reply", body=(row.new_text or "")[:2000], company_id=row.company_id, lead_id=row.lead_id)


def process_pending(session: Session, settings, actor: str) -> dict:
    stats: dict[str, int] = {}
    for row in session.execute(select(InboundEmail).where(InboundEmail.status == "new").order_by(InboundEmail.id)).scalars().all():
        row.category, row.confidence, row.reasons = classify(row)
        act(session, settings, row, actor)
        stats[row.category] = stats.get(row.category, 0) + 1
        session.commit()
    return stats
