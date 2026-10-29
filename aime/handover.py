"""Handover to the sales team: when a prospect shows interest or a meeting is booked, the opportunity is assigned
(aime.assignment: the Sales Managers chosen in Settings, or everyone whose role receives leads; by workload, round
robin or skill; skipping people who are unavailable or at capacity). The owner is told on the dashboard and, when
signed in at the PC, with a desktop notification, and the deal stays with them. Every handover is in the audit log,
the assignment history and the lead's activity. Nothing is sent to the prospect."""

from __future__ import annotations

from sqlalchemy.orm import Session

from aime.crm_ops import log_activity
from aime.models import Alert, Opportunity
from aime.notify import notify_event


def sales_managers(settings) -> list[str]:
    from aime.assignment import configured_pool

    return configured_pool(settings)


def next_owner(session: Session, settings, service: str | None = None) -> str | None:
    """Who the next new deal would go to by the configured rule (aime.assignment), or None."""
    from aime.assignment import choose

    return choose(session, settings, service)[0]


def handover(session: Session, settings, opp: Opportunity, reason: str, actor: str) -> str:
    """Assign the opportunity (if nobody owns it yet) and tell its owner why it needs them now."""
    from aime.automation import audit

    if not opp.owner:
        from aime.assignment import assign_new

        assign_new(session, settings, opp, actor)  # by the configured rule, on record in the assignment history
    who = opp.owner or "the Sales Manager (none assigned: set one in Settings › Sales settings)"
    message = f"Handed over to {who}: {opp.name} — {reason}"
    session.add(Alert(level="info", source="handover", message=message[:500]))
    log_activity(session, message[:300], actor, type_="handover", company_id=opp.company_id, lead_id=opp.lead_id,
                 opportunity_id=opp.id)
    audit(session, actor, "handover", f"opportunity #{opp.id}", f"to {opp.owner or 'unassigned'}: {reason}"[:300])
    notify_event(settings, message)
    return who
