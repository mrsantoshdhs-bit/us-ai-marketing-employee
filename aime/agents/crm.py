"""CRM Management Agent: inbound intake, deduplication, data hygiene, stale leads, lifecycle stages."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.crm_ops import (add_signal, ensure_lead, is_suppressed, log_activity, merge_companies, upsert_company,
                          upsert_contact)
from aime.dedupe import domain_from_email, domain_from_url
from aime.inbound import InboundPayload, load_inbox_files, validate_inquiry
from aime.knowledge.geo import normalize_state
from aime.knowledge.industries import INDUSTRIES
from aime.matching import classify_industry
from aime.models import Alert, Company, FollowUp, InboundInquiry, Lead, Opportunity
from aime.sources.base import CompanyCandidate
from aime.timeutil import utcnow
from aime.website_check import check_website

Operation = Literal["ingest_inbound", "verify_websites", "dedupe", "hygiene", "stale", "lifecycle"]
RECHECK_AFTER = timedelta(days=30)
US_NAMES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america", "america"}


class CRMInput(BaseModel):
    operations: list[Operation] = Field(
        default_factory=lambda: ["verify_websites", "dedupe", "hygiene", "stale", "lifecycle"])
    company_ids: list[int] | None = None  # limit website checks to these companies (e.g. new research results)


class CRMOutput(BaseModel):
    inbound_files_loaded: int = 0
    inbound_files_rejected: list[str] = Field(default_factory=list)
    inbound_processed: int = 0
    inbound_rejected: int = 0
    inbound_lead_ids: list[int] = Field(default_factory=list)
    websites_checked: int = 0
    website_results: dict[str, int] = Field(default_factory=dict)
    merged: int = 0
    hygiene: dict[str, int] = Field(default_factory=dict)
    stale_to_nurture: int = 0
    lifecycle_updates: int = 0


class CRMManagementAgent(BaseAgent[CRMInput, CRMOutput]):
    name = "crm"
    title = "CRM Management Agent"
    responsibilities = [
        "Turn inbound inquiries into companies, consented contacts, leads, and evidence",
        "Reject spam and invalid inquiries; honor the suppression list",
        "Merge duplicate companies without deleting history",
        "Report data-quality gaps, move stale leads to nurture, and keep lifecycle stages current",
    ]
    input_model = CRMInput
    output_model = CRMOutput

    def execute(self, inp: CRMInput, session: Session) -> CRMOutput:
        out = CRMOutput()
        ops = inp.operations
        if "ingest_inbound" in ops:
            out.inbound_files_loaded, batch = load_inbox_files(session, self.settings.inbox_dir)
            out.inbound_files_rejected = batch.rejected
            for problem in batch.rejected:
                session.add(Alert(level="warning", source=self.name, message=f"Inbox file moved to rejected/: {problem}"))
            self._process_inbound(session, out)
            session.commit()  # inquiries are saved before their files leave the inbox
            batch.archive()
        if "verify_websites" in ops and self.settings.network_enabled:
            self._verify_websites(session, out, inp.company_ids)
        if "dedupe" in ops:
            out.merged = self._dedupe(session)
        if "hygiene" in ops:
            out.hygiene = self._hygiene(session)
        if "stale" in ops:
            out.stale_to_nurture = self._stale(session)
        if "lifecycle" in ops:
            out.lifecycle_updates = self._lifecycle(session)
        return out

    # ------------------------------------------------------------------ inbound
    def _process_inbound(self, session: Session, out: CRMOutput) -> None:
        pending = session.execute(select(InboundInquiry).where(InboundInquiry.status == "pending")
                                  .order_by(InboundInquiry.id)).scalars().all()
        for inq in pending:
            p = InboundPayload.model_validate(inq.payload)
            reason = validate_inquiry(p)
            if not reason and p.is_sample and self.settings.is_production:
                reason = "Sample data is not accepted by the production database"
            if reason:
                inq.status, inq.error, inq.processed_at = "rejected", reason, utcnow()
                out.inbound_rejected += 1
                continue
            email = p.email.strip().lower() if p.email else None
            email_domain = domain_from_email(email)
            company_name = (p.company or email_domain or f"Individual — {p.name or email or p.phone}").strip()
            country_ok = not p.country or p.country.strip().lower() in US_NAMES
            state = normalize_state(p.state)
            industry = p.industry if p.industry in INDUSTRIES else classify_industry(
                f"{p.industry or ''} {p.company or ''} {p.message}")
            website = p.website or (f"https://{email_domain}" if email_domain else None)
            cand = CompanyCandidate(
                name=company_name, source="inbound", source_url=f"inbound:{inq.id}",
                source_record_id=f"inbound:{inq.id}", website=website,
                domain=domain_from_url(website) or email_domain, state=state, city=p.city,
                is_us=country_ok, industry=industry, description=None, signal_type="inbound",
                extra={"is_sample": p.is_sample},
            )
            company, _ = upsert_company(session, cand)
            if not country_ok:
                company.country = "ZZ"
            first, last = p.split_name()
            suppressed = is_suppressed(session, email, company.domain)
            consent = "opted_out" if suppressed else ("opt_in" if p.consent_marketing else "inquiry")
            consent_src = f"Inbound inquiry #{inq.id} via {inq.channel}" + (" with marketing opt-in" if p.consent_marketing else "")
            contact = upsert_contact(session, company, email, first, last, p.phone, p.title, "inbound",
                                     consent, consent_src, is_sample=p.is_sample)
            add_signal(session, company, "inbound", p.message, "inbound", f"inbound:{inq.id}",
                       inq.received_at, extra_services=p.services_interested)
            lead, created = ensure_lead(session, company, "inbound", contact)
            lead.last_activity_at = utcnow()
            log_activity(session, f"Inbound inquiry received via {inq.channel}", self.name, type_="inbound",
                         body=p.message[:2000], company_id=company.id, lead_id=lead.id)
            inq.status, inq.lead_id, inq.processed_at = "processed", lead.id, utcnow()
            out.inbound_processed += 1
            out.inbound_lead_ids.append(lead.id)

    # ------------------------------------------------------------------ website verification
    def _verify_websites(self, session: Session, out: CRMOutput, company_ids: list[int] | None) -> None:
        limit = int(self.settings.section("research").get("website_checks_per_run", 25))
        stale = utcnow() - RECHECK_AFTER
        stmt = select(Company).where(Company.merged_into_id.is_(None), Company.domain.is_not(None),
                                     or_(Company.website_checked_at.is_(None), Company.website_checked_at < stale))
        if company_ids:
            stmt = stmt.where(Company.id.in_(company_ids))
        todo = session.execute(stmt.order_by(Company.website_checked_at.is_not(None), Company.id).limit(limit)).scalars().all()
        session.commit()  # no write lock is held during network calls
        for company in todo:
            result = check_website(self.ctx, company.domain, company.name)
            out.websites_checked += 1
            out.website_results[result.status] = out.website_results.get(result.status, 0) + 1
            company.website_status = result.status
            company.website_checked_at = utcnow()
            company.website_title = result.title
            company.validation = {**(company.validation or {}), "website": result.note}
            if result.status == "redirected" and result.final_domain and result.final_domain != company.domain:
                owner = session.execute(select(Company).where(
                    Company.merged_into_id.is_(None), Company.domain == result.final_domain,
                    Company.id != company.id)).scalars().first()
                if owner:
                    merge_companies(session, owner, company, self.name)
                    out.merged += 1
                else:
                    log_activity(session, f"Website {company.domain} redirects to {result.final_domain}; domain updated",
                                 self.name, type_="data_quality", company_id=company.id)
                    company.domain = result.final_domain
                    company.website = f"https://{result.final_domain}"
            session.commit()
            if self.settings.polite_delay:
                time.sleep(self.settings.polite_delay)

    # ------------------------------------------------------------------ maintenance
    def _dedupe(self, session: Session) -> int:
        companies = session.execute(select(Company).where(Company.merged_into_id.is_(None))
                                    .order_by(Company.id)).scalars().all()
        groups: dict[tuple, list[Company]] = defaultdict(list)
        for c in companies:
            if c.domain:
                groups[("domain", c.domain)].append(c)
        merged = 0
        done: set[int] = set()
        for members in groups.values():
            keeper = members[0]
            for dup in members[1:]:
                if dup.id in done:
                    continue
                merge_companies(session, keeper, dup, self.name)
                done.add(dup.id)
                merged += 1
        by_name: dict[tuple, list[Company]] = defaultdict(list)
        for c in companies:
            if c.id not in done and c.merged_into_id is None and c.normalized_name:
                # without a state, only records from the same source share a name (see dedupe.find_duplicate)
                by_name[(c.normalized_name, c.state) if c.state else (c.normalized_name, "", c.source)].append(c)
        for members in by_name.values():
            keeper = members[0]
            for dup in members[1:]:
                if keeper.domain and dup.domain and keeper.domain != dup.domain:
                    continue
                merge_companies(session, keeper, dup, self.name)
                merged += 1
        return merged

    def _hygiene(self, session: Session) -> dict[str, int]:
        active = session.execute(select(Company).where(Company.merged_into_id.is_(None))).scalars().all()
        return {
            "companies": len(active),
            "missing_state": sum(1 for c in active if not c.state),
            "missing_industry": sum(1 for c in active if not c.industry),
            "missing_website": sum(1 for c in active if not (c.website or c.domain)),
            "unverified": sum(1 for c in active if not c.verified),
            "competitors_flagged": sum(1 for c in active if c.is_competitor),
        }

    def _stale(self, session: Session) -> int:
        days = int(self.settings.section("followup").get("stale_lead_days", 30))
        cutoff = utcnow() - timedelta(days=days)
        moved = 0
        for lead in session.execute(select(Lead).where(Lead.status.in_(("new", "qualified")))).scalars():
            last = lead.last_activity_at or lead.created_at
            if last and last < cutoff:
                open_items = session.execute(select(FollowUp.id).where(
                    FollowUp.lead_id == lead.id,
                    FollowUp.status.in_(("pending", "awaiting_approval", "approved", "sending", "uncertain")))).first()
                if open_items:
                    continue
                lead.status = "nurture"
                log_activity(session, f"No activity for {days}+ days: moved to nurture", self.name,
                             type_="status_change", company_id=lead.company_id, lead_id=lead.id)
                moved += 1
        return moved

    def _lifecycle(self, session: Session) -> int:
        updates = 0
        for c in session.execute(select(Company).where(Company.merged_into_id.is_(None))).scalars():
            opps = session.execute(select(Opportunity.stage).where(Opportunity.company_id == c.id)).scalars().all()
            leads = session.execute(select(Lead.status).where(Lead.company_id == c.id)).scalars().all()
            if "won" in opps:
                stage = "customer"
            elif any(s not in ("won", "lost") for s in opps):
                stage = "opportunity"
            elif "qualified" in leads:
                stage = "lead"
            elif leads and all(s == "disqualified" for s in leads):
                stage = "disqualified"
            else:
                stage = "prospect"
            if c.lifecycle_stage != stage:
                c.lifecycle_stage = stage
                updates += 1
        return updates
