"""B2B outreach to researched decision-makers (owner decision, 26 Oct 2026): first-touch emails and
follow-up sequences grounded in recorded evidence. Every message waits for the Sales Manager's approval;
nothing is sent while OUTBOUND_EMAIL_ENABLED=false.

Personalization uses only what the company published: a sentence from its own job post, a sentence
from its website about a topic we serve, or its own description, quoted verbatim with the link kept
on the draft. A lead without quotable evidence gets a review task, never a generic email.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.contact_validation import STATUS_OK
from aime.crm_ops import is_suppressed, log_activity
from aime.integrations.email_smtp import compliance_footer
from aime.knowledge.content_bank import BRAND_LINES
from aime.models import Company, FollowUp, Lead, ResearchSnapshot, Signal
from aime.timeutil import utcnow

TOPICS = {
    "custom_crm": "your CRM setup", "ai_agents": "AI agents", "agent_orchestration": "your AI workflows",
    "bpa": "manual data entry", "sales_automation": "sales follow-ups", "leadgen_automation": "lead capture",
    "saas_dev": "your product roadmap", "custom_software": "internal tools", "ai_consulting": "AI adoption",
}
OPEN = ("pending", "awaiting_approval", "approved", "sending", "uncertain")
STOP_STATUSES = ("replied", "interested", "meeting", "not_interested", "no_response", "unsubscribed", "bounced",
                 "stopped")


@dataclass
class Evidence:
    fact: str  # verbatim quote (may end with "…" when shortened)
    url: str | None
    kind: str  # hiring | website | description
    source_text: str  # the full text the quote was taken from

    def as_dict(self) -> dict:
        return {"fact": self.fact, "url": self.url, "kind": self.kind, "source_text": self.source_text[:1500]}


def policy(settings: Settings) -> dict:
    cfg = settings.section("outreach")
    return {"enabled": bool(cfg.get("b2b_enabled", False)), "drafts_per_run": int(cfg.get("drafts_per_run", 10)),
            "min_grade": str(cfg.get("min_grade", "B")), "intervals": list(cfg.get("intervals_days", [0, 4, 10])),
            "max_touches": int(cfg.get("max_touches", 3)),
            "include_inboxes": bool(cfg.get("include_company_inboxes", True))}


def short_quote(text: str, keywords: list[str] | None = None, max_len: int = 140) -> str:
    """The sentence that contains a keyword (else the first), verbatim, shortened at a word boundary."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s.strip()]
    if not sentences:
        return ""
    keys = [k.lower() for k in keywords or []]
    pick = next((s for s in sentences if any(k in s.lower() for k in keys)), sentences[0])
    if len(pick) > max_len:
        pick = pick[:max_len].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return pick.strip('"“”')


def gather_evidence(session: Session, lead: Lead) -> list[Evidence]:
    """Most specific first: the company's own job post, its website on a topic we serve, its description."""
    out: list[Evidence] = []
    for s in session.execute(select(Signal).where(Signal.company_id == lead.company_id, Signal.signal_type == "hiring")
                             .order_by(Signal.strength.desc(), Signal.observed_at.desc())).scalars():
        if s.text and s.source_url:
            quote = short_quote(s.text, list(s.keywords or []))
            if len(quote) >= 25:
                out.append(Evidence(quote, s.source_url, "hiring", s.text))
                break
    snap = session.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == lead.company_id)
                           .order_by(ResearchSnapshot.id.desc())).scalars().first()
    if snap:
        for need in snap.needs or []:
            if need.get("service") == lead.primary_service and need.get("quote"):
                out.append(Evidence(short_quote(need["quote"], need.get("keywords")), need.get("url"), "website",
                                    need["quote"]))
                break
        if snap.description:
            out.append(Evidence(short_quote(snap.description), snap.description_url, "description", snap.description))
    return out


def outreach_footer(settings: Settings) -> str:
    return compliance_footer(settings, outreach=True)


def _signature(settings: Settings) -> str:
    sender = settings.sender_name or f"The {settings.company_name} team"
    return f"Best regards,\n{sender}\n{settings.company_name}"


def personal_line(ev: Evidence, company: str) -> str:
    if ev.kind == "hiring":
        return f'I came across your job post, which mentions "{ev.fact}"'
    if ev.kind == "website":
        return f'Your website mentions "{ev.fact}"'
    return f'Your website describes {company} this way: "{ev.fact}"'


def draft_outreach(settings: Settings, step: int, lead: Lead, contact, evidence: list[Evidence]) -> tuple[str, str]:
    from aime.meetings import booking_minutes  # the length of the meeting the booking link offers

    company = lead.company.name
    topic = TOPICS.get(lead.primary_service or "", "your operations")
    inbox = contact.role_category == "inbox" or not contact.first_name
    greeting = f"Hello {company} team," if inbox else f"Hi {contact.first_name.split()[0]},"
    if step == 1:
        ev = evidence[0]
        brand = BRAND_LINES.get(lead.primary_service or "", "At {company}, we build software for teams like yours.")
        line = personal_line(ev, company)
        ask = (f"Could you pass this to whoever looks after {topic}? A short call would tell us both quickly "
               "whether it is worth pursuing." if inbox else
               f"Would a {booking_minutes(settings)}-minute call next week be useful to see whether this could help {company}? "
               "If someone else looks after this, I would be grateful for a pointer.")
        subject = f"Question about {topic} at {company}"
        end = "" if line.endswith(('."', '!"', '?"')) else "."  # no doubled full stop after a quoted sentence
        body = f"{greeting}\n\n{line}{end}\n\n{brand.format(company=settings.company_name)}\n\n{ask}\n\n{_signature(settings)}"
    elif step == 2:
        subject = f"Following up: {topic} at {company}"
        body = (f"{greeting}\n\nI wanted to follow up on my note about {topic}. If it is on your list this quarter, "
                "I would be happy to walk through how we would approach it for your team. If not, no problem at all."
                f"\n\n{_signature(settings)}")
    else:
        subject = f"Closing the loop on {topic}"
        body = (f"{greeting}\n\nThank you for taking the time to read my notes. I will not keep following up. If "
                f"{topic} becomes a priority, just reply to this email and we can pick it up from there."
                f"\n\n{_signature(settings)}")
    return subject, body + outreach_footer(settings)


def eligible(session: Session, settings: Settings, lead: Lead, for_send: bool = False) -> tuple[bool, str]:
    """Can this lead's primary contact receive B2B outreach? Returns (ok, reason when not)."""
    pol = policy(settings)
    contact = lead.contact
    company: Company = lead.company
    if not pol["enabled"]:
        return False, "B2B outreach is turned off ([outreach] b2b_enabled)"
    if lead.status != "qualified":
        return False, f"lead is {lead.status}"
    if (lead.grade or "D") > pol["min_grade"]:
        return False, f"grade {lead.grade} is below {pol['min_grade']}"
    if company.is_competitor:
        return False, "possible competitor"
    if contact is None or not contact.email:
        return False, "no published business email"
    if contact.source not in ("website", "sec_filing") or not contact.email_source_url:
        return False, "the email was not published by the company"
    if contact.email_status not in STATUS_OK:
        return False, f"email status is {contact.email_status or 'unknown'}"
    if contact.role_category == "inbox" and not pol["include_inboxes"]:
        return False, "company inboxes are excluded ([outreach] include_company_inboxes)"
    if contact.consent_status == "opted_out" or is_suppressed(session, contact.email, company.domain):
        return False, "on the do-not-contact list"
    if lead.outreach_status in STOP_STATUSES:
        return False, f"outreach stopped ({lead.outreach_status})"
    return True, ""


def set_status(session: Session, lead: Lead, status: str, actor: str, note: str | None = None) -> None:
    if lead.outreach_status == status:
        return
    lead.outreach_status, lead.outreach_updated_at = status, utcnow()
    log_activity(session, f"Outreach: {status.replace('_', ' ')}" + (f" ({note})" if note else ""), actor,
                 type_="outreach", company_id=lead.company_id, lead_id=lead.id)


def record_delivery(session: Session, f: FollowUp, sent_at, delivered_to: str | None, actor: str) -> None:
    """What a delivered email changes, whether the send succeeded or a person confirmed it ("mark sent"): the
    follow-up, the lead's last activity, and the outreach stage that lets the next step be drafted."""
    f.status, f.sent_at, f.blocked_reason = "sent", sent_at, None
    f.delivered_to = delivered_to or f.delivered_to
    lead = f.lead
    lead.last_activity_at = sent_at
    if f.kind == "outreach" and lead.outreach_status in (None, "drafted", "approved"):
        set_status(session, lead, "contacted", actor)


def stop_sequence(session: Session, lead: Lead, status: str, reason: str, actor: str) -> int:
    """Cancel every open email of this lead's sequence and record why. Returns how many were cancelled."""
    cancelled = 0
    for f in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                    FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
        f.status, f.blocked_reason = "cancelled", reason[:300]
        cancelled += 1
    set_status(session, lead, status, actor, reason)
    return cancelled
