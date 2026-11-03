"""Company Research Agent: reads each company's own website and SEC insider filings, records a research
snapshot, and creates evidence-backed decision-maker contacts. It never contacts anyone.

Order: companies with qualified leads first, then by lead score. Only websites already verified as the
company's own are read; SEC-registered companies are also checked for officers. Each company is
re-researched after `recheck_days`. One company's failure is recorded and the batch continues; a failed read is
tried again at the next run, after the companies not read yet (see `_note_failed_read`).

With `[enrichment] work_queue` on (the default) the companies waiting beyond this run's `companies_per_run` are kept
in the durable work queue (aime.work_queue, kind "company_research"), ranked exactly as the scan ranked them: the
next run takes the next ones from there instead of scanning every company again, and the queue is ranked afresh
from a new scan once it runs dry or is older than `queue_rescan_hours` (20). A daily run therefore researches the
same companies, in the same order and number, as without the queue; more runs a day take the next chunk each.
`work_queue = false` turns it off (each run scans, as before).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aime.agents.base import AgentError, BaseAgent, RetryPolicy
from aime.agents.contracts import BlockedItem
from aime.company_research import (WITHDRAWN, build_snapshot, cik_of, has_real_text, is_plausible_website, save_contact, was_erased,
                                   withdraw_missing)
from aime.competitor_guard import leave_to_a_person, live_lead_on_company
from aime.contact_sources.companies_house import company_number_of
from aime.contact_sources.registry import registry_officers, registry_usable
from aime.contact_validation import STATUS_OK, MailDomainChecker, address_was_erased, assign_published_emails, recheck_emails
from aime.crm_ops import OPEN_LEAD_STATUSES, is_suppressed, log_activity, primary_lead
from aime.matching import competitor_evidence
from aime.models import Company, Contact, FollowUp, Lead, ResearchSnapshot
from aime.monitoring import alert_once, resolve_alerts
from aime.outreach import REACHED_PROSPECT, company_opted_out
from aime.people import extract_people, merge_findings
from aime.provenance import backfill_legacy
from aime.redact import redact
from aime.site_reader import SiteReader
from aime.sources.sec_officers import sec_officers
from aime import work_queue
from aime.db import kv_get, kv_set
from aime.timeutil import utcnow
from aime.website_check import known_sites_still_prove, page_names_company

BLOCKED_STATUSES = ("robots_disallowed", "robots_unavailable", "http_401", "http_403", "http_429", "http_503")
CONFIDENCE_RANK = {"high": 2, "medium": 1}
FAILED_READS_BEFORE_WAITING = 3  # after this many in a row a company waits for the normal re-check
ALL_FAILED_MEANS_FAILURE = 3  # this many failures and no success in a run: no longer about one site (the network?)
HOLD_ALERT = "research:held"
LEGACY_BACKFILL_PER_RUN = 500  # contacts given their "legacy: <source>" provenance per run, oldest first
HELD_READS_STOP_RUN = 3  # this many held and nothing read: the rest of the run would only read the same sign-in page
PERSON_FOUND_TASK = "Decision-maker found after the first email: "  # how the task starts, so later re-reads find it
PERSON_FOUND_NOTE = "News of a person, not this lead's review"  # with a reason, ticking it closes no review (SalesWorkflowAgent._ensure_task)
QUEUE_KIND = "company_research"
QUEUE_SCANNED = "work_queue.company_research.scanned_at"  # when the queue was last ranked from a full scan
QUEUE_RESCAN_HOURS = 20  # a daily run always finds the ranking older than this, and so scans as before
FAILED_LAST = 10 ** 9  # a failed read's place: after every company not read yet, as _candidates ranks it


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
    registry_officers: int = 0  # from an official company register (aime.contact_sources), when one is switched on
    person_emails: int = 0
    inboxes: int = 0
    contacts_withdrawn: int = 0
    emails_rechecked: int = 0
    competitors: list[int] = Field(default_factory=list)
    no_people: list[int] = Field(default_factory=list)
    held_back: list[int] = Field(default_factory=list)  # read, but a sign-in page may have answered: nothing was written
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
        backfill_legacy(session, LEGACY_BACKFILL_PER_RUN)  # contacts from before provenance existed: "legacy: <source>"
        self._registers = registry_usable(self.ctx, session)
        limit = inp.limit or int(cfg.get("companies_per_run", 15))
        recheck_days = int(cfg.get("recheck_days", 30))
        self._known_sites_failed = False
        queued = bool(cfg.get("work_queue", True)) and not inp.company_ids
        worker = work_queue.worker_id()
        if queued:
            batch, items = self._queued_batch(session, inp, now, recheck_days, limit, worker,
                                              float(cfg.get("queue_rescan_hours", QUEUE_RESCAN_HOURS)))
            session.commit()  # the claims stand even if this run dies: their leases end and they are claimed again
        else:
            batch, items = self._candidates(session, inp, now, recheck_days)[:limit], {}
        done: set[int] = set()
        for company in batch:
            if len(out.held_back) >= HELD_READS_STOP_RUN and not out.companies_researched:
                break
            done.add(company.id)
            item = items.get(company.id)
            try:
                held = len(out.held_back)
                self._research(session, company, out, int(cfg.get("max_pages_per_site", 6)))
                if item is not None:
                    if len(out.held_back) > held:  # nothing was learned: it keeps its place for the next run
                        work_queue.release(session, [item], worker)
                    else:
                        work_queue.complete(session, item, worker, {"status": session.get(Company, company.id).research_status})
            except Exception as exc:  # one site must not stop the batch
                session.rollback()
                company = session.get(Company, company.id)
                reason = redact(f"{type(exc).__name__}: {exc}")[:300]
                session.add(build_snapshot(company, [], [], "failed", error=reason))
                self._note_failed_read(session, company, out, reason)
                if item is not None:  # tried again after the companies not read yet, as without the queue
                    work_queue.fail(session, item, worker, reason, base=timedelta(0), priority=FAILED_LAST)
            session.commit()
        work_queue.release(session, [i for cid, i in items.items() if cid not in done], worker)  # the run stopped early
        out.emails_rechecked = self._recheck_unknown_emails(session, int(cfg.get("email_rechecks_per_run", 20)))
        if out.held_back and not out.companies_researched:
            alert_once(session, "warning", self.name, f"Company research is waiting: {len(out.held_back)} website(s) answered with a page that does not "
                       "show the company, and sites confirmed before could not be confirmed either, so the internet connection may be behind a "
                       "sign-in page. Nothing changes for those companies or their leads; they are read again on a coming run.",
                       self.settings, key=HOLD_ALERT)
        elif not out.held_back:
            resolve_alerts(session, (HOLD_ALERT,), set())
        session.commit()
        if len(out.failed) >= ALL_FAILED_MEANS_FAILURE and not out.companies_researched:
            raise AgentError(f"None of the {len(out.failed)} companies could be researched; first: {out.failed[0].reason}")
        return out

    def _due(self, company: Company | None, now: datetime, recheck_days: int) -> bool:
        """Still what _candidates would pick: a queued company may have been merged, marked or researched since."""
        if company is None or company.merged_into_id is not None or company.is_competitor:
            return False
        if company.researched_at is not None and company.researched_at >= now - timedelta(days=recheck_days):
            return False
        return bool(is_plausible_website(company) or cik_of(company) or (getattr(self, "_registers", False) and company_number_of(company)))

    def _queued_batch(self, session: Session, inp: ResearchInput, now: datetime, recheck_days: int, limit: int, worker: str,
                      rescan_hours: float) -> tuple[list[Company], dict[int, int]]:
        """This run's companies from the work queue: the next ones in the ranking of the last scan, still due. The queue is
        ranked afresh from a scan when it runs dry or its ranking is older than `rescan_hours`; the companies beyond this
        run's share wait there for the next run. Returns the companies and each one's queue item."""
        from aime.models import WorkItem

        scanned = kv_get(session, QUEUE_SCANNED)
        try:
            at = datetime.fromisoformat(scanned)
            stale = now - at >= timedelta(hours=rescan_hours) or at > now
        except (TypeError, ValueError):
            stale = True
        batch: list[Company] = []
        items: dict[int, int] = {}

        def take(claimed: list) -> None:
            for item in claimed:
                company = session.get(Company, int(item.key)) if item.key.isdigit() else None
                if company is not None and company.id not in items and self._due(company, now, recheck_days):
                    batch.append(company)
                    items[company.id] = item.id
                else:
                    work_queue.complete(session, item.id, worker, {"skipped": "no longer due"})

        if work_queue.busy(session, QUEUE_KIND, worker, now):
            return [], {}  # another run is working through the queue: nothing is read twice at once
        if not stale:
            take(work_queue.claim(session, QUEUE_KIND, limit, worker, now=now))
        if len(batch) < limit:  # dry or stale: scan once, rank every waiting company again, take what is missing
            ranked = self._candidates(session, inp, now, recheck_days)
            mine = set(items)
            waiting = {int(k) for k in session.execute(select(WorkItem.key).where(
                WorkItem.kind == QUEUE_KIND, WorkItem.status == "pending")).scalars() if k.isdigit()}
            for rank, company in enumerate(ranked):
                if company.id not in mine:
                    work_queue.enqueue(session, QUEUE_KIND, str(company.id), {"rank": rank}, priority=rank, now=now, reprioritize=True)
            for cid in waiting - {c.id for c in ranked}:  # no longer due: they leave the queue
                session.execute(update(WorkItem).where(WorkItem.kind == QUEUE_KIND, WorkItem.key == str(cid), WorkItem.status == "pending")
                                .values(status="done", result={"skipped": "no longer due"}, finished_at=now))
            kv_set(session, QUEUE_SCANNED, now.isoformat())
            session.flush()
            while len(batch) < limit:
                claimed = work_queue.claim(session, QUEUE_KIND, limit - len(batch), worker, now=now)
                if not claimed:
                    break
                take(claimed)
        return batch, items

    def _note_failed_read(self, session: Session, company: Company, out: ResearchOutput, reason: str) -> None:
        """Nothing was read, so this is no research: researched_at stays as it was and the next run tries again, or
        one passing 500 would hide the company for `recheck_days`. A site that keeps failing then waits for the normal
        re-check, so it cannot take a place in every batch."""
        company.research_status = "failed"
        out.failed.append(BlockedItem(id=company.id, reason=reason))
        session.flush()
        latest = session.execute(select(ResearchSnapshot.status, ResearchSnapshot.error).where(ResearchSnapshot.company_id == company.id)
                                 .order_by(ResearchSnapshot.id.desc()).limit(FAILED_READS_BEFORE_WAITING)).all()
        if len(latest) == FAILED_READS_BEFORE_WAITING and all(st == "failed" or error for st, error in latest):
            company.researched_at = utcnow()

    def _recheck_unknown_emails(self, session: Session, limit: int) -> int:
        """Published emails whose mail-server check failed (DNS timeout) are checked again, a few companies a run."""
        ids = session.execute(select(Contact.company_id).where(Contact.email_status == "unchecked",
                                                               Contact.company_id.is_not(None))
                              .distinct().limit(max(0, limit))).scalars().all()
        for cid in ids:
            recheck_emails(session, session.get(Company, cid), self.mail_checker, only=("unchecked",))
        return len(ids)

    def _candidates(self, session: Session, inp: ResearchInput, now: datetime, recheck_days: int) -> list[Company]:
        stmt = select(Company).where(Company.merged_into_id.is_(None), Company.is_competitor.is_(False))
        if inp.company_ids:
            stmt = stmt.where(Company.id.in_(inp.company_ids))
        else:
            stmt = stmt.where((Company.researched_at.is_(None)) | (Company.researched_at < now - timedelta(days=recheck_days)))
        registers = getattr(self, "_registers", False)
        companies = [c for c in session.execute(stmt).scalars()
                     if is_plausible_website(c) or cik_of(c) or (registers and company_number_of(c))]
        leads = {lead.company_id: lead for lead in session.execute(select(Lead)).scalars()}

        def priority(c: Company) -> tuple:
            lead = leads.get(c.id)
            return (c.research_status == "failed", 0 if lead and lead.status == "qualified" else 1,
                    -(lead.score if lead else 0), c.id)
        return sorted(companies, key=priority)

    def _sign_in_page_may_have_answered(self, session: Session, company: Company, pages: list) -> bool:
        """The website check's reading of an outage: over plain HTTP, where a sign-in page can answer for any address, a page that does not
        name the company proves nothing, and if sites known to be good do not prove good either, nothing was learned about this company.
        HTTPS at the company's own domain, and a real error, are answers. No other site to ask: it counts as not known to be its site."""
        home = pages[0]
        if not home.url.startswith("http://") or home.status not in ("ok", "offsite_redirect"):
            return False
        if home.ok and page_names_company(company.name, home.title, home.meta.get("og:site_name")):
            return False
        if not self._known_sites_failed:
            self._known_sites_failed = not known_sites_still_prove(self.ctx, session, [company.id])
        return self._known_sites_failed

    def _tell_of_person_found_later(self, session: Session, lead: Lead | None, company: Company, found: list[Contact | None]) -> None:
        """A named decision-maker who is news since the first email went to another address gets a task for a person, once for the lead: first
        seen since, or now with an address of their own (someone known with no address could not have been written to). The sequence stays with
        the address it went to (_contact_stays) and writing to someone new is a person's decision. No contact changes and no email."""
        if lead is None or lead.status not in OPEN_LEAD_STATUSES or lead.outreach_status == "unsubscribed" or company_opted_out(session, company):
            return
        sent = session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email", FollowUp.status.in_(("sent", "sending", "uncertain")),
                                                      REACHED_PROSPECT).order_by(FollowUp.id)).scalars().all()
        if not sent or session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task",
                                                                 FollowUp.subject.startswith(PERSON_FOUND_TASK, autoescape=True)).limit(1)).first():
            return
        first_at = sent[0].sent_at or sent[0].created_at
        written_to = {(f.to_email or "").lower() for f in sent} - {""}
        written_ids = {f.contact_id for f in sent}

        def found_later(c: Contact) -> bool:
            return (bool(c.last_name) and c.role_category != "inbox" and c.id not in written_ids and (c.email or "").lower() not in written_to
                    and c.consent_status != "opted_out" and not is_suppressed(session, c.email) and (bool(c.email) or c.created_at >= first_at))
        person = next((c for c in found if c and found_later(c)), None)
        if person is None:
            return
        # The name stays on the contact, which a request to erase them reaches through contact_id.
        session.add(FollowUp(lead_id=lead.id, contact_id=person.id, company_id=company.id, channel="task", step=1, due_at=utcnow(), status="pending",
                             blocked_reason=PERSON_FOUND_NOTE, subject=f"{PERSON_FOUND_TASK}{company.name}"[:300],
                             body=("Re-reading this company's site or filings named a decision-maker after our first email went to a different address. The "
                                   "sequence stays with that address and nothing goes out by itself: whether to write to the new person is a person's decision, "
                                   "on the lead's page.")))
        log_activity(session, "Task created: a decision-maker found after the first email", self.name, type_="task", company_id=company.id, lead_id=lead.id)

    def _research(self, session: Session, company: Company, out: ResearchOutput, max_pages: int) -> None:
        pages = []
        status = "done"
        if is_plausible_website(company):
            reader = SiteReader(self.ctx, company.domain, max_pages=max_pages, delay=self.settings.polite_delay)
            pages = reader.read()
            if self._sign_in_page_may_have_answered(session, company, pages):  # no snapshot, no withdrawal, no flag, not researched: read again soon
                out.held_back.append(company.id)
                return
            out.pages_read += sum(1 for p in pages if p.ok)
            if not pages[0].ok:
                status = ("blocked" if pages[0].status in BLOCKED_STATUSES
                          else "redirected" if pages[0].status == "offsite_redirect" else "failed")
        people = extract_people(pages, company.name) if pages else []
        cik = cik_of(company)
        sec_failure = None
        if cik:
            try:
                officers = sec_officers(self.ctx, cik)
            except httpx.HTTPError as exc:
                if not pages or status != "done":
                    raise
                # the site was read and what it says stays; only the filings are tried again
                sec_failure = redact(f"SEC filings could not be read: {type(exc).__name__}: {exc}")[:300]
                officers = []
            out.sec_officers += len(officers)
            people = merge_findings(people + officers)
            if status != "done" and officers:
                status = "done"  # the website could not be read, but the filings name the officers
        elif not pages:
            status = "no_website"
        registered, register_failure = registry_officers(self.ctx, session, company) if getattr(self, "_registers", False) else ([], None)
        if registered:
            out.registry_officers += len(registered)
            people = merge_findings(people + registered)
            if status != "done":
                status = "done"  # the website could not be read, but the official register names the officers
        if register_failure:
            log_activity(session, f"Company register not read: {register_failure}", self.name, type_="research", company_id=company.id)
        people = [f for f in people if not was_erased(session, company, f)]  # or the snapshot would keep the name, and a Review task show it
        if status == "done" and not people:
            status = "no_people"
            if not sec_failure:
                out.no_people.append(company.id)
        lead = primary_lead(session, company.id)
        service = lead.primary_service if lead else None
        contacts = []
        for finding in people:
            contact, created = save_contact(session, company, finding, service)
            contacts.append(contact)
            out.contacts_created += int(created)
        out.people_found += len(people)
        site_read = bool(pages) and pages[0].ok and any(map(has_real_text, pages))
        # A filtering proxy can answer over HTTPS too: only a page that names the company says its people have gone.
        if site_read and page_names_company(company.name, pages[0].title, pages[0].meta.get("og:site_name")):
            for c in withdraw_missing(session, company, people, pages):
                out.contacts_withdrawn += 1
                log_activity(session, f"Decision-maker withdrawn: {c.display_name} ({c.title}) is no longer found on "
                             f"{company.domain}", self.name, type_="research", company_id=company.id,
                             lead_id=lead.id if lead else None)
        snapshot = build_snapshot(company, pages, people, status, error=sec_failure)
        snapshot.emails = [e for e in snapshot.emails if not address_was_erased(session, e["email"])]  # and the name printed by it
        session.add(snapshot)
        titles = " ".join(p.title for p in pages if p.ok and p.kind == "home")  # the page title is its own words too
        phrase = competitor_evidence(f"{snapshot.description or ''} {' '.join(snapshot.services_offered or [])} {titles}")
        if phrase and not company.is_competitor and not company.competitor_cleared_by:
            live = live_lead_on_company(session, company.id)
            if live:
                leave_to_a_person(session, company, live, phrase, self.name, self.settings)
            else:
                company.is_competitor = True
                out.competitors.append(company.id)
                log_activity(session, f'Marked as a possible competitor: its website says "{phrase}"', self.name,
                             type_="research", body=snapshot.description, company_id=company.id,
                             lead_id=lead.id if lead else None)
        emails = assign_published_emails(session, company, snapshot.emails, self.mail_checker)
        out.person_emails += emails["person_emails"]
        out.inboxes += emails["inboxes"]
        if not company.phone and snapshot.phones:
            company.phone = snapshot.phones[0]["phone"][:40]  # as published on the company's own site
        researched = session.execute(select(Contact).where(Contact.company_id == company.id, Contact.source.in_(
            ("website", "sec_filing", "companies_house")))).scalars().all()
        if lead and not self._contact_stays(session, lead):
            if researched and (lead.contact_id is None or (lead.contact and lead.contact.source in ("website", "sec_filing", "companies_house",
                                                                                                    WITHDRAWN))):
                lead.contact_id = best_contact(researched).id
            elif lead.contact and lead.contact.source == WITHDRAWN:
                lead.contact_id = None
        self._tell_of_person_found_later(session, lead, company, contacts)
        if status == "failed" or sec_failure:
            self._note_failed_read(session, company, out, sec_failure or f"Website could not be read ({pages[0].status})")
        else:
            company.research_status, company.researched_at = status, utcnow()
            out.companies_researched += 1
        read = sum(1 for p in pages if p.ok)
        log_activity(session, f"Company research: {read} page(s) read, {len(people)} decision-maker(s) named"
                     + (f" ({status})" if status != "done" else ""), self.name, type_="research",
                     body=snapshot.summary, company_id=company.id, lead_id=lead.id if lead else None)

    @staticmethod
    def _contact_stays(session: Session, lead: Lead) -> bool:
        """The lead keeps its contact, whoever a re-read finds. A closed lead, or one whose contact said no, keeps the record of who
        it was about. Once an email reached the prospect the sequence stays with the address it went to: "I wanted to follow up on
        my note" means nothing to someone else, and a do-not-contact entry on that address would stop covering the lead. A
        rehearsal in test mode reached nobody. An open lead with no contact can always be given one."""
        if lead.status not in OPEN_LEAD_STATUSES:
            return True
        if lead.contact_id is None:
            return False
        return (lead.outreach_status in ("unsubscribed", "bounced") or (lead.contact is not None and lead.contact.consent_status == "opted_out")
                or session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                             FollowUp.status.in_(("sent", "sending", "uncertain")), REACHED_PROSPECT).limit(1)).first() is not None)


def best_contact(contacts: list[Contact]) -> Contact | None:
    """The lead's primary contact: a named person with a usable published email, then the company inbox,
    then the named person with the best evidence and role fit."""
    def rank(c: Contact) -> tuple:
        reachable = c.email_status in STATUS_OK
        tier = 2 if reachable and c.role_category != "inbox" else 1 if reachable else 0
        return tier, CONFIDENCE_RANK.get(c.confidence or "", 0), c.relevance or 0, -c.id
    return max(contacts, key=rank, default=None)
