"""CRM record operations shared by the agents (upserts, signals, activities, suppression, merges)."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aime.dedupe import domain_from_email, find_duplicate, normalize_name
from aime.matching import detect_competitor, match_services
from aime.models import Activity, Company, Contact, FollowUp, Lead, Opportunity, Signal, Suppression
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow

OPEN_LEAD_STATUSES = ("new", "qualified", "nurture")


def fingerprint(*parts: str | None) -> str:
    raw = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def upsert_company(session: Session, cand: CompanyCandidate) -> tuple[Company, bool]:
    existing = find_duplicate(session, cand.name, cand.domain, cand.state, cand.source_record_id)
    if existing:
        # Fill gaps only; never overwrite verified data with weaker data.
        for attr in ("website", "domain", "state", "city", "industry", "employee_range", "location_text"):
            if getattr(existing, attr) in (None, "") and getattr(cand, attr, None):
                setattr(existing, attr, getattr(cand, attr))
        if cand.verified and not existing.verified:
            existing.verified = True
            if cand.phone:
                existing.phone = cand.phone
        if cand.description and not existing.description:
            existing.description = cand.description
        if detect_competitor(cand.signal_text or cand.description):
            existing.is_competitor = True
        return existing, False
    company = Company(
        name=cand.name.strip()[:200],
        normalized_name=normalize_name(cand.name),
        domain=cand.domain,
        website=cand.website,
        industry=cand.industry,
        state=cand.state,
        city=cand.city,
        country="US",
        location_text=cand.location_text,
        employee_range=cand.employee_range,
        description=cand.description,
        phone=cand.phone if cand.verified else None,
        source=cand.source,
        source_url=cand.source_url,
        source_record_id=cand.source_record_id,
        verified=cand.verified,
        is_competitor=detect_competitor(cand.signal_text or cand.description),
        is_sample=bool(cand.extra.get("is_sample")),
    )
    session.add(company)
    session.flush()
    return company, True


def add_signal(session: Session, company: Company, signal_type: str, text: str | None, source: str,
               source_url: str | None, observed_at: datetime | None = None,
               extra_services: list[str] | None = None) -> Signal | None:
    if not text and not extra_services:
        return None
    fp = fingerprint(source_url, (text or "")[:300])
    exists = session.execute(
        select(Signal.id).where(Signal.company_id == company.id, Signal.fingerprint == fp)
    ).first()
    if exists:
        return None
    matches = match_services(text)
    services = [m.service_id for m in matches]
    keywords = sorted({k for m in matches for k in m.keywords})
    strength = max((m.strength for m in matches), default=0.0)
    if extra_services:  # services the person explicitly asked about rank first
        services = list(dict.fromkeys(list(extra_services) + services))
        strength = 1.0
    sig = Signal(
        company_id=company.id, signal_type=signal_type, text=(text or "")[:2000], source=source,
        source_url=source_url, fingerprint=fp, observed_at=observed_at or utcnow(),
        matched_services=services, keywords=keywords, strength=strength,
    )
    session.add(sig)
    session.flush()
    return sig


def ensure_lead(session: Session, company: Company, source: str, contact: Contact | None = None,
                campaign_id: int | None = None) -> tuple[Lead, bool]:
    stmt = select(Lead).where(Lead.company_id == company.id, Lead.status.in_(OPEN_LEAD_STATUSES))
    if contact is not None:
        stmt = stmt.where(Lead.contact_id == contact.id)
    else:
        stmt = stmt.where(Lead.source == source)
    lead = session.execute(stmt).scalars().first()
    if lead:
        return lead, False
    lead = Lead(company_id=company.id, contact_id=contact.id if contact else None, source=source,
                status="new", campaign_id=campaign_id, is_sample=company.is_sample,
                last_activity_at=utcnow())
    session.add(lead)
    session.flush()
    return lead, True


def log_activity(session: Session, subject: str, actor: str, type_: str = "system", body: str | None = None,
                 company_id: int | None = None, lead_id: int | None = None, opportunity_id: int | None = None) -> Activity:
    act = Activity(company_id=company_id, lead_id=lead_id, opportunity_id=opportunity_id, type=type_,
                   subject=subject[:300], body=body, actor=actor)
    session.add(act)
    return act


def is_suppressed(session: Session, email: str | None = None, domain: str | None = None) -> bool:
    values = []
    if email:
        values.append(email.lower())
        dom = email.rsplit("@", 1)[-1].lower() if "@" in email else None
        if dom:
            values.append(dom)
    if domain:
        values.append(domain.lower())
    if not values:
        return False
    return session.execute(select(Suppression.id).where(Suppression.value.in_(values))).first() is not None


def suppress(session: Session, value: str, reason: str) -> None:
    value = value.strip().lower()
    if not value:
        return
    if session.execute(select(Suppression.id).where(Suppression.value == value)).first():
        return
    session.add(Suppression(value=value, kind="email" if "@" in value else "domain", reason=reason))
    # Stop anything queued for that address/domain.
    contacts = session.execute(select(Contact).where(
        or_(Contact.email == value, Contact.email.like(f"%@{value}")))).scalars().all()
    for c in contacts:
        c.consent_status = "opted_out"
        for f in session.execute(select(FollowUp).where(
                FollowUp.contact_id == c.id, FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
            f.status = "cancelled"
            f.blocked_reason = "Contact unsubscribed / suppressed"


def upsert_contact(session: Session, company: Company | None, email: str | None, first_name: str | None,
                   last_name: str | None, phone: str | None, title: str | None, source: str,
                   consent_status: str, consent_source: str, is_sample: bool = False) -> Contact:
    contact = None
    if email:
        contact = session.execute(select(Contact).where(Contact.email == email.lower())).scalars().first()
    if contact is None:
        contact = Contact(email=email.lower() if email else None, source=source, is_sample=is_sample)
        session.add(contact)
    contact.company_id = contact.company_id or (company.id if company else None)
    contact.first_name = contact.first_name or first_name
    contact.last_name = contact.last_name or last_name
    contact.phone = contact.phone or phone
    contact.title = contact.title or title
    rank = {"opted_out": 3, "opt_in": 2, "inquiry": 1, "none": 0}
    if contact.consent_status != "opted_out" and rank.get(consent_status, 0) >= rank.get(contact.consent_status, 0):
        contact.consent_status = consent_status
        contact.consent_source = consent_source
        contact.consent_at = utcnow()
    session.flush()
    return contact


def merge_companies(session: Session, keeper: Company, dup: Company, actor: str) -> None:
    """Move everything from dup into keeper and mark dup as merged (no hard deletes)."""
    for sig in list(dup.signals):
        clash = session.execute(select(Signal.id).where(
            Signal.company_id == keeper.id, Signal.fingerprint == sig.fingerprint)).first()
        if clash:
            session.delete(sig)
        else:
            sig.company_id = keeper.id
    for contact in session.execute(select(Contact).where(Contact.company_id == dup.id)).scalars():
        contact.company_id = keeper.id
    keeper_research = session.execute(select(Lead).where(
        Lead.company_id == keeper.id, Lead.source == "research", Lead.status.in_(OPEN_LEAD_STATUSES))).scalars().first()
    for lead in session.execute(select(Lead).where(Lead.company_id == dup.id)).scalars():
        if lead.source == "research" and keeper_research is not None and lead.status in OPEN_LEAD_STATUSES:
            lead.status = "merged"
            lead.disqualify_reason = f"Merged into lead #{keeper_research.id}"
        lead.company_id = keeper.id
    for opp in session.execute(select(Opportunity).where(Opportunity.company_id == dup.id)).scalars():
        opp.company_id = keeper.id
    for attr in ("website", "domain", "state", "city", "industry", "employee_range", "description", "phone"):
        if not getattr(keeper, attr) and getattr(dup, attr):
            setattr(keeper, attr, getattr(dup, attr))
    keeper.verified = keeper.verified or dup.verified
    dup.merged_into_id = keeper.id
    dup.lifecycle_stage = "merged"
    log_activity(session, f"Merged duplicate company #{dup.id} ({dup.name}) into #{keeper.id}", actor,
                 type_="merge", company_id=keeper.id)


def contact_email_domain(contact: Contact | None) -> str | None:
    return domain_from_email(contact.email) if contact and contact.email else None
