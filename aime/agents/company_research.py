"""Company Research Agent: reads each company's own website and SEC insider filings, records a research
snapshot, and creates evidence-backed decision-maker contacts. It never contacts anyone.

Order: companies with qualified leads first, then by lead score. Only websites already verified as the
company's own are read; SEC-registered companies are also checked for officers. Each company is
re-researched after `recheck_days`. One company's failure is recorded and the batch continues.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent, RetryPolicy
from aime.agents.contracts import BlockedItem
from aime.company_research import build_snapshot, cik_of, is_plausible_website, save_contact
from aime.contact_validation import STATUS_OK, MailDomainChecker, assign_published_emails
from aime.crm_ops import log_activity
from aime.models import Company, Contact, Lead
from aime.people import extract_people, merge_findings
from aime.redact import redact
from aime.site_reader import SiteReader
from aime.sources.sec_officers import sec_officers
from aime.timeutil import utcnow

BLOCKED_STATUSES = ("robots_disallowed", "http_401", "http_403", "http_429", "http_503")
CONFIDENCE_RANK = {"high": 2, "medium": 1}


class ResearchInput(BaseModel):
    company_ids: list[int] | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    now: datetime | None = None


class ResearchOutput(BaseModel):
    companies_researched: int = 0
    pages_read: int = 0
    people_found: int = 0
    contacts_created: int = 0
    sec_officers: int = 0
    person_emails: int = 0
    inboxes: int = 0
    no_people: list[int] = Field(default_factory=list)
    failed: list[BlockedItem] = Field(default_factory=list)
    skipped: str | None = None


class CompanyResearchAgent(BaseAgent[ResearchInput, ResearchOutput]):
    name = "company_research"
    title = "Company Research Agent"
    responsibilities = [
        "Read each company's own website politely (robots.txt, a few pages, size cap)",
        "Record what the company says it does, the services it lists, and related topics, with quotes",
        "Identify decision-makers only where the company or its SEC filings name them, with evidence",
        "Attach only emails the company publishes on its own domain; check the domain accepts mail",
        "Never guess names, titles or emails, and never contact anyone",
    ]
    input_model = ResearchInput
    output_model = ResearchOutput
    retry_policy = RetryPolicy(max_attempts=1)  # per-company failures are recorded; the batch continues

    def __init__(self, ctx, mail_checker=None):
        super().__init__(ctx)
        self.mail_checker = mail_checker or MailDomainChecker()

    def execute(self, inp: ResearchInput, session: Session) -> ResearchOutput:
        out = ResearchOutput()
        if not self.settings.network_enabled:
            out.skipped = "network access is disabled (NETWORK_ENABLED=false)"
            return out
        cfg = self.settings.section("enrichment")
        now = inp.now or utcnow()
        limit = inp.limit or int(cfg.get("companies_per_run", 15))
        for company in self._candidates(session, inp, now, int(cfg.get("recheck_days", 30)))[:limit]:
            try:
                self._research(session, company, out, int(cfg.get("max_pages_per_site", 6)))
            except Exception as exc:  # one site must not stop the batch
                session.rollback()
                company = session.get(Company, company.id)
                reason = redact(f"{type(exc).__name__}: {exc}")[:300]
                company.research_status, company.researched_at = "failed", utcnow()
                session.add(build_snapshot(company, [], [], "failed", error=reason))
                out.failed.append(BlockedItem(id=company.id, reason=reason))
            session.commit()
        return out

    def _candidates(self, session: Session, inp: ResearchInput, now: datetime, recheck_days: int) -> list[Company]:
        stmt = select(Company).where(Company.merged_into_id.is_(None), Company.is_competitor.is_(False))
        if inp.company_ids:
            stmt = stmt.where(Company.id.in_(inp.company_ids))
        else:
            stmt = stmt.where((Company.researched_at.is_(None)) | (Company.researched_at < now - timedelta(days=recheck_days)))
        companies = [c for c in session.execute(stmt).scalars() if is_plausible_website(c) or cik_of(c)]
        leads = {lead.company_id: lead for lead in session.execute(select(Lead)).scalars()}

        def priority(c: Company) -> tuple:
            lead = leads.get(c.id)
            return (0 if lead and lead.status == "qualified" else 1, -(lead.score if lead else 0), c.id)
        return sorted(companies, key=priority)

    def _research(self, session: Session, company: Company, out: ResearchOutput, max_pages: int) -> None:
        pages = []
        status = "done"
        if is_plausible_website(company):
            reader = SiteReader(self.ctx, company.domain, max_pages=max_pages, delay=self.settings.polite_delay)
            pages = reader.read()
            out.pages_read += sum(1 for p in pages if p.ok)
            if not pages[0].ok:
                status = "blocked" if pages[0].status in BLOCKED_STATUSES else "failed"
        people = extract_people(pages, company.name) if pages else []
        cik = cik_of(company)
        if cik:
            officers = sec_officers(self.ctx, cik)
            out.sec_officers += len(officers)
            people = merge_findings(people + officers)
            if status != "done" and officers:
                status = "done"  # the website could not be read, but the filings name the officers
        elif not pages:
            status = "no_website"
        if status == "done" and not people:
            status = "no_people"
            out.no_people.append(company.id)
        lead = session.execute(select(Lead).where(Lead.company_id == company.id).order_by(Lead.id)).scalars().first()
        service = lead.primary_service if lead else None
        contacts = []
        for finding in people:
            contact, created = save_contact(session, company, finding, service)
            contacts.append(contact)
            out.contacts_created += int(created)
        out.people_found += len(people)
        snapshot = build_snapshot(company, pages, people, status)
        session.add(snapshot)
        emails = assign_published_emails(session, company, snapshot.emails, self.mail_checker)
        out.person_emails += emails["person_emails"]
        out.inboxes += emails["inboxes"]
        if not company.phone and snapshot.phones:
            company.phone = snapshot.phones[0]["phone"][:40]  # as published on the company's own site
        researched = session.execute(select(Contact).where(Contact.company_id == company.id, Contact.source.in_(
            ("website", "sec_filing")))).scalars().all()
        if lead and researched and (lead.contact_id is None or (lead.contact and lead.contact.source in ("website", "sec_filing"))):
            lead.contact_id = best_contact(researched).id
        company.research_status, company.researched_at = status, utcnow()
        out.companies_researched += 1
        read = sum(1 for p in pages if p.ok)
        log_activity(session, f"Company research: {read} page(s) read, {len(people)} decision-maker(s) named"
                     + (f" ({status})" if status != "done" else ""), self.name, type_="research",
                     body=snapshot.summary, company_id=company.id, lead_id=lead.id if lead else None)


def best_contact(contacts: list[Contact]) -> Contact | None:
    """The lead's primary contact: a named person with a usable published email, then the company inbox,
    then the named person with the best evidence and role fit."""
    def rank(c: Contact) -> tuple:
        reachable = c.email_status in STATUS_OK
        tier = 2 if reachable and c.role_category != "inbox" else 1 if reachable else 0
        return tier, CONFIDENCE_RANK.get(c.confidence or "", 0), c.relevance or 0, -c.id
    return max(contacts, key=rank, default=None)
