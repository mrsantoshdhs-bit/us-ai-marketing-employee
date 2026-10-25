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
from aime.scoring import LeadFacts, SignalFact, categorize, score_lead
from aime.timeutil import utcnow


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
                     # website verified after the last score: apply that evidence now, not a day later
                     or (checked is not None and checked > lead.last_scored_at))
            if not needs:
                continue
            contact: Contact | None = lead.contact
            signals = session.execute(select(Signal).where(Signal.company_id == company.id)).scalars().all()
            inquiry = session.execute(select(InboundInquiry).where(InboundInquiry.lead_id == lead.id)
                                      .order_by(InboundInquiry.id.desc())).scalars().first()
            payload = inquiry.payload if inquiry else {}
            facts = LeadFacts(
                source=lead.source,
                industry=company.industry,
                target_industries=targets,
                state=company.state,
                country=company.country,
                has_website=bool(company.website or company.domain),
                employee_range=company.employee_range,
                verified=company.verified,
                is_competitor=company.is_competitor,
                suppressed=is_suppressed(session, contact.email if contact else None, company.domain),
                contact_consent=contact.consent_status if contact else None,
                signals=[SignalFact(s.signal_type, list(s.matched_services or []), s.strength, s.observed_at,
                                    bool(s.source_url)) for s in signals],
                explicit_services=list(payload.get("services_interested") or []),
                stated_timeline=bool(payload.get("timeline")),
                stated_budget=bool(payload.get("budget")),
                website_status=company.website_status,
                seeks_external_help=any(seeks_external_help(s.text) for s in signals if s.signal_type == "hiring"),
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
            elif result.grade in ("A", "B") and prev_status in ("new", "nurture"):
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

    @staticmethod
    def _human_touched(session: Session, lead: Lead) -> bool:
        return session.execute(select(Activity.id).where(
            Activity.lead_id == lead.id, Activity.actor == "human")).first() is not None
