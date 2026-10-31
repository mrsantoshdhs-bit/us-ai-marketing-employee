"""Who gets a new deal, and every change of ownership on record.

The pool is the Sales Managers chosen in Settings › Sales settings (`[handover] sales_manager`), or, when none are
chosen, every active user whose role receives leads. People marked unavailable, and people at their capacity of open
deals, are skipped. The rule (`[handover] strategy`):
- a deal on a lead someone in the pool already owns stays with them (one person per customer);
- workload (default): the fewest open deals, ties in pool order;
- round_robin: the next person after the last one assigned;
- skill: people who list the deal's service among their skills first, then the fewest open deals.
When the lead's owner is on leave or gone, the lead goes to the deal's new owner too, on record.
Nobody available: the deal stays unassigned and the administrators are alerted. A deal whose owner has done nothing
for `[handover] escalate_after_hours` hours (0 = off) is escalated to the owner's manager; it is not moved."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aime.models import Activity, Alert, Assignment, FollowUp, Lead, Meeting, Opportunity, Team, User
from aime.timeutil import utcnow

STRATEGIES = ("workload", "round_robin", "skill")
OPEN = ("won", "lost")


def configured_pool(settings) -> list[str]:
    value = settings.section("handover").get("sales_manager") or []
    names = [value] if isinstance(value, str) else list(value)
    return [n.strip() for n in names if n and n.strip()]


def open_load(session: Session, names) -> dict[str, int]:
    return dict(session.execute(select(Opportunity.owner, func.count(Opportunity.id)).where(
        Opportunity.owner.in_(list(names)), Opportunity.stage.not_in(OPEN)).group_by(Opportunity.owner)).all())


def pool(session: Session, settings) -> list:
    """Everyone who may receive a new deal now, in the configured order."""
    from aime.permissions import permissions_of

    names = configured_pool(settings)
    users = {u.username: u for u in session.execute(select(User).where(User.active.is_(True))).scalars()}
    if names:
        everyone = {u.username for u in session.execute(select(User)).scalars()}
        # a configured name without an account (sign-in off) is taken as given; a deactivated account is skipped
        people = [users.get(n) or SimpleNamespace(username=n, available=True, capacity=0, skills=[])
                  for n in names if n in users or n not in everyone]
    else:
        people = sorted((u for u in users.values() if "receive_leads" in permissions_of(session, u.role)),
                        key=lambda u: u.id)
    load = open_load(session, [u.username for u in people])
    return [u for u in people if u.available is not False and not (u.capacity and load.get(u.username, 0) >= u.capacity)]


def choose(session: Session, settings, service: str | None = None) -> tuple[str | None, str, str]:
    """(username or None, method, reason) for a new deal."""
    from aime.db import kv_get, kv_set

    people = pool(session, settings)
    if not people:
        return None, "none", "nobody is available to receive it (check availability and capacity on Users)"
    strategy = settings.section("handover").get("strategy", "workload")
    strategy = strategy if strategy in STRATEGIES else "workload"
    names = [u.username for u in people]
    if strategy == "skill":
        skilled = [u.username for u in people if service and service in (u.skills or [])]
        if skilled:
            load = open_load(session, skilled)
            pick = min(skilled, key=lambda n: (load.get(n, 0), names.index(n)))
            return pick, "skill", f"sells {service}; fewest open deals among them"
    if strategy == "round_robin":
        last = (kv_get(session, "assignment.round_robin") or {}).get("last")
        everyone = configured_pool(settings) or names  # the turn order includes people skipped right now
        order = everyone[everyone.index(last) + 1:] + everyone[:everyone.index(last) + 1] if last in everyone else everyone
        pick = next(n for n in order if n in names)
        kv_set(session, "assignment.round_robin", {"last": pick})
        return pick, "round_robin", "next in turn"
    load = open_load(session, names)
    pick = min(names, key=lambda n: (load.get(n, 0), names.index(n)))
    return pick, "workload", f"fewest open deals ({load.get(pick, 0)})"


def record(session: Session, *, lead_id: int | None, opportunity_id: int | None, from_owner: str | None,
           to_owner: str | None, method: str, reason: str, actor: str) -> Assignment:
    row = Assignment(lead_id=lead_id, opportunity_id=opportunity_id, from_owner=from_owner, to_owner=to_owner,
                     method=method, reason=reason[:300], actor=actor)
    session.add(row)
    return row


def _away(session: Session, username: str) -> bool:
    """Marked unavailable, or the account is closed. Someone at capacity or outside a configured pool is not: they are still
    working, and no lead is taken from them by a rule (the deal goes by the rule, as it always has)."""
    user = session.execute(select(User).where(User.username == username)).scalars().first()
    return user is not None and (not user.active or user.available is False)


def owner_choices(session: Session, scope, current: str | None = None) -> list[SimpleNamespace]:
    """Who a person may pick as owner: everyone active they may see who can work a record, those away marked. Read-only roles
    are not offered, except whoever holds it now (the list must show the owner, or Save would silently unassign)."""
    from aime.permissions import permissions_of

    return [SimpleNamespace(name=u.username, away=u.available is False)
            for u in session.execute(select(User).where(User.active.is_(True)).order_by(User.username)).scalars()
            if (scope is None or scope.all or u.username in scope.usernames)
            and (u.username == current or "edit_records" in permissions_of(session, u.role))]


def open_work(session: Session, username: str) -> str:
    """How much is open in this person's name, in words."""
    from aime.crm_ops import OPEN_LEAD_STATUSES

    leads = select(Lead.id).where(Lead.owner == username, Lead.status.in_(OPEN_LEAD_STATUSES))
    numbers = {
        "open lead": session.execute(select(func.count(Lead.id)).where(Lead.id.in_(leads))).scalar_one(),
        "open deal": open_load(session, [username]).get(username, 0),
        "email waiting for approval": session.execute(select(func.count(FollowUp.id)).where(
            FollowUp.lead_id.in_(leads), FollowUp.channel == "email", FollowUp.status == "awaiting_approval")).scalar_one(),
        "coming meeting": session.execute(select(func.count(Meeting.id)).where(
            Meeting.lead_id.in_(leads), Meeting.status == "scheduled", Meeting.ends_at >= utcnow())).scalar_one(),
    }
    return ", ".join(f"{n} {what}{'' if n == 1 else 's'}" for what, n in numbers.items() if n) or "no open work"


def assign_new(session: Session, settings, opp: Opportunity, actor: str) -> str | None:
    """Give an unowned deal (and its lead, if unowned) an owner by the configured rule."""
    if opp.owner:
        return opp.owner
    session.flush()  # deals assigned earlier in this run count towards the balance
    lead = session.get(Lead, opp.lead_id) if opp.lead_id else None
    available = {u.username for u in pool(session, settings)}
    if lead is not None and lead.owner and lead.owner in available:
        # the salesperson already working this lead keeps the customer: one person per customer
        owner, method, reason = lead.owner, "lead_owner", "already works this lead"
    else:
        owner, method, reason = choose(session, settings, opp.service)
    opp.owner = owner
    if lead is not None and owner and not lead.owner:
        lead.owner = owner
    elif lead is not None and owner and lead.owner != owner and _away(session, lead.owner):
        # on leave or gone: the customer goes with the deal, or the reply is answered by one person and the lead held by another
        from aime.crm_ops import log_activity

        old, lead.owner = lead.owner, owner
        why = f"went with its new deal: {old} is away"
        record(session, lead_id=lead.id, opportunity_id=None, from_owner=old, to_owner=owner, method=method, reason=why, actor=actor)
        log_activity(session, f"Lead moved from {old} to {owner}: {why}", actor, type_="assignment", company_id=lead.company_id,
                     lead_id=lead.id, opportunity_id=opp.id)
    record(session, lead_id=opp.lead_id, opportunity_id=opp.id, from_owner=None, to_owner=owner, method=method,
           reason=reason, actor=actor)
    if owner is None:
        session.add(Alert(level="warning", source="handover",
                          message=f"Unassigned deal: {opp.name}: {reason}"[:500]))
    return owner


def reassign(session: Session, *, to_owner: str | None, actor: str, reason: str, lead: Lead | None = None,
             opp: Opportunity | None = None) -> str:
    """A person moves a lead (and its open deals) or one deal to someone else, or back to unassigned."""
    from aime.crm_ops import log_activity

    # the same owner again (a double click, browser back and resubmit): nothing moved, so nothing goes on record
    if opp is not None:
        unchanged = opp.owner == to_owner
    else:
        unchanged = lead.owner == to_owner and not session.execute(select(Opportunity.id).where(
            Opportunity.lead_id == lead.id, Opportunity.stage.not_in(OPEN),
            Opportunity.owner.is_distinct_from(to_owner))).first()
    if unchanged:
        return f"Already with {to_owner or 'nobody'}: nothing changed"
    if opp is not None:
        old, opp.owner = opp.owner, to_owner
        record(session, lead_id=opp.lead_id, opportunity_id=opp.id, from_owner=old, to_owner=to_owner,
               method="manual", reason=reason, actor=actor)
        target, lead_id, company_id = f"deal {opp.name}", opp.lead_id, opp.company_id
    else:
        old, lead.owner = lead.owner, to_owner
        record(session, lead_id=lead.id, opportunity_id=None, from_owner=old, to_owner=to_owner, method="manual",
               reason=reason, actor=actor)
        for o in session.execute(select(Opportunity).where(Opportunity.lead_id == lead.id,
                                                           Opportunity.stage.not_in(OPEN))).scalars():
            if o.owner != to_owner:
                o_old, o.owner = o.owner, to_owner
                record(session, lead_id=lead.id, opportunity_id=o.id, from_owner=o_old, to_owner=to_owner,
                       method="manual", reason=f"with its lead: {reason}" if reason else "with its lead", actor=actor)
        target, lead_id, company_id = f"lead #{lead.id}", lead.id, lead.company_id
    note = f"Assigned {target} from {old or 'unassigned'} to {to_owner or 'unassigned'}" + (f": {reason}" if reason else "")
    # On record in Activity and Assignment. No alert: the person who did it knows, and alerts are not what the new
    # owner reads.
    log_activity(session, note[:300], actor, type_="assignment", company_id=company_id, lead_id=lead_id,
                 opportunity_id=opp.id if opp is not None else None)
    return note


def close_manual_notices(session: Session) -> int:
    """Close the alerts that hand reassignments used to leave open (their record stays in Activity and Assignment)."""
    rows = session.execute(select(Alert).where(Alert.source == "handover", Alert.acknowledged.is_(False),
                                               Alert.message.like("Assigned %(by %)"))).scalars().all()
    for a in rows:
        a.acknowledged, a.resolved_at = True, utcnow()
    return len(rows)


def manager_of(session: Session, username: str) -> str | None:
    user = session.execute(select(User).where(User.username == username)).scalars().first()
    if user is None:
        return None
    if user.manager:
        return user.manager
    if user.team_id:
        team = session.get(Team, user.team_id)
        if team and team.manager and team.manager != username:
            return team.manager
    return None


def escalations(session: Session, settings, now: datetime, actor: str) -> int:
    """Alert the owner's manager (or the administrators) about deals the owner has not touched in time. Once per
    assignment; the deal stays with its owner until a person reassigns it."""
    hours = int(settings.section("handover").get("escalate_after_hours", 24) or 0)
    if hours <= 0:
        return 0
    count = 0
    for opp in session.execute(select(Opportunity).where(Opportunity.stage.not_in(OPEN),
                                                         Opportunity.owner.is_not(None))).scalars().all():
        given = session.execute(select(Assignment).where(Assignment.opportunity_id == opp.id,
                                                         Assignment.to_owner == opp.owner,
                                                         Assignment.method != "escalation")
                                .order_by(Assignment.id.desc())).scalars().first()
        if given is None or now - given.created_at < timedelta(hours=hours):
            continue
        done = session.execute(select(Assignment.id).where(Assignment.opportunity_id == opp.id,
                                                           Assignment.method == "escalation",
                                                           Assignment.id > given.id)).first()
        acted = session.execute(select(Activity.id).where(
            (Activity.opportunity_id == opp.id) | ((Activity.lead_id == opp.lead_id) & (Activity.lead_id.is_not(None))),
            Activity.actor == opp.owner, Activity.created_at >= given.created_at)).first()
        if done or acted:
            continue
        boss = manager_of(session, opp.owner)
        to = boss or "the administrators"
        reason = f"no action by {opp.owner} in {hours} h since it was assigned; {to} told"
        record(session, lead_id=opp.lead_id, opportunity_id=opp.id, from_owner=opp.owner, to_owner=opp.owner,
               method="escalation", reason=reason, actor=actor)
        session.add(Alert(level="warning", source="handover",
                          message=f"Escalated to {to}: {opp.name}: {reason}"[:500]))
        count += 1
    return count
