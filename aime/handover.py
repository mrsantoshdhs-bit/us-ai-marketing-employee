"""Handover to the Sales Managers: when a prospect shows interest or a meeting is booked, the opportunity is assigned
to one of the configured Sales Managers (`[handover] sales_manager`, set by the Super Admin: one dashboard username
or several), who is told on the dashboard and, when signed in at the PC, with a desktop notification. With several,
each new opportunity goes to the one with the fewest open opportunities (ties: in the order chosen), and it stays
with them. Every handover is in the audit log and on the lead's activity. Nothing is sent to the prospect."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.crm_ops import log_activity
from aime.models import Alert, Opportunity
from aime.notify import notify_event


def sales_managers(settings) -> list[str]:
    value = settings.section("handover").get("sales_manager") or []
    names = [value] if isinstance(value, str) else list(value)
    return [n.strip() for n in names if n and n.strip()]


def next_owner(session: Session, settings) -> str | None:
    """The Sales Manager with the fewest open opportunities (ties: the first in the configured order)."""
    names = sales_managers(settings)
    if len(names) < 2:
        return names[0] if names else None
    load = dict(session.execute(select(Opportunity.owner, func.count(Opportunity.id)).where(
        Opportunity.owner.in_(names), Opportunity.stage.not_in(("won", "lost"))).group_by(Opportunity.owner)).all())
    return min(names, key=lambda n: (load.get(n, 0), names.index(n)))


def handover(session: Session, settings, opp: Opportunity, reason: str, actor: str) -> str:
    """Assign the opportunity (if nobody owns it yet) and tell its owner why it needs them now."""
    from aime.automation import audit

    if not opp.owner:
        session.flush()  # opportunities assigned earlier in this run count towards the balance
        opp.owner = next_owner(session, settings)
    who = opp.owner or "the Sales Manager (none assigned: set one in Settings › Sales settings)"
    message = f"Handed over to {who}: {opp.name} — {reason}"
    session.add(Alert(level="info", source="handover", message=message[:500]))
    log_activity(session, message[:300], actor, type_="handover", company_id=opp.company_id, lead_id=opp.lead_id,
                 opportunity_id=opp.id)
    audit(session, actor, "handover", f"opportunity #{opp.id}", f"to {opp.owner or 'unassigned'}: {reason}"[:300])
    notify_event(settings, message)
    return who
