"""The handover package (Option B, Phase 6): what the Sales Manager needs to take a deal over, written from the records
alone, with every line naming the record it was read from.

**Conditions** (settings section "handover_rules", each one can be switched off): an interested reply with every required
fact stated (requirement, budget and timeline by default); a meeting booked; a pricing question; an explicit request for a
person; a lead score of at least `min_score` with a budget stated; a VIP account (by company domain or name); the
conversation engine asking for a person (an ai_decisions row of kind "handover" that no person rejected, or an open alert
keyed `conversation:handover:<lead id>`, which `request_handover` writes). They are checked where a deal was already handed
over (aime.handover.handover: the interested-reply path of aime.reply_intel, booking.book and meetings.record_meeting) and by
the periodic pipeline checks for the leads the conversation engine works and the VIP accounts. Checking never changes what
those callers decided: it only writes the package.

**The package.** Who the prospect is (company, contact, the latest research snapshot), what they need, budget and timeline
as they stated them (their own words, found again in the email they came from), stakeholders, objections and how they were
answered, the meeting in the prospect's and the manager's time zone, risks, the recommended next step and links to the
thread and the research. Each line carries a `source`: an inbound email (`{"type": "inbound_email", "id"}`), a research
snapshot (`{"type": "research_snapshot", "id"}`) or a CRM field (`{"type": "crm", "table", "id", "field"}`). A fact the
conversation engine recorded counts only when the email it names belongs to this lead and its quote is in that email; a
budget or a timeline counts only with a quote. Anything else is left out, and what is not known yet is listed as such. No
model writes or rewrites a line: the package is composed deterministically, so it works with no LLM.

**Versions.** A new version is written only when the content changes (hash of the sections and conditions), so the same
handover twice writes nothing. A new version supersedes the one before; a version written while the manager owns the deal
stays accepted; one written while waiting keeps the SLA clock (`waiting_since`).

**Ownership.** The deal's owner (or an administrator) accepts the package: the lead's conversation state becomes
handed_over, which the conversation engine reads as "AI drafting paused" (`ai_paused`); aime.ai.state lets only a person
set it. Returning it, with a reason, puts the conversation back where it was. Not accepted within `accept_within_hours`:
escalated (aime.assignment.handover_sla). The internal email to the owner is aime.handover_notify, off by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from aime.models import (AIDecision, Alert, Company, Contact, Conversation, ConversationFact, FollowUp, HandoverPackage,
                         InboundEmail, Lead, Meeting, Opportunity, ResearchSnapshot)
from aime.timeutil import utcnow

log = logging.getLogger("aime.handover_package")

SECTION = "handover_rules"
CLOSED = ("won", "lost")
QUOTE_REQUIRED = frozenset({"budget", "timeline"})  # shown only in the prospect's own words
ESCALATION_KEY = "conversation:handover:{lead_id}"
HUMAN = re.compile(r"\b(?:speak|talk|chat|meet)\s+(?:to|with)\s+(?:a\s+|an\s+|the\s+|your\s+)?(?:real\s+|actual\s+)?"
                   r"(?:person|human|someone|somebody|sales\s*(?:person|rep|representative|manager|team)|manager|team|account\s+manager)\b"
                   r"|\b(?:a\s+)?(?:real|actual)\s+(?:person|human)\b|\bnot\s+(?:a\s+)?(?:bot|robot|an?\s+ai)\b|\bhuman\s+being\b",
                   re.I)
PRICE = re.compile(r"\b(?:price|prices|pricing|cost|costs|quote|rates?|how\s+much|fees?)\b", re.I)
TITLES = {"why": "Why it is handed over now", "prospect": "Who the prospect is", "needs": "What they need",
          "budget_timeline": "Budget and timeline, as they stated them", "stakeholders": "Stakeholders",
          "objections": "Objections and how they were answered", "questions": "Their questions",
          "meeting": "Meeting", "risks": "Risks", "next_step": "Recommended next step", "links": "Thread and research"}


# =============================================================================================== settings

def rules(settings) -> dict:
    from aime import settings_store

    return settings_store.get(settings, SECTION)


# =============================================================================================== reading the records

def _norm(text: str | None) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split()).lower()


def quoted_in(quote: str | None, row: InboundEmail) -> bool:
    """The quote is in the email, word for word (spacing, case and typographic quotes aside)."""
    q = _norm(quote).strip(" \"'")
    return bool(q) and q in _norm(f"{row.new_text or ''}\n{row.body_text or ''}")


def crm(table: str, rid: int, field: str) -> dict:
    return {"type": "crm", "table": table, "id": rid, "field": field}


def email_src(row: InboundEmail, quote: str | None = None) -> dict:
    out = {"type": "inbound_email", "id": row.id}
    if quote:
        out["quote"] = quote[:300]
    return out


def cite_label(src: dict) -> str:
    kind = src.get("type")
    if kind == "inbound_email":
        return f"email #{src['id']}"
    if kind == "research_snapshot":
        return f"research #{src['id']}"
    if kind == "crm":
        return f"CRM {src['table']}.{src['field']} #{src['id']}"
    return "?"


def cited(src) -> bool:
    """A source the reader can open: an inbound email, a research snapshot or a CRM field, each with its record id."""
    if not isinstance(src, dict) or not isinstance(src.get("id"), int):
        return False
    return (src.get("type") in ("inbound_email", "research_snapshot")
            or (src.get("type") == "crm" and bool(src.get("table")) and bool(src.get("field"))))


def emails_of(session: Session, lead_id: int) -> dict[int, InboundEmail]:
    """The lead's real inbound emails (never a simulated one), by id."""
    rows = session.execute(select(InboundEmail).where(InboundEmail.lead_id == lead_id, InboundEmail.is_simulated.is_(False))
                           .order_by(InboundEmail.received_at, InboundEmail.id)).scalars().all()
    return {r.id: r for r in rows}


def fact_source(f: ConversationFact, emails: dict[int, InboundEmail]) -> dict | None:
    """Where a recorded fact can be read, or None: it is then not written. A person's entry or correction is a CRM
    record; anything else must name an email of this lead, with a quote that is in it (budget and timeline: always)."""
    if f.status == "withdrawn":
        return None
    if f.source == "human" or f.status in ("confirmed", "corrected"):
        if f.confirmed_by:
            return crm("conversation_facts", f.id, "value")
        if f.status == "corrected":
            return None  # a correction nobody signed is no one's word
    row = emails.get(f.evidence_inbound_id) if f.evidence_inbound_id else None
    if row is None:
        return None
    if f.evidence_quote:
        return email_src(row, f.evidence_quote) if quoted_in(f.evidence_quote, row) else None
    return None if f.kind in QUOTE_REQUIRED else email_src(row)


def facts_of(session: Session, lead_id: int, emails: dict[int, InboundEmail]) -> list[tuple[ConversationFact, dict]]:
    out = []
    for f in session.execute(select(ConversationFact).where(ConversationFact.lead_id == lead_id)
                             .order_by(ConversationFact.created_at, ConversationFact.id)).scalars():
        src = fact_source(f, emails)
        if src is not None:
            out.append((f, src))
    return out


def _sentence(text: str, match: re.Match) -> str:
    start = max(text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start()), text.rfind("?", 0, match.start()),
                text.rfind("!", 0, match.start())) + 1
    ends = [i for i in (text.find(c, match.end()) for c in ".?!\n") if i >= 0]
    return " ".join(text[start:(min(ends) + 1) if ends else len(text)].split())[:300]


def asked_for_a_person(row: InboundEmail) -> str | None:
    """The prospect's own sentence asking for a person, from what they wrote above the quote, or None."""
    text = row.new_text or ""
    m = HUMAN.search(text)
    return _sentence(text, m) if m else None


def upcoming_meeting(session: Session, opp: Opportunity) -> Meeting | None:
    conds = [Meeting.opportunity_id == opp.id]
    if opp.lead_id:
        conds.append(Meeting.lead_id == opp.lead_id)
    return session.execute(select(Meeting).where(or_(*conds), Meeting.status == "scheduled", Meeting.is_simulated.is_(False))
                           .order_by(Meeting.starts_at)).scalars().first()


def latest_snapshot(session: Session, company_id: int) -> ResearchSnapshot | None:
    return session.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == company_id,
                                                          ResearchSnapshot.status.in_(("done", "no_people")))
                           .order_by(ResearchSnapshot.fetched_at.desc(), ResearchSnapshot.id.desc())).scalars().first()


def _answer_to(session: Session, row: InboundEmail) -> FollowUp | None:
    """Our sent answer to this email, if there is one."""
    if not row.message_id:
        return None
    return session.execute(select(FollowUp).where(FollowUp.in_reply_to == row.message_id, FollowUp.status == "sent",
                                                  FollowUp.channel == "email").order_by(FollowUp.id)).scalars().first()


# =============================================================================================== the conditions

def _vip(company: Company | None, vips: list[str]) -> str | None:
    if company is None or not vips:
        return None
    names = {(company.domain or "").lower(), (company.normalized_name or "").lower(), (company.name or "").strip().lower()}
    hit = next((v for v in vips if v and v in names), None)
    return hit


def escalation_from_engine(session: Session, lead_id: int) -> dict | None:
    """The conversation engine asked for a person: an open alert written by request_handover, or a handover proposal in
    the AI decision log that no person rejected and the policy did not block."""
    alert = session.execute(select(Alert).where(Alert.key == ESCALATION_KEY.format(lead_id=lead_id),
                                                Alert.acknowledged.is_(False))).scalars().first()
    if alert is not None:
        return {"why": alert.message[:200], "source": crm("alerts", alert.id, "message")}
    d = session.execute(select(AIDecision).where(AIDecision.lead_id == lead_id, AIDecision.kind == "handover",
                                                 AIDecision.verdict != "block",
                                                 or_(AIDecision.human_action.is_(None), AIDecision.human_action != "rejected"))
                        .order_by(AIDecision.id.desc())).scalars().first()
    if d is not None:
        reason = str((d.proposal or {}).get("reason") or "the AI proposed a handover")[:200]
        return {"why": reason, "source": crm("ai_decisions", d.id, "proposal")}
    return None


def evaluate(session: Session, settings, opp: Opportunity) -> list[dict]:
    """The handover conditions this deal meets now: [{"key", "why", "source"}], each cited. Empty when the rules are off,
    the deal is closed or has no lead (no thread to cite)."""
    r = rules(settings)
    if not r.get("enabled") or opp is None or opp.stage in CLOSED or not opp.lead_id:
        return []
    lead = session.get(Lead, opp.lead_id)
    if lead is None:
        return []
    emails = emails_of(session, lead.id)
    facts = facts_of(session, lead.id, emails)
    kinds = {f.kind for f, _ in facts}
    out: list[dict] = []
    if r.get("interested_requirements_complete"):
        interested = [e for e in emails.values() if e.category == "interested"]
        missing = [k for k in r.get("required_facts") or [] if k not in kinds]
        if interested and not missing:
            out.append({"key": "interested_requirements_complete",
                        "why": "interested, and stated " + (", ".join(r.get("required_facts") or []) or "what they need"),
                        "source": email_src(interested[-1])})
    if r.get("meeting_booked") and (m := upcoming_meeting(session, opp)) is not None:
        out.append({"key": "meeting_booked", "why": f"a meeting is booked for {m.starts_at:%Y-%m-%d %H:%M} UTC",
                    "source": crm("meetings", m.id, "starts_at")})
    if r.get("pricing_asked"):
        asked = [e for e in emails.values() if e.intent == "pricing_request"]
        if asked:
            out.append({"key": "pricing_asked", "why": "they asked about price: a person answers, the AI never quotes",
                        "source": email_src(asked[-1])})
        elif q := next(((f, s) for f, s in facts if f.kind == "question" and PRICE.search(f.value or "")), None):
            out.append({"key": "pricing_asked", "why": "they asked about price: a person answers, the AI never quotes",
                        "source": q[1]})
    if r.get("human_requested"):
        hit = next(((e, s) for e in reversed(list(emails.values())) if (s := asked_for_a_person(e))), None)
        if hit:
            out.append({"key": "human_requested", "why": "they asked to speak with a person",
                        "source": email_src(hit[0], hit[1])})
        elif f := next(((f, s) for f, s in facts if HUMAN.search(f.value or "") or HUMAN.search(f.evidence_quote or "")), None):
            out.append({"key": "human_requested", "why": "they asked to speak with a person", "source": f[1]})
    if r.get("min_score") and (lead.score or 0) >= int(r["min_score"]) and "budget" in kinds:
        out.append({"key": "score_with_budget", "why": f"lead score {lead.score} (at least {r['min_score']}) with a budget stated",
                    "source": crm("leads", lead.id, "score")})
    if (vip := _vip(lead.company, r.get("vip_accounts") or [])) is not None:
        out.append({"key": "vip_account", "why": f"a VIP account ({vip})", "source": crm("companies", lead.company_id, "domain")})
    if r.get("conversation_escalation") and (esc := escalation_from_engine(session, lead.id)) is not None:
        out.append({"key": "conversation_escalation", "why": f"the conversation engine asked for a person: {esc['why']}",
                    "source": esc["source"]})
    return out


# =============================================================================================== composing

def _line(text: str, source: dict) -> dict:
    return {"text": " ".join(str(text).split())[:600], "source": source}


def _quoted(f: ConversationFact, src: dict) -> str:
    quote = src.get("quote")
    return f"{f.value} (their words: “{quote}”)" if quote and _norm(quote) != _norm(f.value) else (
        f"“{quote}”" if quote else f.value)


def compose(session: Session, settings, opp: Opportunity, conditions: list[dict]) -> tuple[dict, list[str]]:
    """({key: {"title", "lines"}}, what is not known yet). Every line has a source; nothing is written without one."""
    from aime import booking
    from aime.knowledge.industries import industry_label

    lead = session.get(Lead, opp.lead_id) if opp.lead_id else None
    company = session.get(Company, opp.company_id)
    contact = session.get(Contact, lead.contact_id) if lead and lead.contact_id else None
    emails = emails_of(session, lead.id) if lead else {}
    facts = facts_of(session, lead.id, emails) if lead else []
    s: dict[str, list[dict]] = {k: [] for k in TITLES}
    unknown: list[str] = []

    s["why"] = [_line(c["why"], c["source"]) for c in conditions]

    # who the prospect is
    if company is not None:
        s["prospect"].append(_line(f"Company: {company.name}", crm("companies", company.id, "name")))
        if company.website or company.domain:
            field = "website" if company.website else "domain"
            s["prospect"].append(_line(f"Website: {company.website or company.domain}", crm("companies", company.id, field)))
        if company.industry:
            s["prospect"].append(_line(f"Industry: {industry_label(company.industry)}", crm("companies", company.id, "industry")))
        place = ", ".join(x for x in (company.city, company.state, company.country) if x)
        if place:
            s["prospect"].append(_line(f"Location: {place}", crm("companies", company.id, "country")))
        if company.employee_range:
            s["prospect"].append(_line(f"Size: {company.employee_range} employees", crm("companies", company.id, "employee_range")))
        snap = latest_snapshot(session, company.id)
        if snap is not None:
            src = {"type": "research_snapshot", "id": snap.id}
            if snap.description:
                s["prospect"].append(_line(f"In their own words: “{snap.description[:300]}”",
                                           {**src, "url": snap.description_url} if snap.description_url else src))
            if snap.services_offered:
                s["prospect"].append(_line("Services they list: " + ", ".join(str(x) for x in snap.services_offered[:8]), src))
            for need in (snap.needs or [])[:5]:
                if isinstance(need, dict) and need.get("quote"):
                    s["prospect"].append(_line(f"Possible need for {need.get('service') or 'our services'} (their site: "
                                               f"“{str(need['quote'])[:200]}”)", {**src, "url": need.get("url")}))
        else:
            unknown.append("Research: the company's website has not been read yet")
    if contact is not None:
        name = " ".join(x for x in (contact.first_name, contact.last_name) if x)
        if name or contact.title:
            s["prospect"].append(_line(f"Contact: {name or 'name not recorded'}" + (f", {contact.title}" if contact.title else ""),
                                       crm("contacts", contact.id, "title" if contact.title else "first_name")))
        if contact.email:
            s["prospect"].append(_line(f"Email: {contact.email} ({(contact.email_status or 'unchecked').replace('_', ' ')})",
                                       crm("contacts", contact.id, "email")))

    # what they said
    by_kind: dict[str, list[tuple[ConversationFact, dict]]] = {}
    for f, src in facts:
        by_kind.setdefault(f.kind, []).append((f, src))
    for f, src in by_kind.get("requirement", []):
        s["needs"].append(_line(_quoted(f, src), src))
    for kind, title in (("budget", "Budget"), ("timeline", "Timeline")):
        rows = by_kind.get(kind, [])
        for f, src in rows:
            if src.get("type") == "crm":
                s["budget_timeline"].append(_line(f"{title} (recorded by {f.confirmed_by}): {f.value}", src))
            else:
                s["budget_timeline"].append(_line(f"{title}, in their words: “{src['quote']}”", src))
        if not rows:
            unknown.append(f"{title}: not stated in their emails yet")
    for kind in ("stakeholder", "authority"):
        for f, src in by_kind.get(kind, []):
            s["stakeholders"].append(_line(_quoted(f, src), src))
    unanswered = []
    for kind in ("objection", "concern"):
        for f, src in by_kind.get(kind, []):
            s["objections"].append(_line(f"{'Objection' if kind == 'objection' else 'Concern'}: {_quoted(f, src)}", src))
            row = emails.get(f.evidence_inbound_id) if f.evidence_inbound_id else None
            answer = _answer_to(session, row) if row is not None else None
            if answer is not None:
                s["objections"].append(_line(f"Answered in our email of {answer.sent_at or answer.updated_at:%Y-%m-%d}: "
                                             f"“{answer.subject}”", crm("followups", answer.id, "sent_at")))
            elif f.status == "answered" and f.confirmed_by:
                s["objections"].append(_line(f"Marked answered by {f.confirmed_by}", crm("conversation_facts", f.id, "status")))
            else:
                unanswered.append((f, src))
    for f, src in by_kind.get("question", []):
        s["questions"].append(_line(_quoted(f, src), src))
    pricing = [e for e in emails.values() if e.intent == "pricing_request"]
    if pricing and not any(PRICE.search(f.value or "") for f, _ in by_kind.get("question", [])):
        s["questions"].append(_line("They asked about price", email_src(pricing[-1])))
    if not by_kind.get("requirement"):
        unknown.append("What they need: not stated in their emails yet")

    # the meeting, in both time zones
    m = upcoming_meeting(session, opp)
    c = booking.cfg(settings)
    if m is not None:
        theirs = m.timezone if m.source == "calendar" and m.timezone else booking.prospect_zone(lead, settings=settings)
        s["meeting"].append(_line(f"{m.title}: {booking.label(m.starts_at, theirs)} for the prospect ({theirs}); "
                                  f"{booking.label(m.starts_at, c['owner_tz'])} for you ({c['owner_tz']})",
                                  crm("meetings", m.id, "starts_at")))
        if m.inbound_email_id and m.inbound_email_id in emails:
            s["meeting"].append(_line("Booked through the notice in this email", email_src(emails[m.inbound_email_id])))
    else:
        unknown.append("Meeting: none booked yet")

    # risks
    for f, src in unanswered:
        s["risks"].append(_line(f"No answer recorded yet to: {f.value}", src))
    if company is not None and company.is_competitor and not company.competitor_cleared_by:
        s["risks"].append(_line("Marked as a possible competitor", crm("companies", company.id, "is_competitor")))
    if contact is not None:
        if contact.consent_status == "opted_out":
            s["risks"].append(_line("The contact opted out: write to them only if they wrote to you", crm("contacts", contact.id, "consent_status")))
        if contact.email_status in ("bounced", "no_mail_server", "role_address"):
            s["risks"].append(_line(f"Their address is {contact.email_status.replace('_', ' ')}", crm("contacts", contact.id, "email_status")))
    if lead is not None and lead.disqualify_reason:
        s["risks"].append(_line(f"Disqualified before: {lead.disqualify_reason}", crm("leads", lead.id, "disqualify_reason")))

    # recommended next step: each based on a record
    human = next(((e, q) for e in reversed(list(emails.values())) if (q := asked_for_a_person(e))), None)
    if m is not None:
        s["next_step"].append(_line(f"Prepare for the call on {booking.label(m.starts_at, c['owner_tz'])}: read what they need "
                                    "and their objections first", crm("meetings", m.id, "starts_at")))
    if pricing:
        s["next_step"].append(_line("Answer their price question yourself: the AI never quotes a price or terms",
                                    email_src(pricing[-1])))
    if human:
        s["next_step"].append(_line("Write to them personally: they asked for a person", email_src(human[0], human[1])))
    if not s["next_step"]:
        interested = [e for e in emails.values() if e.category == "interested"]
        if interested:
            s["next_step"].append(_line("Offer them a call", email_src(interested[-1])))
    if opp.next_step:
        s["next_step"].append(_line(f"Next step on the deal: {opp.next_step}", crm("opportunities", opp.id, "next_step")))

    # links
    if lead is not None:
        s["links"].append(_line(f"Thread and lead: /leads/{lead.id}", crm("leads", lead.id, "id")))
        for e in list(emails.values())[-3:]:
            s["links"].append(_line(f"Their email of {e.received_at:%Y-%m-%d}: /inbox/{e.id}", email_src(e)))
    if company is not None:
        s["links"].append(_line(f"Company and research: /companies/{company.id}", crm("companies", company.id, "id")))
    s["links"].append(_line(f"Deal: /opportunities/{opp.id}", crm("opportunities", opp.id, "id")))

    sections = {k: {"title": TITLES[k], "lines": [ln for ln in lines if cited(ln["source"])]} for k, lines in s.items() if lines}
    return sections, unknown


def render_text(opp_name: str, version: int, sections: dict, unknown: list[str]) -> str:
    out = [f"Handover: {opp_name} (version {version})", ""]
    for sec in sections.values():
        out.append(sec["title"])
        out += [f"- {ln['text']} [{cite_label(ln['source'])}]" for ln in sec["lines"]]
        out.append("")
    if unknown:
        out.append("Not known yet")
        out += [f"- {u}" for u in unknown]
    return "\n".join(out).strip() + "\n"


def content_hash(sections: dict, conditions: list[dict], unknown: list[str]) -> str:
    raw = json.dumps({"sections": sections, "conditions": conditions, "unknown": unknown}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# =============================================================================================== writing

def latest(session: Session, opp_id: int) -> HandoverPackage | None:
    return session.execute(select(HandoverPackage).where(HandoverPackage.opportunity_id == opp_id)
                           .order_by(HandoverPackage.version.desc())).scalars().first()


def write(session: Session, settings, opp: Opportunity, conditions: list[dict], actor: str,
          now: datetime | None = None) -> tuple[HandoverPackage, bool]:
    """The package for these conditions: (package, written). The same content as the latest version writes nothing."""
    from aime.automation import audit

    now = now or utcnow()
    sections, unknown = compose(session, settings, opp, conditions)
    digest = content_hash(sections, conditions, unknown)
    last = latest(session, opp.id)
    if last is not None and last.content_hash == digest and last.status != "superseded":
        return last, False
    version = (last.version if last else 0) + 1
    pkg = HandoverPackage(opportunity_id=opp.id, lead_id=opp.lead_id, version=version, sections={**sections, "unknown": unknown},
                          conditions=conditions, content_hash=digest, status="open", waiting_since=now, created_at=now,
                          created_by=actor[:80], owner=opp.owner,
                          summary=render_text(opp.name, version, sections, unknown))
    if last is not None:
        if last.status == "accepted":  # the manager owns it already: the update is theirs, nothing to accept again
            pkg.status, pkg.accepted_by, pkg.accepted_at, pkg.prior_state = "accepted", last.accepted_by, last.accepted_at, last.prior_state
        elif last.status == "open":  # still waiting: the SLA clock keeps running from the first ask
            pkg.waiting_since, pkg.sla_escalated_at = last.waiting_since, last.sla_escalated_at
        last.status = "superseded" if last.status in ("open", "accepted") else last.status
    session.add(pkg)
    session.flush()
    audit(session, actor, "handover_package", f"opportunity #{opp.id}",
          f"version {version}: {', '.join(c['key'] for c in conditions)}"[:300])
    return pkg, True


def on_handover(session: Session, settings, opp: Opportunity | None, actor: str, now: datetime | None = None) -> HandoverPackage | None:
    """Called by aime.handover.handover after its alert: write the package when a condition is met. It never changes what
    the caller decided, and a failure to compose it is logged, never raised: the handover itself stands."""
    if opp is None:
        return None
    try:
        conditions = evaluate(session, settings, opp)
        if not conditions:
            return None
        return write(session, settings, opp, conditions, actor, now)[0]
    except Exception:  # noqa: BLE001 - the package is an addition; the alert and the assignment are already written
        log.exception("Handover package for opportunity #%s could not be written", opp.id)
        return None


def request_handover(session: Session, lead_id: int, reason: str, actor: str = "conversation engine") -> Alert:
    """For the conversation engine (needs_handover): ask for a person on this lead. One open alert per lead; the next
    pipeline check hands the deal over with its package."""
    key = ESCALATION_KEY.format(lead_id=lead_id)
    alert = session.execute(select(Alert).where(Alert.key == key, Alert.acknowledged.is_(False))).scalars().first()
    if alert is None:
        alert = Alert(level="info", source="handover", key=key, message=f"Handover asked for lead #{lead_id}: {reason}"[:500])
        session.add(alert)
        session.flush()
    return alert


def candidates(session: Session, settings) -> list[Opportunity]:
    """The open deals the periodic check looks at: those of the leads the conversation engine works (a conversation, a fact,
    a handover proposal or request) and of the VIP accounts. Leads it does not know are left to the existing handover points."""
    r = rules(settings)
    if not r.get("enabled"):
        return []
    lead_ids = set(session.execute(select(Conversation.lead_id)).scalars())
    lead_ids |= set(session.execute(select(ConversationFact.lead_id).distinct()).scalars())
    lead_ids |= {x for x in session.execute(select(AIDecision.lead_id).where(AIDecision.kind == "handover")).scalars() if x}
    for key in session.execute(select(Alert.key).where(Alert.key.like("conversation:handover:%"),
                                                       Alert.acknowledged.is_(False))).scalars():
        tail = key.rsplit(":", 1)[-1]
        if tail.isdigit():
            lead_ids.add(int(tail))
    vips = r.get("vip_accounts") or []
    if vips:
        lead_ids |= set(session.execute(select(Lead.id).join(Company, Company.id == Lead.company_id).where(or_(
            func.lower(Company.domain).in_(vips), func.lower(Company.normalized_name).in_(vips),
            func.lower(Company.name).in_(vips)))).scalars())
    if not lead_ids:
        return []
    return list(session.execute(select(Opportunity).where(Opportunity.lead_id.in_(lead_ids), Opportunity.stage.not_in(CLOSED))
                                .order_by(Opportunity.id)).scalars())


def periodic_check(session: Session, settings, actor: str, now: datetime | None = None) -> int:
    """Hand over the deals that meet a condition and whose package is new or changed (aime.handover.handover, which writes
    the package). Held leads (opted out, disqualified, a lost deal) are not handed over. Returns how many were."""
    from aime.handover import handover
    from aime.meetings import booking_hold

    done = 0
    for opp in candidates(session, settings):
        lead = session.get(Lead, opp.lead_id)
        if lead is None or booking_hold(session, lead, lead.contact, None) is not None:
            continue
        conditions = evaluate(session, settings, opp)
        if not conditions:
            continue
        last = latest(session, opp.id)
        sections, unknown = compose(session, settings, opp, conditions)
        if last is not None and last.content_hash == content_hash(sections, conditions, unknown):
            continue
        handover(session, settings, opp, "; ".join(c["why"] for c in conditions)[:300], actor)
        done += 1
    return done


# =============================================================================================== accept and return

def ai_paused(session: Session, lead_id: int) -> bool:
    """Whether AI drafting is paused for this lead: a person accepted its handover (conversation state handed_over)."""
    state = session.execute(select(Conversation.state).where(Conversation.lead_id == lead_id)).scalar_one_or_none()
    return state == "handed_over"


def _conversation(session: Session, lead: Lead) -> Conversation:
    from aime.ai.state import state_of_lead

    conv = session.execute(select(Conversation).where(Conversation.lead_id == lead.id)).scalars().first()
    if conv is None:
        conv = Conversation(lead_id=lead.id, state=state_of_lead(lead))
        session.add(conv)
        session.flush()
    return conv


def may_act(opp: Opportunity, user: dict | None, settings) -> bool:
    """The deal's owner, or an administrator. Without sign-in (DASHBOARD_AUTH off) the one user is the owner."""
    if not settings.dashboard_auth:
        return True
    if not user:
        return False
    return user.get("username") == opp.owner or bool({"admin_ops", "configure_system"} & set(user.get("perms", ())))


def accept(session: Session, settings, pkg: HandoverPackage, actor: str, now: datetime | None = None) -> str:
    """The manager takes the deal over: AI drafting for the lead pauses (conversation state handed_over, a person's move)."""
    from aime.ai.state import check_transition
    from aime.automation import audit
    from aime.crm_ops import log_activity

    now = now or utcnow()
    last = latest(session, pkg.opportunity_id)
    if last is None or last.id != pkg.id:
        raise ValueError("a newer version of this handover package exists: open it and accept that one")
    if pkg.status == "accepted":
        return f"Already accepted by {pkg.accepted_by}"
    if pkg.status != "open":
        raise ValueError(f"this handover package is {pkg.status}")
    opp = session.get(Opportunity, pkg.opportunity_id)
    lead = session.get(Lead, opp.lead_id) if opp.lead_id else None
    if lead is not None:
        conv = _conversation(session, lead)
        ok, why = check_transition(conv.state, "handed_over", actor="person")
        if not ok:
            raise ValueError(f"the conversation cannot be handed over: {why}")
        pkg.prior_state = conv.state if conv.state != "handed_over" else pkg.prior_state
        conv.state = "handed_over"
    pkg.status, pkg.accepted_by, pkg.accepted_at = "accepted", actor[:80], now
    log_activity(session, f"Handover accepted by {actor} (package version {pkg.version}): AI drafting for this lead is paused",
                 actor, type_="handover", company_id=opp.company_id, lead_id=opp.lead_id, opportunity_id=opp.id)
    audit(session, actor, "handover_accepted", f"opportunity #{opp.id}", f"package version {pkg.version}")
    return f"Accepted: {opp.name} is yours, and AI drafting for this lead is paused"


def return_to_ai(session: Session, settings, pkg: HandoverPackage, actor: str, reason: str, now: datetime | None = None) -> str:
    """The manager gives it back, with a reason: the conversation goes back where it was, and so to the AI queue."""
    from aime.ai.state import check_transition, state_of_lead
    from aime.automation import audit
    from aime.crm_ops import log_activity

    now = now or utcnow()
    reason = " ".join((reason or "").split())
    if len(reason) < 3:
        raise ValueError("say why it goes back (a few words)")
    last = latest(session, pkg.opportunity_id)
    if last is None or last.id != pkg.id:
        raise ValueError("a newer version of this handover package exists: open it")
    if pkg.status not in ("open", "accepted"):
        raise ValueError(f"this handover package is {pkg.status}")
    opp = session.get(Opportunity, pkg.opportunity_id)
    lead = session.get(Lead, opp.lead_id) if opp.lead_id else None
    if lead is not None:
        conv = _conversation(session, lead)
        if conv.state == "handed_over":
            back = pkg.prior_state if pkg.prior_state and pkg.prior_state != "handed_over" else state_of_lead(lead)
            back = back if back != "handed_over" else "discovering"
            ok, why = check_transition(conv.state, back, actor="person")
            if not ok:
                raise ValueError(f"the conversation cannot go back: {why}")
            conv.state = back
    pkg.status, pkg.returned_by, pkg.returned_at, pkg.return_reason = "returned", actor[:80], now, reason[:300]
    log_activity(session, f"Handover returned to the AI by {actor}: {reason}"[:300], actor, type_="handover",
                 company_id=opp.company_id, lead_id=opp.lead_id, opportunity_id=opp.id)
    audit(session, actor, "handover_returned", f"opportunity #{opp.id}", f"package version {pkg.version}: {reason}"[:300])
    return "Returned: the conversation is back in the AI queue"
