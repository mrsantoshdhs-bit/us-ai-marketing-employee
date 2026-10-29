"""Pipeline automation: the moves the system makes on its own, each recorded, each a task for a person when
a decision is needed. Run by the Sales Workflow Agent (followups job).

- Stalled opportunity: open, and nothing logged for `stalled_days` -> a task to decide the next step.
- No response: the whole outreach sequence was sent and nobody replied for `no_response_days` after the
  last step -> outreach stage "no_response", lead to nurture (no further email).
- Meeting held: a scheduled meeting ended more than two hours ago -> a task to record the outcome.
Tasks are never duplicated: one open task per subject per lead.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.models import Activity, FollowUp, Lead, Meeting, Opportunity
from aime.outreach import OPEN, policy, set_status


def _task(session: Session, lead_id: int, company_id: int | None, subject: str, body: str) -> bool:
    exists = session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task",
                                                       FollowUp.status == "pending", FollowUp.subject == subject)).first()
    if exists:
        return False
    session.add(FollowUp(lead_id=lead_id, company_id=company_id, channel="task", step=1, status="pending",
                         subject=subject[:300], body=body))
    return True


def pipeline_checks(session: Session, settings, now: datetime, actor: str) -> dict:
    cfg = settings.section("pipeline")
    stalled_days = int(cfg.get("stalled_days", 10))
    wait_days = int(cfg.get("no_response_days", 7))
    stats = {"stalled_tasks": 0, "no_response": 0, "meeting_tasks": 0}

    for opp in session.execute(select(Opportunity).where(Opportunity.stage.not_in(("won", "lost")))).scalars().all():
        last = session.execute(select(func.max(Activity.created_at)).where(
            (Activity.opportunity_id == opp.id) | (Activity.lead_id == opp.lead_id))).scalar_one()
        last = max(d for d in (last, opp.updated_at, opp.created_at) if d)
        if opp.lead_id and now - last > timedelta(days=stalled_days):
            days = (now - last).days
            if _task(session, opp.lead_id, opp.company_id, f"Stalled: {opp.name}",
                     f"No activity for {days} days (stage {opp.stage}). Next step on record: {opp.next_step or 'none'}. "
                     "Decide: follow up, change the stage, or close as lost."):
                stats["stalled_tasks"] += 1

    max_touches = policy(settings)["max_touches"]
    for lead in session.execute(select(Lead).where(Lead.outreach_status == "contacted")).scalars().all():
        sent = session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.kind == "outreach",
                                                      FollowUp.status == "sent").order_by(FollowUp.step.desc())).scalars().first()
        open_steps = session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email",
                                                               FollowUp.status.in_(OPEN))).first()
        if sent and sent.step >= max_touches and not open_steps and sent.sent_at and now - sent.sent_at > timedelta(days=wait_days):
            set_status(session, lead, "no_response", actor, f"no reply {wait_days} days after the last of {max_touches} emails")
            if lead.status == "qualified":
                lead.status = "nurture"
            stats["no_response"] += 1

    for m in session.execute(select(Meeting).where(Meeting.status == "scheduled", Meeting.lead_id.is_not(None),
                                                   Meeting.ends_at < now - timedelta(hours=2))).scalars().all():
        if _task(session, m.lead_id, m.company_id, f"Record the outcome of the meeting with {m.invitee_name or m.invitee_email}",
                 f"Meeting {m.starts_at:%Y-%m-%d %H:%M} UTC ({m.title}). On the Meetings page mark it held or no-show, "
                 "then set the opportunity's next step."):
            stats["meeting_tasks"] += 1
    from aime.assignment import escalations

    stats["escalated"] = escalations(session, settings, now, actor)  # untouched deals: the owner's manager is told
    return stats
