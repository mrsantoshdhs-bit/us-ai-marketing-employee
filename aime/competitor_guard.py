"""What a competitor flag may not do. It sits on the company, and the next scoring run closes every open lead there, so words read
on a site or in a post do not set it over a lead in conversation (the owner's rule: only a person ends that). And a flag needs a lead:
only a lead's page shows why and offers Not a competitor."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.automation import return_to_person
from aime.config import Settings
from aime.crm_ops import OPEN_LEAD_STATUSES, ensure_lead, log_activity
from aime.matching import competitor_in_post
from aime.models import Company, FollowUp, Lead, Signal
from aime.monitoring import alert_once
from aime.outreach import RUNNING_STAGES

# Not every stage past the first email: a company whose only lead is stopped, no_response, not_interested, bounced or unsubscribed stays
# flaggable, or the flag no longer guards its re-engagement email (outreach.eligible), the reopening of a stopped lead, or a new lead there.
LIVE_STAGES = (*RUNNING_STAGES, "replied", "interested", "meeting")


def live_lead_on_company(session: Session, company_id: int) -> Lead | None:
    return session.execute(select(Lead).where(Lead.company_id == company_id, Lead.status.in_(OPEN_LEAD_STATUSES),
                                              Lead.outreach_status.in_(LIVE_STAGES)).order_by(Lead.id)).scalars().first()


def leave_to_a_person(session: Session, company: Company, lead: Lead, phrase: str, actor: str, settings: Settings) -> None:
    """The flag is not set, so a person is told once, and what they can do: no button sets the flag by hand. Without the flag an email
    already approved would go on the next send run, before anyone reads this, so each goes back for approval. Only when the warning is
    new: this runs every morning, and a person who approves one again after it has decided."""
    new = alert_once(session, "warning", "competitor check",
                     f'{company.name} says "{phrase}", so it may sell software or AI work, but lead #{lead.id} is in conversation, and a '
                     "competitor flag would end it. It was not flagged, and any email approved for this company went back for approval. "
                     "If it is a competitor, use Stop outreach or disqualify the lead by hand on its page.",
                     settings, key=f"competitor_live:{company.id}", until_resolved=True)
    if not new:
        return
    log_activity(session, f'Not marked as a possible competitor: it says "{phrase}", and lead #{lead.id} is in conversation, so a person decides',
                 actor, type_="data_quality", company_id=company.id, lead_id=lead.id)
    open_leads = select(Lead.id).where(Lead.company_id == company.id, Lead.status.in_(OPEN_LEAD_STATUSES))
    for f in session.execute(select(FollowUp).where(FollowUp.lead_id.in_(open_leads), FollowUp.channel == "email",
                                                    FollowUp.status == "approved")).scalars():
        return_to_person(f, f'its company says "{phrase}", so read the warning about a possible competitor first')


def give_flagged_companies_a_lead(session: Session, actor: str) -> None:
    """A company flagged with no lead shows no reason, and no page offers Not a competitor. The lead is scored closed by the next run and
    no gate opens for it, since each refuses a flagged company. Not for a SEC filer: a filing makes no lead (the owner's decision of 1 Nov
    2026). Words are written as research._record_flag_words writes them: the daily review reads only what it can read again."""
    from aime.agents.research import EVIDENCE_ONLY_SOURCES

    has_a_lead = select(Lead.id).where(Lead.company_id == Company.id, Lead.status != "merged").exists()
    for company in session.execute(select(Company).where(Company.is_competitor.is_(True), Company.merged_into_id.is_(None),
                                                         Company.source.not_in(EVIDENCE_ONLY_SOURCES), ~has_a_lead)).scalars().all():
        lead, _ = ensure_lead(session, company, "research")
        posts = session.execute(select(Signal.text).where(Signal.company_id == company.id, Signal.signal_type == "hiring")).scalars()
        phrase = next(filter(None, map(competitor_in_post, [*posts, company.description])), None)
        if phrase:
            log_activity(session, f'Marked as a possible competitor: it says "{phrase}"', actor, type_="data_quality",
                         company_id=company.id, lead_id=lead.id)
