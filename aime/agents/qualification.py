"""Lead Qualification Agent: evidence-based scoring and status transitions."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.agents.base import BaseAgent
from aime.crm_ops import is_suppressed, log_activity
from aime.matching import seeks_external_help
from aime.models import Activity, Company, Contact, FollowUp, InboundInquiry, Lead, Signal
from aime.outreach import set_status
from aime.pipeline import clear_stage
from aime.scoring import LeadFacts, categorize, read_company, score_lead, signal_fact
from aime.timeutil import utcnow

# Who writes the activity log besides people. People are recorded by user name, whatever the name is, so a name that is
# not here counts as a person: an agent left off this list keeps a lead qualified, it never drops one.
NOT_A_PERSON = {"marketing_manager", "research", "company_research", "linkedin_content", "publishing", "qualification",
                "sales_workflow", "inbox_monitor", "crm", "analytics", "publish_policy", "command line", "content audit",
                "owner-approved refresh", "system", ""}
# A person or the prospect ended outreach to this lead: a score does not reopen it. (Re-engagement looks at nurture.)
CLOSED_STAGES = ("not_interested", "no_response", "reengage")


class QualifyInput(BaseModel):
    lead_ids: list[int] | None = None
    rescore_all: bool = False
    stale_hours: int = Field(default=24, ge=1)


class QualifyOutput(BaseModel):
    scored: int = 0
    grade_counts: dict[str, int] = Field(default_factory=dict)
    newly_qualified: list[int] = Field(default_factory=list)
    downgraded: list[int] = Field(default_factory=list)
    disqualified: list[int] = Field(default_factory=list)


class LeadQualificationAgent(BaseAgent[QualifyInput, QualifyOutput]):
    name = "qualification"
    title = "Lead Qualification Agent"
    responsibilities = [
        "Score leads 0-100 on fit, evidenced need, recorded intent, and data quality",
        "Explain every point awarded so humans can audit the score",
        "Qualify A/B leads, nurture C/D, and disqualify non-US, competitor, or suppressed leads",
        "Never award intent without recorded evidence",
    ]
    input_model = QualifyInput
    output_model = QualifyOutput

    def execute(self, inp: QualifyInput, session: Session) -> QualifyOutput:
        now = utcnow()
        targets = list(self.settings.section("targeting").get("industries", []))
        thresholds = self.settings.section("qualification")
        stmt = select(Lead).where(Lead.status.in_(("new", "qualified", "nurture")))
        if inp.lead_ids:
            stmt = stmt.where(Lead.id.in_(inp.lead_ids))
        leads = session.execute(stmt).scalars().all()

        latest_signal = dict(session.execute(
            select(Signal.company_id, func.max(Signal.created_at)).group_by(Signal.company_id)).all())
        out = QualifyOutput()
        grades: Counter[str] = Counter()

        for lead in leads:
            stale_cutoff = now - timedelta(hours=inp.stale_hours)
            newest = latest_signal.get(lead.company_id)
            company: Company = lead.company
            checked = company.website_checked_at
            needs = (inp.rescore_all or bool(inp.lead_ids) or lead.last_scored_at is None
                     or lead.last_scored_at < stale_cutoff
                     or (newest is not None and newest > lead.last_scored_at)
                     # website verified or company researched after the last score: apply that evidence now
                     or (checked is not None and checked > lead.last_scored_at)
                     or (company.researched_at is not None and company.researched_at > lead.last_scored_at))
            if not needs:
                continue
            contact: Contact | None = lead.contact
            signals = session.execute(select(Signal).where(Signal.company_id == company.id)).scalars().all()
            inquiry = session.execute(select(InboundInquiry).where(InboundInquiry.lead_id == lead.id)
                                      .order_by(InboundInquiry.id.desc())).scalars().first()
            payload = inquiry.payload if inquiry else {}
            facts = LeadFacts(
                source=lead.source,
                target_industries=targets,
                **read_company(company, signals),
                has_website=bool(company.website or company.domain),
                employee_range=company.employee_range,
                verified=company.verified,
                is_competitor=company.is_competitor,
                suppressed=is_suppressed(session, contact.email if contact else None, company.domain),
                contact_consent=contact.consent_status if contact else None,
                signals=[signal_fact(s) for s in signals],
                explicit_services=list(payload.get("services_interested") or []),
                stated_timeline=bool(payload.get("timeline")),
                stated_budget=bool(payload.get("budget")),
                website_status=company.website_status,
                seeks_external_help=any(seeks_external_help(s.text) for s in signals if s.signal_type == "hiring"),
                contact_source=contact.source if contact else None,
                contact_role=contact.role_category if contact else None,
                contact_email_status=contact.email_status if contact else None,
                now=now,
            )
            result = score_lead(facts, thresholds)
            prev_status = lead.status
            lead.score, lead.grade = result.total, result.grade
            lead.fit_score, lead.need_score = result.fit, result.need
            lead.intent_score, lead.data_score = result.intent, result.data
            lead.score_breakdown = result.reasons
            lead.matched_services = result.matched_services
            lead.primary_service = result.matched_services[0] if result.matched_services else None
            lead.last_scored_at = now
            out.scored += 1
            grades[result.grade] += 1

            if result.disqualify_reason:
                lead.status = "disqualified"
                lead.disqualify_reason = result.disqualify_reason
                out.disqualified.append(lead.id)
                if prev_status != "disqualified":
                    # Nothing unsent stays in the approval queue for a newly disqualified lead (e.g. a competitor
                    # found by research); items a person adds later are left alone.
                    for f in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.status.in_(
                            ("pending", "awaiting_approval", "approved")))).scalars():
                        f.status, f.blocked_reason = "cancelled", f"Lead disqualified: {result.disqualify_reason}"[:300]
                    clear_stage(session, lead, self.name, "the lead was disqualified")
                    if lead.outreach_status == "contacted":
                        set_status(session, lead, "stopped", self.name, "the lead was disqualified")
            elif prev_status == "nurture" and self._sequence_parked_by_score(session, lead):
                lead.status = "qualified"  # a person emailed this lead: an aged job post does not end the sequence
            elif (result.grade in ("A", "B") and prev_status in ("new", "nurture")
                  and self._may_requalify(session, lead, company, signals, prev_status)):
                lead.status = "qualified"
                out.newly_qualified.append(lead.id)
            elif result.grade in ("C", "D") and prev_status == "new":
                lead.status = "nurture"
            elif (result.grade in ("C", "D") and prev_status == "qualified" and lead.source == "research"
                  and not self._human_touched(session, lead)):
                # The system qualified it on weaker evidence than it now has; a person never acted on it.
                lead.status = "nurture"
                out.downgraded.append(lead.id)
                for task in session.execute(select(FollowUp).where(
                        FollowUp.lead_id == lead.id, FollowUp.channel == "task", FollowUp.status == "pending")).scalars():
                    task.status, task.blocked_reason = "cancelled", "Score fell below the qualification threshold"
            lead.category = categorize(lead.source, result.grade, lead.primary_service, lead.status == "disqualified")
            if lead.status != prev_status:
                log_activity(session, f"Lead {prev_status} → {lead.status} (score {result.total}, grade {result.grade})",
                             self.name, type_="status_change", company_id=company.id, lead_id=lead.id)
        out.grade_counts = dict(grades)
        return out

    def _may_requalify(self, session: Session, lead: Lead, company: Company, signals: list[Signal],
                       prev_status: str) -> bool:
        """A lead that a person or the stale-lead rule moved to nurture is qualified again only on evidence
        recorded after that move (a new signal or a new research read); the same score is not enough."""
        if prev_status != "nurture":
            return True
        if lead.outreach_status in CLOSED_STAGES:
            return False
        last = session.execute(select(Activity).where(Activity.lead_id == lead.id, Activity.type == "status_change")
                               .order_by(Activity.created_at.desc(), Activity.id.desc())).scalars().first()
        if last is None or last.actor == self.name or not ("→ nurture" in last.subject or "moved to nurture" in last.subject):
            return True
        moved = last.created_at
        return (any(s.created_at and s.created_at > moved for s in signals)
                or (company.researched_at is not None and company.researched_at > moved))

    def _sequence_parked_by_score(self, session: Session, lead: Lead) -> bool:
        """Its sequence is running, and only this agent's own downgrade moved it to nurture (before that was prevented)."""
        if lead.outreach_status != "contacted":
            return False
        last = session.execute(select(Activity).where(Activity.lead_id == lead.id, Activity.type == "status_change")
                               .order_by(Activity.created_at.desc(), Activity.id.desc())).scalars().first()
        return last is not None and last.actor == self.name and "→ nurture" in (last.subject or "")

    @staticmethod
    def _human_touched(session: Session, lead: Lead) -> bool:
        """A person worked on this lead, or an email a person approved may have reached it."""
        if lead.outreach_status == "contacted" or session.execute(select(FollowUp.id).where(
                FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                FollowUp.status.in_(("sent", "sending", "uncertain")))).first():
            return True
        actors = session.execute(select(Activity.actor).where(Activity.lead_id == lead.id).distinct()).scalars()
        return any((actor or "") not in NOT_A_PERSON for actor in actors)
