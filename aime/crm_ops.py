"""CRM record operations shared by the agents (upserts, signals, activities, suppression, merges)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.orm import Session

from aime.dedupe import _MULTI_PART_TLDS, HOSTED_PLATFORMS, clean_company_name, domain_from_email, find_duplicate, normalize_name, registrable_domain
from aime.matching import competitor_evidence, competitor_in_post, detect_competitor, match_services
from aime.models import Activity, Alert, AuditEvent, Company, Contact, FollowUp, InboundEmail, Lead, Opportunity, ResearchSnapshot, Signal, Suppression
from aime.sources.base import CompanyCandidate
from aime.stages import CLOSED
from aime.timeutil import utcnow

OPEN_LEAD_STATUSES = ("new", "qualified", "nurture")


def primary_lead(session: Session, company_id: int) -> Lead | None:
    """The lead a company's work belongs to: an open one (research leads first, then the newest), else the
    newest that was not merged away."""
    leads = session.execute(select(Lead).where(Lead.company_id == company_id).order_by(Lead.id.desc())).scalars().all()
    open_leads = sorted((x for x in leads if x.status in OPEN_LEAD_STATUSES), key=lambda x: (x.source != "research", -x.id))
    return (open_leads or [x for x in leads if x.status != "merged"] or leads or [None])[0]


def lead_that_went_on(session: Session, lead: Lead) -> Lead:
    """Where a merged lead went: the lead its merge record names, whatever became of that lead since, as another may be the
    company's primary by now. One set to merged by hand names none, so the company's primary lead. Any other lead is its own."""
    if lead.status != "merged":
        return lead
    named = re.fullmatch(r"Merged into lead #(\d+)", lead.disqualify_reason or "")
    return (session.get(Lead, int(named[1])) if named else None) or primary_lead(session, lead.company_id)


def lead_with_the_deals(session: Session, lead: Lead) -> Lead:
    """The lead whose deals a reply or a booking on this lead acts on. Replies still find a merged lead, by its thread and its
    contact, while its deal may have moved on (_merge_lead). A deal still on it is its own, and the lead that went on may hold
    someone else's: so its own while it has any, else along the merges."""
    seen = set()
    while lead.status == "merged" and lead.id not in seen and not session.execute(select(Opportunity.id).where(Opportunity.lead_id == lead.id)).first():
        seen.add(lead.id)
        lead = lead_that_went_on(session, lead)
    return lead


MOVED_BY_MERGE = "Deal moved by a merge from lead #"  # _merge_lead's line on the deal: whose it was


def came_with_the_merges(session: Session, lead: Lead, opp: Opportunity) -> bool:
    """The deal was moved where it is by merges, from this lead or one it went on to: it is this lead's person's own. One merged by an older
    build has no such line, and a person decides about it."""
    chain, seen = {lead.id}, set()
    while lead.status == "merged" and lead.id not in seen:
        seen.add(lead.id)
        lead = lead_that_went_on(session, lead)
        chain.add(lead.id)
    moved = session.execute(select(Activity.subject).where(Activity.opportunity_id == opp.id, Activity.subject.startswith(MOVED_BY_MERGE))).scalars()
    return any((m := re.match(r"\d+", s[len(MOVED_BY_MERGE):])) and int(m[0]) in chain for s in moved)


def leads_replying_into(session: Session, opp: Opportunity) -> list[int]:
    """lead_with_the_deals seen from a deal: its own lead and the merged leads whose replies reach it."""
    merged = session.execute(select(Lead).where(Lead.company_id == opp.company_id, Lead.status == "merged", Lead.id != opp.lead_id)).scalars().all()
    return [opp.lead_id, *(x.id for x in merged if lead_with_the_deals(session, x).id == opp.lead_id)]


def fingerprint(*parts: str | None) -> str:
    raw = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


SOURCE_LABELS = {"hn_hiring": "Hacker News hiring post", "sec_edgar": "SEC filing business address",
                 "csv_import": "your imported list", "inbound": "the inquiry form", "manual": "entered by hand"}


def us_location_evidence(source: str | None, state: str | None, location_text: str | None) -> str | None:
    """Why the company counts as a US company, in words: the US state, or the location text that names the US."""
    label = SOURCE_LABELS.get(source or "", source or "source")
    if state:
        return f"US state {state} ({label})"[:300]
    if location_text:
        return f'{label}: "{location_text.strip()[:200]}"'[:300]
    return None


def upsert_company(session: Session, cand: CompanyCandidate) -> tuple[Company, bool]:
    existing = find_duplicate(session, cand.name, cand.domain, cand.state, cand.source_record_id, cand.source)
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
        if not existing.location_evidence:
            existing.location_evidence = us_location_evidence(existing.source, existing.state, existing.location_text)
        # a company on file is judged by CRMManagementAgent._flag_competitors, which has the guards and logs what it read
        return existing, False
    company = Company(
        name=clean_company_name(cand.name)[:200],
        normalized_name=normalize_name(clean_company_name(cand.name)),
        domain=cand.domain,
        website=cand.website,
        industry=cand.industry,
        state=cand.state,
        city=cand.city,
        country="US",
        location_text=cand.location_text,
        location_evidence=us_location_evidence(cand.source, cand.state, cand.location_text),
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


BARE_SUFFIXES = _MULTI_PART_TLDS | HOSTED_PLATFORMS  # not one company's domain, so a walk up a host never stops on one
BOUNCED_REASON = "Bounced"  # how the reason of an entry that records a dead address starts
ERASURE_REASON = "Erased on the person's request"


def canonical_address(address: str) -> str:
    """Lower case, with the +tag of the local part removed: pat+news@x.example and pat@x.example reach one person."""
    local, at, host = address.strip().lower().partition("@")
    plus = local.find("+")
    return f"{local[:plus] if plus > 0 else local}{at}{host}"


def canonical_sql(column):
    """canonical_address in SQL, for a stored value."""
    low = func.lower(column)
    plus, at = func.instr(low, "+"), func.instr(low, "@")
    return case((and_(plus > 1, plus < at), func.substr(low, 1, plus - 1).op("||")(func.substr(low, at))), else_=low)


def _host_and_parents(host: str) -> list[str]:
    """The host and each parent of it, up to its registrable domain: never a bare suffix such as co.uk."""
    host = host.strip(" .").lower()
    site, found = registrable_domain(host), [host]
    while host != site:
        host = host.partition(".")[2]
        if "." not in host or host in BARE_SUFFIXES:
            break
        found.append(host)
    return found


def address_ref(address: str) -> str:
    """How the audit log names a person's address: it stays traceable and leaves nothing to erase."""
    return f"address #{hashlib.sha256(address.strip().lower().encode()).hexdigest()[:12]}"


def _covering_values(email: str | None = None, domain: str | None = None) -> list[str]:
    """The do-not-contact values, canonical, that speak for this address or domain."""
    values = []
    if email:
        values.append(canonical_address(email))
        if "@" in email:
            host = email.rsplit("@", 1)[-1]
            values += [host.lower(), *_host_and_parents(host)]  # the spelling as written too: an entry may keep a stray dot
    if domain:
        values += [domain.lower(), *_host_and_parents(domain)]
    return values


def _entries_covering(email: str | None = None, domain: str | None = None):
    """The do-not-contact entries that speak for this address or domain, lifted ones included."""
    values = _covering_values(email, domain)
    return select(Suppression).where(canonical_sql(Suppression.value).in_(values)) if values else None


def is_suppressed(session: Session, email: str | None = None, domain: str | None = None) -> bool:
    entries = _entries_covering(email, domain)
    if entries is None:
        return False
    return session.execute(entries.where(Suppression.lifted_at.is_(None))).first() is not None


def lift_time(session: Session, email: str | None = None, domain: str | None = None) -> datetime | None:
    """When the owner lifted what covered this address: the newest lift. None when nothing ever covered it or an entry
    still stands, so what was said before a lift stops counting only for an address the owner really took off the list."""
    entries = _entries_covering(email, domain)
    rows = session.execute(entries).scalars().all() if entries is not None else []
    if not rows or any(r.lifted_at is None for r in rows):
        return None
    return max(r.lifted_at for r in rows)


def _under_a_listed_domain(host, table):
    """SQL for: a domain on the list is a parent of this host. Bare suffixes are left out, as in _host_and_parents."""
    value = Suppression.value
    return exists(select(Suppression.id).where(
        Suppression.lifted_at.is_(None), Suppression.kind == "domain", value.contains("."), value.not_in(BARE_SUFFIXES),
        func.substr(host, -(func.length(value) + 1)) == literal(".").op("||")(value))).correlate(table)


_ON_THE_LIST = select(Suppression.value).where(Suppression.lifted_at.is_(None)).correlate(None)
_CANONICAL_ON_THE_LIST = select(canonical_sql(Suppression.value)).where(Suppression.lifted_at.is_(None)).correlate(None)
_CONTACT_HOST = func.lower(func.substr(Contact.email, func.instr(Contact.email, "@") + 1))
_COMPANY_DOMAIN = func.lower(Company.domain)
# is_suppressed for a query over contacts: the address with or without a +tag, its domain or a parent of it, or its company's domain is on the do-not-contact list.
# suppress() marks the contacts on file at that moment; one found later, or at another domain than its company's, is caught here.
CONTACT_NOT_SUPPRESSED = and_(
    canonical_sql(Contact.email).not_in(_CANONICAL_ON_THE_LIST),
    _CONTACT_HOST.not_in(_ON_THE_LIST),
    ~_under_a_listed_domain(_CONTACT_HOST, Contact),
    or_(Contact.company_id.is_(None),
        Contact.company_id.not_in(select(Company.id).where(or_(
            _COMPANY_DOMAIN.in_(_ON_THE_LIST), _under_a_listed_domain(_COMPANY_DOMAIN, Company))).correlate(None))))


def _contacts_under(session: Session, value: str) -> list[Contact]:
    """The contacts a new entry speaks for, by the rules of is_suppressed."""
    if "@" in value:
        return session.execute(select(Contact).where(canonical_sql(Contact.email) == canonical_address(value))).scalars().all()
    pattern = value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")  # the value, not a pattern
    at_it = [Contact.email == value, Contact.email.like(f"%@{pattern}", escape="\\")]
    if "." in value and value not in BARE_SUFFIXES:
        at_it.append(Contact.email.like(f"%@%.{pattern}", escape="\\"))
    return session.execute(select(Contact).where(or_(*at_it))).scalars().all()


def suppress(session: Session, value: str, reason: str, bounce: bool = False) -> None:
    """Put an address or a domain on the do-not-contact list. `bounce`: the address is dead and nobody said no, so consent is
    left as it is and only what is queued for this very address stops, not everything on its contact. The entry and
    email_status "bounced" block the address at every gate all the same."""
    from aime.automation import audit

    value = value.strip().lower()
    if not value:
        return
    row = session.execute(select(Suppression).where(Suppression.value == value)).scalars().first()
    if row is None:
        session.add(Suppression(value=value, kind="email" if "@" in value else "domain", reason=reason))
    elif row.lifted_at is not None:  # on the list again: one row per value, and the lift it replaces stays in the audit log
        audit(session, "system", "suppression_relisted", f"suppression #{row.id}",
              f"lifted {row.lifted_at:%Y-%m-%d %H:%M} by {row.lifted_by}: {row.lift_reason}; now: {reason}")
        row.lifted_at, row.lifted_by, row.lift_reason = None, None, None
        row.reason, row.created_at = reason, utcnow()  # emails sent while it was lifted were allowed
    elif not bounce and row.reason.startswith(BOUNCED_REASON):
        row.reason = reason  # a real no after a bounce: an opt-out from here on, and the contact says so below
    else:
        if reason == ERASURE_REASON:
            row.reason = reason  # never lifted: see lift_suppression
        return
    if bounce:
        contacts = session.execute(select(Contact).where(func.lower(Contact.email) == value)).scalars().all()
        for c in contacts:
            c.email_status = "bounced"
        # A row with no to_email goes to its contact, so it is queued for this very address; a colleague's row names its own.
        for f in session.execute(select(FollowUp).where(
                or_(func.lower(FollowUp.to_email) == value, and_(FollowUp.to_email.is_(None), FollowUp.contact_id.in_([c.id for c in contacts]))),
                FollowUp.channel == "email", FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
            f.status, f.blocked_reason = "cancelled", "The address bounced"
        return
    # Stop anything queued for that address/domain.
    for c in _contacts_under(session, value):
        if c.consent_status != "opted_out":
            c.consent_status, c.consent_at = "opted_out", utcnow()  # when: emails sent before it were allowed
        for f in session.execute(select(FollowUp).where(
                FollowUp.contact_id == c.id, FollowUp.status.in_(("pending", "awaiting_approval", "approved")))).scalars():
            f.status = "cancelled"
            f.blocked_reason = "Contact unsubscribed / suppressed"


def _records_an_erasure(session: Session, row: Suppression) -> bool:
    """erase_person's reason on the entry, or, for an address that was on the list before it, its audit line."""
    if row.reason == ERASURE_REASON:
        return True
    target = address_ref(row.value)
    return session.execute(select(AuditEvent.id).where(AuditEvent.action == "person_erased", AuditEvent.target == target)).first() is not None


def lift_suppression(session: Session, suppression_or_value: Suppression | str, actor: str, reason: str) -> str | None:
    """The owner takes an entry off the do-not-contact list. Returns why not, with nothing changed, or None. The row stays, with
    who, when and why, so a build that does not read those keeps blocking. Contacts at this address, with or without a +tag as suppress()
    marks them, go back to neutral once no spelling of it is listed; a domain's contacts stay as they are, since any of them may have said no themselves. It drafts nothing, sends nothing and
    reopens nothing: the lead stays closed until a person reopens it (outreach.reopen_stopped). Who may do this is the route's job."""
    from aime.automation import audit, return_to_person

    row = suppression_or_value
    if not isinstance(row, Suppression):
        row = session.execute(select(Suppression).where(Suppression.value == (row or "").strip().lower())).scalars().first()
    reason = (reason or "").strip()
    if row is None:
        return "that is not on the do-not-contact list"
    if not reason:
        return "give the reason"
    if _records_an_erasure(session, row):
        return "this entry records that a person's data was erased, and that is never lifted"
    if row.lifted_at is not None:
        return "this entry was lifted already"
    row.lifted_at, row.lifted_by, row.lift_reason = utcnow(), actor[:80], reason[:300]
    person = canonical_address(row.value)
    contacts = session.execute(select(Contact).where(canonical_sql(Contact.email) == person)).scalars().all()
    if session.execute(select(Suppression.id).where(canonical_sql(Suppression.value) == person, Suppression.id != row.id, Suppression.lifted_at.is_(None))).first():
        contacts = []  # another spelling of this address is still on the list, and its own lift clears them
    for c in contacts:
        if c.consent_status == "opted_out":
            c.consent_status, c.consent_at = "none", None
        if c.email_status == "bounced":
            c.email_status = "unchecked"  # the mail-server check reads it again before any gate counts it as usable
    audit(session, actor, "suppression_lifted", f"suppression #{row.id}", f"{row.kind}; it was: {row.reason}; why: {reason}")
    # suppress() cancels by the contact's own address, so an email approved for another host of a listed company only waited behind the entry. That
    # approval came before the no: a person looks again. It is not sent and not deleted. A reply is read by its address alone, as at the gate.
    listed = canonical_address(row.value)
    for f in session.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "approved")).scalars().all():
        recipient = f.to_email or (f.contact.email if f.contact else None)
        if listed in _covering_values(recipient, f.lead.company.domain if f.kind == "outreach" and f.lead else None):
            return_to_person(f, "it was approved before a do-not-contact entry that covered it, and that entry was lifted")
            log_activity(session, f"Follow-up #{f.id} went back to a person: it was approved before a do-not-contact entry that was lifted", actor,
                         type_="followup", company_id=f.company_id, lead_id=f.lead_id)
    for company_id in {c.company_id for c in contacts} or {None}:
        log_activity(session, f"Do-not-contact entry lifted by {actor}: {row.value}. Nothing goes out by itself: a closed lead stays closed, and a lead never closed may get a new draft on the next run, which a person still approves",
                     actor, body=reason, company_id=company_id)
    return None


ERASED = "[erased on request]"


WROTE_AGAIN = "They wrote again after this draft: read it on Replies"


def later_reply_waiting(session: Session, f: FollowUp) -> InboundEmail | None:
    """A reply from the person this answer goes to, written after the email it answers, that no person has read yet. The answer was written
    without it: it is not approved or sent until someone has."""
    if f.kind != "response" or not f.to_email or not f.lead_id:
        return None
    answered = session.execute(select(InboundEmail.id).where(or_(InboundEmail.suggested_followup_id == f.id, InboundEmail.message_id == f.in_reply_to))
                               .order_by(InboundEmail.id)).scalars().first()
    if answered is None:
        return None
    return session.execute(select(InboundEmail).where(
        InboundEmail.lead_id == f.lead_id, InboundEmail.id > answered, InboundEmail.status == "needs_review",
        func.lower(InboundEmail.from_email) == f.to_email.lower()).order_by(InboundEmail.id.desc())).scalars().first()


def erase_person(session: Session, email: str, actor: str) -> dict[str, int]:
    """A person asked us to delete their data (LinkedIn's API terms for lead form data; privacy laws): their contact
    details, the inquiries and emails they sent us, our emails to them and the activity lines naming them are erased.
    The address stays only on the suppression list, so nothing is ever sent to it again. Company records stay."""
    from aime.automation import audit
    from aime.models import InboundInquiry, SkippedMail
    from aime.people import name_hash

    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("Give the person's email address")
    suppress(session, email, "Erased on the person's request")
    counts = {"contacts": 0, "inquiries": 0, "emails_received": 0, "emails_to_them": 0, "activity_lines": 0}
    names: set[str] = set()

    def erase_email(f: FollowUp) -> None:
        if f.status in ("pending", "awaiting_approval", "approved"):
            f.status, f.blocked_reason = "cancelled", "Contact erased on request"
        f.subject, f.body, f.to_email, f.evidence = ERASED, ERASED, None, None
        counts["emails_to_them"] += 1

    def scrub(text: str | None) -> str | None:
        return re.sub(re.escape(email), "[erased]", text, flags=re.I) if text else text

    for c in session.execute(select(Contact).where(Contact.email == email)).scalars().all():
        names |= {n for n in (c.first_name, c.last_name) if n and len(n) > 2}
        if c.first_name or c.last_name:  # the name goes, and the company's pages still print it: the next re-read must know
            c.erased_name_hash = name_hash(f"{c.first_name or ''} {c.last_name or ''}")
        for f in session.execute(select(FollowUp).where(FollowUp.contact_id == c.id)).scalars():
            erase_email(f)
        c.first_name = c.last_name = c.phone = c.title = c.evidence = c.evidence_url = None
        c.email, c.email_source_url, c.email_evidence = None, None, None
        c.consent_status, c.consent_source = "opted_out", ERASED
        counts["contacts"] += 1
    for f in session.execute(select(FollowUp).where(func.lower(FollowUp.to_email) == email)).scalars():  # a reply draft may link no contact
        erase_email(f)
    for inq in session.execute(select(InboundInquiry)).scalars():
        if str((inq.payload or {}).get("email") or "").strip().lower() == email:
            inq.payload = {"erased": True}
            counts["inquiries"] += 1
    for e in session.execute(select(InboundEmail).where(InboundEmail.from_email == email)).scalars():
        e.from_email, e.subject, e.body_text, e.new_text, e.reply_to = ERASED, ERASED, ERASED, ERASED, None
        counts["emails_received"] += 1
    for e in session.execute(select(InboundEmail).where(InboundEmail.reply_to == email)).scalars():
        e.reply_to = None
    for skipped in session.execute(select(SkippedMail).where(SkippedMail.from_email == email)).scalars().all():
        session.delete(skipped)  # the short record of mail read as unrelated: sender, subject and date, nothing else
        counts["emails_received"] += 1
    for a in session.execute(select(Activity).where(or_(Activity.subject.contains(email),
                                                        Activity.body.contains(email)))).scalars():
        a.subject, a.body = scrub(a.subject), scrub(a.body)
        for text in names:
            a.subject = a.subject.replace(text, "[erased]")
            a.body = a.body.replace(text, "[erased]") if a.body else a.body
        counts["activity_lines"] += 1
    # What a person typed about them, and older audit lines: the entry itself stays, only their address leaves the words.
    for entry in session.execute(select(Suppression).where(or_(Suppression.lift_reason.contains(email), Suppression.reason.contains(email)))).scalars():
        entry.lift_reason, entry.reason = scrub(entry.lift_reason), scrub(entry.reason)
    for event in session.execute(select(AuditEvent).where(or_(AuditEvent.detail.contains(email), AuditEvent.target.contains(email)))).scalars():
        event.detail, event.target = scrub(event.detail), scrub(event.target)
    audit(session, actor, "person_erased", address_ref(email),
          ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts


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


def _was_worked(session: Session, lead: Lead) -> bool:
    """Mail went to this lead's contact, or may have, or its sequence is running."""
    from aime.outreach import RUNNING_STAGES

    return lead.outreach_status in RUNNING_STAGES or session.execute(select(FollowUp.id).where(
        FollowUp.lead_id == lead.id, FollowUp.channel == "email", FollowUp.status.in_(("sent", "sending", "uncertain")))).first() is not None


def _reads_as_a_seller(session: Session, company: Company) -> bool:
    """What the next competitors run would read on this company and flag it for (CRMManagementAgent._flag_competitors)."""
    from aime.agents.crm import _own_words

    if company.is_competitor or company.competitor_cleared_by:
        return False
    snap = session.execute(select(ResearchSnapshot).where(ResearchSnapshot.company_id == company.id).order_by(ResearchSnapshot.id.desc())).scalars().first()
    if snap and snap.description:
        return bool(competitor_evidence(_own_words(snap, company)))
    posts = session.execute(select(Signal.text).where(Signal.company_id == company.id, Signal.signal_type == "hiring")).scalars()
    return any(map(competitor_in_post, posts)) or bool(competitor_evidence(company.website_title))


def _untouched(session: Session, lead: Lead) -> bool:
    return lead.outreach_status is None and not session.execute(select(FollowUp.id).where(
        FollowUp.lead_id == lead.id, FollowUp.channel == "email", FollowUp.status != "cancelled")).first() and not session.execute(
        select(Opportunity.id).where(Opportunity.lead_id == lead.id)).first()


def _merge_lead(session: Session, gone: Lead, survivor: Lead) -> None:
    gone.status = "merged"
    gone.disqualify_reason = f"Merged into lead #{survivor.id}"
    # The deal moves so that the open lead's page shows it and no second one opens; a reply on this lead finds it by lead_with_the_deals.
    # Not where the survivor has a deal of its own it would be taken for: each stays with the person it is about, as a merge always left it.
    deals = session.execute(select(Opportunity).where(Opportunity.lead_id == gone.id)).scalars().all()
    theirs = session.execute(select(Opportunity.stage).where(Opportunity.lead_id == survivor.id)).scalars().all()
    if not theirs or (set(theirs) <= set(CLOSED) and any(x.stage not in CLOSED for x in deals)):
        for opp in deals:
            opp.lead_id = survivor.id
            log_activity(session, f"{MOVED_BY_MERGE}{gone.id} to lead #{survivor.id}", "company merge", type_="merge",
                         company_id=opp.company_id, lead_id=survivor.id, opportunity_id=opp.id)


def _carry_competitor_word(session: Session, keeper: Company, dup: Company, keeper_domain: str | None, actor: str) -> None:
    """A person's "not a competitor" and an open flag outlive a merge, where that is safe. The clear moves only onto the same site,
    unjudged: a keeper's own site is not what the person read. The flag moves unless a lead at the keeper is past its first email,
    which the next rescore would end, and only a person ends a sequence. Its log line does not read "Marked as a possible competitor"
    and its actor is no agent's name, so _review_competitor_flags leaves it for a person even where the keeper has an old mark of its
    own. Whatever does not move is told to a person."""
    def tell(message: str) -> None:
        session.add(Alert(level="warning", source=actor[:60], message=f"{keeper.name} (#{keeper.id}), after merging duplicate #{dup.id}: {message}"[:500]))

    if dup.competitor_cleared_by:
        if not keeper.is_competitor and not keeper.competitor_cleared_by and (not keeper_domain or keeper_domain == dup.domain):
            keeper.competitor_cleared_by = dup.competitor_cleared_by
            log_activity(session, f"Not a competitor, as {dup.competitor_cleared_by} said of merged duplicate #{dup.id}", actor,
                         type_="data_quality", company_id=keeper.id)
        else:
            tell(f"{dup.competitor_cleared_by} cleared the duplicate as not a competitor. That does not carry over to this company: "
                 "please look at it and clear it here if it applies.")
    elif dup.is_competitor and keeper.competitor_cleared_by:
        tell(f"the duplicate is flagged as a possible competitor, and {keeper.competitor_cleared_by} cleared this company. The clear stands: please check it.")
    elif dup.is_competitor and not keeper.is_competitor:
        leads = session.execute(select(Lead).where(Lead.company_id == keeper.id, Lead.status.in_(OPEN_LEAD_STATUSES))).scalars().all()
        if any(x.outreach_status not in (None, "drafted", "approved") for x in leads):
            tell("the duplicate is flagged as a possible competitor. It is not flagged here, because a lead here is already in its "
                 "sequence and a flag would end it: please decide.")
            return
        keeper.is_competitor = True
        why = session.execute(select(Activity.subject).where(Activity.company_id == dup.id, Activity.subject.startswith("Marked as a possible competitor"))
                              .order_by(Activity.id.desc())).scalars().first()
        log_activity(session, f"Possible-competitor flag carried over from merged duplicate #{dup.id}" + (f": {why}" if why else ""),
                     "company merge", type_="data_quality", company_id=keeper.id)


def _first_emails_back_to_a_person(session: Session, keeper: Company, dup: Company, keeper_leads: list[Lead], dup_leads: list[Lead], actor: str) -> None:
    """Two records become one company and one of them was already emailed: an open first email on the other would be a second first email
    to that company, even at another address. It goes back to a person, with the reason, and is not deleted. Not after a dead address, as nobody got that email."""
    from aime.automation import return_to_person

    for still_open, other in ((keeper_leads, dup_leads), (dup_leads, keeper_leads)):
        emailed = next((x for x in other if x.outreach_status != "bounced" and _was_worked(session, x)), None)
        if emailed is None:
            continue
        for f in session.execute(select(FollowUp).where(FollowUp.lead_id.in_([x.id for x in still_open]), FollowUp.channel == "email", FollowUp.kind == "outreach",
                                                        FollowUp.step == 1, FollowUp.status.in_(("awaiting_approval", "approved")))).scalars().all():
            return_to_person(f, f"this company was merged with a record already emailed on lead {emailed.id}")
            session.add(Alert(level="warning", source=actor[:60], message=(
                f"{keeper.name} (#{keeper.id}), after merging duplicate #{dup.id}: first email #{f.id} on lead {f.lead_id} went back to a person, "
                f"because lead {emailed.id} of this company was already emailed. Please decide.")[:500]))


def merge_companies(session: Session, keeper: Company, dup: Company, actor: str) -> None:
    """Move everything from dup into keeper and mark dup as merged (no hard deletes)."""
    keeper_domain = keeper.domain
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
    dup_leads = session.execute(select(Lead).where(Lead.company_id == dup.id)).scalars().all()
    keeper_leads = session.execute(select(Lead).where(Lead.company_id == keeper.id)).scalars().all()
    _first_emails_back_to_a_person(session, keeper, dup, keeper_leads, dup_leads, actor)
    if keeper_research is not None and not dup.is_competitor and _untouched(session, keeper_research) and not _reads_as_a_seller(session, keeper):
        # The lead someone was already emailed on goes on: the untouched one would write them a second first email. Not a lead
        # whose address is dead, as nobody got that email and the keeper's contact may be alive, and not over a keeper the next
        # competitors run would flag: on an emailed lead it leaves the flag off, and the sequence would go on past that check.
        worked = next((x for x in dup_leads if x.source == "research" and x.status in OPEN_LEAD_STATUSES and x.outreach_status != "bounced"
                       and _was_worked(session, x)), None)
        if worked is not None:
            if worked.outreach_status in ("not_interested", "no_response", "unsubscribed", "stopped"):
                session.add(Alert(level="warning", source=actor[:60], message=(
                    f"{keeper.name} (#{keeper.id}), after merging duplicate #{dup.id}: lead #{keeper_research.id} was closed in favour of lead #{worked.id}, which was "
                    f"already emailed and whose stage is {worked.outreach_status.replace('_', ' ')}. So nothing goes to the contact of the closed lead: please decide.")[:500]))
            _merge_lead(session, keeper_research, worked)
            for task in session.execute(select(FollowUp).where(FollowUp.lead_id == keeper_research.id, FollowUp.channel == "task",
                                                               FollowUp.status == "pending", FollowUp.subject.startswith("Review "))).scalars():
                task.status, task.blocked_reason = "cancelled", f"Merged into lead #{worked.id}"
            keeper_research = worked
    for lead in dup_leads:
        if lead is not keeper_research and lead.source == "research" and keeper_research is not None and lead.status in OPEN_LEAD_STATUSES:
            _merge_lead(session, lead, keeper_research)
        lead.company_id = keeper.id
    for opp in session.execute(select(Opportunity).where(Opportunity.company_id == dup.id)).scalars():
        opp.company_id = keeper.id
    for mail in session.execute(select(InboundEmail).where(InboundEmail.company_id == dup.id)).scalars():
        mail.company_id = keeper.id  # what they wrote, an opt-out above all, stays with the company
    for attr in ("website", "domain", "state", "city", "industry", "employee_range", "description", "phone"):
        if not getattr(keeper, attr) and getattr(dup, attr):
            setattr(keeper, attr, getattr(dup, attr))
    if dup.name_set_by and not keeper.name_set_by:  # a person's correction outlives the merge
        keeper.name, keeper.name_set_by = dup.name, dup.name_set_by
    _carry_competitor_word(session, keeper, dup, keeper_domain, actor)
    keeper.verified = keeper.verified or dup.verified
    if not keeper.source_record_id and dup.source_record_id:
        keeper.source_record_id = dup.source_record_id  # e.g. the SEC CIK, so officers are still looked up
    dup.merged_into_id = keeper.id
    dup.lifecycle_stage = "merged"
    log_activity(session, f"Merged duplicate company #{dup.id} ({dup.name}) into #{keeper.id}", actor,
                 type_="merge", company_id=keeper.id)


def contact_email_domain(contact: Contact | None) -> str | None:
    return domain_from_email(contact.email) if contact and contact.email else None
