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

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aime.config import Settings
from aime.contact_validation import STATUS_OK
from aime.crm_ops import is_suppressed, log_activity
from aime.dedupe import company_to_address
from aime.integrations.email_smtp import compliance_footer
from aime.knowledge.content_bank import BRAND_LINES
from aime.models import Company, FollowUp, Lead, ResearchSnapshot, Signal
from aime.timeutil import utcnow

TOPICS = {
    "custom_crm": "your CRM setup", "ai_agents": "AI agents", "agent_orchestration": "your AI workflows",
    "bpa": "manual data entry", "sales_automation": "sales follow-ups", "leadgen_automation": "lead capture",
    "saas_dev": "your product roadmap", "custom_software": "internal tools", "ai_consulting": "AI adoption",
}
MAX_SUBJECT_CHARS = 70
TEST_SENT = "test_sent"  # the stage of a lead whose email went only to a test address (OUTBOUND_TEST_RECIPIENTS)
RUNNING_STAGES = ("contacted", TEST_SENT)  # a sequence is under way
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
            "include_inboxes": bool(cfg.get("include_company_inboxes", True)),
            "reengage_after_days": int(cfg.get("reengage_after_days", 0))}


# A full stop that ends an abbreviation or an initial does not end the sentence: "U.S.", "e.g.", "Inc.".
_NOT_THE_END = re.compile(r"(?:\b[A-Za-z]\.|\b(?:etc|vs|inc|ltd|llc|corp|co|dr|mr|mrs|ms|st|jr|sr|no|approx|prev)\.)$", re.I)
_NOT_PROSE = re.compile(r"[|$€£\"•→]|--|https?://|www\.|\S+@\S|\[email|\b\w+\.(?:com|io|ai|org|net|co)\b|\d+\s?[kK]\b|^[-*\d(]"
                        r"|^[\w/&+ -]{1,30}:\s", re.I)
# What a person would not quote back: how to get the job, what it pays, who may apply, the technology list.
_JOB_TALK = re.compile(r"\b(?:salary|salaries|compensation|visa|sponsor\w*|citizen\w*|clearance|equity|bonus|benefits?|pto|401k|apply|applicants?|"
                       r"resumes?|cv|interview\w*|hiring|hire|recruit\w*|candidates?|jobs?|roles?|positions?|openings?|full[- ]time|part[- ]time|"
                       r"onsite|on-site|hybrid|remote|in[- ]office|relocat\w*|time ?zones?|yrs|years? of|experience|you|you'?ll|you'?re|you'?ve|"
                       r"your|looking for|join|must|nice to have|stack|languages?|engineers?|developers?|teammates?|colleagues?|comfortable|"
                       r"expect to|manager|ownership|"
                       r"we use|i|i'?m|i'?ve)\b", re.I)
# What a website says that is not about the company's work: a person's career, a customer's words, a button.
_NOT_ABOUT_THE_WORK = re.compile(r"\b(?:previously|prior to|formerly|holds? a|joined|alumni|prev\.|ph\.?d|co-?founder|founder|ceo|cto|served as|"
                                 r"career|graduated|brings|shoutout|really neat|made in|powered by|built with|copyright|all rights|cookies?|privacy|sign up|sign in|log in|"
                                 r"learn more|read more|click|book a|thank you|welcome to|introducing|contact us|subscribe)\b", re.I)
MIN_QUOTE_WORDS, MAX_QUOTE_CHARS = 5, 170


def _sentences(text: str) -> list[str]:
    """Each line of the text, split into sentences. A line break always ends one: a post's header never runs into its text."""
    out: list[str] = []
    for line in text.splitlines():
        parts: list[str] = []
        for piece in re.split(r"(?<=[.!?])\s+", " ".join(line.split())):
            if parts and (_NOT_THE_END.search(parts[-1]) or piece[:1].islower()):
                parts[-1] += " " + piece
            elif piece:
                parts.append(piece)
        out += parts
    return out


def _reads_as_prose(sentence: str, ended: bool = True) -> bool:
    """A sentence someone wrote, not a post's header, a price, a link or a list. `ended`: it must end like a sentence (a
    company's own one-line description may not)."""
    words = sentence.split()
    return (len(words) >= MIN_QUOTE_WORDS and sentence.count(",") * 3 < len(words) and sentence[0].isupper()
            and not (ended and sentence[-1] not in ".!?") and not _NOT_PROSE.search(sentence))


def _shortened(sentence: str, max_len: int) -> str:
    """Verbatim; a long sentence ends at its last clause that fits, else at a word."""
    if len(sentence) <= max_len:
        return sentence
    cut = sentence[:max_len]
    clause = max(cut.rfind(", "), cut.rfind(" - "), cut.rfind(" – "), cut.rfind(" — "), cut.rfind("; "))
    cut = cut[:clause] if clause >= max_len // 2 else cut.rsplit(" ", 1)[0]
    return cut.rstrip(",;:-–— ") + "…"


def _has_word(sentence: str, keywords: list[str]) -> bool:
    low = sentence.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", low) for k in keywords)


def short_quote(text: str, keywords: list[str] | None = None, max_len: int = MAX_QUOTE_CHARS) -> str:
    """From a job post: the first sentence that says what the company does, one with a keyword if there is one. Empty when
    no sentence is fit to quote: no email is better than one that quotes a job post's header back at its author."""
    fit = [s for s in _sentences(text) if _reads_as_prose(s) and not _JOB_TALK.search(s)]
    if not fit:
        return ""
    return _shortened(next((s for s in fit if keywords and _has_word(s, keywords)), fit[0]), max_len)


def website_quote(text: str, keywords: list[str] | None = None, max_len: int = MAX_QUOTE_CHARS) -> str:
    """From a company's own pages: a sentence about its work with one of the keywords; without keywords, its description."""
    fit = [s for s in _sentences(text) if _reads_as_prose(s, ended=keywords is not None) and not _NOT_ABOUT_THE_WORK.search(s)]
    if keywords is not None:
        fit = [s for s in fit if _has_word(s, keywords)]
    return _shortened(fit[0], max_len) if fit else ""


def gather_evidence(session: Session, lead: Lead) -> list[Evidence]:
    """What the company says about its own work comes first: its description, its website on a topic we serve, then its
    job post, which is written for engineers. Only sentences fit to quote count; none at all means a review task."""
    out: list[Evidence] = []
    snap = session.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == lead.company_id)
                           .order_by(ResearchSnapshot.id.desc())).scalars().first()
    if snap:
        quote = website_quote(snap.description) if snap.description else ""
        if len(quote) >= 25:
            out.append(Evidence(quote, snap.description_url, "description", snap.description))
        for need in snap.needs or []:
            if need.get("service") == lead.primary_service and need.get("quote"):
                quote = website_quote(need["quote"], list(need.get("keywords") or []))
                if len(quote) >= 25:
                    out.append(Evidence(quote, need.get("url"), "website", need["quote"]))
                break
    for s in session.execute(select(Signal).where(Signal.company_id == lead.company_id, Signal.signal_type == "hiring")
                             .order_by(Signal.strength.desc(), Signal.observed_at.desc())).scalars():
        if s.text and s.source_url:
            quote = short_quote(s.text, list(s.keywords or []))
            if len(quote) >= 25:
                out.append(Evidence(quote, s.source_url, "hiring", s.text))
                break
    return out


PUBLISHED_SOURCES = ("website", "sec_filing")
GIVEN_TO_US = ("We emailed you because this address was given to us directly as a business contact; "
               "it was not taken from your website.")


def outreach_footer(settings: Settings, contact=None) -> str:
    """The opt-out footer, with the true reason: a published business address, or one that was given to us (a manual
    entry or a test contact), which must never be described as published."""
    given = contact is not None and getattr(contact, "source", None) not in PUBLISHED_SOURCES
    return compliance_footer(settings, outreach=True, why=GIVEN_TO_US if given else None)


def _signature(settings: Settings) -> str:
    sender = settings.sender_name or f"The {settings.company_name} team"
    return f"Best regards,\n{sender}\n{settings.company_name}"


def personal_line(ev: Evidence, company: str | None) -> str:
    if ev.kind == "hiring":
        return f'I came across your job post, which says "{ev.fact}"'
    if ev.kind == "website":
        return f'Your website says "{ev.fact}"'
    return f'Your website describes {company or "your company"} this way: "{ev.fact}"'


def _addressed(lead: Lead, contact) -> tuple[str | None, str, str]:
    """(the company's name fit to write, or None), the greeting, and " at <company>" for a subject line."""
    record = lead.company
    company = company_to_address(record.name, getattr(record, "website_title", None), getattr(record, "domain", None))
    inbox = contact.role_category == "inbox" or not contact.first_name
    team = f"Hello {company} team," if company else "Hello team,"
    return company, team if inbox else f"Hi {contact.first_name.split()[0]},", f" at {company}" if company else ""


def _subject(start: str, at_company: str) -> str:
    """A long company name stays in the greeting: a subject over the limit would fail the quality checks."""
    return start + at_company if len(start + at_company) <= MAX_SUBJECT_CHARS else start


def draft_outreach(settings: Settings, step: int, lead: Lead, contact, evidence: list[Evidence]) -> tuple[str, str]:
    from aime.meetings import booking_minutes  # the length of the meeting the booking link offers

    named, greeting, at_company = _addressed(lead, contact)
    company = named or "your team"
    topic = TOPICS.get(lead.primary_service or "", "your operations")
    inbox = contact.role_category == "inbox" or not contact.first_name
    if step == 1:
        ev = evidence[0]
        brand = BRAND_LINES.get(lead.primary_service or "", "At {company}, we build software for teams like yours.")
        line = personal_line(ev, named)
        ask = (f"Could you pass this to whoever looks after {topic}? A short call would tell us both quickly "
               "whether it is worth pursuing." if inbox else
               f"Would a {booking_minutes(settings)}-minute call next week be useful to see whether this could help {company}? "
               "If someone else looks after this, I would be grateful for a pointer.")
        subject = _subject(f"Question about {topic}", at_company)
        end = "" if line.endswith(('."', '!"', '?"')) else "."  # no doubled full stop after a quoted sentence
        body = f"{greeting}\n\n{line}{end}\n\n{brand.format(company=settings.company_name)}\n\n{ask}\n\n{_signature(settings)}"
    elif step == 2:
        subject = _subject(f"Following up: {topic}", at_company)
        body = (f"{greeting}\n\nI wanted to follow up on my note about {topic}. If it is on your list this quarter, "
                "I would be happy to walk through how we would approach it for your team. If not, no problem at all."
                f"\n\n{_signature(settings)}")
    else:
        subject = f"Closing the loop on {topic}"
        body = (f"{greeting}\n\nThank you for taking the time to read my notes. I will not keep following up. Should "
                f"{topic} become a priority, just reply to this email and we can pick it up from there."
                f"\n\n{_signature(settings)}")
    return subject, body + outreach_footer(settings, contact)


def draft_reengage(settings: Settings, lead: Lead, contact) -> tuple[str, str]:
    """The one re-engagement email, months after a sequence nobody answered: no new claim, no quote, no link, and a
    promise not to write again."""
    _, greeting, at_company = _addressed(lead, contact)
    topic = TOPICS.get(lead.primary_service or "", "your operations")
    subject = _subject(f"Checking back in: {topic}", at_company)
    body = (f"{greeting}\n\nA few months ago I wrote about {topic}{at_company}. Priorities change, so I wanted to "
            f"check in once: if you are looking at {topic} now, I would be glad to share how we would approach it. If "
            f"not, there is no need to reply, and I will not write again.\n\n{_signature(settings)}")
    return subject, body + outreach_footer(settings, contact)


def eligible(session: Session, settings: Settings, lead: Lead, for_send: bool = False,
             reengage: bool = False, running: bool = False) -> tuple[bool, str]:
    """Can this lead's primary contact receive B2B outreach? Returns (ok, reason when not). `reengage`: the one
    re-engagement email, which also reaches a lead parked in nurture after an unanswered sequence. `running`: a later
    step of a sequence a person started. Neither depends on the grade: it decided the first email, and it falls by
    itself as evidence ages. Every other check still applies."""
    pol = policy(settings)
    contact = lead.contact
    company: Company = lead.company
    if not pol["enabled"]:
        return False, "B2B outreach is turned off ([outreach] b2b_enabled)"
    if lead.status != "qualified" and not (reengage and lead.status == "nurture"):
        return False, f"lead is {lead.status}"
    # The one check-in and the later steps go to someone already emailed: by design their evidence has aged, and the grade with it.
    if not (running or reengage) and (lead.grade or "D") > pol["min_grade"]:
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
    if lead.outreach_status in STOP_STATUSES and not (reengage and lead.outreach_status == "no_response"):
        return False, f"outreach stopped ({lead.outreach_status})"
    return True, ""


def set_status(session: Session, lead: Lead, status: str, actor: str, note: str | None = None) -> None:
    if lead.outreach_status == status:
        return
    lead.outreach_status, lead.outreach_updated_at = status, utcnow()
    log_activity(session, f"Outreach: {status.replace('_', ' ')}" + (f" ({note})" if note else ""), actor,
                 type_="outreach", company_id=lead.company_id, lead_id=lead.id)


def delivered_to_prospect(f: FollowUp) -> bool:
    return not f.delivered_to or not f.to_email or f.delivered_to.lower() == f.to_email.lower()


# Emails that really went to the prospect, for counting. In test mode delivered_to is our own address.
REACHED_PROSPECT = or_(FollowUp.delivered_to.is_(None), FollowUp.to_email.is_(None),
                       func.lower(FollowUp.delivered_to) == func.lower(FollowUp.to_email))


def record_delivery(session: Session, f: FollowUp, sent_at, delivered_to: str | None, actor: str) -> None:
    """What a delivered email changes, whether the send succeeded or a person confirmed it ("mark sent"): the
    follow-up, the lead's last activity, and the outreach stage that lets the next step be drafted."""
    f.status, f.sent_at, f.blocked_reason = "sent", sent_at, None
    f.delivered_to = delivered_to or f.delivered_to
    f.delivery_status = f.delivery_status if f.delivery_status in ("bounced", "replied") else "sent"
    lead = f.lead
    lead.last_activity_at = sent_at
    if f.kind == "outreach" and lead.outreach_status in (None, "drafted", "approved", "reengage", TEST_SENT):
        # Test mode: it went to our own address, so the prospect has heard nothing from us. A rehearsal still walks the sequence.
        set_status(session, lead, "contacted" if delivered_to_prospect(f) else TEST_SENT, actor)


def stop_sequence(session: Session, lead: Lead, status: str, reason: str, actor: str) -> int:
    """Cancel every open email of this lead's sequence and record why. Returns how many were cancelled. A later reply
    never changes the "unsubscribed" stage: the opt-out still stands."""
    cancelled = 0
    for f in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                    FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
        f.status, f.blocked_reason = "cancelled", reason[:300]
        cancelled += 1
    if lead.outreach_status != "unsubscribed":
        set_status(session, lead, status, actor, reason)
    return cancelled
