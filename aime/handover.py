"""Handover to the Sales Manager: when a prospect shows interest or a meeting is booked, the opportunity is assigned
to the configured Sales Manager (`[handover] sales_manager`, set by the Super Admin; a dashboard username), who is
told on the dashboard and, when signed in at the PC, with a desktop notification. Every handover is in the audit log
and on the lead's activity. Nothing is sent to the prospect."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aime.crm_ops import log_activity
from aime.models import Alert, Opportunity
from aime.notify import notify_event


def sales_manager(settings) -> str | None:
    return (settings.section("handover").get("sales_manager") or "").strip() or None


def handover(session: Session, settings, opp: Opportunity, reason: str, actor: str) -> str:
    """Assign the opportunity (if nobody owns it yet) and tell its owner why it needs them now."""
    from aime.automation import audit

    if not opp.owner:
        opp.owner = sales_manager(settings)
    who = opp.owner or "the Sales Manager (none assigned: set one in Settings › Sales settings)"
    message = f"Handed over to {who}: {opp.name} — {reason}"
    session.add(Alert(level="info", source="handover", message=message[:500]))
    log_activity(session, message[:300], actor, type_="handover", company_id=opp.company_id, lead_id=opp.lead_id,
                 opportunity_id=opp.id)
    audit(session, actor, "handover", f"opportunity #{opp.id}", f"to {opp.owner or 'unassigned'}: {reason}"[:300])
    notify_event(settings, message)
    return who
