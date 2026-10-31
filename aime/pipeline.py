"""Pipeline automation: the moves the system makes on its own, each recorded, each a task for a person when
a decision is needed. Run by the Sales Workflow Agent (followups job).

- Stalled opportunity: open, and nothing logged for `stalled_days` -> a task to decide the next step.
- No response: the outreach sequence ended (all sent, the next step rejected, or the lead parked) and nobody replied
  for `no_response_days` after the last email -> outreach stage "no_response", lead to nurture (no further email).
- Meeting held: a scheduled meeting ended more than two hours ago -> a task to record the outcome.
- A stage nothing is behind any more ("drafted" or "approved" with no email left) closes, and says so in Activity.
- Emails waiting for a person: one reminder that stays open while any has waited `draft_reminder_days`; one waiting
  `draft_fresh_days` says it should be read again. It is never cancelled or sent by this: approving it is the fresh look.
Tasks are never duplicated: one open task per subject per lead.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.crm_ops import log_activity
from aime.models import Activity, FollowUp, Lead, Meeting, Opportunity
from aime.monitoring import alert_once, resolve_alerts
from aime.outreach import OPEN, policy, set_status
from aime.timeutil import utcnow

DRAFTS_ALERT = "drafts:waiting"
READ_AGAIN = "Written "  # how the note below starts, so a later run can bring its day count up to date


def _task(session: Session, lead_id: int, company_id: int | None, subject: str, body: str) -> bool:
    exists = session.execute(select(FollowUp.id).where(FollowUp.lead_id == lead_id, FollowUp.channel == "task",
                                                       FollowUp.status == "pending", FollowUp.subject == subject)).first()
    if exists:
        return False
    session.add(FollowUp(lead_id=lead_id, company_id=company_id, channel="task", step=1, status="pending",
                         subject=subject[:300], body=body))
    return True


def clear_stage(session: Session, lead: Lead, actor: str, why: str) -> None:
    """Nothing went out and nothing is waiting: the lead is not "drafted" or "approved" any more. The stage goes back to
    none rather than "stopped", because no person ended anything: if the lead qualifies again, it can be drafted again."""
    if lead.outreach_status in ("drafted", "approved"):
        lead.outreach_status, lead.outreach_updated_at = None, utcnow()
        log_activity(session, f"Outreach: stage closed, no email is waiting ({why})", actor, type_="outreach",
                     company_id=lead.company_id, lead_id=lead.id)


def _close_finished_stages(session: Session, actor: str) -> None:
    for lead in session.execute(select(Lead).where(Lead.outreach_status.in_(("drafted", "approved")))).scalars().all():
        emails = session.execute(select(FollowUp.status).where(FollowUp.lead_id == lead.id, FollowUp.channel == "email")).scalars().all()
        if not any(st in (*OPEN, "sent") for st in emails):
            clear_stage(session, lead, actor, "its email was cancelled")
    for lead in session.execute(select(Lead).where(Lead.outreach_status == "contacted", Lead.status == "disqualified")).scalars().all():
        set_status(session, lead, "stopped", actor, "the lead was disqualified")


def _remind_about_drafts(session: Session, settings, actor: str) -> None:
    cfg = settings.section("followup")
    remind_after, fresh_days = int(cfg.get("draft_reminder_days", 2)), int(cfg.get("draft_fresh_days", 14))
    now = utcnow()  # the real clock, as created_at is: a run's own `now` may be set for its send window
    waiting = session.execute(select(FollowUp).where(FollowUp.channel == "email", FollowUp.status == "awaiting_approval",
                                                     FollowUp.created_at < now - timedelta(days=remind_after))).scalars().all()
    for f in waiting:
        age = (now - f.created_at).days
        if age > fresh_days and (not f.blocked_reason or f.blocked_reason.startswith(READ_AGAIN)):
            f.blocked_reason = f"{READ_AGAIN}{age} days ago: read it again before approving, its wording may be out of date"
    if waiting:
        oldest = max((now - f.created_at).days for f in waiting)
        alert_once(session, "warning", actor, f"{len(waiting)} email(s) have waited more than {remind_after} days for a "
                   f"person's approval (the oldest {oldest} days). Open Follow-ups to approve or reject them.", settings,
                   key=DRAFTS_ALERT)
    else:
        resolve_alerts(session, (DRAFTS_ALERT,), set())


def pipeline_checks(session: Session, settings, now: datetime, actor: str) -> dict:
    cfg = settings.section("pipeline")
    stalled_days = int(cfg.get("stalled_days", 10))
    wait_days = int(cfg.get("no_response_days", 7))
    stats = {"stalled_tasks": 0, "no_response": 0, "meeting_tasks": 0}

    for opp in session.execute(select(Opportunity).where(Opportunity.stage.not_in(("won", "lost")))).scalars().all():
        # two indexed lookups: an OR of the two columns makes SQLite scan every activity for each deal
        by_opp = session.execute(select(func.max(Activity.created_at)).where(Activity.opportunity_id == opp.id)).scalar_one()
        by_lead = session.execute(select(func.max(Activity.created_at)).where(
            Activity.lead_id == opp.lead_id)).scalar_one() if opp.lead_id else None
        last = max(d for d in (by_opp, by_lead, opp.updated_at, opp.created_at) if d)
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
        if not sent or open_steps or not sent.sent_at or now - sent.sent_at <= timedelta(days=wait_days):
            continue
        next_rejected = session.execute(select(FollowUp.id).where(
            FollowUp.lead_id == lead.id, FollowUp.kind == "outreach", FollowUp.step > sent.step, FollowUp.status == "rejected")).first()
        if sent.step >= max_touches:
            how = f"the last of {max_touches} emails"
        elif next_rejected:
            how = "the last email; a person rejected the next one"
        elif lead.status == "nurture":
            how = "the last email; the lead was parked"
        else:
            continue  # the next step is still to come
        set_status(session, lead, "no_response", actor, f"no reply {wait_days} days after {how}")
        if lead.status == "qualified":
            lead.status = "nurture"
            log_activity(session, "Lead qualified → nurture (outreach ended with no reply)", actor, type_="status_change",
                         company_id=lead.company_id, lead_id=lead.id)
        stats["no_response"] += 1

    for m in session.execute(select(Meeting).where(Meeting.status == "scheduled", Meeting.lead_id.is_not(None),
                                                   Meeting.ends_at < now - timedelta(hours=2))).scalars().all():
        if _task(session, m.lead_id, m.company_id, f"Record the outcome of the meeting with {m.invitee_name or m.invitee_email}",
                 f"Meeting {m.starts_at:%Y-%m-%d %H:%M} UTC ({m.title}). On the Meetings page mark it held or no-show, "
                 "then set the opportunity's next step."):
            stats["meeting_tasks"] += 1
    from aime.assignment import close_manual_notices, escalations

    stats["escalated"] = escalations(session, settings, now, actor)  # untouched deals: the owner's manager is told
    close_manual_notices(session)
    _close_finished_stages(session, actor)
    _remind_about_drafts(session, settings, actor)
    return stats
