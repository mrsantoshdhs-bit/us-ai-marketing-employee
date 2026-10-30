"""Customer handover: when a deal is won, the lead becomes a customer and whoever owns the deal gets one handover
task with what was sold, for how much, to whom, and the onboarding checklist, so delivery starts from the record
instead of from memory. Nothing is sent to the customer; the task is for the team."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aime.crm_ops import log_activity
from aime.knowledge.services import service_name
from aime.models import FollowUp, Lead, Meeting, Opportunity, Quote
from aime.timeutil import utcnow

CHECKLIST = (
    "Confirm the scope and the start date with the customer",
    "Send the contract or invoice for the agreed price",
    "Introduce the delivery lead and book the kickoff call",
    "Collect the access and documents the project needs",
    "Note the kickoff date on the deal page",
)


def task_subject(opp: Opportunity) -> str:
    return f"Customer handover: {opp.name}"[:300]


def handover(session: Session, opp: Opportunity, actor: str) -> bool:
    """Mark the lead converted and add the handover task, once per deal (winning it again after a reopen brings the
    cancelled task back instead of adding a second one). Returns whether a task was added or brought back."""
    lead = session.get(Lead, opp.lead_id) if opp.lead_id else None
    if lead is None:
        return False
    lead.status = "converted"
    subject = task_subject(opp)
    earlier = session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task",
                                                     FollowUp.subject == subject)).scalars().first()
    if earlier is not None and earlier.status != "cancelled":
        return False
    quote = session.execute(select(Quote).where(Quote.opportunity_id == opp.id, Quote.status == "accepted")
                            .order_by(Quote.decided_at.desc(), Quote.id.desc())).scalars().first()
    held = session.execute(select(Meeting).where(Meeting.opportunity_id == opp.id, Meeting.status == "completed")
                           .order_by(Meeting.starts_at.desc())).scalars().first()
    contact = lead.contact
    if quote:
        price = f"{quote.currency} {quote.amount:,.2f} (quote #{quote.id}, accepted)" + (
            f"; terms: {quote.terms}" if quote.terms else "")
    elif opp.est_value:
        price = f"USD {opp.est_value:,.2f} (value on the deal; no accepted quote on record)"
    else:
        price = "no price on record: add the agreed price to the deal"
    who = f"{contact.display_name} <{contact.email}>" if contact and contact.email else "no contact on record"
    lines = [
        f"Won by {opp.won_by or actor} on {utcnow():%Y-%m-%d}. Deal owner: {opp.owner or 'none: assign one'}.",
        f"Service: {service_name(opp.service)}.",
        f"Price: {price}.",
        f"Customer contact: {who}.",
        f"Last meeting held: {held.starts_at:%Y-%m-%d} ({held.title})." if held else "No completed meeting on record.",
        "",
        "Onboarding checklist:",
        *(f"- {item}" for item in CHECKLIST),
        "",
        "Nothing has been sent to the customer. Mark this task done when the kickoff is booked.",
    ]
    if earlier is not None:
        earlier.status, earlier.blocked_reason, earlier.due_at, earlier.body = "pending", None, utcnow(), "\n".join(lines)
    else:
        session.add(FollowUp(lead_id=lead.id, company_id=opp.company_id, contact_id=lead.contact_id, channel="task",
                             step=1, status="pending", due_at=utcnow(), subject=subject, body="\n".join(lines)))
    log_activity(session, f"Deal won: customer handover task for {opp.owner or actor}", actor, type_="opportunity",
                 company_id=opp.company_id, lead_id=lead.id, opportunity_id=opp.id)
    return True


def undo_handover(session: Session, opp: Opportunity, actor: str) -> None:
    """The deal is no longer won (a person reopened it, or corrected it to lost): the lead stops being a customer unless
    another won deal keeps it one, and delivery is no longer told to onboard them. A finished task stays on record."""
    lead = session.get(Lead, opp.lead_id) if opp.lead_id else None
    if lead is None:
        return
    another_win = session.execute(select(Opportunity.id).where(Opportunity.lead_id == lead.id, Opportunity.id != opp.id,
                                                               Opportunity.stage == "won")).first()
    if lead.status == "converted" and not another_win:
        lead.status = "qualified"
    for task in session.execute(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.channel == "task",
                                                       FollowUp.subject == task_subject(opp),
                                                       FollowUp.status == "pending")).scalars():
        task.status, task.blocked_reason = "cancelled", f"The deal is no longer won (changed by {actor})"
